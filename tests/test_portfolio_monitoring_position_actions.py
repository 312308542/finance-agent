"""盘中持仓动作接入工作流测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from finance_agent.agents.workflows.portfolio_monitoring import (
    PortfolioMonitoringInput,
    PortfolioMonitoringWorkflow,
)
from finance_agent.monitoring.models import PositionAction


def test_workflow_prefers_position_action_over_signal_and_pnl_rules() -> None:
    """已有盘中动作时，工作流不得被看多信号或盈亏规则覆盖。"""

    position = SimpleNamespace(
        position_id="position:600519",
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        unrealized_pnl_pct=0.08,
    )
    signal = SimpleNamespace(signal_id="signal:1", direction="bullish")
    action = PositionAction(
        position_id=position.position_id,
        action="reduce",
        severity="high",
        reason_codes=("structure_invalidated",),
        evaluated_at=datetime(2026, 6, 12, 10, 30, tzinfo=UTC),
        quote_snapshot_id="quote:1",
    )
    workflow_input = PortfolioMonitoringInput(
        owner_id="owner:1",
        portfolio=SimpleNamespace(portfolio_id="portfolio:1"),
        positions=(position,),
        signals_by_asset={position.asset_id: signal},
        risks_by_asset={position.asset_id: ()},
        memories_by_asset={position.asset_id: ()},
        as_of=action.evaluated_at,
        position_actions_by_position={position.position_id: action},
    )

    result = PortfolioMonitoringWorkflow().run(workflow_input)

    decision = result.decisions[0]
    assert decision.suggested_action == "reduce"
    assert decision.severity == "high"
    assert decision.decision_type == "position_monitoring"


def test_workflow_keeps_unexecutable_boundary_and_intended_action() -> None:
    """不可执行动作应转为等待，同时保留原计划动作供审计。"""

    position = SimpleNamespace(
        position_id="position:600519",
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        unrealized_pnl_pct=-0.12,
    )
    action = PositionAction(
        position_id=position.position_id,
        action="unexecutable",
        intended_action="exit",
        severity="high",
        reason_codes=("quote_missing",),
        evaluated_at=datetime(2026, 6, 12, 10, 30, tzinfo=UTC),
    )
    workflow_input = PortfolioMonitoringInput(
        owner_id="owner:1",
        portfolio=SimpleNamespace(portfolio_id="portfolio:1"),
        positions=(position,),
        signals_by_asset={},
        risks_by_asset={},
        memories_by_asset={},
        as_of=action.evaluated_at,
        position_actions_by_position={position.position_id: action},
    )

    decision = PortfolioMonitoringWorkflow().run(workflow_input).decisions[0]

    assert decision.suggested_action == "wait"
    assert decision.decision_type == "position_monitoring_unexecutable"
    assert decision.intended_action == "exit"
    assert "quote_missing" in decision.summary
