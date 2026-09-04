"""推荐设置与生命周期数据契约测试。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from finance_agent.recommendations.lifecycle import (
    LEGAL_STATE_TRANSITIONS,
    RECOMMENDATION_STATES,
    LifecycleEvidence,
    RecommendationLifecycleEngine,
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


def test_candidate_needs_two_valid_closes_before_buy_ready() -> None:
    engine = RecommendationLifecycleEngine()

    first = engine.transition(None, _evidence(day=1, eligible=True, setup_id="setup-1"))
    second = engine.transition(
        first.state,
        _evidence(day=2, eligible=True, setup_id="setup-1"),
    )

    assert first.to_state == "setup_confirming"
    assert second.to_state == "buy_ready"


def test_active_position_uses_lower_retention_threshold() -> None:
    transition = RecommendationLifecycleEngine().transition(
        _state("active", active_days=6),
        _evidence(
            day=7,
            eligible=False,
            alpha_score=61,
            entry_threshold=70,
            retention_threshold=58,
        ),
    )

    assert transition.to_state == "active"
    assert transition.reason_codes == ("retention_threshold_met",)
    assert transition.active_days == 7


def test_high_quality_intraday_breakout_can_confirm_once() -> None:
    transition = RecommendationLifecycleEngine().transition(
        None,
        _evidence(day=1, eligible=True, high_quality_intraday_breakout=True),
    )

    assert transition.to_state == "buy_ready"
    assert transition.reason_codes == ("high_quality_intraday_breakout",)


def test_same_evidence_generates_deterministic_event_id() -> None:
    engine = RecommendationLifecycleEngine()
    evidence = _evidence(day=1, eligible=True)

    first = engine.transition(None, evidence)
    replayed = engine.transition(None, evidence)

    assert first.event_id == replayed.event_id
    assert first.state_id == replayed.state_id


def test_legal_transition_table_rejects_skipping_from_watch_to_active() -> None:
    assert "active" not in LEGAL_STATE_TRANSITIONS["watch"]

    with pytest.raises(ValueError, match="非法的推荐生命周期迁移"):
        RecommendationLifecycleEngine().ensure_legal_transition("watch", "active")


def test_structure_invalidation_immediately_moves_active_to_exit_pending() -> None:
    transition = RecommendationLifecycleEngine().transition(
        _state("active", active_days=4),
        _evidence(day=5, eligible=False, structure_invalidated=True),
    )

    assert transition.to_state == "exit_pending"
    assert transition.reason_codes == ("structure_invalidated",)


def test_ordinary_volatility_does_not_exit_valid_active_position() -> None:
    transition = RecommendationLifecycleEngine().transition(
        _state("active", active_days=4),
        _evidence(
            day=5,
            eligible=False,
            alpha_score=60,
            retention_threshold=58,
            ordinary_volatility=True,
        ),
    )

    assert transition.to_state == "active"
    assert "ordinary_volatility_tolerated" in transition.reason_codes


def test_sell_enters_three_day_cooldown_and_requires_new_setup_to_break() -> None:
    engine = RecommendationLifecycleEngine()
    sold = engine.transition(
        _state("active", active_days=8),
        _evidence(
            day=9,
            eligible=False,
            sold=True,
            cooldown_until=date(2026, 9, 14),
        ),
    )
    held = engine.transition(
        sold.state,
        _evidence(day=10, eligible=True, trade_date=date(2026, 9, 11)),
    )
    broken = engine.transition(
        held.state,
        _evidence(
            day=11,
            eligible=True,
            trade_date=date(2026, 9, 11),
            new_independent_catalyst=True,
            new_structure_setup=True,
            setup_id="setup-2",
        ),
    )

    assert sold.to_state == "cooldown"
    assert held.to_state == "cooldown"
    assert broken.to_state == "setup_confirming"
    assert broken.reason_codes == ("cooldown_broken_by_new_setup",)


def _state(
    current_state: str,
    *,
    active_days: int = 0,
) -> RecommendationState:
    return RecommendationState(
        state_id="state:1",
        owner_id="default-owner",
        strategy_id="strategy:ashare:adaptive_v1",
        asset_id="ashare:600519",
        setup_id="setup-1",
        current_state=current_state,  # type: ignore[arg-type]
        previous_state=None,
        decision_snapshot_id="decision:1",
        state_changed_at=datetime(2026, 9, 7, tzinfo=UTC),
        consecutive_valid_closes=1,
        active_days=active_days,
        cooldown_until=None,
        payload={},
    )


def _evidence(
    *,
    day: int,
    eligible: bool,
    setup_id: str = "setup-1",
    trade_date: date | None = None,
    **overrides: object,
) -> LifecycleEvidence:
    values: dict[str, object] = {
        "owner_id": "default-owner",
        "strategy_id": "strategy:ashare:adaptive_v1",
        "asset_id": "ashare:600519",
        "setup_id": setup_id,
        "decision_snapshot_id": f"decision:{day}",
        "as_of": datetime(2026, 9, 7, tzinfo=UTC) + timedelta(days=day - 1),
        "trade_date": trade_date or date(2026, 9, 7) + timedelta(days=day - 1),
        "eligible": eligible,
        "alpha_score": 75.0 if eligible else 50.0,
        "entry_threshold": 70.0,
        "retention_threshold": 58.0,
        "structure_invalidated": False,
        "high_quality_intraday_breakout": False,
        "ordinary_volatility": False,
        "sold": False,
        "cooldown_until": None,
        "new_independent_catalyst": False,
        "new_structure_setup": False,
        "reason_codes": (),
        "payload": {},
    }
    values.update(overrides)
    return LifecycleEvidence(**values)  # type: ignore[arg-type]
