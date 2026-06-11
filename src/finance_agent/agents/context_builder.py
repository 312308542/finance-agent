"""Agent 输入上下文构建器。

Agent 不直接查询散落的数据表。ContextBuilder 负责从事实库读取基础数据、
证据和风险线索，组装成结构化 JSON，并把短生命周期上下文缓存到 Redis。
缓存失效后可以随时从 PostgreSQL + TimescaleDB 重建。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_agent.ports.cache import CacheClient
from finance_agent.storage.orm import (
    AssetORM,
    CapitalFlowSnapshotORM,
    CryptoDerivativeSnapshotORM,
    EventRecordORM,
    EvidenceORM,
    FundamentalSnapshotORM,
    MarketBarORM,
)

JsonDict = dict[str, Any]


class AgentContextBuilder:
    """构建并缓存单个标的的 Agent 分析输入。"""

    def __init__(
        self,
        session: Session,
        cache: CacheClient,
        *,
        ttl_seconds: int = 900,
    ) -> None:
        self.session = session
        self.cache = cache
        self.ttl_seconds = ttl_seconds

    def build_asset_context(
        self,
        *,
        asset_id: str,
        horizon: str,
        timeframe: str = "1d",
        bar_limit: int = 120,
        evidence_limit: int = 20,
        event_limit: int = 20,
        use_cache: bool = True,
    ) -> JsonDict:
        """构建单个标的上下文。

        返回结构面向后续 Agent 分析，不用于长期审计。推荐运行真正落库时，
        仍然需要保存引用到的 `evidence_id`、风险项和最终 Agent 输出。
        """

        cache_key = self.asset_context_key(
            asset_id=asset_id,
            horizon=horizon,
            timeframe=timeframe,
            bar_limit=bar_limit,
            evidence_limit=evidence_limit,
            event_limit=event_limit,
        )
        if use_cache:
            cached = self.cache.get_json(cache_key)
            if isinstance(cached, dict):
                return cached

        context = {
            "schema_version": "1.0",
            "context_type": "asset_analysis",
            "asset_id": asset_id,
            "horizon": horizon,
            "timeframe": timeframe,
            "generated_at": datetime.now().astimezone().isoformat(),
            "asset": self._load_asset(asset_id),
            "latest_market_bars": self._load_recent_bars(
                asset_id=asset_id,
                timeframe=timeframe,
                limit=bar_limit,
            ),
            "latest_fundamental": self._load_latest_fundamental(asset_id),
            "latest_capital_flow": self._load_latest_capital_flow(asset_id),
            "latest_derivatives": self._load_latest_derivatives(asset_id),
            "recent_events": self._load_recent_events(asset_id, limit=event_limit),
            "evidence": self._load_recent_evidence(asset_id, limit=evidence_limit),
            "cache_policy": {
                "source_of_truth": "postgresql_timescaledb",
                "ttl_seconds": self.ttl_seconds,
                "rebuildable": True,
            },
        }
        self.cache.set_json(cache_key, context, ttl_seconds=self.ttl_seconds)
        return context

    @staticmethod
    def asset_context_key(
        *,
        asset_id: str,
        horizon: str,
        timeframe: str,
        bar_limit: int,
        evidence_limit: int,
        event_limit: int,
    ) -> str:
        """生成 Agent 上下文缓存键。"""

        return (
            "agent_context:"
            f"{asset_id}:{horizon}:{timeframe}:bars{bar_limit}:events{event_limit}:"
            f"evidence{evidence_limit}"
        )

    def _load_asset(self, asset_id: str) -> JsonDict | None:
        asset = self.session.get(AssetORM, asset_id)
        if asset is None:
            return None
        return {
            "asset_id": asset.asset_id,
            "symbol": asset.symbol,
            "name": asset.name,
            "market": asset.market,
            "asset_type": asset.asset_type,
            "exchange": asset.exchange,
            "currency": asset.currency,
            "sector": asset.sector,
            "base_asset": asset.base_asset,
            "quote_asset": asset.quote_asset,
            "tradable": asset.tradable,
            "status": asset.status,
        }

    def _load_recent_bars(self, *, asset_id: str, timeframe: str, limit: int) -> list[JsonDict]:
        statement = (
            select(MarketBarORM)
            .where(
                MarketBarORM.asset_id == asset_id,
                MarketBarORM.timeframe == timeframe,
                MarketBarORM.is_closed.is_(True),
                MarketBarORM.status.in_(("available", "revised")),
            )
            .order_by(MarketBarORM.timestamp.desc())
            .limit(limit)
        )
        rows = list(self.session.scalars(statement))
        return [
            {
                "timestamp": row.timestamp.isoformat(),
                "open": _to_json_value(row.open),
                "high": _to_json_value(row.high),
                "low": _to_json_value(row.low),
                "close": _to_json_value(row.close),
                "volume": _to_json_value(row.volume),
                "amount": _to_json_value(row.amount),
                "source": row.source,
                "adjustment": row.adjustment,
                "status": row.status,
            }
            for row in reversed(rows)
        ]

    def _load_latest_fundamental(self, asset_id: str) -> JsonDict | None:
        statement = (
            select(FundamentalSnapshotORM)
            .where(FundamentalSnapshotORM.asset_id == asset_id)
            .order_by(FundamentalSnapshotORM.as_of.desc())
            .limit(1)
        )
        row = self.session.scalars(statement).one_or_none()
        if row is None:
            return None
        return {
            "snapshot_id": row.snapshot_id,
            "report_period": row.report_period,
            "pe_ttm": _to_json_value(row.pe_ttm),
            "pb": _to_json_value(row.pb),
            "roe": _to_json_value(row.roe),
            "revenue_growth_yoy": _to_json_value(row.revenue_growth_yoy),
            "net_profit_growth_yoy": _to_json_value(row.net_profit_growth_yoy),
            "debt_to_asset": _to_json_value(row.debt_to_asset),
            "operating_cashflow": _to_json_value(row.operating_cashflow),
            "source": row.source,
            "status": row.status,
            "missing_fields": row.missing_fields,
            "as_of": row.as_of.isoformat(),
        }

    def _load_latest_capital_flow(self, asset_id: str) -> JsonDict | None:
        statement = (
            select(CapitalFlowSnapshotORM)
            .where(CapitalFlowSnapshotORM.asset_id == asset_id)
            .order_by(CapitalFlowSnapshotORM.as_of.desc())
            .limit(1)
        )
        row = self.session.scalars(statement).one_or_none()
        if row is None:
            return None
        return {
            "snapshot_id": row.snapshot_id,
            "window": row.window,
            "main_net_inflow": _to_json_value(row.main_net_inflow),
            "northbound_net_inflow": _to_json_value(row.northbound_net_inflow),
            "turnover_rate": _to_json_value(row.turnover_rate),
            "amount": _to_json_value(row.amount),
            "source": row.source,
            "status": row.status,
            "as_of": row.as_of.isoformat(),
        }

    def _load_latest_derivatives(self, asset_id: str) -> JsonDict | None:
        statement = (
            select(CryptoDerivativeSnapshotORM)
            .where(CryptoDerivativeSnapshotORM.asset_id == asset_id)
            .order_by(CryptoDerivativeSnapshotORM.as_of.desc())
            .limit(1)
        )
        row = self.session.scalars(statement).one_or_none()
        if row is None:
            return None
        return {
            "snapshot_id": row.snapshot_id,
            "funding_rate": _to_json_value(row.funding_rate),
            "next_funding_time": row.next_funding_time.isoformat()
            if row.next_funding_time
            else None,
            "open_interest": _to_json_value(row.open_interest),
            "open_interest_value": _to_json_value(row.open_interest_value),
            "long_short_ratio": _to_json_value(row.long_short_ratio),
            "basis_rate": _to_json_value(row.basis_rate),
            "liquidation_risk_score": _to_json_value(row.liquidation_risk_score),
            "source": row.source,
            "status": row.status,
            "as_of": row.as_of.isoformat(),
        }

    def _load_recent_events(self, asset_id: str, *, limit: int) -> list[JsonDict]:
        statement = (
            select(EventRecordORM)
            .where(EventRecordORM.asset_id == asset_id)
            .order_by(
                EventRecordORM.published_at.desc().nullslast(),
                EventRecordORM.collected_at.desc(),
            )
            .limit(limit)
        )
        return [
            {
                "event_id": row.event_id,
                "event_type": row.event_type,
                "title": row.title,
                "summary": row.summary,
                "sentiment": row.sentiment,
                "importance": row.importance,
                "source": row.source,
                "url": row.url,
                "published_at": row.published_at.isoformat() if row.published_at else None,
                "collected_at": row.collected_at.isoformat(),
            }
            for row in self.session.scalars(statement)
        ]

    def _load_recent_evidence(self, asset_id: str, *, limit: int) -> list[JsonDict]:
        statement = (
            select(EvidenceORM)
            .where(EvidenceORM.asset_id == asset_id)
            .order_by(EvidenceORM.as_of.desc().nullslast(), EvidenceORM.collected_at.desc())
            .limit(limit)
        )
        return [
            {
                "evidence_id": row.evidence_id,
                "evidence_type": row.evidence_type,
                "source": row.source,
                "title": row.title,
                "summary": row.summary,
                "data_ref": row.data_ref,
                "url": row.url,
                "reliability": row.reliability,
                "as_of": row.as_of.isoformat() if row.as_of else None,
                "collected_at": row.collected_at.isoformat(),
            }
            for row in self.session.scalars(statement)
        ]


def _to_json_value(value: Any) -> Any:
    """转换数据库数值为 JSON 友好的值。"""

    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value
