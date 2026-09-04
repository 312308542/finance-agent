"""生成收盘推荐使用的市场状态和热门板块不可变快照。"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from statistics import median, pstdev
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.application.market_context_service import (
    MarketRegimeInput,
    MarketRegimeService,
)
from finance_agent.application.theme_context_service import ThemeContextService
from finance_agent.data.freshness import ashare_market_close_at
from finance_agent.storage.repositories import (
    DataSnapshotRepository,
    MarketDataRepository,
    UniverseRepository,
)
from finance_agent.storage.snapshot_contracts import build_data_snapshot

JsonDict = dict[str, Any]


class ClosingDecisionContextService:
    """只读收盘事实并追加市场、板块决策上下文。"""

    def __init__(
        self,
        session: Session | None = None,
        *,
        universe_repository: Any | None = None,
        market_data_repository: Any | None = None,
        snapshot_repository: Any | None = None,
        theme_contexts: Any | None = None,
        minimum_market_assets: int = 100,
        minimum_market_coverage_ratio: float = 0.95,
        maximum_cross_section_skew: timedelta = timedelta(minutes=5),
    ) -> None:
        if session is None and any(
            item is None
            for item in (
                universe_repository,
                market_data_repository,
                snapshot_repository,
                theme_contexts,
            )
        ):
            raise ValueError("未提供数据库会话时必须注入全部事实适配器。")
        self.universes = universe_repository or UniverseRepository(session)  # type: ignore[arg-type]
        self.market_data = market_data_repository or MarketDataRepository(session)  # type: ignore[arg-type]
        self.snapshots = snapshot_repository or DataSnapshotRepository(session)  # type: ignore[arg-type]
        self.theme_contexts = theme_contexts or ThemeContextService(session)
        self.minimum_market_assets = max(1, int(minimum_market_assets))
        if not 0 < float(minimum_market_coverage_ratio) <= 1:
            raise ValueError("minimum_market_coverage_ratio 必须位于 0 到 1 之间。")
        self.minimum_market_coverage_ratio = float(minimum_market_coverage_ratio)
        self.maximum_cross_section_skew = maximum_cross_section_skew

    def refresh(
        self,
        *,
        context_type: str,
        market: str,
        universe_id: str,
        lookback_bars: int = 61,
    ) -> JsonDict:
        """按任务类型刷新一份上下文快照。"""

        if context_type == "market":
            return self.refresh_market(
                market=market,
                universe_id=universe_id,
                lookback_bars=lookback_bars,
            )
        if context_type == "sector":
            return self.refresh_sectors(
                market=market,
                universe_id=universe_id,
                lookback_bars=lookback_bars,
            )
        raise ValueError(f"不支持的决策上下文类型: {context_type}")

    def refresh_market(
        self,
        *,
        market: str,
        universe_id: str,
        lookback_bars: int,
    ) -> JsonDict:
        """从同一收盘截面的全市场日 K 计算市场状态。"""

        members = self.universes.list_members(universe_id, included_only=True)
        asset_ids = sorted({str(item.asset_id) for item in members if item.asset_id})
        bars = self.market_data.list_recent_bars_for_assets(
            asset_ids=asset_ids,
            timeframe="1d",
            limit_per_asset=max(61, int(lookback_bars)),
        )
        grouped = _bars_by_asset(bars)
        complete_histories = {
            asset_id: rows
            for asset_id, rows in grouped.items()
            if len(rows) >= max(61, int(lookback_bars))
        }
        as_of = _latest_timestamp(bars)
        if as_of is None:
            raise ValueError("没有闭合日 K，不能生成收盘市场快照。")

        histories = {
            asset_id: rows
            for asset_id, rows in complete_histories.items()
            if as_of - _effective_bar_time(rows[-1]) <= self.maximum_cross_section_skew
        }
        stale_asset_count = len(complete_histories) - len(histories)
        missing_history_asset_count = len(asset_ids) - len(complete_histories)
        coverage_ratio = len(histories) / max(len(asset_ids), 1)
        if (
            len(histories) < self.minimum_market_assets
            or coverage_ratio < self.minimum_market_coverage_ratio
        ):
            quality_status = "partial"
            payload = _insufficient_market_payload(
                sample_count=len(histories),
                universe_count=len(asset_ids),
                as_of=as_of,
                stale_asset_count=stale_asset_count,
                missing_history_asset_count=missing_history_asset_count,
            )
        else:
            metrics = _market_metrics(histories)
            regime = MarketRegimeService().evaluate(
                MarketRegimeInput(
                    index_trend_20d=metrics["trend_20d"],
                    index_trend_60d=metrics["trend_60d"],
                    volatility_20d=metrics["volatility_20d"],
                    advance_decline_ratio=metrics["advance_decline_ratio"],
                    limit_up_down_ratio=metrics["limit_up_down_ratio"],
                    northbound_flow_score=0.0,
                    evidence_ids=tuple(
                        f"market-bar:{asset_id}:{histories[asset_id][-1].timestamp.isoformat()}"
                        for asset_id in sorted(histories)
                    ),
                )
            )
            quality_status = "available"
            payload = regime.to_dict() | {
                "as_of": as_of.isoformat(),
                "quality_status": quality_status,
                "sample_count": len(histories),
                "universe_count": len(asset_ids),
                "coverage_ratio": coverage_ratio,
                "stale_asset_count": stale_asset_count,
                "missing_history_asset_count": missing_history_asset_count,
                "metrics": metrics,
            }
        snapshot = build_data_snapshot(
            snapshot_type="market_regime",
            market=market,
            as_of=as_of,
            captured_at=as_of,
            provider="finance_agent:closing_decision_context",
            provider_version="market-regime-v1",
            quality_status=quality_status,
            payload=payload,
            metadata={"universe_id": universe_id},
        )
        self.snapshots.insert_snapshot(snapshot)
        return {
            "status": quality_status,
            "data_snapshot_id": snapshot.data_snapshot_id,
            "market": market,
            "regime": payload["regime"],
            "sample_count": payload["sample_count"],
            "as_of": as_of.isoformat(),
        }

    def refresh_sectors(
        self,
        *,
        market: str,
        universe_id: str,
        lookback_bars: int,
    ) -> JsonDict:
        """冻结候选池的热门板块阶段、龙头和证据。"""

        members = self.universes.list_members(universe_id, included_only=True)
        asset_ids = sorted({str(item.asset_id) for item in members if item.asset_id})
        bars = self.market_data.list_recent_bars_for_assets(
            asset_ids=asset_ids,
            timeframe="1d",
            limit_per_asset=max(1, int(lookback_bars)),
        )
        as_of = _latest_timestamp(bars)
        if as_of is None:
            raise ValueError("没有闭合日 K，不能生成热门板块快照。")
        market_snapshot = (
            self.snapshots.get_latest(snapshot_type="market_regime", market=market)
            if hasattr(self.snapshots, "get_latest")
            else None
        )
        market_context_current = (
            market_snapshot is not None
            and isinstance(getattr(market_snapshot, "as_of", None), datetime)
            and abs(as_of - market_snapshot.as_of) <= self.maximum_cross_section_skew
            and str(getattr(market_snapshot, "quality_status", "")) == "available"
        )
        market_regime = (
            str((market_snapshot.payload or {}).get("regime") or "risk_off")
            if market_context_current
            else "risk_off"
        )
        contexts = self.theme_contexts.build_for_members(
            members,
            market_regime=market_regime,
            as_of=as_of,
        )
        grouped = _bars_by_asset(bars)
        fresh_asset_ids = {
            asset_id
            for asset_id, rows in grouped.items()
            if rows and as_of - _effective_bar_time(rows[-1]) <= self.maximum_cross_section_skew
        }
        stale_asset_count = len(asset_ids) - len(fresh_asset_ids)

        sectors: dict[str, JsonDict] = {}
        assets: list[JsonDict] = []
        evidence_ids: set[str] = set()
        for asset_id in sorted(contexts):
            if asset_id not in fresh_asset_ids:
                continue
            context = contexts[asset_id]
            rows = _context_sequence(context, "sectors")
            leadership = _context_mapping(context, "leadership")
            for row in rows:
                sector_id = str(row.get("sector_id") or "")
                if not sector_id:
                    continue
                current = sectors.get(sector_id)
                if current is None or float(row.get("strength_score") or 0) > float(
                    current.get("strength_score") or 0
                ):
                    sectors[sector_id] = {
                        **row,
                        "leader_asset_ids": list(
                            (current or {}).get("leader_asset_ids", [])
                        ),
                        "challenger_asset_ids": list(
                            (current or {}).get("challenger_asset_ids", [])
                        ),
                    }
                sector = sectors[sector_id]
                if leadership and str(leadership.get("sector_id") or "") == sector_id:
                    role = str(leadership.get("role") or "")
                    key = (
                        "leader_asset_ids"
                        if role == "leader"
                        else "challenger_asset_ids"
                        if role == "challenger"
                        else None
                    )
                    if key is not None and asset_id not in sector[key]:
                        sector[key].append(asset_id)
            context_evidence = _context_strings(context, "evidence_ids")
            evidence_ids.update(context_evidence)
            assets.append(
                {
                    "asset_id": asset_id,
                    "sectors": rows,
                    "leadership": leadership,
                    "evidence_ids": list(context_evidence),
                }
            )
        quality_status = (
            "available"
            if sectors and stale_asset_count == 0 and market_context_current
            else "partial"
        )
        payload = {
            "as_of": as_of.isoformat(),
            "quality_status": quality_status,
            "sector_opportunities": [sectors[key] for key in sorted(sectors)],
            "assets": assets,
            "evidence_ids": sorted(evidence_ids),
            "universe_id": universe_id,
            "lookback_bars": int(lookback_bars),
            "stale_asset_count": stale_asset_count,
            "reason_codes": (
                [
                    *(
                        ["sector_cross_section_stale_assets"]
                        if stale_asset_count
                        else []
                    ),
                    *(
                        ["market_regime_generation_mismatch"]
                        if not market_context_current
                        else []
                    ),
                ]
            ),
            "market_snapshot_id": (
                str(market_snapshot.data_snapshot_id)
                if market_context_current
                else None
            ),
        }
        snapshot = build_data_snapshot(
            snapshot_type="sector_opportunities",
            market=market,
            as_of=as_of,
            captured_at=as_of,
            provider="finance_agent:closing_decision_context",
            provider_version="sector-opportunity-v1",
            quality_status=quality_status,
            payload=payload,
            metadata={"universe_id": universe_id},
        )
        self.snapshots.insert_snapshot(snapshot)
        return {
            "status": quality_status,
            "data_snapshot_id": snapshot.data_snapshot_id,
            "market": market,
            "sector_count": len(sectors),
            "asset_count": len(assets),
            "as_of": as_of.isoformat(),
        }


def _bars_by_asset(rows: Sequence[Any]) -> dict[str, list[Any]]:
    deduped: dict[str, dict[datetime, Any]] = {}
    for row in rows:
        asset_id = str(row.asset_id)
        timestamp = row.timestamp
        current = deduped.setdefault(asset_id, {}).get(timestamp)
        if current is None or str(getattr(row, "source", "")) < str(
            getattr(current, "source", "")
        ):
            deduped[asset_id][timestamp] = row
    return {
        asset_id: [by_time[key] for key in sorted(by_time)]
        for asset_id, by_time in deduped.items()
    }


def _latest_timestamp(rows: Sequence[Any]) -> datetime | None:
    values = [_effective_bar_time(row) for row in rows]
    values = [value for value in values if value is not None]
    return max(values) if values else None


def _effective_bar_time(row: Any) -> datetime | None:
    """返回行情行可参与决策的真实知识时点。"""

    timestamp = getattr(row, "timestamp", None)
    if not isinstance(timestamp, datetime):
        return None
    normalized = timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=UTC)
    timeframe = str(getattr(row, "timeframe", "1d") or "1d")
    return ashare_market_close_at(normalized) if timeframe == "1d" else (
        getattr(row, "end_timestamp", None) or normalized
    )


def _market_metrics(histories: Mapping[str, Sequence[Any]]) -> JsonDict:
    trend_20: list[float] = []
    trend_60: list[float] = []
    volatility: list[float] = []
    latest_returns: list[float] = []
    for rows in histories.values():
        closes = [float(row.close) for row in rows]
        returns = [
            current / previous - 1
            for previous, current in zip(closes, closes[1:], strict=False)
            if previous > 0
        ]
        if len(closes) < 61 or len(returns) < 20:
            continue
        trend_20.append(closes[-1] / closes[-21] - 1)
        trend_60.append(closes[-1] / closes[-61] - 1)
        volatility.append(pstdev(returns[-20:]) * math.sqrt(252))
        latest_returns.append(returns[-1])
    advances = sum(value > 0 for value in latest_returns)
    declines = sum(value < 0 for value in latest_returns)
    limit_ups = sum(value >= 0.095 for value in latest_returns)
    limit_downs = sum(value <= -0.095 for value in latest_returns)
    return {
        "trend_20d": median(trend_20),
        "trend_60d": median(trend_60),
        "volatility_20d": median(volatility),
        "advance_decline_ratio": advances / max(declines, 1),
        "limit_up_down_ratio": (
            limit_ups / max(limit_downs, 1) if limit_ups or limit_downs else 1.0
        ),
    }


def _insufficient_market_payload(
    *,
    sample_count: int,
    universe_count: int,
    as_of: datetime,
    stale_asset_count: int = 0,
    missing_history_asset_count: int = 0,
) -> JsonDict:
    return {
        "regime": "risk_off",
        "legacy_regime": "bear",
        "strength": "high",
        "risk_multiplier": 1.5,
        "risk_budget": {
            "total_exposure": 0.0,
            "per_position_risk": 0.0,
            "allow_new_buys": False,
            "allow_sector_override": False,
        },
        "reason_codes": ["market_history_coverage_insufficient"],
        "reasons": ["有效市场历史覆盖不足，关闭新增买入。"],
        "evidence_ids": [],
        "as_of": as_of.isoformat(),
        "quality_status": "partial",
        "sample_count": sample_count,
        "universe_count": universe_count,
        "coverage_ratio": sample_count / max(universe_count, 1),
        "stale_asset_count": stale_asset_count,
        "missing_history_asset_count": missing_history_asset_count,
        "metrics": {},
    }


def _context_sequence(context: Any, name: str) -> list[JsonDict]:
    value = context.get(name) if isinstance(context, Mapping) else getattr(context, name, ())
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _context_mapping(context: Any, name: str) -> JsonDict | None:
    value = context.get(name) if isinstance(context, Mapping) else getattr(context, name, None)
    return dict(value) if isinstance(value, Mapping) else None


def _context_strings(context: Any, name: str) -> tuple[str, ...]:
    value = context.get(name) if isinstance(context, Mapping) else getattr(context, name, ())
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return ()
    return tuple(str(item) for item in value if str(item).strip())
