"""M3 数据可靠性与推荐入池闭环冒烟验证。

该脚本先作为 TDD 红灯脚本：在 M3 表和推荐入池入口实现前应失败；
实现后用于验证持仓历史、数据质量快照、观察池事件和推荐结果入池闭环。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from finance_agent.agents.personal_assistant import PersonalFinanceAgentService
from finance_agent.application import DataQualityService, PortfolioService, WatchlistService
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.repositories import RecommendationRepository


def main() -> None:
    """执行一次 M3 数据可靠性与推荐入池冒烟验证。"""

    session_factory = create_session_factory()
    owner_id = "local_user"
    as_of = datetime(2026, 5, 17, 16, 0, tzinfo=UTC)
    portfolio_id = "portfolio:local:ashare:m3"
    watchlist_id = "watchlist:local:ashare:m3:intake"
    asset_id = "ashare:002594"
    run_id = "run:m3:recommendation:intake:202605171600"
    recommendation_id = "asset_rec:ashare:002594:swing:m3"

    with session_scope(session_factory) as session:
        portfolios = PortfolioService(session)
        watchlists = WatchlistService(session)
        recommendations = RecommendationRepository(session)
        data_quality = DataQualityService(session)

        portfolios.upsert_portfolio(
            portfolio_id=portfolio_id,
            owner_id=owner_id,
            name="M3 数据可靠性冒烟组合",
            portfolio_type="manual",
            base_currency="CNY",
            risk_profile="balanced_growth",
            total_equity=Decimal("150000.00"),
            cash=Decimal("56000.00"),
            market_value=Decimal("94000.00"),
            max_position_weight=Decimal("0.250000"),
            max_drawdown_alert=Decimal("0.080000"),
            as_of=as_of,
            payload={"source": "smoke_m3_reliability"},
        )
        portfolios.upsert_position(
            position_id=f"position:{portfolio_id}:ashare:600519:long",
            portfolio_id=portfolio_id,
            asset_id="ashare:600519",
            symbol="600519",
            market="ashare",
            side="long",
            quantity=Decimal("100"),
            avg_cost=Decimal("1680.00"),
            last_price=Decimal("1724.00"),
            market_value=Decimal("172400.00"),
            unrealized_pnl=Decimal("4400.00"),
            unrealized_pnl_pct=Decimal("0.026190"),
            portfolio_weight=Decimal("0.180000"),
            as_of=as_of,
            payload={"source": "smoke_m3_reliability"},
        )
        watchlists.upsert_watchlist(
            watchlist_id=watchlist_id,
            owner_id=owner_id,
            name="M3 推荐入池观察池",
            market="ashare",
            purpose="recommendation_intake",
            payload={"source": "smoke_m3_reliability"},
        )
        recommendations.upsert_run(
            run_id=run_id,
            strategy="balanced_swing_v1",
            market="ashare",
            horizon="swing",
            limit=5,
            status="available",
            started_at=as_of - timedelta(minutes=2),
            finished_at=as_of - timedelta(minutes=1),
            summary="M3 推荐入池冒烟运行。",
            payload={"source": "smoke_m3_reliability"},
        )
        recommendations.upsert_asset_recommendation(
            recommendation_id=recommendation_id,
            run_id=run_id,
            asset_id=asset_id,
            symbol="002594",
            name="比亚迪",
            market="ashare",
            horizon="swing",
            action="buy_candidate",
            rank=1,
            total_score=Decimal("82.500000"),
            confidence=Decimal("0.760000"),
            conviction="high",
            score_id="score:m3:ashare:002594",
            factor_frame_id="factor:m3:ashare:002594",
            signal_ids=["signal:m3:ashare:002594:swing"],
            risk_ids=[],
            evidence_ids=[],
            watch_conditions={
                "conditions": ["趋势继续保持 bullish", "回撤不跌破 20 日均线"]
            },
            invalid_if={"conditions": ["新增 high 风险", "信号转 bearish"]},
            summary="002594 当前评分和置信度较高，适合进入买入前观察池。",
            payload={"source": "smoke_m3_reliability"},
        )
        quality = data_quality.upsert_quality_snapshot(
            quality_id=f"quality:{asset_id}:market_bar:202605171600",
            asset_id=asset_id,
            symbol="002594",
            market="ashare",
            data_domain="market_bar",
            provider="akshare",
            status="available",
            freshness_status="fresh",
            checked_at=as_of,
            latest_data_at=as_of - timedelta(minutes=5),
            missing_items=[],
            issue_count=0,
            payload={"source": "smoke_m3_reliability"},
        )

        agent = PersonalFinanceAgentService(session)
        result = agent.sync_recommendations_to_watchlist(
            owner_id=owner_id,
            recommendation_run_id=run_id,
            watchlist_id=watchlist_id,
            as_of=as_of,
            limit=3,
            workflow_run_id="workflow:smoke:m3:recommendation_intake:202605171600",
        )

        portfolio_snapshots = portfolios.repository.list_portfolio_snapshots(
            portfolio_id=portfolio_id,
            limit=5,
        )
        position_snapshots = portfolios.repository.list_position_snapshots(
            portfolio_id=portfolio_id,
            asset_id="ashare:600519",
            limit=5,
        )
        watchlist_events = watchlists.repository.list_watchlist_events(
            watchlist_id=watchlist_id,
            limit=10,
        )
        quality_items = data_quality.list_latest_quality(asset_id=asset_id, limit=5)
        summary = {
            "workflow_run_id": result.workflow_run_id,
            "portfolio_snapshot_count": len(portfolio_snapshots),
            "position_snapshot_count": len(position_snapshots),
            "watchlist_event_count": len(watchlist_events),
            "data_quality_count": len(quality_items),
            "quality_id": quality.quality_id,
            "watchlist_item_ids": list(result.watchlist_item_ids),
            "decision_ids": list(result.decision_ids),
            "memory_ids": list(result.memory_ids),
            "review_task_ids": list(result.review_task_ids),
        }

    print(summary)


if __name__ == "__main__":
    main()
