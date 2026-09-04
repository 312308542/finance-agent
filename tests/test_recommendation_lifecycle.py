"""推荐设置与生命周期数据契约测试。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from finance_agent.recommendations.lifecycle import (
    RECOMMENDATION_STATES,
    RecommendationState,
    RecommendationTransition,
    StockSetup,
)

NOW = datetime(2026, 9, 7, 7, 0, tzinfo=UTC)


def test_lifecycle_contract_contains_all_legal_states() -> None:
    assert RECOMMENDATION_STATES == (
        "discovered",
        "watch",
        "setup_confirming",
        "buy_ready",
        "active",
        "weakening",
        "exit_pending",
        "exited",
        "cooldown",
    )


def test_setup_state_and_transition_are_immutable() -> None:
    setup = StockSetup(
        setup_id="setup:1",
        owner_id="default-owner",
        decision_snapshot_id="decision:1",
        asset_id="ashare:600519",
        strategy_id="strategy:ashare:adaptive_v1",
        setup_type="retest",
        planned_horizon_days=10,
        entry_zone={"low": "9.80", "high": "10.10"},
        invalidation_price=Decimal("9.40"),
        target_zone={"low": "11.20", "high": "11.80"},
        expected_net_return=Decimal("0.08"),
        downside_risk=Decimal("0.03"),
        confidence=Decimal("0.82"),
        as_of=NOW,
        payload={},
    )
    state = RecommendationState(
        state_id="state:1",
        owner_id="default-owner",
        strategy_id="strategy:ashare:adaptive_v1",
        asset_id="ashare:600519",
        setup_id=setup.setup_id,
        current_state="watch",
        previous_state=None,
        decision_snapshot_id="decision:1",
        state_changed_at=NOW,
        consecutive_valid_closes=0,
        active_days=0,
        cooldown_until=None,
        payload={},
    )
    transition = RecommendationTransition(
        event_id="event:1",
        state_id=state.state_id,
        owner_id=state.owner_id,
        strategy_id=state.strategy_id,
        asset_id=state.asset_id,
        setup_id=setup.setup_id,
        from_state=None,
        to_state="watch",
        reason_codes=("discovered",),
        decision_snapshot_id="decision:1",
        occurred_at=NOW,
        consecutive_valid_closes=0,
        active_days=0,
        cooldown_until=None,
        payload={},
    )

    with pytest.raises(FrozenInstanceError):
        setup.confidence = Decimal("1")  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        state.current_state = "active"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        transition.to_state = "buy_ready"  # type: ignore[misc]
