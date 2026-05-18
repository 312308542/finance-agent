"""M4 Agent 决策层冒烟验证。

验证目标：
- 推荐结果不能直接规则搬运入池，必须先经过 Agent 决策 Workflow。
- Agent 需要同时给出是否入池、是否买入、是否卖出、是否换股。
- 决策结果需要落到观察池、决策日志、Finance Memory 和复盘任务。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from finance_agent.agents.personal_assistant import PersonalFinanceAgentService
from finance_agent.application import MemoryService, PortfolioService, WatchlistService
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.repositories import (
    RecommendationRepository,
    RiskRepository,
    SignalSnapshotRepository,
)


def main() -> None:
    """执行一次 M4 Agent 决策层冒烟验证。"""

    session_factory = create_session_factory()
    owner_id = "local_user"
    as_of = datetime(2026, 5, 17, 18, 0, tzinfo=UTC)
    portfolio_id = "portfolio:local:ashare:m4:v2"
    watchlist_id = "watchlist:local:ashare:m4:agent_decision:v2"
    recommendation_run_id = "run:m4:agent_decision:v2:202605171800"
    candidate_asset_id = "ashare:m4v2:002594"
    weak_position_asset_id = "ashare:m4v2:600519"

    with session_scope(session_factory) as session:
        portfolios = PortfolioService(session)
        watchlists = WatchlistService(session)
        memory = MemoryService(session)
        recommendations = RecommendationRepository(session)
        signals = SignalSnapshotRepository(session)
        risks = RiskRepository(session)

        portfolios.upsert_portfolio(
            portfolio_id=portfolio_id,
            owner_id=owner_id,
            name="M4 Agent 决策冒烟组合",
            portfolio_type="manual",
            base_currency="CNY",
            risk_profile="balanced_growth",
            total_equity=Decimal("200000.00"),
            cash=Decimal("30000.00"),
            market_value=Decimal("170000.00"),
            max_position_weight=Decimal("0.250000"),
            max_drawdown_alert=Decimal("0.080000"),
            as_of=as_of,
            payload={"source": "smoke_m4_agent_decision"},
        )
        portfolios.upsert_position(
            position_id=f"position:{portfolio_id}:{weak_position_asset_id}:long",
            portfolio_id=portfolio_id,
            asset_id=weak_position_asset_id,
            symbol="600519",
            market="ashare",
            side="long",
            quantity=Decimal("100"),
            avg_cost=Decimal("1700.00"),
            last_price=Decimal("1600.00"),
            market_value=Decimal("160000.00"),
            unrealized_pnl=Decimal("-10000.00"),
            unrealized_pnl_pct=Decimal("-0.058824"),
            portfolio_weight=Decimal("0.800000"),
            as_of=as_of,
            payload={"source": "smoke_m4_agent_decision"},
        )
        watchlists.upsert_watchlist(
            watchlist_id=watchlist_id,
            owner_id=owner_id,
            name="M4 Agent 决策观察池",
            market="ashare",
            purpose="agent_decision",
            payload={"source": "smoke_m4_agent_decision"},
        )
        recommendations.upsert_run(
            run_id=recommendation_run_id,
            strategy="balanced_swing_v1",
            market="ashare",
            horizon="swing",
            limit=5,
            status="available",
            started_at=as_of - timedelta(minutes=5),
            finished_at=as_of - timedelta(minutes=3),
            summary="M4 Agent 决策冒烟推荐运行。",
            payload={"source": "smoke_m4_agent_decision"},
        )
        recommendations.upsert_asset_recommendation(
            recommendation_id=f"asset_rec:{candidate_asset_id}:swing:m4",
            run_id=recommendation_run_id,
            asset_id=candidate_asset_id,
            symbol="002594",
            name="比亚迪",
            market="ashare",
            horizon="swing",
            action="buy_candidate",
            rank=1,
            total_score=Decimal("88.000000"),
            confidence=Decimal("0.820000"),
            conviction="high",
            score_id=f"score:{candidate_asset_id}:m4",
            factor_frame_id=f"factor:{candidate_asset_id}:m4",
            signal_ids=[f"signal:{candidate_asset_id}:swing:m4"],
            risk_ids=[],
            evidence_ids=[],
            watch_conditions={"conditions": ["趋势维持 bullish", "放量突破"]},
            invalid_if={"conditions": ["信号转 bearish", "出现 high 风险"]},
            summary="002594 评分、趋势和置信度均较高，具备进入买入候选的条件。",
            payload={"source": "smoke_m4_agent_decision"},
        )
        signals.upsert_signal_snapshot(
            signal_id=f"signal:{candidate_asset_id}:swing:m4",
            asset_id=candidate_asset_id,
            symbol="002594",
            market="ashare",
            horizon="swing",
            direction="bullish",
            score=Decimal("86.000000"),
            confidence=Decimal("0.800000"),
            rule_version="smoke_m4_agent_decision",
            status="available",
            as_of=as_of,
            payload={"source": "smoke_m4_agent_decision"},
        )
        signals.upsert_signal_snapshot(
            signal_id=f"signal:{weak_position_asset_id}:swing:m4",
            asset_id=weak_position_asset_id,
            symbol="600519",
            market="ashare",
            horizon="swing",
            direction="bearish",
            score=Decimal("38.000000"),
            confidence=Decimal("0.720000"),
            rule_version="smoke_m4_agent_decision",
            status="available",
            as_of=as_of,
            payload={"source": "smoke_m4_agent_decision"},
        )
        risk = risks.upsert_risk_finding(
            risk_id=f"risk:{weak_position_asset_id}:trend_break:m4",
            asset_id=weak_position_asset_id,
            scope="asset",
            risk_type="trend_break",
            severity="high",
            score=Decimal("0.800000"),
            title="趋势破位且仓位过高",
            description="当前持仓信号转弱且组合权重过高，应考虑换出或减仓。",
            as_of=as_of,
            evidence_ids=[],
            payload={"source": "smoke_m4_agent_decision"},
        )
        memory.upsert_memory(
            memory_id=f"memory:{weak_position_asset_id}:m4_prior_review",
            owner_id=owner_id,
            memory_type="decision_review",
            scope="asset",
            asset_id=weak_position_asset_id,
            content="历史复盘提示：该持仓一旦跌破趋势线且仓位超过 50%，应优先考虑减仓。",
            confidence=Decimal("0.900000"),
            payload={"source": "smoke_m4_agent_decision"},
        )

        agent = PersonalFinanceAgentService(session)
        result = agent.decide_recommendation_actions(
            owner_id=owner_id,
            recommendation_run_id=recommendation_run_id,
            portfolio_id=portfolio_id,
            watchlist_id=watchlist_id,
            as_of=as_of,
            limit=3,
            workflow_run_id="workflow:smoke:m4:agent_decision:v2:202605171800",
        )

        events = watchlists.list_events(watchlist_id=watchlist_id, limit=10)
        intake_events = [
            event for event in events if event.event_type == "candidate_intake_reason"
        ]
        if not intake_events:
            raise AssertionError("Agent 入池决策必须写入 candidate_intake_reason 事件。")
        intake_payload = intake_events[0].payload
        if not intake_payload.get("intake_reason"):
            raise AssertionError("candidate_intake_reason 事件必须保存 Agent 入池原因。")
        if not intake_payload.get("risk_rebuttal"):
            raise AssertionError("candidate_intake_reason 事件必须保存风险反驳。")
        intake_memories = memory.memories.list_active_memories(
            owner_id=owner_id,
            asset_id=candidate_asset_id,
            memory_type="candidate_intake_reason",
            limit=5,
        )
        if not intake_memories:
            raise AssertionError("Agent 入池决策必须沉淀 candidate_intake_reason 记忆。")
        summary = {
            "workflow_run_id": result.workflow_run_id,
            "agent_actions": [decision.agent_action for decision in result.result.decisions],
            "trade_actions": [decision.trade_action for decision in result.result.decisions],
            "watchlist_item_ids": list(result.watchlist_item_ids),
            "decision_ids": list(result.decision_ids),
            "memory_ids": list(result.memory_ids),
            "intake_reason_memory_ids": [
                memory.memory_id for memory in intake_memories
            ],
            "intake_reason": intake_payload.get("intake_reason"),
            "review_task_ids": list(result.review_task_ids),
            "watchlist_event_count": len(events),
            "risk_ids": [risk.risk_id],
        }

    print(summary)


if __name__ == "__main__":
    main()
