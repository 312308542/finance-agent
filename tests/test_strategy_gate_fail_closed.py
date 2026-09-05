"""验证缺失、异常或过期准入证据不会放行新增买入。"""

from copy import deepcopy

import pytest

from finance_agent.research.validation_gate import StrategyValidationGate


def historical_result():
    return {
        "backtest_id": "bt:qualified",
        "status": "available",
        "payload": {"schema_version": "strategy_walk_forward_v2"},
        "metrics": {
            "gate_passed": True,
            "valid_cross_sections": 120,
            "total_cross_sections": 150,
            "horizon_mean_excess_returns": {"5": 0.02, "10": 0.03, "20": 0.04},
            "t10_block_bootstrap": {"ci_95": [0.01, 0.05]},
            "positive_t10_phase_count": 3,
            "drawdown_gap": 0.01,
            "rank_ic": {"mean": 0.04, "count": 120},
            "turnover": {"weekly_mean": 0.2, "observations": 24},
            "execution": {"unexecutable_rate": 0.01},
        },
    }


def forward_metrics():
    return {"t20_count": 60, "median_excess": 0.02, "rolling_excess": 0.02}


def validated_state():
    return {
        "state": "validated",
        "historical_evidence_id": "bt:qualified",
        "forward_metrics": forward_metrics(),
    }


def test_complete_evidence_allows_history_and_runtime_buy():
    gate = StrategyValidationGate()
    assert gate.evaluate_history(historical_result()).allowed
    assert gate.evaluate_runtime(validated_state(), action="buy_ready").allowed


@pytest.mark.parametrize("field", [
    "gate_passed", "valid_cross_sections", "t10_block_bootstrap", "drawdown_gap",
    "rank_ic", "turnover", "execution", "horizon_mean_excess_returns",
])
def test_missing_historical_metric_blocks_admission(field):
    result = historical_result()
    result["metrics"].pop(field)
    decision = StrategyValidationGate().evaluate_history(result)
    assert not decision.allowed
    assert decision.next_state == "research"


@pytest.mark.parametrize("value", [0, -1, None, "bad", float("nan"), float("inf"), 120.5, True])
def test_invalid_valid_count_does_not_fall_back_to_total(value):
    result = historical_result()
    result["metrics"]["valid_cross_sections"] = value
    assert not StrategyValidationGate().evaluate_history(result).allowed


@pytest.mark.parametrize("value", [None, "", " "])
def test_missing_historical_identity_blocks_admission(value):
    result = historical_result()
    result["backtest_id"] = value
    assert not StrategyValidationGate().evaluate_history(result).allowed


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True, "bad"])
@pytest.mark.parametrize("path", [
    ("drawdown_gap",), ("rank_ic", "mean"), ("turnover", "weekly_mean"),
    ("execution", "unexecutable_rate"), ("horizon_mean_excess_returns", "10"),
])
def test_nonfinite_or_malformed_historical_values_block_admission(path, value):
    result = historical_result()
    target = result["metrics"]
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    assert not StrategyValidationGate().evaluate_history(result).allowed


@pytest.mark.parametrize("field", ["median_excess", "rolling_excess"])
@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), "bad"])
def test_incomplete_forward_evidence_never_promotes_or_allows_buys(field, value):
    metrics = forward_metrics()
    metrics[field] = value
    state = validated_state()
    decision = StrategyValidationGate().evaluate_forward(state=state, outcomes=metrics)
    assert not decision.allowed
    assert decision.next_state == "validated"
    state["state"] = "trial"
    assert StrategyValidationGate().evaluate_forward(state=state, outcomes=metrics).next_state == "trial"


def test_forward_promotion_requires_historical_evidence():
    decision = StrategyValidationGate().evaluate_forward(
        state={"state": "trial"}, outcomes=forward_metrics(),
    )
    assert not decision.allowed
    assert decision.next_state == "trial"


@pytest.mark.parametrize("field", ["historical_evidence_id", "forward_metrics"])
def test_validated_label_alone_does_not_allow_buy(field):
    state = deepcopy(validated_state())
    state.pop(field)
    assert not StrategyValidationGate().evaluate_runtime(state, action="buy_ready").allowed


@pytest.mark.parametrize("state", ["research", "historical_passed", "trial", "disabled", "unknown"])
def test_only_validated_may_buy_but_exit_management_is_retained(state):
    record = {**validated_state(), "state": state}
    gate = StrategyValidationGate()
    assert not gate.evaluate_runtime(record, action="buy_ready").allowed
    for action in ("watch", "hold", "active", "weakening", "exit_pending", "reduce", "exit"):
        assert gate.evaluate_runtime(record, action=action).allowed


def test_unknown_action_cannot_pass_even_with_validated_state():
    assert not StrategyValidationGate().evaluate_runtime(validated_state(), action="unexpected").allowed


def test_insufficient_forward_data_does_not_count_as_third_strategy_failure():
    state = {**validated_state(), "state": "trial", "consecutive_failure_count": 2}
    decision = StrategyValidationGate().evaluate_forward(
        state=state, outcomes={"t20_count": 59, "median_excess": -0.01, "rolling_excess": 0.01},
    )
    assert decision.next_state == "trial"
    assert not decision.allowed


@pytest.mark.parametrize("ci", [[0.01], [0.01, float("nan")], [0.01, float("inf")], [0.05, 0.01]])
def test_bootstrap_interval_requires_two_ordered_finite_bounds(ci):
    result = historical_result()
    result["metrics"]["t10_block_bootstrap"]["ci_95"] = ci
    assert not StrategyValidationGate().evaluate_history(result).allowed


@pytest.mark.parametrize("state", ["research", "trial", "validated", "disabled"])
def test_runtime_gate_never_weakens_avoid_risk_advice(state):
    assert StrategyValidationGate().evaluate_runtime({"state": state}, action="avoid").allowed
