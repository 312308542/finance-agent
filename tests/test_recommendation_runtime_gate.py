"""验证生产推荐入口必须读取持久化准入状态。"""

from types import SimpleNamespace

import pytest
from test_adaptive_recommendation_service import STRATEGY_ID, _budget, _service, _snapshot, _state
from test_strategy_gate_fail_closed import validated_state

from finance_agent.pipelines.recommendation import recommendation_strategy_gate


@pytest.mark.parametrize("state", ["research", "historical_passed", "trial", "disabled"])
def test_runtime_gate_downgrades_buy_and_persists_original_advice(state):
    service, store, states = _service(previous=_state("setup_confirming"))
    service.trial_states = SimpleNamespace(
        get_trial_state=lambda _: SimpleNamespace(**{**validated_state(), "state": state}),
    )
    result = service.rank_from_screening(
        screening_id="screen:adaptive", score_strategy_id=STRATEGY_ID,
        decision_snapshot=_snapshot(structure_confirmed=True), portfolio_budget=_budget(),
    )
    assert result.recommendation_count == 1
    assert result.buy_ready_count == 0
    assert store.assets[0]["action"] == "watch"
    assert states.transitions[0].to_state == "watch"
    assert states.transitions[0].payload["validation_gate"]["original_state"] == "buy_ready"
    assert store.assets[0]["payload"]["validation_gate"]["allowed"] is False
    assert store.assets[0]["payload"]["validation_gate"]["original_action"] == "buy_candidate"
    assert store.assets[0]["payload"]["intended_action"] == "buy_candidate"


def test_caller_cannot_spoof_validated_state_or_evidence():
    service, store, _ = _service(previous=_state("setup_confirming"))
    service.trial_states = SimpleNamespace(get_trial_state=lambda _: None)
    result = service.rank_from_screening(
        screening_id="screen:adaptive", score_strategy_id=STRATEGY_ID,
        decision_snapshot=_snapshot(structure_confirmed=True), portfolio_budget=_budget(),
        trial_state="validated", validation_evidence_id="bt:spoofed",
    )
    assert result.buy_ready_count == 0
    assert store.assets[0]["payload"]["validation_state"] == "research"
    assert store.assets[0]["payload"]["validation_evidence_id"] is None


def test_verified_persisted_state_permits_qualified_buy():
    service, store, states = _service(previous=_state("setup_confirming"))
    service.trial_states = SimpleNamespace(
        get_trial_state=lambda _: SimpleNamespace(**validated_state()),
    )
    result = service.rank_from_screening(
        screening_id="screen:adaptive", score_strategy_id=STRATEGY_ID,
        decision_snapshot=_snapshot(structure_confirmed=True), portfolio_budget=_budget(),
    )
    assert result.buy_ready_count == 1
    assert store.assets[0]["action"] == "buy_candidate"
    assert states.transitions[0].to_state == "buy_ready"


@pytest.mark.parametrize("strategy", [STRATEGY_ID, "strategy:ashare:legacy_default"])
def test_pipeline_gate_never_bypasses_unvalidated_strategy(strategy):
    gate = recommendation_strategy_gate(market="ashare", strategy_id=strategy, trial_states=None)
    assert not gate["allowed"]
    assert gate["trial_state"] == "research"


def test_disabled_strategy_keeps_held_lifecycle_management():
    service, store, states = _service(previous=_state("active"))
    service.trial_states = SimpleNamespace(
        get_trial_state=lambda _: SimpleNamespace(state="disabled"),
    )
    result = service.rank_from_screening(
        screening_id="screen:adaptive", score_strategy_id=STRATEGY_ID,
        decision_snapshot=_snapshot(structure_confirmed=True), portfolio_budget=_budget(),
    )
    assert result.recommendation_count == 1
    assert store.assets[0]["action"] == "hold"
    assert states.transitions[0].to_state == "active"
