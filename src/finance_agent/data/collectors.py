"""数据采集编排服务。

Collector 负责把 Provider 的结构化结果落到标准表，并把每次调用归档到
`raw_records`。它不负责因子计算、评分或推荐决策。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.data.models import (
    CapitalFlowSnapshotsResult,
    EventRecordsResult,
    ProviderResult,
    UniverseSeedsResult,
)
from finance_agent.data.providers import (
    AshareCapitalFlowProvider,
    AshareEventProvider,
    AshareSectorProvider,
)
from finance_agent.storage.repositories import (
    AssetRepository,
    CapitalFlowRepository,
    EventRepository,
    RawRecordRepository,
    UniverseRepository,
)

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class ArchivedProviderResult:
    """带 raw_records 归档编号的 Provider 调用结果。"""

    result: ProviderResult
    raw_record_id: str


class AshareP1Collector:
    """A 股 P1 数据采集编排器。

    当前覆盖行业/概念种子、资金流排名、新闻公告事件。后续接入财务/估值
    P2 时，可以沿用同样的归档和落表模式。
    """

    def __init__(
        self,
        session: Session,
        *,
        sector_provider: AshareSectorProvider | None = None,
        flow_provider: AshareCapitalFlowProvider | None = None,
        event_provider: AshareEventProvider | None = None,
    ) -> None:
        self.assets = AssetRepository(session)
        self.universes = UniverseRepository(session)
        self.capital_flows = CapitalFlowRepository(session)
        self.events = EventRepository(session)
        self.raw_records = RawRecordRepository(session)
        self.sector_provider = sector_provider or AshareSectorProvider()
        self.flow_provider = flow_provider or AshareCapitalFlowProvider()
        self.event_provider = event_provider or AshareEventProvider()

    def archive_provider_result(
        self,
        result: ProviderResult,
        *,
        endpoint: str,
        request_params: JsonDict,
        symbol: str | None = None,
        market: str | None = "ashare",
    ) -> str:
        """把 Provider 返回结果归档到 raw_records。"""

        payload = result.payload | {
            "status": result.status,
            "error_message": result.error_message,
        }
        if isinstance(result, UniverseSeedsResult):
            payload["seeds"] = [seed.__dict__ for seed in result.seeds]
        elif isinstance(result, CapitalFlowSnapshotsResult):
            payload["snapshots"] = [snapshot.__dict__ for snapshot in result.snapshots]
        elif isinstance(result, EventRecordsResult):
            payload["events"] = [event.__dict__ for event in result.events]
            payload["evidence"] = [item.__dict__ for item in result.evidence]

        record = self.raw_records.insert_raw_record(
            provider=result.provider_name,
            endpoint=endpoint,
            symbol=symbol,
            market=market,
            request_params=request_params,
            response_payload=payload,
            status=result.status,
            error_message=result.error_message,
            as_of=result.collected_at,
            collected_at=result.collected_at,
        )
        return record.raw_record_id

    def collect_industry_members(
        self,
        *,
        industry_name: str,
        universe_id: str,
        universe_name: str,
        strategy_context: str,
        limit: int | None = None,
    ) -> ArchivedProviderResult:
        """采集行业成分种子，并写入候选池定义和成员。"""

        result = self.sector_provider.fetch_industry_members(
            industry_name=industry_name,
            limit=limit,
        )
        raw_record_id = self.archive_provider_result(
            result,
            endpoint="stock_board_industry_cons_em",
            request_params={"symbol": industry_name, "limit": limit},
        )

        self.universes.upsert_universe(
            universe_id=universe_id,
            name=universe_name,
            source="akshare:stock_board_industry_cons_em",
            market="ashare",
            strategy_context=strategy_context,
            as_of=result.collected_at,
            total_before_filter=len(result.seeds),
            total_after_filter=len(result.seeds),
            status=result.status,
            payload={
                "provider_payload": result.payload,
                "raw_record_id": raw_record_id,
                "error": result.error_message,
            },
        )
        if result.status != "available":
            return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

        for seed in result.seeds:
            self.assets.upsert_asset(
                asset_id=seed.asset_id,
                symbol=seed.symbol,
                name=seed.name,
                market=seed.market,
                asset_type="stock",
                payload=seed.payload,
            )
        self.universes.replace_members(
            universe_id=universe_id,
            members=[
                {
                    "member_id": f"universe_member:{universe_id}:{seed.symbol}",
                    "asset_id": seed.asset_id,
                    "symbol": seed.symbol,
                    "market": seed.market,
                    "as_of": seed.as_of or result.collected_at,
                    "rank_hint": seed.rank_hint,
                    "payload": seed.payload | {"raw_record_id": raw_record_id},
                }
                for seed in result.seeds
            ],
        )
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def collect_flow_rank(
        self,
        *,
        indicator: str = "今日",
        limit: int | None = None,
    ) -> ArchivedProviderResult:
        """采集个股资金流排名，并写入资金流快照。"""

        result = self.flow_provider.fetch_flow_rank(indicator=indicator, limit=limit)
        raw_record_id = self.archive_provider_result(
            result,
            endpoint="stock_individual_fund_flow_rank",
            request_params={"indicator": indicator, "limit": limit},
        )
        if result.status != "available":
            return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

        for snapshot in result.snapshots:
            self.assets.upsert_asset(
                asset_id=snapshot.asset_id,
                symbol=snapshot.symbol,
                name=snapshot.symbol,
                market=snapshot.market,
                asset_type="stock",
                payload={"source": snapshot.source},
            )
            self.capital_flows.upsert_capital_flow_snapshot(
                snapshot_id=snapshot.snapshot_id,
                asset_id=snapshot.asset_id,
                symbol=snapshot.symbol,
                market=snapshot.market,
                main_net_inflow=snapshot.main_net_inflow,
                northbound_net_inflow=snapshot.northbound_net_inflow,
                turnover_rate=snapshot.turnover_rate,
                amount=snapshot.amount,
                window=snapshot.window,
                source=snapshot.source,
                status=snapshot.status,
                as_of=snapshot.as_of,
                payload=snapshot.payload | {"raw_record_id": raw_record_id},
            )
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def collect_stock_news(
        self,
        *,
        symbol: str,
        asset_name: str | None = None,
        limit: int | None = None,
    ) -> ArchivedProviderResult:
        """采集个股新闻，并写入事件和证据表。"""

        result = self.event_provider.fetch_stock_news(symbol=symbol, limit=limit)
        raw_record_id = self.archive_provider_result(
            result,
            endpoint="stock_news_em",
            request_params={"symbol": symbol, "limit": limit},
            symbol=symbol,
        )
        if result.status != "available":
            return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

        self.assets.upsert_asset(
            asset_id=f"ashare:{symbol}",
            symbol=symbol,
            name=asset_name or symbol,
            market="ashare",
            asset_type="stock",
            payload={"source": "akshare:stock_news_em"},
        )
        for event in result.events:
            self.events.upsert_event(
                event_id=event.event_id,
                asset_id=event.asset_id,
                symbol=event.symbol,
                market=event.market,
                event_type=event.event_type,
                title=event.title,
                summary=event.summary,
                sentiment=event.sentiment,
                importance=event.importance,
                source=event.source,
                url=event.url,
                published_at=event.published_at,
                collected_at=event.collected_at,
                payload=event.payload | {"raw_record_id": raw_record_id},
            )
        for item in result.evidence:
            self.events.upsert_evidence(
                evidence_id=item.evidence_id,
                evidence_type=item.evidence_type,
                asset_id=item.asset_id,
                source=item.source,
                title=item.title,
                summary=item.summary,
                data_ref=item.data_ref,
                url=item.url,
                reliability=item.reliability,
                as_of=item.as_of,
                collected_at=item.collected_at,
                payload=item.payload | {"raw_record_id": raw_record_id},
            )
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)
