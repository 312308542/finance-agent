"""点时历史数据源与策略 walk-forward 数据库编排。"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finance_agent.data.normalizers import is_main_board_ashare_stock_symbol
from finance_agent.factors.service import (
    build_capital_flow_group,
    build_event_group,
    build_fundamental_group,
    build_risk_group,
    build_valuation_group,
)
from finance_agent.research.strategy_walk_forward import (
    MINIMUM_ASSET_AVAILABLE_WEIGHT,
    SUPPORTED_HORIZONS,
    PointInTimeFactorSnapshot,
    WalkForwardOutcome,
    build_price_feature_snapshot,
    evaluate_historical_gate,
    label_forward_position,
)
from finance_agent.scoring.service import compute_asset_score
from finance_agent.scoring.strategies import default_scoring_strategy_seeds
from finance_agent.storage.event_validation import (
    STOCK_NEWS_SOURCE,
    active_event_predicate,
)
from finance_agent.storage.orm import (
    AssetUniverseMemberORM,
    CapitalFlowSnapshotORM,
    EventRecordORM,
    FundamentalSnapshotORM,
    MarketBarORM,
    RiskFindingORM,
)
from finance_agent.storage.repositories import BacktestRepository

JsonDict = dict[str, Any]

DEFAULT_UNIVERSE_ID = "universe:merged:ashare:recommendation"
DEFAULT_MARKET_BAR_SOURCE = "canonical:ashare:kline"
DEFAULT_EVENT_LOOKBACK_DAYS = 90
DEFAULT_FEATURE_BAR_LIMIT = 500
FIXED_ROUND_TRIP_COST = 0.003
ASHARE_TIMEZONE = ZoneInfo("Asia/Shanghai")
ASHARE_CLOSE_TIME = time(hour=15)
FINAL_STATUSES = ("available", "partial")
EXPECTED_FACTOR_GROUPS = (
    "technical",
    "capital_flow",
    "sector_strength",
    "leadership",
    "event",
    "fundamental",
    "liquidity",
    "valuation",
    "risk",
    "event_decay",
)


@dataclass(frozen=True)
class CandidateAsset:
    """历史截面候选资产。"""

    asset_id: str
    symbol: str


@dataclass(frozen=True)
class StrategyWalkForwardRequest:
    """独立历史研究请求。"""

    strategy_id: str
    universe_id: str
    start_at: datetime
    end_at: datetime
    topn: int = 20
    round_trip_cost: float = FIXED_ROUND_TRIP_COST
    dry_run: bool = False

    def __post_init__(self) -> None:
        if self.topn <= 0:
            raise ValueError("topn 必须为正数")
        if self.start_at >= self.end_at:
            raise ValueError("历史研究开始时间必须早于结束时间")
        if not math.isclose(self.round_trip_cost, FIXED_ROUND_TRIP_COST, abs_tol=1e-12):
            raise ValueError("历史研究交易成本固定为 0.003")


class PointInTimeReader(Protocol):
    """点时数据读取协议，便于用固定样本验证 fail-closed 规则。"""

    def list_signal_dates(self, *, start_at: datetime, end_at: datetime) -> list[date]: ...

    def list_candidates(self, *, signal_date: date) -> list[CandidateAsset]: ...

    def list_bars(self, *, asset_id: str, end_at: datetime) -> pd.DataFrame: ...

    def list_forward_bars(
        self,
        *,
        asset_id: str,
        signal_date: date,
        horizon_days: int,
    ) -> pd.DataFrame: ...

    def list_fundamentals(self, *, asset_id: str) -> list[Any]: ...

    def list_capital_flows(self, *, asset_id: str) -> list[Any]: ...

    def list_events(self, *, asset_id: str) -> list[Any]: ...

    def list_risks(self, *, asset_id: str) -> list[Any]: ...

    def list_theme_memberships(self, *, asset_id: str) -> list[Any]: ...

    def data_versions(self) -> JsonDict: ...


class StrategyResearchSource(Protocol):
    """runner 消费的高层数据源协议。"""

    def list_signal_dates(self, *, start_at: datetime, end_at: datetime) -> list[date]: ...

    def list_candidates(self, *, signal_date: date) -> list[CandidateAsset]: ...

    def build_factor_snapshot(
        self,
        *,
        asset_id: str,
        symbol: str,
        as_of: datetime,
        strategy_id: str,
    ) -> PointInTimeFactorSnapshot: ...

    def score_snapshot(
        self,
        snapshot: PointInTimeFactorSnapshot,
        *,
        strategy_id: str,
    ) -> float: ...

    def label_asset(
        self,
        *,
        asset_id: str,
        signal_date: date,
        horizon_days: int,
        round_trip_cost: float,
    ) -> WalkForwardOutcome | None: ...

    def data_versions(self) -> JsonDict: ...


class BacktestResultWriter(Protocol):
    """历史结果持久化协议。"""

    def upsert_result(self, **kwargs: Any) -> Any: ...


class SqlPointInTimeReader:
    """使用 SQLAlchemy 读取历史截面所需的原始数据。"""

    def __init__(
        self,
        session: Session,
        *,
        market_bar_source: str = DEFAULT_MARKET_BAR_SOURCE,
        asset_limit: int | None = None,
        code_commit: str | None = None,
    ) -> None:
        self.session = session
        self.market_bar_source = market_bar_source
        self.asset_limit = max(int(asset_limit), 1) if asset_limit is not None else None
        self.code_commit = code_commit

    def list_signal_dates(self, *, start_at: datetime, end_at: datetime) -> list[date]:
        """查询研究窗口内实际存在的 A 股交易日。"""

        statement = (
            select(func.date(MarketBarORM.timestamp))
            .where(
                *self._bar_predicates(),
                MarketBarORM.timestamp >= _ensure_aware(start_at),
                MarketBarORM.timestamp <= _ensure_aware(end_at),
            )
            .distinct()
            .order_by(func.date(MarketBarORM.timestamp))
        )
        return [value for value in self.session.scalars(statement) if isinstance(value, date)]

    def list_candidates(self, *, signal_date: date) -> list[CandidateAsset]:
        """按当日有收盘 K 线和至少 120 根预热记录重建候选池。"""

        cutoff = _ashare_close_at(signal_date)
        warmed_assets = (
            select(MarketBarORM.asset_id.label("asset_id"))
            .where(
                *self._bar_predicates(),
                MarketBarORM.timestamp <= cutoff,
            )
            .group_by(MarketBarORM.asset_id)
            .having(func.count(MarketBarORM.timestamp.distinct()) >= 120)
            .subquery("walk_forward_warmed_assets")
        )
        statement = (
            select(MarketBarORM.asset_id, MarketBarORM.symbol)
            .where(
                *self._bar_predicates(),
                func.date(MarketBarORM.timestamp) == signal_date,
                MarketBarORM.asset_id.in_(select(warmed_assets.c.asset_id)),
            )
            .distinct()
            .order_by(MarketBarORM.asset_id)
        )
        if self.asset_limit is not None:
            statement = statement.limit(self.asset_limit)
        return [
            CandidateAsset(asset_id=asset_id, symbol=symbol)
            for asset_id, symbol in self.session.execute(statement)
            if is_main_board_ashare_stock_symbol(symbol)
        ]

    def list_bars(self, *, asset_id: str, end_at: datetime) -> pd.DataFrame:
        """读取截面前最近一段 canonical 日 K，返回时间升序 DataFrame。"""

        statement = (
            select(MarketBarORM)
            .where(
                *self._bar_predicates(),
                MarketBarORM.asset_id == asset_id,
                MarketBarORM.timestamp <= _ensure_aware(end_at),
            )
            .order_by(MarketBarORM.timestamp.desc())
            .limit(DEFAULT_FEATURE_BAR_LIMIT)
        )
        return _bars_to_frame(reversed(list(self.session.scalars(statement))))

    def list_forward_bars(
        self,
        *,
        asset_id: str,
        signal_date: date,
        horizon_days: int,
    ) -> pd.DataFrame:
        """读取信号日及其后标签所需的交易日 K 线。"""

        start_at = datetime.combine(signal_date, time.min, tzinfo=UTC)
        statement = (
            select(MarketBarORM)
            .where(
                *self._bar_predicates(),
                MarketBarORM.asset_id == asset_id,
                MarketBarORM.timestamp >= start_at,
            )
            .order_by(MarketBarORM.timestamp)
            .limit(horizon_days + 1)
        )
        return _bars_to_frame(self.session.scalars(statement))

    def list_fundamentals(self, *, asset_id: str) -> list[FundamentalSnapshotORM]:
        statement = (
            select(FundamentalSnapshotORM)
            .where(FundamentalSnapshotORM.asset_id == asset_id)
            .order_by(FundamentalSnapshotORM.as_of)
        )
        return list(self.session.scalars(statement))

    def list_capital_flows(self, *, asset_id: str) -> list[CapitalFlowSnapshotORM]:
        statement = (
            select(CapitalFlowSnapshotORM)
            .where(CapitalFlowSnapshotORM.asset_id == asset_id)
            .order_by(CapitalFlowSnapshotORM.as_of)
        )
        return list(self.session.scalars(statement))

    def list_events(self, *, asset_id: str) -> list[EventRecordORM]:
        statement = (
            select(EventRecordORM)
            .where(
                EventRecordORM.asset_id == asset_id,
                active_event_predicate(EventRecordORM),
            )
            .order_by(EventRecordORM.published_at)
        )
        return list(self.session.scalars(statement))

    def list_risks(self, *, asset_id: str) -> list[RiskFindingORM]:
        statement = (
            select(RiskFindingORM)
            .where(RiskFindingORM.asset_id == asset_id)
            .order_by(RiskFindingORM.as_of)
        )
        return list(self.session.scalars(statement))

    def list_theme_memberships(self, *, asset_id: str) -> list[AssetUniverseMemberORM]:
        statement = select(AssetUniverseMemberORM).where(
            AssetUniverseMemberORM.asset_id == asset_id,
            AssetUniverseMemberORM.included.is_(True),
        )
        return list(self.session.scalars(statement))

    def data_versions(self) -> JsonDict:
        """读取研究涉及表的最高数据水位。"""

        return {
            "code_commit": self.code_commit,
            "diagnostic_asset_limit": self.asset_limit,
            "market_bar_source": self.market_bar_source,
            "market_bars_max_at": _iso_or_none(
                self.session.scalar(
                    select(func.max(MarketBarORM.timestamp)).where(*self._bar_predicates())
                )
            ),
            "fundamentals_max_at": _iso_or_none(
                self.session.scalar(select(func.max(FundamentalSnapshotORM.as_of)))
            ),
            "capital_flows_max_at": _iso_or_none(
                self.session.scalar(select(func.max(CapitalFlowSnapshotORM.as_of)))
            ),
            "events_max_at": _iso_or_none(
                self.session.scalar(select(func.max(EventRecordORM.published_at)))
            ),
            "risks_max_at": _iso_or_none(
                self.session.scalar(select(func.max(RiskFindingORM.as_of)))
            ),
        }

    def _bar_predicates(self) -> tuple[Any, ...]:
        return (
            MarketBarORM.market == "ashare",
            MarketBarORM.timeframe == "1d",
            MarketBarORM.source == self.market_bar_source,
            MarketBarORM.is_closed.is_(True),
            MarketBarORM.status.in_(FINAL_STATUSES),
        )


class PointInTimeStrategyDataSource:
    """把原始历史记录转换为严格点时因子、评分和前向标签。"""

    def __init__(
        self,
        session: Session | None = None,
        *,
        reader: PointInTimeReader | None = None,
        asset_limit: int | None = None,
        code_commit: str | None = None,
    ) -> None:
        if reader is None and session is None:
            raise ValueError("必须提供 session 或 point-in-time reader")
        self.reader = reader or SqlPointInTimeReader(
            session,
            asset_limit=asset_limit,
            code_commit=code_commit,
        )
        self._strategies = {
            item["strategy_id"]: item for item in default_scoring_strategy_seeds()
        }

    def list_signal_dates(self, *, start_at: datetime, end_at: datetime) -> list[date]:
        return self.reader.list_signal_dates(start_at=start_at, end_at=end_at)

    def list_candidates(self, *, signal_date: date) -> list[CandidateAsset]:
        return self.reader.list_candidates(signal_date=signal_date)

    def build_factor_snapshot(
        self,
        *,
        asset_id: str,
        symbol: str,
        as_of: datetime,
        strategy_id: str,
    ) -> PointInTimeFactorSnapshot:
        """构建点时因子快照，任何不可证明输入均按 unavailable 处理。"""

        strategy = self._strategy(strategy_id)
        price_snapshot = build_price_feature_snapshot(
            self.reader.list_bars(asset_id=asset_id, end_at=as_of),
            asset_id=asset_id,
            symbol=symbol,
            as_of=as_of,
        )
        groups = dict(price_snapshot.groups)

        fundamentals = list(self.reader.list_fundamentals(asset_id=asset_id))
        report_history = [
            row for row in fundamentals if _fundamental_visible(row, as_of=as_of)
        ]
        latest_report = _latest_record(report_history, observed_at=_fundamental_observed_at)
        groups["fundamental"] = (
            build_fundamental_group(latest_report)
            if latest_report is not None
            else _unavailable_group("fundamental", "reliable_disclosure_time")
        )

        valuation_history = [
            row for row in fundamentals if _valuation_visible(row, as_of=as_of)
        ]
        latest_valuation = _latest_record(
            valuation_history,
            observed_at=_fundamental_observed_at,
        )
        groups["valuation"] = (
            build_valuation_group(latest_valuation, history=valuation_history)
            if latest_valuation is not None
            else _unavailable_group("valuation", "point_in_time_valuation")
        )

        flow_history = [
            row
            for row in self.reader.list_capital_flows(asset_id=asset_id)
            if _record_status_visible(row) and _record_at_or_before(row, "as_of", as_of)
        ]
        latest_flow = _latest_record(flow_history, observed_at=lambda row: row.as_of)
        groups["capital_flow"] = (
            build_capital_flow_group(latest_flow, history=flow_history)
            if latest_flow is not None
            else _unavailable_group("capital_flow", "point_in_time_capital_flow")
        )

        event_cutoff = as_of - timedelta(days=DEFAULT_EVENT_LOOKBACK_DAYS)
        events = [
            row
            for row in self.reader.list_events(asset_id=asset_id)
            if _event_visible(row, as_of=as_of, cutoff=event_cutoff)
        ]
        groups["event"] = build_event_group(events)
        groups["event_decay"] = _build_event_decay_group(events, as_of=as_of)

        risks = [
            row
            for row in self.reader.list_risks(asset_id=asset_id)
            if _record_at_or_before(row, "as_of", as_of)
            and _ensure_aware(row.as_of) >= event_cutoff
        ]
        groups["risk"] = build_risk_group(risks)

        groups.update(
            _visible_theme_groups(
                self.reader.list_theme_memberships(asset_id=asset_id),
                as_of=as_of,
            )
        )
        for group_name in EXPECTED_FACTOR_GROUPS:
            groups.setdefault(group_name, _unavailable_group(group_name, "point_in_time_source"))

        available_weight = sum(
            float(weight)
            for group_name, weight in strategy["group_weights"].items()
            if _group_is_usable(groups.get(group_name))
        )
        source_ids = _group_source_ids(groups)
        return PointInTimeFactorSnapshot(
            asset_id=asset_id,
            symbol=symbol,
            as_of=_ensure_aware(as_of),
            groups=groups,
            available_weight=available_weight,
            source_ids=source_ids,
        )

    def score_snapshot(
        self,
        snapshot: PointInTimeFactorSnapshot,
        *,
        strategy_id: str,
    ) -> float:
        """复用生产透明评分纯函数计算历史截面分数。"""

        strategy = SimpleNamespace(**self._strategy(strategy_id))
        missing_groups = [
            name for name, group in snapshot.groups.items() if group.get("status") == "unavailable"
        ]
        partial_groups = [
            name for name, group in snapshot.groups.items() if group.get("status") == "partial"
        ]
        factor = SimpleNamespace(
            market="ashare",
            status="available" if not missing_groups and not partial_groups else "partial",
            missing_groups=missing_groups,
            payload={
                "factor_groups": list(snapshot.groups.values()),
                "partial_groups": partial_groups,
            },
        )
        return float(compute_asset_score(factor, strategy=strategy)["total_score"])

    def label_asset(
        self,
        *,
        asset_id: str,
        signal_date: date,
        horizon_days: int,
        round_trip_cost: float,
    ) -> WalkForwardOutcome | None:
        bars = self.reader.list_forward_bars(
            asset_id=asset_id,
            signal_date=signal_date,
            horizon_days=horizon_days,
        )
        return label_forward_position(
            bars,
            signal_date=signal_date,
            horizon_days=horizon_days,
            round_trip_cost=round_trip_cost,
        )

    def data_versions(self) -> JsonDict:
        return dict(self.reader.data_versions())

    def _strategy(self, strategy_id: str) -> JsonDict:
        strategy = self._strategies.get(strategy_id)
        if strategy is None or strategy.get("market") != "ashare":
            raise ValueError(f"找不到 A 股评分策略：{strategy_id}")
        return strategy


class StrategyWalkForwardRunner:
    """独立运行历史截面排序、标签、门槛判断和结果持久化。"""

    def __init__(
        self,
        *,
        source: StrategyResearchSource,
        repository: BacktestResultWriter,
    ) -> None:
        self.source = source
        self.repository = repository

    def run(self, request: StrategyWalkForwardRequest) -> JsonDict:
        """执行一次严格点时历史研究。"""

        signal_dates = self.source.list_signal_dates(
            start_at=request.start_at,
            end_at=request.end_at,
        )
        coverage_by_date: dict[date, list[float]] = {}
        outcomes: list[WalkForwardOutcome] = []
        excluded_counts: dict[str, int] = {
            "no_candidates": 0,
            "unscored_candidates": 0,
            "incomplete_forward_labels": 0,
        }
        total_candidates = 0

        for signal_date in signal_dates:
            candidates = self.source.list_candidates(signal_date=signal_date)
            coverage_by_date[signal_date] = []
            total_candidates += len(candidates)
            if not candidates:
                excluded_counts["no_candidates"] += 1
                continue

            scored: list[tuple[float, CandidateAsset]] = []
            as_of = _ashare_close_at(signal_date)
            for candidate in candidates:
                snapshot = self.source.build_factor_snapshot(
                    asset_id=candidate.asset_id,
                    symbol=candidate.symbol,
                    as_of=as_of,
                    strategy_id=request.strategy_id,
                )
                coverage_by_date[signal_date].append(snapshot.available_weight)
                try:
                    score = self.source.score_snapshot(
                        snapshot,
                        strategy_id=request.strategy_id,
                    )
                except (TypeError, ValueError):
                    excluded_counts["unscored_candidates"] += 1
                    continue
                if math.isfinite(score):
                    scored.append((score, candidate))
                else:
                    excluded_counts["unscored_candidates"] += 1
            scored.sort(key=lambda item: (-item[0], item[1].asset_id))
            selected = [candidate for _score, candidate in scored[: request.topn]]
            if not selected:
                continue

            for horizon_days in SUPPORTED_HORIZONS:
                all_labels = {
                    candidate.asset_id: self.source.label_asset(
                        asset_id=candidate.asset_id,
                        signal_date=signal_date,
                        horizon_days=horizon_days,
                        round_trip_cost=request.round_trip_cost,
                    )
                    for candidate in candidates
                }
                benchmark_labels = [label for label in all_labels.values() if label is not None]
                selected_labels = [all_labels.get(candidate.asset_id) for candidate in selected]
                if (
                    not benchmark_labels
                    or len(benchmark_labels) != len(candidates)
                    or any(label is None for label in selected_labels)
                ):
                    excluded_counts["incomplete_forward_labels"] += 1
                    continue
                complete_selected = [label for label in selected_labels if label is not None]
                benchmark_return = float(
                    np.mean([label.gross_return for label in benchmark_labels])
                )
                gross_return = float(
                    np.mean([label.gross_return for label in complete_selected])
                )
                net_return = float(np.mean([label.net_return for label in complete_selected]))
                outcomes.append(
                    WalkForwardOutcome(
                        signal_date=signal_date,
                        entry_date=max(label.entry_date for label in complete_selected),
                        exit_date=max(label.exit_date for label in complete_selected),
                        horizon_days=horizon_days,
                        gross_return=gross_return,
                        net_return=net_return,
                        benchmark_return=benchmark_return,
                        excess_return=net_return - benchmark_return,
                    )
                )

        gate = evaluate_historical_gate(outcomes, coverage_by_date=coverage_by_date)
        data_versions = self.source.data_versions()
        backtest_id = build_walk_forward_backtest_id(request, data_versions=data_versions)
        parameters = {
            "topn": request.topn,
            "round_trip_cost": request.round_trip_cost,
            "entry": "next_trading_day_open",
            "horizons": list(SUPPORTED_HORIZONS),
        }
        payload = {
            "schema_version": "strategy_walk_forward_v1",
            "parameters": parameters,
            "coverage": {
                "signal_date_count": len(signal_dates),
                "candidate_count": total_candidates,
                "minimum_asset_available_weight": MINIMUM_ASSET_AVAILABLE_WEIGHT,
            },
            "exclusions": excluded_counts,
            "gate_reasons": list(gate.reasons),
        }
        if not request.dry_run:
            self.repository.upsert_result(
                backtest_id=backtest_id,
                market="ashare",
                strategy_id=request.strategy_id,
                universe_id=request.universe_id,
                start_at=_ensure_aware(request.start_at),
                end_at=_ensure_aware(request.end_at),
                rebalance_frequency="daily_close",
                metrics=gate.metrics,
                data_versions=data_versions,
                status=gate.status,
                payload=payload,
            )
        return {
            "status": gate.status,
            "passed": gate.passed,
            "backtest_id": backtest_id,
            "strategy_id": request.strategy_id,
            "universe_id": request.universe_id,
            "metrics": gate.metrics,
            "data_versions": data_versions,
            "gate_reasons": list(gate.reasons),
            "payload": payload,
            "persisted": not request.dry_run,
        }


def run_strategy_walk_forward(
    session: Session,
    *,
    strategy_id: str,
    start_at: datetime,
    end_at: datetime,
    universe_id: str = DEFAULT_UNIVERSE_ID,
    topn: int = 20,
    round_trip_cost: float = FIXED_ROUND_TRIP_COST,
    dry_run: bool = False,
    asset_limit: int | None = None,
    code_commit: str | None = None,
) -> JsonDict:
    """数据库入口；与旧 factor_score_topn 回测实现完全隔离。"""

    source = PointInTimeStrategyDataSource(
        session,
        asset_limit=asset_limit,
        code_commit=code_commit,
    )
    request = StrategyWalkForwardRequest(
        strategy_id=strategy_id,
        universe_id=universe_id,
        start_at=_ensure_aware(start_at),
        end_at=_ensure_aware(end_at),
        topn=topn,
        round_trip_cost=round_trip_cost,
        dry_run=dry_run,
    )
    return StrategyWalkForwardRunner(
        source=source,
        repository=BacktestRepository(session),
    ).run(request)


def build_walk_forward_backtest_id(
    request: StrategyWalkForwardRequest,
    *,
    data_versions: Mapping[str, Any],
) -> str:
    """使用策略、参数和数据水位生成稳定且可复现的结果 ID。"""

    payload = {
        "strategy_id": request.strategy_id,
        "universe_id": request.universe_id,
        "start_at": _ensure_aware(request.start_at).isoformat(),
        "end_at": _ensure_aware(request.end_at).isoformat(),
        "topn": request.topn,
        "round_trip_cost": request.round_trip_cost,
        "data_versions": dict(data_versions),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
    return f"bt:wf:{digest}"


def _fundamental_visible(row: Any, *, as_of: datetime) -> bool:
    if not _record_status_visible(row) or not getattr(row, "report_period", None):
        return False
    observed_at = _fundamental_observed_at(row)
    return observed_at is not None and observed_at <= _ensure_aware(as_of)


def _valuation_visible(row: Any, *, as_of: datetime) -> bool:
    if not _record_status_visible(row):
        return False
    if getattr(row, "pe_ttm", None) is None and getattr(row, "pb", None) is None:
        return False
    if getattr(row, "report_period", None):
        return _fundamental_visible(row, as_of=as_of)
    return _record_at_or_before(row, "as_of", as_of)


def _fundamental_observed_at(row: Any) -> datetime | None:
    payload = getattr(row, "payload", None) or {}
    for key in (
        "disclosed_at",
        "disclosure_at",
        "announcement_at",
        "published_at",
        "report_disclosed_at",
    ):
        parsed = _parse_payload_datetime(payload.get(key))
        if parsed is not None:
            return parsed
    if getattr(row, "report_period", None) is None:
        value = getattr(row, "as_of", None)
        return _ensure_aware(value) if isinstance(value, datetime) else None
    return None


def _event_visible(row: Any, *, as_of: datetime, cutoff: datetime) -> bool:
    published_at = getattr(row, "published_at", None)
    if not isinstance(published_at, datetime):
        return False
    published = _ensure_aware(published_at)
    if published > _ensure_aware(as_of) or published < _ensure_aware(cutoff):
        return False
    if getattr(row, "source", None) != STOCK_NEWS_SOURCE:
        return True
    payload = getattr(row, "payload", None) or {}
    return (payload.get("entity_validation") or {}).get("status") == "passed"


def _build_event_decay_group(events: Sequence[Any], *, as_of: datetime) -> JsonDict:
    if not events:
        return _unavailable_group("event_decay", "point_in_time_events")
    decay_scores: list[float] = []
    weighted_negative = 0.0
    weighted_positive = 0.0
    for event in events:
        published_at = _ensure_aware(event.published_at)
        age_hours = max(0.0, (_ensure_aware(as_of) - published_at).total_seconds() / 3600)
        decay = math.exp(-age_hours / 48.0)
        decay_scores.append(decay)
        if event.sentiment == "negative":
            weighted_negative += decay
        elif event.sentiment == "positive":
            weighted_positive += decay
    average_decay = sum(decay_scores) / len(decay_scores)
    score = max(
        0.0,
        min(100.0, 100 * average_decay - weighted_negative * 20 + weighted_positive * 5),
    )
    return {
        "group": "event_decay",
        "status": "available",
        "score": score,
        "factors": {
            "event_decay_score": average_decay,
            "weighted_negative_event_count": weighted_negative,
            "weighted_positive_event_count": weighted_positive,
            "recent_event_count": len(events),
        },
        "missing_factors": [],
        "source_ids": [event.event_id for event in events],
    }


def _visible_theme_groups(memberships: Sequence[Any], *, as_of: datetime) -> JsonDict:
    selected: dict[str, JsonDict] = {}
    for membership in memberships:
        if not _membership_covers(membership, as_of=as_of):
            continue
        payload = getattr(membership, "payload", None) or {}
        theme_context = payload.get("theme_context") or {}
        factor_groups = theme_context.get("factor_groups") or payload.get("factor_groups") or []
        for raw_group in factor_groups:
            if not isinstance(raw_group, Mapping):
                continue
            group_name = str(raw_group.get("group") or "")
            if group_name not in {"sector_strength", "leadership"}:
                continue
            score = _finite_or_none(raw_group.get("score"))
            if score is None:
                continue
            group = dict(raw_group)
            group["group"] = group_name
            group["score"] = score
            group["status"] = str(group.get("status") or "available")
            source_ids = list(group.get("source_ids") or group.get("evidence_ids") or [])
            source_ids.append(str(membership.id))
            group["source_ids"] = list(dict.fromkeys(source_ids))
            current = selected.get(group_name)
            if current is None or score > float(current["score"]):
                selected[group_name] = group
    return selected


def _membership_covers(row: Any, *, as_of: datetime) -> bool:
    if not bool(getattr(row, "included", True)):
        return False
    row_as_of = getattr(row, "as_of", None)
    if not isinstance(row_as_of, datetime) or _ensure_aware(row_as_of) > _ensure_aware(as_of):
        return False
    payload = getattr(row, "payload", None) or {}
    valid_from = _parse_payload_date(
        payload.get("effective_from") or payload.get("valid_from")
    )
    valid_to = _parse_payload_date(payload.get("effective_to") or payload.get("valid_to"))
    target_date = _ensure_aware(as_of).astimezone(ASHARE_TIMEZONE).date()
    return valid_from is not None and valid_to is not None and valid_from <= target_date <= valid_to


def _record_status_visible(row: Any) -> bool:
    return str(getattr(row, "status", "available")) in FINAL_STATUSES


def _record_at_or_before(row: Any, field: str, as_of: datetime) -> bool:
    value = getattr(row, field, None)
    return isinstance(value, datetime) and _ensure_aware(value) <= _ensure_aware(as_of)


def _latest_record(
    rows: Sequence[Any],
    *,
    observed_at: Any,
) -> Any | None:
    latest_row: Any | None = None
    latest_at: datetime | None = None
    for row in rows:
        current_at = observed_at(row)
        if current_at is not None and (latest_at is None or current_at > latest_at):
            latest_at = current_at
            latest_row = row
    return latest_row


def _group_is_usable(group: Mapping[str, Any] | None) -> bool:
    if not group or group.get("status") not in FINAL_STATUSES:
        return False
    return _finite_or_none(group.get("score")) is not None


def _group_source_ids(groups: Mapping[str, Mapping[str, Any]]) -> tuple[str, ...]:
    source_ids: list[str] = []
    for group in groups.values():
        for value in group.get("source_ids") or group.get("evidence_ids") or []:
            text_value = str(value)
            if text_value and text_value not in source_ids:
                source_ids.append(text_value)
    return tuple(source_ids)


def _unavailable_group(group: str, reason: str) -> JsonDict:
    return {
        "group": group,
        "status": "unavailable",
        "score": None,
        "factors": {},
        "missing_factors": [reason],
        "source_ids": [],
    }


def _bars_to_frame(rows: Any) -> pd.DataFrame:
    values = list(rows)
    return pd.DataFrame(
        {
            "timestamp": [row.timestamp for row in values],
            "open": [float(row.open) for row in values],
            "high": [float(row.high) for row in values],
            "low": [float(row.low) for row in values],
            "close": [float(row.close) for row in values],
            "volume": [float(row.volume) for row in values],
            "amount": [float(row.amount) if row.amount is not None else np.nan for row in values],
        }
    )


def _ashare_close_at(signal_date: date) -> datetime:
    local = datetime.combine(signal_date, ASHARE_CLOSE_TIME, tzinfo=ASHARE_TIMEZONE)
    return local.astimezone(UTC)


def _ensure_aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _parse_payload_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _ensure_aware(value)
    if isinstance(value, date):
        return datetime.combine(value, time.max, tzinfo=ASHARE_TIMEZONE).astimezone(UTC)
    if not value:
        return None
    text_value = str(value).strip()
    try:
        if len(text_value) == 10:
            parsed_date = date.fromisoformat(text_value)
            return datetime.combine(
                parsed_date,
                time.max,
                tzinfo=ASHARE_TIMEZONE,
            ).astimezone(UTC)
        return _ensure_aware(datetime.fromisoformat(text_value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _parse_payload_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return _ensure_aware(value).astimezone(ASHARE_TIMEZONE).date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _finite_or_none(value: Any) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _iso_or_none(value: Any) -> str | None:
    return _ensure_aware(value).isoformat() if isinstance(value, datetime) else None
