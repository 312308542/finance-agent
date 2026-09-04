"""多策略前向观察、到期结算和试运行状态机。"""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from statistics import median
from typing import Any, Protocol

import numpy as np
import pandas as pd
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from finance_agent.research.strategy_walk_forward import (
    SUPPORTED_HORIZONS,
    WalkForwardOutcome,
    evaluate_historical_gate,
    label_forward_position,
)
from finance_agent.research.strategy_walk_forward_runner import (
    DEFAULT_MARKET_BAR_SOURCE,
    FIXED_ROUND_TRIP_COST,
)
from finance_agent.research.validation_gate import StrategyValidationGate
from finance_agent.storage.orm import (
    AssetScoreORM,
    MarketBarORM,
    MarketCalendarORM,
    ScreeningResultItemORM,
    StrategyObservationOutcomeORM,
    StrategyObservationPositionORM,
)
from finance_agent.storage.repositories import StrategyObservationRepository

JsonDict = dict[str, Any]

BASELINE_STRATEGY_ID = "strategy:ashare:short_swing"
DEFAULT_UNIVERSE_ID = "universe:merged:ashare:recommendation"
MINIMUM_T10_SAMPLES = 30
MINIMUM_T20_SAMPLES = 20
VALIDATED_T20_SAMPLES = 60
IMMEDIATE_DRAWDOWN_GAP = 0.10
FINAL_SCORE_STATUSES = ("available", "partial")
FINAL_BAR_STATUSES = ("available", "partial")


class ObservationRepository(Protocol):
    """观察服务所需的仓储协议。"""

    def upsert_run(self, **kwargs: Any) -> Any: ...

    def upsert_positions(self, positions: Sequence[JsonDict]) -> int: ...

    def ensure_outcomes(self, outcomes: Sequence[JsonDict]) -> int: ...

    def list_due_outcomes(self, *, as_of: date, limit: int = 500) -> list[Any]: ...

    def list_pending_without_due(self, *, limit: int = 500) -> list[Any]: ...

    def update_outcome_due_dates(self, updates: Sequence[JsonDict]) -> int: ...

    def update_position_entries(self, entries: Sequence[JsonDict]) -> int: ...

    def mature_outcomes(self, outcomes: Sequence[JsonDict]) -> int: ...

    def get_trial_state(self, strategy_id: str) -> Any | None: ...

    def upsert_trial_state(self, **kwargs: Any) -> Any: ...

    def list_recent_matured_outcomes(
        self,
        *,
        strategy_id: str,
        horizon_days: int | None = None,
        limit: int = 500,
    ) -> list[Any]: ...


class ObservationSource(Protocol):
    """观察服务所需的评分、日历和价格读取协议。"""

    def list_top_scores(
        self,
        *,
        screening_id: str,
        strategy_id: str,
        limit: int,
    ) -> list[Any]: ...

    def future_trade_dates(self, *, trade_date: date, count: int) -> list[date]: ...

    def data_versions(self, *, screening_id: str) -> JsonDict: ...

    def resolve_outcome(
        self,
        outcome: Any,
        *,
        round_trip_cost: float,
    ) -> JsonDict: ...


