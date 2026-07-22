from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from finance_agent.agents.workflows.portfolio_monitoring import (
    PortfolioMonitoringInput,
    PortfolioMonitoringWorkflow,
)
from finance_agent.agents.workflows.recommendation_decision import (
    RecommendationDecisionInput,
    RecommendationDecisionWorkflow,
)

AS_OF = datetime(2026, 7, 20, 9, 40, tzinfo=UTC)


def _recommendation() -> SimpleNamespace:
    return SimpleNamespace(
        recommendation_id="recommendation:1",
        asset_id="ashare:600519",
        symbol="600519",
        name="测试股票",
        market="ashare",
        action="strong_buy",
        total_score=Decimal("90"),
        confidence=Decimal("0.90"),
        conviction="high",
        score_id="score:1",
        factor_frame_id="factor:1",
        signal_ids=(),
        risk_ids=(),
        evidence_ids=("evidence:recommendation:1",),
    )


def _signal() -> SimpleNamespace:
    return SimpleNamespace(
        signal_id="signal:1",
        direction="bullish",
        score=Decimal("90"),
        confidence=Decimal("0.90"),
    )


def test_recommendation_gate_blocks_buy_when_snapshot_expired() -> None:
    result = RecommendationDecisionWorkflow().run(
        RecommendationDecisionInput(
            owner_id="owner:1",
            recommendation_run_id="run:1",
            portfolio_id="portfolio:1",
            watchlist=SimpleNamespace(watchlist_id="watchlist:1"),
            recommendations=(_recommendation(),),
            positions=(),
            watchlist_items=(),
            signals_by_asset={"ashare:600519": _signal()},
            risks_by_asset={"ashare:600519": ()},
            memories_by_asset={"ashare:600519": ()},
            as_of=AS_OF,
            data_snapshot_id="snapshot:expired",
            decision_gate_id="gate:expired",
            decision_gate_status="expired",
        )
    )

    decision = result.decisions[0]
    assert decision.trade_action == "wait"
    assert decision.agent_action == "wait_for_decision_gate"
    assert decision.decision_gate_id == "gate:expired"
    assert decision.data_snapshot_id == "snapshot:expired"


def test_portfolio_gate_blocks_reduce_but_keeps_alert_context() -> None:
    position = SimpleNamespace(
        position_id="position:1",
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        unrealized_pnl_pct=Decimal("-0.10"),
    )
    result = PortfolioMonitoringWorkflow().run(
        PortfolioMonitoringInput(
            owner_id="owner:1",
            portfolio=SimpleNamespace(portfolio_id="portfolio:1"),
            positions=(position,),
            signals_by_asset={"ashare:600519": SimpleNamespace(signal_id="signal:1", direction="bearish")},
            risks_by_asset={"ashare:600519": ()},
            memories_by_asset={"ashare:600519": ()},
            as_of=AS_OF,
            data_snapshot_id=None,
            decision_gate_id="gate:data-unavailable",
            decision_gate_status="data_unavailable",
        )
    )

    decision = result.decisions[0]
    assert decision.suggested_action == "wait"
    assert decision.decision_type == "reduce_gate_wait"
    assert decision.decision_gate_status == "data_unavailable"
    assert "暂不进入可执行动作" in decision.summary


def test_approved_gate_preserves_existing_recommendation_action() -> None:
    result = RecommendationDecisionWorkflow().run(
        RecommendationDecisionInput(
            owner_id="owner:1",
            recommendation_run_id="run:1",
            portfolio_id="portfolio:1",
            watchlist=SimpleNamespace(watchlist_id="watchlist:1"),
            recommendations=(_recommendation(),),
            positions=(),
            watchlist_items=(),
            signals_by_asset={"ashare:600519": _signal()},
            risks_by_asset={"ashare:600519": ()},
            memories_by_asset={"ashare:600519": ()},
            as_of=AS_OF,
            data_snapshot_id="snapshot:available",
            decision_gate_id="gate:approved",
            decision_gate_status="approved",
        )
    )

    assert result.decisions[0].trade_action == "buy"
    assert result.decision_gate_status == "approved"
