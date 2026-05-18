"""私人金融助手 M2 表和服务层冒烟验证。

该脚本验证私人金融助手长期闭环的最小地基：

- 组合和持仓可写入。
- 私人观察池和投资假设可写入。
- 监控提醒、Workflow 审计、决策日志、Finance Memory、关系边和复盘任务可写入。

运行前需要先执行 `alembic upgrade head`，并确保 PostgreSQL + TimescaleDB 可用。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from finance_agent.application import (
    MemoryService,
    PortfolioService,
    WatchlistService,
    WorkflowService,
)
from finance_agent.storage.db import create_session_factory, session_scope


def main() -> None:
    """执行一次可重复运行的私人金融助手 M2 冒烟验证。"""

    session_factory = create_session_factory()
    owner_id = "local_user"
    as_of = datetime(2026, 5, 17, 10, 0, tzinfo=UTC)
    due_at = as_of + timedelta(days=7)

    with session_scope(session_factory) as session:
        portfolios = PortfolioService(session)
        watchlists = WatchlistService(session)
        memory = MemoryService(session)
        workflows = WorkflowService(session)

        portfolio = portfolios.upsert_portfolio(
            portfolio_id="portfolio:local:ashare",
            owner_id=owner_id,
            name="本地 A 股观察组合",
            portfolio_type="manual",
            base_currency="CNY",
            risk_profile="balanced_growth",
            total_equity=Decimal("100000.00"),
            cash=Decimal("38000.00"),
            market_value=Decimal("62000.00"),
            max_position_weight=Decimal("0.200000"),
            max_drawdown_alert=Decimal("0.080000"),
            as_of=as_of,
            payload={"source": "smoke_personal_assistant_m2"},
        )
        portfolios.upsert_position(
            position_id="position:portfolio:local:ashare:ashare:600519:long",
            portfolio_id=portfolio.portfolio_id,
            asset_id="ashare:600519",
            symbol="600519",
            market="ashare",
            side="long",
            quantity=Decimal("100"),
            avg_cost=Decimal("1680.00"),
            last_price=Decimal("1715.60"),
            market_value=Decimal("171560.00"),
            unrealized_pnl=Decimal("3560.00"),
            unrealized_pnl_pct=Decimal("0.021190"),
            portfolio_weight=Decimal("0.171560"),
            as_of=as_of,
            payload={"note": "冒烟样例持仓"},
        )
        snapshot = portfolios.load_portfolio_snapshot(portfolio.portfolio_id)

        watchlist = watchlists.upsert_watchlist(
            watchlist_id="watchlist:local:ashare:swing",
            owner_id=owner_id,
            name="A 股波段候选观察池",
            market="ashare",
            purpose="swing_candidate",
            payload={"source": "smoke_personal_assistant_m2"},
        )
        item = watchlists.add_or_update_item(
            watchlist_item_id="watchlist_item:local:ashare:600519",
            watchlist_id=watchlist.watchlist_id,
            asset_id="ashare:600519",
            symbol="600519",
            market="ashare",
            source_type="manual",
            reason="白酒龙头回撤后重新站上短期均线，需要持续观察量价和资金流。",
            watch_conditions={"price_area": "1650-1750", "volume": "温和放量"},
            trigger_conditions={"breakout": "放量突破 1750"},
            invalid_conditions={"drawdown": "跌破 1600 且资金流持续转弱"},
            risk_level="medium",
            next_review_at=due_at,
            payload={"source": "smoke_personal_assistant_m2"},
        )
        thesis = watchlists.record_thesis(
            thesis_id="thesis:local:ashare:600519:swing",
            owner_id=owner_id,
            asset_id="ashare:600519",
            source_type="watchlist",
            source_id=item.watchlist_item_id,
            thesis="如果价格稳定在关键均线之上且资金流改善，可以作为波段买入候选。",
            supporting_points=[
                {"type": "technical", "text": "短期趋势修复"},
                {"type": "business", "text": "龙头资产流动性较好"},
            ],
            risk_points=[
                {"type": "valuation", "text": "估值弹性有限"},
                {"type": "market", "text": "消费板块整体风险偏好可能下降"},
            ],
            invalid_if={"price": "跌破 1600", "flow": "主力资金连续转弱"},
        )

        alert = memory.record_alert(
            alert_id="alert:local:ashare:600519:price_move",
            owner_id=owner_id,
            portfolio_id=portfolio.portfolio_id,
            asset_id="ashare:600519",
            alert_type="price_move",
            severity="medium",
            triggered_by="price",
            trigger_condition="持仓标的日内涨幅超过 2%，需要检查是否加仓或继续持有。",
            current_value=Decimal("0.021190"),
            threshold_value=Decimal("0.020000"),
            as_of=as_of,
            payload={"source": "smoke_personal_assistant_m2"},
        )

        workflow_started = workflows.start_run(
            workflow_run_id="workflow:local:portfolio_monitoring:20260517",
            owner_id=owner_id,
            workflow_type="portfolio_monitoring",
            trigger_type="alert",
            trigger_ref=alert.alert_id,
            started_at=as_of,
            input_ref="agent_context:smoke:portfolio_monitoring",
            payload={"portfolio_id": portfolio.portfolio_id},
        )
        workflows.record_event(
            workflow_event_id="workflow_event:local:portfolio_monitoring:20260517:1",
            workflow_run_id=workflow_started.workflow_run_id,
            event_type="node_started",
            agent_name="portfolio_decision",
            message="组合决策节点开始检查持仓和观察池。",
            created_at=as_of,
            payload={"watchlist_item_id": item.watchlist_item_id},
        )
        workflow = workflows.finish_run(
            workflow_run_id=workflow_started.workflow_run_id,
            owner_id=owner_id,
            workflow_type="portfolio_monitoring",
            trigger_type="alert",
            trigger_ref=alert.alert_id,
            status="succeeded",
            started_at=as_of,
            finished_at=as_of + timedelta(minutes=2),
            input_ref="agent_context:smoke:portfolio_monitoring",
            output_ref="report:smoke:portfolio_monitoring",
            payload={"decision": "hold", "risk_rebuttal": "估值和板块情绪仍需观察"},
        )

        decision = memory.record_decision(
            decision_id="decision:local:ashare:600519:hold:20260517",
            owner_id=owner_id,
            portfolio_id=portfolio.portfolio_id,
            asset_id="ashare:600519",
            decision_type="hold",
            source_alert_id=alert.alert_id,
            workflow_run_id=workflow.workflow_run_id,
            suggested_action="hold",
            user_action="unknown",
            summary="当前持仓盈利但触发条件尚未完全确认，建议继续持有并观察 1750 突破情况。",
            reason_ids=[thesis.thesis_id],
            risk_ids=[],
            evidence_ids=[],
            created_at=as_of,
            payload={"source": "smoke_personal_assistant_m2"},
        )
        finance_memory = memory.upsert_memory(
            memory_id="memory:local:ashare:600519:watch_reason",
            owner_id=owner_id,
            memory_type="watch_reason",
            scope="asset",
            asset_id="ashare:600519",
            source_decision_id=decision.decision_id,
            content="600519 当前作为波段观察标的，核心条件是放量突破 1750，否则只继续观察。",
            confidence=Decimal("0.850000"),
            payload={"source": "smoke_personal_assistant_m2"},
        )
        memory.link_memory_edge(
            edge_id="memory_edge:decision:600519:watch_reason",
            owner_id=owner_id,
            source_type="decision",
            source_id=decision.decision_id,
            relation_type="supports",
            target_type="memory",
            target_id=finance_memory.memory_id,
            confidence=Decimal("0.900000"),
            reason="决策日志沉淀为后续观察理由。",
        )
        review = memory.schedule_review(
            review_task_id="review:local:ashare:600519:20260524",
            owner_id=owner_id,
            asset_id="ashare:600519",
            source_decision_id=decision.decision_id,
            review_type="watchlist_followup",
            due_at=due_at,
            review_questions=[
                {"question": "是否放量突破 1750？"},
                {"question": "资金流是否改善？"},
                {"question": "风险反驳中的估值压力是否缓解？"},
            ],
            payload={"source": "smoke_personal_assistant_m2"},
        )

        summary = {
            "portfolio_id": portfolio.portfolio_id,
            "position_count": len(snapshot.positions),
            "watchlist_item_count": len(
                watchlists.list_active_items(owner_id=owner_id, watchlist_id=watchlist.watchlist_id)
            ),
            "workflow_run_id": workflow.workflow_run_id,
            "decision_id": decision.decision_id,
            "memory_id": finance_memory.memory_id,
            "review_task_id": review.review_task_id,
        }

    print(summary)


if __name__ == "__main__":
    main()