class SqlStrategyObservationStore:
    """组合现有观察仓储，并隔离仓位入场字段的补写 SQL。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.repository = StrategyObservationRepository(session)

    def upsert_run(self, **kwargs: Any) -> Any:
        return self.repository.upsert_run(**kwargs)

    def upsert_positions(self, positions: Sequence[JsonDict]) -> int:
        return self.repository.upsert_positions(positions)

    def ensure_outcomes(self, outcomes: Sequence[JsonDict]) -> int:
        return self.repository.ensure_outcomes(outcomes)

    def list_due_outcomes(self, *, as_of: date, limit: int = 500) -> list[Any]:
        return self.repository.list_due_outcomes(as_of=as_of, limit=limit)

    def list_pending_without_due(self, *, limit: int = 500) -> list[Any]:
        statement = (
            select(StrategyObservationOutcomeORM)
            .where(
                StrategyObservationOutcomeORM.status == "pending",
                StrategyObservationOutcomeORM.due_trade_date.is_(None),
            )
            .order_by(
                StrategyObservationOutcomeORM.created_at,
                StrategyObservationOutcomeORM.outcome_id,
            )
            .limit(limit)
        )
        return list(self.session.scalars(statement))

    def update_outcome_due_dates(self, updates: Sequence[JsonDict]) -> int:
        """在真实交易日出现后只回填到期日，不提前生成收益标签。"""

        updated = 0
        for item in _dedupe_dicts(updates, key="outcome_id"):
            result = self.session.execute(
                update(StrategyObservationOutcomeORM)
                .where(
                    StrategyObservationOutcomeORM.outcome_id == item["outcome_id"],
                    StrategyObservationOutcomeORM.status == "pending",
                    StrategyObservationOutcomeORM.due_trade_date.is_(None),
                )
                .values(
                    due_trade_date=item["due_trade_date"],
                    reason=item.get("reason"),
                    updated_at=datetime.now().astimezone(),
                )
            )
            updated += int(result.rowcount or 0)
        if updates:
            self.session.flush()
        return updated

    def mature_outcomes(self, outcomes: Sequence[JsonDict]) -> int:
        return self.repository.mature_outcomes(outcomes)

    def get_trial_state(self, strategy_id: str) -> Any | None:
        return self.repository.get_trial_state(strategy_id)

    def upsert_trial_state(self, **kwargs: Any) -> Any:
        return self.repository.upsert_trial_state(**kwargs)

    def list_recent_matured_outcomes(
        self,
        *,
        strategy_id: str,
        horizon_days: int | None = None,
        limit: int = 500,
    ) -> list[Any]:
        return self.repository.list_recent_matured_outcomes(
            strategy_id=strategy_id,
            horizon_days=horizon_days,
            limit=limit,
        )

    def update_position_entries(self, entries: Sequence[JsonDict]) -> int:
        """只补写已存在仓位的 T+1 入场事实，不创建新仓位。"""

        updated = 0
        for item in _dedupe_dicts(entries, key="position_id"):
            values = {
                key: value
                for key, value in item.items()
                if key
                in {
                    "entry_date",
                    "entry_price",
                    "benchmark_entry_price",
                    "status",
                    "payload",
                    "updated_at",
                }
            }
            values.setdefault("updated_at", datetime.now().astimezone())
            result = self.session.execute(
                update(StrategyObservationPositionORM)
                .where(StrategyObservationPositionORM.position_id == item["position_id"])
                .values(**values)
            )
            updated += int(result.rowcount or 0)
        if entries:
            self.session.flush()
        return updated


class SqlStrategyObservationSource:
    """从评分截面、交易日历和 canonical 日 K 读取观察输入。"""

    def __init__(
        self,
        session: Session,
        *,
        market_bar_source: str = DEFAULT_MARKET_BAR_SOURCE,
        code_commit: str | None = None,
    ) -> None:
        self.session = session
        self.market_bar_source = market_bar_source
        self.code_commit = code_commit

    def list_top_scores(
        self,
        *,
        screening_id: str,
        strategy_id: str,
        limit: int,
    ) -> list[AssetScoreORM]:
        statement = (
            select(AssetScoreORM)
            .where(
                AssetScoreORM.screening_id == screening_id,
                AssetScoreORM.strategy_id == strategy_id,
                AssetScoreORM.status.in_(FINAL_SCORE_STATUSES),
            )
            .order_by(
                AssetScoreORM.rank,
                AssetScoreORM.total_score.desc(),
                AssetScoreORM.asset_id,
            )
            .limit(limit)
        )
        return list(self.session.scalars(statement))

    def future_trade_dates(self, *, trade_date: date, count: int) -> list[date]:
        statement = (
            select(MarketCalendarORM.trade_date)
            .where(
                MarketCalendarORM.market == "ashare",
                MarketCalendarORM.trade_date > trade_date,
                MarketCalendarORM.is_trading_day.is_(True),
                MarketCalendarORM.status.in_(FINAL_BAR_STATUSES),
            )
            .distinct()
            .order_by(MarketCalendarORM.trade_date)
            .limit(count)
        )
        dates = list(self.session.scalars(statement))
        if len(dates) >= count:
            return dates
        fallback = (
            select(func.date(MarketBarORM.timestamp))
            .where(
                *self._bar_predicates(),
                func.date(MarketBarORM.timestamp) > trade_date,
            )
            .distinct()
            .order_by(func.date(MarketBarORM.timestamp))
            .limit(count)
        )
        return [value for value in self.session.scalars(fallback) if isinstance(value, date)]

    def data_versions(self, *, screening_id: str) -> JsonDict:
        score_max_at = self.session.scalar(
            select(func.max(AssetScoreORM.as_of)).where(
                AssetScoreORM.screening_id == screening_id
            )
        )
        bar_max_at = self.session.scalar(
            select(func.max(MarketBarORM.timestamp)).where(*self._bar_predicates())
        )
        return {
            "code_commit": self.code_commit,
            "screening_id": screening_id,
            "asset_scores_max_at": _iso_or_none(score_max_at),
            "market_bars_max_at": _iso_or_none(bar_max_at),
            "market_bar_source": self.market_bar_source,
        }

    def resolve_outcome(
        self,
        outcome: Any,
        *,
        round_trip_cost: float,
    ) -> JsonDict:
        """结算单个仓位，并用同一 screening 候选池计算等权基准。"""

        payload = dict(getattr(outcome, "payload", None) or {})
        asset_id = str(payload.get("asset_id") or "")
        screening_id = str(payload.get("screening_id") or "")
        signal_date = _as_date(payload.get("signal_date"))
        horizon_days = int(outcome.horizon_days)
        if not asset_id or not screening_id or signal_date is None:
            return _pending_result("missing_outcome_context", round_trip_cost=round_trip_cost)

        benchmark_assets = list(
            self.session.scalars(
                select(ScreeningResultItemORM.asset_id)
                .where(
                    ScreeningResultItemORM.screening_id == screening_id,
                    ScreeningResultItemORM.passed.is_(True),
                )
                .order_by(ScreeningResultItemORM.asset_id)
            )
        )
        asset_ids = list(dict.fromkeys([asset_id, *benchmark_assets]))
        bars = self._bars_for_assets(
            asset_ids=asset_ids,
            signal_date=signal_date,
            due_trade_date=getattr(outcome, "due_trade_date", None),
        )
        labels = {
            candidate_id: label_forward_position(
                frame,
                signal_date=signal_date,
                horizon_days=horizon_days,
                round_trip_cost=round_trip_cost,
            )
            for candidate_id, frame in bars.items()
        }
        target = labels.get(asset_id)
        if target is None:
            return _pending_result("missing_price", round_trip_cost=round_trip_cost)
        benchmark_labels = [
            labels.get(candidate_id)
            for candidate_id in benchmark_assets
            if labels.get(candidate_id) is not None
        ]
        if not benchmark_labels:
            return _pending_result("missing_benchmark_price", round_trip_cost=round_trip_cost)
        benchmark_return = float(
            np.mean([label.gross_return for label in benchmark_labels if label is not None])
        )
        return {
            "status": "matured",
            "entry_date": target.entry_date,
            "entry_price": _decimal8(target.entry_price),
            "benchmark_entry_price": None,
            "exit_date": target.exit_date,
            "exit_price": _decimal8(target.exit_price),
            "gross_return": _decimal8(target.gross_return),
            "net_return": _decimal8(target.net_return),
            "benchmark_return": _decimal8(benchmark_return),
            "excess_return": _decimal8(target.net_return - benchmark_return),
            "reason": None,
            "payload": {
                "round_trip_cost": round_trip_cost,
                "benchmark_asset_count": len(benchmark_assets),
                "benchmark_available_count": len(benchmark_labels),
            },
        }

    def _bars_for_assets(
        self,
        *,
        asset_ids: Sequence[str],
        signal_date: date,
        due_trade_date: date | None,
    ) -> dict[str, pd.DataFrame]:
        if not asset_ids or due_trade_date is None:
            return {}
        start_at = datetime.combine(signal_date, time.min, tzinfo=UTC)
        end_at = datetime.combine(due_trade_date, time.max, tzinfo=UTC)
        statement = (
            select(MarketBarORM)
            .where(
                *self._bar_predicates(),
                MarketBarORM.asset_id.in_(asset_ids),
                MarketBarORM.timestamp >= start_at,
                MarketBarORM.timestamp <= end_at,
            )
            .order_by(MarketBarORM.asset_id, MarketBarORM.timestamp)
        )
        grouped: dict[str, list[MarketBarORM]] = defaultdict(list)
        for row in self.session.scalars(statement):
            grouped[row.asset_id].append(row)
        return {asset_id: _bars_to_frame(rows) for asset_id, rows in grouped.items()}

    def _bar_predicates(self) -> tuple[Any, ...]:
        return (
            MarketBarORM.market == "ashare",
            MarketBarORM.timeframe == "1d",
            MarketBarORM.source == self.market_bar_source,
            MarketBarORM.is_closed.is_(True),
            MarketBarORM.status.in_(FINAL_BAR_STATUSES),
        )


@dataclass(frozen=True)
class StrategyObservationService:
    """编排每日观察、到期结算和周度状态评估。"""

    repository: ObservationRepository
    source: ObservationSource
    topn: int = 20
    round_trip_cost: float = FIXED_ROUND_TRIP_COST
    default_universe_id: str = DEFAULT_UNIVERSE_ID

    def __post_init__(self) -> None:
        if self.topn <= 0:
            raise ValueError("观察 Top N 必须为正数")
        if not math.isclose(self.round_trip_cost, FIXED_ROUND_TRIP_COST, abs_tol=1e-12):
            raise ValueError("前向观察交易成本固定为 0.003")

    def capture(
        self,
        *,
        screening_id: str,
        trade_date: date,
        strategy_ids: Sequence[str],
    ) -> JsonDict:
        """从同一 screening 为多策略追加每日 Top N 观察仓位。"""

        normalized_strategy_ids = tuple(dict.fromkeys(str(item) for item in strategy_ids))
        if not normalized_strategy_ids:
            raise ValueError("至少需要一个观察策略")
        scores_by_strategy = {
            strategy_id: self.source.list_top_scores(
                screening_id=screening_id,
                strategy_id=strategy_id,
                limit=self.topn,
            )
            for strategy_id in normalized_strategy_ids
        }
        universe_ids = {
            str(score.universe_id)
            for scores in scores_by_strategy.values()
            for score in scores
            if getattr(score, "universe_id", None)
        }
        if len(universe_ids) > 1:
            raise ValueError("同一观察批次不能混用多个候选池")
        universe_id = next(iter(universe_ids), self.default_universe_id)
        observation_id = build_observation_id(trade_date=trade_date, universe_id=universe_id)
        future_dates = self.source.future_trade_dates(
            trade_date=trade_date,
            count=max(SUPPORTED_HORIZONS),
        )
        data_versions = self.source.data_versions(screening_id=screening_id)
        self.repository.upsert_run(
            observation_id=observation_id,
            trade_date=trade_date,
            universe_id=universe_id,
            screening_id=screening_id,
            status="captured",
            data_versions=data_versions,
            payload={
                "strategy_ids": list(normalized_strategy_ids),
                "topn": self.topn,
                "round_trip_cost": self.round_trip_cost,
            },
        )

        positions: list[JsonDict] = []
        outcomes: list[JsonDict] = []
        for strategy_id, scores in scores_by_strategy.items():
            for ordinal, score in enumerate(scores[: self.topn], start=1):
                position_id = build_position_id(
                    observation_id=observation_id,
                    strategy_id=strategy_id,
                    asset_id=str(score.asset_id),
                )
                rank = int(getattr(score, "rank", None) or ordinal)
                positions.append(
                    {
                        "position_id": position_id,
                        "observation_id": observation_id,
                        "strategy_id": strategy_id,
                        "asset_id": str(score.asset_id),
                        "symbol": str(score.symbol),
                        "rank": rank,
                        "score_id": str(score.score_id),
                        "signal_date": trade_date,
                        "entry_date": future_dates[0] if future_dates else None,
                        "entry_price": None,
                        "benchmark_entry_price": None,
                        "status": "pending",
                        "payload": {
                            "total_score": str(score.total_score),
                            "factor_frame_id": str(score.factor_frame_id),
                            "score_as_of": _iso_or_none(getattr(score, "as_of", None)),
                        },
                    }
                )
                for horizon_days in SUPPORTED_HORIZONS:
                    due_trade_date = (
                        future_dates[horizon_days - 1]
                        if len(future_dates) >= horizon_days
                        else None
                    )
                    outcomes.append(
                        {
                            "outcome_id": build_outcome_id(
                                position_id=position_id,
                                horizon_days=horizon_days,
                            ),
                            "position_id": position_id,
                            "horizon_days": horizon_days,
                            "due_trade_date": due_trade_date,
                            "status": "pending",
                            "reason": (
                                None if due_trade_date is not None else "missing_trading_calendar"
                            ),
                            "payload": {
                                "asset_id": str(score.asset_id),
                                "symbol": str(score.symbol),
                                "signal_date": trade_date.isoformat(),
                                "screening_id": screening_id,
                                "universe_id": universe_id,
                                "strategy_id": strategy_id,
                                "score_id": str(score.score_id),
                                "round_trip_cost": self.round_trip_cost,
                            },
                        }
                    )
        position_count = self.repository.upsert_positions(positions)
        outcome_count = self.repository.ensure_outcomes(outcomes)
        return {
            "status": "captured" if positions else "unavailable",
            "observation_id": observation_id,
            "trade_date": trade_date,
            "universe_id": universe_id,
            "position_count": position_count,
            "outcome_count": outcome_count,
            "strategy_counts": {
                strategy_id: len(scores) for strategy_id, scores in scores_by_strategy.items()
            },
        }

    def settle_due(self, *, as_of: date, limit: int = 500) -> JsonDict:
        """结算到期标签；缺价格时保留 pending 并记录原因。"""

        due_date_backfilled_count = self._backfill_due_dates(limit=limit)
        due = self.repository.list_due_outcomes(as_of=as_of, limit=limit)
        entry_updates: list[JsonDict] = []
        outcome_updates: list[JsonDict] = []
        matured_count = 0
        pending_count = 0
        for outcome in due:
            result = self.source.resolve_outcome(
                outcome,
                round_trip_cost=self.round_trip_cost,
            )
            status = str(result.get("status") or "pending")
            payload = dict(getattr(outcome, "payload", None) or {})
            payload.update(result.get("payload") or {})
            if status != "matured":
                pending_count += 1
                outcome_updates.append(
                    {
                        "outcome_id": outcome.outcome_id,
                        "status": "pending",
                        "reason": str(result.get("reason") or "missing_price"),
                        "payload": payload,
                    }
                )
                continue
            matured_count += 1
            entry_updates.append(
                {
                    "position_id": outcome.position_id,
                    "entry_date": result.get("entry_date"),
                    "entry_price": result.get("entry_price"),
                    "benchmark_entry_price": result.get("benchmark_entry_price"),
                    "status": "entered",
                }
            )
            outcome_updates.append(
                {
                    "outcome_id": outcome.outcome_id,
                    "status": "matured",
                    "exit_date": result.get("exit_date"),
                    "exit_price": result.get("exit_price"),
                    "gross_return": result.get("gross_return"),
                    "net_return": result.get("net_return"),
                    "benchmark_return": result.get("benchmark_return"),
                    "excess_return": result.get("excess_return"),
                    "reason": None,
                    "payload": payload,
                }
            )
        self.repository.update_position_entries(entry_updates)
        self.repository.mature_outcomes(outcome_updates)
        return {
            "status": "available",
            "due_date_backfilled_count": due_date_backfilled_count,
            "due_count": len(due),
            "matured_count": matured_count,
            "pending_count": pending_count,
        }

    def _backfill_due_dates(self, *, limit: int) -> int:
        pending = self.repository.list_pending_without_due(limit=limit)
        calendar_cache: dict[date, list[date]] = {}
        updates: list[JsonDict] = []
        for outcome in pending:
            payload = dict(getattr(outcome, "payload", None) or {})
            signal_date = _as_date(payload.get("signal_date"))
            horizon_days = int(outcome.horizon_days)
            if signal_date is None or horizon_days not in SUPPORTED_HORIZONS:
                continue
            if signal_date not in calendar_cache:
                calendar_cache[signal_date] = self.source.future_trade_dates(
                    trade_date=signal_date,
                    count=max(SUPPORTED_HORIZONS),
                )
            future_dates = calendar_cache[signal_date]
            if len(future_dates) < horizon_days:
                continue
            updates.append(
                {
                    "outcome_id": outcome.outcome_id,
                    "due_trade_date": future_dates[horizon_days - 1],
                    "reason": None,
                }
            )
        return self.repository.update_outcome_due_dates(updates)

    def apply_historical_result(self, *, strategy_id: str, result: Any) -> Any:
        """历史门槛通过后把新增策略切换到受控试运行。"""

        status = str(_value(result, "status", ""))
        metrics = dict(_value(result, "metrics", {}) or {})
        passed = status == "available" and bool(metrics.get("gate_passed"))
        backtest_id = str(_value(result, "backtest_id", "") or "")
        current = self.repository.get_trial_state(strategy_id)
        if current is not None and str(current.state) in {"disabled", "validated"}:
            return current
        payload = dict(getattr(current, "payload", None) or {})
        payload["historical_result"] = {
            "status": status,
            "backtest_id": backtest_id or None,
            "gate_passed": bool(metrics.get("gate_passed")),
            "data_versions": dict(_value(result, "data_versions", {}) or {}),
        }
        return self.repository.upsert_trial_state(
            strategy_id=strategy_id,
            strategy_version=_strategy_version(strategy_id),
            state="trial" if passed else "research",
            historical_evidence_id=backtest_id if passed else None,
            forward_metrics={},
            consecutive_failure_count=0,
            disabled_reason=None,
            last_evaluated_at=None,
            payload=payload,
        )

    def evaluate_weekly(
        self,
        *,
        strategy_id: str,
        as_of: datetime,
        metrics: Mapping[str, Any] | None = None,
    ) -> Any:
        """按周评估试运行策略，执行关闭、保持或晋级。"""

        current = self.repository.get_trial_state(strategy_id)
        if current is None:
            raise ValueError(f"找不到策略试运行状态：{strategy_id}")
        if str(current.state) != "trial":
            return current
        normalized_as_of = _ensure_aware(as_of)
        last_evaluated_at = getattr(current, "last_evaluated_at", None)
        if isinstance(last_evaluated_at, datetime) and _same_iso_week(
            last_evaluated_at,
            normalized_as_of,
        ):
            return current

        forward_metrics = dict(metrics or self.build_forward_metrics(strategy_id=strategy_id))
        integrity_violations = [
            str(value) for value in forward_metrics.get("data_integrity_violations") or []
        ]
        drawdown_gap = _number(forward_metrics.get("drawdown_gap"), default=0.0)
        immediate_reason = None
        if integrity_violations:
            immediate_reason = "data_integrity:" + ",".join(integrity_violations)
        elif drawdown_gap >= IMMEDIATE_DRAWDOWN_GAP:
            immediate_reason = "drawdown_gap_above_10pct"

        payload = dict(getattr(current, "payload", None) or {})
        if immediate_reason:
            return self._save_state(
                current=current,
                state="disabled",
                metrics=forward_metrics,
                failure_count=int(current.consecutive_failure_count),
                disabled_reason=immediate_reason,
                as_of=normalized_as_of,
                payload=payload,
            )

        sample_counts = dict(forward_metrics.get("sample_counts") or {})
        if int(sample_counts.get("20", 0)) >= VALIDATED_T20_SAMPLES and bool(
            forward_metrics.get("gate_passed")
        ):
            return self._save_state(
                current=current,
                state="validated",
                metrics=forward_metrics,
                failure_count=0,
                disabled_reason=None,
                as_of=normalized_as_of,
                payload=payload,
            )

        enough_samples = (
            int(sample_counts.get("10", 0)) >= MINIMUM_T10_SAMPLES
            and int(sample_counts.get("20", 0)) >= MINIMUM_T20_SAMPLES
        )
        medians = dict(forward_metrics.get("median_excess_returns") or {})
        horizon_values = [
            _optional_number(medians.get(str(horizon))) for horizon in SUPPORTED_HORIZONS
        ]
        non_positive_count = sum(value is not None and value <= 0 for value in horizon_values)
        t10_value = _optional_number(medians.get("10"))
        failed = (
            enough_samples
            and t10_value is not None
            and t10_value <= 0
            and non_positive_count >= 2
        )
        failure_count = (
            int(current.consecutive_failure_count) + 1
            if failed
            else (0 if enough_samples else int(current.consecutive_failure_count))
        )
        state = "disabled" if failure_count >= 3 else str(current.state)
        disabled_reason = "three_consecutive_forward_failures" if state == "disabled" else None
        gate_decision = StrategyValidationGate().evaluate_forward(
            state=current,
            outcomes=forward_metrics,
        )
        if gate_decision.next_state == "disabled":
            state = "disabled"
            disabled_reason = ",".join(gate_decision.reason_codes) or "forward_validation_failed"
        return self._save_state(
            current=current,
            state=state,
            metrics=forward_metrics,
            failure_count=failure_count,
            disabled_reason=disabled_reason,
            as_of=normalized_as_of,
            payload=payload,
        )

    def build_forward_metrics(self, *, strategy_id: str) -> JsonDict:
        """从成熟标签按信号截面聚合周度评估指标。"""

        rows = self.repository.list_recent_matured_outcomes(
            strategy_id=strategy_id,
            limit=5_000,
        )
        grouped: dict[tuple[date, int], dict[str, list[float]]] = defaultdict(
            lambda: defaultdict(list)
        )
        integrity_violations: list[str] = []
        for row in rows:
            payload = dict(getattr(row, "payload", None) or {})
            signal_date = _as_date(payload.get("signal_date"))
            horizon_days = int(row.horizon_days)
            if signal_date is None or horizon_days not in SUPPORTED_HORIZONS:
                integrity_violations.append(f"invalid_outcome_context:{row.outcome_id}")
                continue
            metrics = {
                "gross_return": getattr(row, "gross_return", None),
                "net_return": getattr(row, "net_return", None),
                "benchmark_return": getattr(row, "benchmark_return", None),
                "excess_return": getattr(row, "excess_return", None),
            }
            if any(_optional_number(value) is None for value in metrics.values()):
                integrity_violations.append(f"missing_outcome_return:{row.outcome_id}")
                continue
            for key, value in metrics.items():
                grouped[(signal_date, horizon_days)][key].append(float(value))

        aggregates: dict[tuple[date, int], dict[str, float]] = {
            key: {metric: float(np.mean(values)) for metric, values in metrics.items()}
            for key, metrics in grouped.items()
        }
        sample_counts = {
            str(horizon): sum(1 for _day, item_horizon in aggregates if item_horizon == horizon)
            for horizon in SUPPORTED_HORIZONS
        }
        median_excess = {
            str(horizon): _median_or_none(
                [
                    metrics["excess_return"]
                    for (_day, item_horizon), metrics in aggregates.items()
                    if item_horizon == horizon
                ]
            )
            for horizon in SUPPORTED_HORIZONS
        }
        complete_dates = sorted(
            day
            for day in {item[0] for item in aggregates}
            if all((day, horizon) in aggregates for horizon in SUPPORTED_HORIZONS)
        )
        gate_passed = False
        drawdown_gap = 0.0
        if complete_dates:
            gate_outcomes = [
                WalkForwardOutcome(
                    signal_date=signal_date,
                    entry_date=signal_date,
                    exit_date=signal_date,
                    horizon_days=horizon,
                    gross_return=aggregates[(signal_date, horizon)]["gross_return"],
                    net_return=aggregates[(signal_date, horizon)]["net_return"],
                    benchmark_return=aggregates[(signal_date, horizon)]["benchmark_return"],
                    excess_return=aggregates[(signal_date, horizon)]["excess_return"],
                )
                for signal_date in complete_dates
                for horizon in SUPPORTED_HORIZONS
            ]
            gate = evaluate_historical_gate(
                gate_outcomes,
                coverage_by_date={day: [1.0] * 20 for day in complete_dates},
            )
            gate_passed = gate.passed
            drawdown_gap = float(gate.metrics.get("drawdown_gap") or 0.0)
        return {
            "sample_counts": sample_counts,
            "median_excess_returns": median_excess,
            "drawdown_gap": drawdown_gap,
            "data_integrity_violations": list(dict.fromkeys(integrity_violations)),
            "gate_passed": gate_passed,
        }

    def _save_state(
        self,
        *,
        current: Any,
        state: str,
        metrics: JsonDict,
        failure_count: int,
        disabled_reason: str | None,
        as_of: datetime,
        payload: JsonDict,
    ) -> Any:
        return self.repository.upsert_trial_state(
            strategy_id=str(current.strategy_id),
            strategy_version=str(current.strategy_version),
            state=state,
            historical_evidence_id=getattr(current, "historical_evidence_id", None),
            forward_metrics=metrics,
            consecutive_failure_count=failure_count,
            disabled_reason=disabled_reason,
            last_evaluated_at=as_of,
            payload=payload,
        )


def create_strategy_observation_service(
    session: Session,
    *,
    code_commit: str | None = None,
) -> StrategyObservationService:
    """构建生产数据库观察服务。"""

    return StrategyObservationService(
        repository=SqlStrategyObservationStore(session),
        source=SqlStrategyObservationSource(session, code_commit=code_commit),
    )


def build_observation_id(*, trade_date: date, universe_id: str) -> str:
    digest = hashlib.sha256(universe_id.encode("utf-8")).hexdigest()[:12]
    return f"obs:{trade_date:%Y%m%d}:{digest}"


def build_position_id(*, observation_id: str, strategy_id: str, asset_id: str) -> str:
    digest = _stable_digest(observation_id, strategy_id, asset_id)
    return f"position:{digest}"


def build_outcome_id(*, position_id: str, horizon_days: int) -> str:
    return f"outcome:{_stable_digest(position_id, horizon_days)}"


def _stable_digest(*values: Any) -> str:
    raw = "|".join(str(value) for value in values)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _strategy_version(strategy_id: str) -> str:
    suffix = strategy_id.rsplit("_v", maxsplit=1)
    return f"v{suffix[1]}" if len(suffix) == 2 and suffix[1].isdigit() else "v1"


def _pending_result(reason: str, *, round_trip_cost: float) -> JsonDict:
    return {
        "status": "pending",
        "reason": reason,
        "payload": {"round_trip_cost": round_trip_cost},
    }


def _bars_to_frame(rows: Sequence[MarketBarORM]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": [row.timestamp for row in rows],
            "open": [float(row.open) for row in rows],
            "high": [float(row.high) for row in rows],
            "low": [float(row.low) for row in rows],
            "close": [float(row.close) for row in rows],
            "volume": [float(row.volume) for row in rows],
            "amount": [float(row.amount) if row.amount is not None else np.nan for row in rows],
        }
    )


def _decimal8(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.00000001"))


def _dedupe_dicts(rows: Sequence[JsonDict], *, key: str) -> list[JsonDict]:
    deduped: dict[str, JsonDict] = {}
    for row in rows:
        deduped[str(row[key])] = dict(row)
    return list(deduped.values())


def _value(source: Any, key: str, default: Any) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _same_iso_week(left: datetime, right: datetime) -> bool:
    left_calendar = _ensure_aware(left).isocalendar()
    right_calendar = _ensure_aware(right).isocalendar()
    return (left_calendar.year, left_calendar.week) == (right_calendar.year, right_calendar.week)


def _ensure_aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _optional_number(value: Any) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _number(value: Any, *, default: float) -> float:
    parsed = _optional_number(value)
    return parsed if parsed is not None else default


def _median_or_none(values: Sequence[float]) -> float | None:
    return float(median(values)) if values else None


def _iso_or_none(value: Any) -> str | None:
    return _ensure_aware(value).isoformat() if isinstance(value, datetime) else None
