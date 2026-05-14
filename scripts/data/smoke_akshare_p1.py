"""AKShare P1 数据链路冒烟验证。

验证内容：
- 行业/概念种子源 Provider 能返回结构化结果。
- 个股资金流排名能归一化并写入 `capital_flow_snapshots`。
- 个股新闻能归一化并写入 `event_records` 和 `evidence`。

部分东方财富接口在当前网络环境下可能被上游断开；脚本会输出结构化
错误，不把单个 Provider 失败当成整个冒烟脚本失败。
"""

from __future__ import annotations

from datetime import UTC, datetime

from finance_agent.data.providers import (
    AshareCapitalFlowProvider,
    AshareEventProvider,
    AshareSectorProvider,
)
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.repositories import (
    AssetRepository,
    CapitalFlowRepository,
    EventRepository,
    UniverseRepository,
)


def main() -> None:
    """执行 AKShare P1 冒烟验证。"""

    sector_provider = AshareSectorProvider()
    flow_provider = AshareCapitalFlowProvider()
    event_provider = AshareEventProvider()

    as_of = datetime.now(tz=UTC)
    industry = sector_provider.fetch_industry_members(industry_name="银行", limit=5)
    flow_rank = flow_provider.fetch_flow_rank(indicator="今日", limit=5)
    news = event_provider.fetch_stock_news(symbol="000001", limit=3)

    session_factory = create_session_factory()
    with session_scope(session_factory) as session:
        assets = AssetRepository(session)
        universes = UniverseRepository(session)
        capital_flows = CapitalFlowRepository(session)
        events = EventRepository(session)

        universes.upsert_universe(
            universe_id="universe:akshare_p1_smoke:industry_bank",
            name="AKShare P1 冒烟行业种子池",
            source="akshare:stock_board_industry_cons_em",
            market="ashare",
            strategy_context="p1_smoke",
            as_of=as_of,
            total_before_filter=len(industry.seeds),
            total_after_filter=len(industry.seeds),
            status=industry.status,
            payload={"provider_payload": industry.payload, "error": industry.error_message},
        )
        if industry.status == "available":
            for seed in industry.seeds:
                assets.upsert_asset(
                    asset_id=seed.asset_id,
                    symbol=seed.symbol,
                    name=seed.name,
                    market=seed.market,
                    asset_type="stock",
                    payload=seed.payload,
                )
            universes.replace_members(
                universe_id="universe:akshare_p1_smoke:industry_bank",
                members=[
                    {
                        "member_id": f"universe_member:akshare_p1_smoke:{seed.symbol}",
                        "asset_id": seed.asset_id,
                        "symbol": seed.symbol,
                        "market": seed.market,
                        "as_of": seed.as_of or as_of,
                        "rank_hint": seed.rank_hint,
                        "payload": seed.payload,
                    }
                    for seed in industry.seeds
                ],
            )

        if flow_rank.status == "available":
            for snapshot in flow_rank.snapshots:
                assets.upsert_asset(
                    asset_id=snapshot.asset_id,
                    symbol=snapshot.symbol,
                    name=snapshot.symbol,
                    market=snapshot.market,
                    asset_type="stock",
                    payload={"source": snapshot.source},
                )
                capital_flows.upsert_capital_flow_snapshot(
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
                    payload=snapshot.payload,
                )

        if news.status == "available":
            assets.upsert_asset(
                asset_id="ashare:000001",
                symbol="000001",
                name="平安银行",
                market="ashare",
                asset_type="stock",
                payload={"source": "akshare_p1_smoke"},
            )
            for event in news.events:
                events.upsert_event(
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
                    payload=event.payload,
                )
            for item in news.evidence:
                events.upsert_evidence(
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
                    payload=item.payload,
                )

    print(
        {
            "industry_status": industry.status,
            "industry_count": len(industry.seeds),
            "industry_error": industry.error_message,
            "flow_status": flow_rank.status,
            "flow_count": len(flow_rank.snapshots),
            "flow_error": flow_rank.error_message,
            "news_status": news.status,
            "news_count": len(news.events),
            "news_evidence_count": len(news.evidence),
            "news_error": news.error_message,
        }
    )


if __name__ == "__main__":
    main()
