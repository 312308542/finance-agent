from __future__ import annotations

from types import SimpleNamespace

from finance_agent.research.validation_gate import StrategyValidationGate


def _result(*, schema_version: str, metrics: dict | None = None, status: str = "available"):
    return SimpleNamespace(
        backtest_id="bt:test",
        status=status,
        metrics=metrics or {},
        payload={"schema_version": schema_version},
    )


def _passing_metrics() -> dict:
    return {
        "gate_passed": True,
        "valid_cross_sections": 120,
        "horizon_mean_excess_returns": {"5": 0.02, "10": 0.03, "20": 0.04},
        "t10_block_bootstrap": {"ci_95": [0.01, 0.05]},
        "positive_t10_phase_count": 3,
        "drawdown_gap": 0.01,
        "rank_ic": {"mean": 0.04},
        "turnover": {"weekly_mean": 0.2},
        "execution": {"unexecutable_rate": 0.01},
    }


def test_legacy_replayed_backtest_is_research_only() -> None:
    decision = StrategyValidationGate().evaluate_history(
        _result(schema_version="factor_score_topn_v1", metrics={"gate_passed": True})
    )

    assert decision.allowed is False
    assert decision.reason_codes == ("legacy_backtest_not_gating_eligible",)
    assert decision.next_state == "research"


def test_walk_forward_v2_passes_only_when_hard_metrics_pass() -> None:
    decision = StrategyValidationGate().evaluate_history(
        _result(schema_version="strategy_walk_forward_v2", metrics=_passing_metrics())
    )

    assert decision.allowed is True
    assert decision.next_state == "historical_passed"
    assert decision.evidence_id == "bt:test"


def test_forward_validation_keeps_trial_when_t20_samples_are_insufficient() -> None:
    decision = StrategyValidationGate().evaluate_forward(
        state=SimpleNamespace(state="trial"),
        outcomes={"t20_count": 59, "median_excess": 0.012, "rolling_excess": 0.012},
    )

    assert decision.next_state == "trial"
    assert decision.allowed is False
    assert "t20_samples_below_60" in decision.reason_codes


def test_negative_rolling_excess_disables_new_buys() -> None:
    decision = StrategyValidationGate().evaluate_forward(
        state=SimpleNamespace(state="validated"),
        outcomes={"t20_count": 80, "median_excess": 0.012, "rolling_excess": -0.01},
    )

    assert decision.next_state == "disabled"
    assert decision.allowed is False
    assert "rolling_excess_negative" in decision.reason_codes
