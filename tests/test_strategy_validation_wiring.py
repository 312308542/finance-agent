from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from test_strategy_trial_state import MIXED, THEME, _service, _trial_state

from finance_agent.research import strategy_observation_service as observation_module
from finance_agent.research import strategy_walk_forward_runner
from finance_agent.research.validation_gate import StrategyValidationGate
from finance_agent.scheduler.base_data_scheduler import BaseDataScheduler, BaseDataSchedulerConfig
from finance_agent.storage import db
from finance_agent.storage.repositories import StrategyObservationRepository

NOW = datetime(2026, 9, 7, tzinfo=UTC)


def _passing_history() -> dict[str, Any]:
    return {
        "strategy_id": MIXED,
        "status": "available",
        "backtest_id": "bt:wf:v2:passed",
        "payload": {"schema_version": "strategy_walk_forward_v2"},
        "metrics": {
            "gate_passed": True,
            "valid_cross_sections": 150,
            "horizon_mean_excess_returns": {"5": 0.01, "10": 0.02, "20": 0.03},
            "t10_block_bootstrap": {"ci_95": [0.005, 0.03]},
            "positive_t10_phase_count": 3,
            "drawdown_gap": 0.01,
            "rank_ic": {"mean": 0.05},
            "turnover": {"weekly_mean": 0.20},
            "execution": {"unexecutable_rate": 0.01},
        },
    }


def _forward_metrics(**overrides: Any) -> dict[str, Any]:
    return {
        "sample_counts": {"5": 60, "10": 60, "20": 60},
        "median_excess_returns": {"5": 0.01, "10": 0.02, "20": 0.03},
        "rolling_excess": 0.02,
        "drawdown_gap": 0.01,
        "data_integrity_violations": [],
        **overrides,
    }


@pytest.mark.parametrize("invalid", ["legacy", "missing_id", "bad_rank_ic", "wrong_strategy"])
def test_historical_admission_requires_matching_complete_v2_evidence(invalid: str) -> None:
    service, _repository, _source = _service()
    result = _passing_history()
    if invalid == "legacy":
        result["payload"] = {"schema_version": "strategy_walk_forward_v1"}
    elif invalid == "missing_id":
        result.pop("backtest_id")
    elif invalid == "bad_rank_ic":
        result["metrics"]["rank_ic"] = {"mean": float("nan")}
    else:
        result["strategy_id"] = THEME

    state = service.apply_historical_result(strategy_id=MIXED, result=result)

    assert state.state == "research"
    assert state.historical_evidence_id is None


def test_complete_v2_evidence_enters_trial_without_resetting_later_weekly_failures() -> None:
    service, repository, _source = _service()
    state = service.apply_historical_result(strategy_id=MIXED, result=_passing_history())
    assert state.state == "trial"
    state.consecutive_failure_count = 2
    state.last_evaluated_at = NOW
    state.forward_metrics = _forward_metrics()
    repository.states[MIXED] = state

    repeated = service.apply_historical_result(strategy_id=MIXED, result=_passing_history())

    assert repeated.consecutive_failure_count == 2
    assert repeated.last_evaluated_at == NOW
    assert repeated.forward_metrics == _forward_metrics()


def test_invalid_history_revokes_evidence_without_reopening_validated_state() -> None:
    service, repository, _source = _service()
    state = _trial_state()
    state.state = "validated"
    state.forward_metrics = _forward_metrics()
    repository.states[MIXED] = state

    saved = service.apply_historical_result(
        strategy_id=MIXED,
        result={"status": "unavailable", "metrics": {}},
    )

    assert saved.state == "validated"
    assert saved.historical_evidence_id is None
    assert not StrategyValidationGate().evaluate_runtime(saved, action="buy").allowed


@pytest.mark.parametrize("state_name", ["trial", "validated"])
def test_three_distinct_weekly_hard_failures_persist_and_disable(state_name: str) -> None:
    service, repository, _source = _service()
    state = _trial_state()
    state.state = state_name
    repository.states[MIXED] = state
    metrics = _forward_metrics(median_excess_returns={"5": 0.01, "10": 0.01, "20": 0.0})

    for week in range(3):
        as_of = NOW + timedelta(weeks=week)
        service.evaluate_weekly(strategy_id=MIXED, as_of=as_of, metrics=metrics)
        repeated = service.evaluate_weekly(strategy_id=MIXED, as_of=as_of, metrics=metrics)
        assert repeated.consecutive_failure_count == week + 1

    assert repository.states[MIXED].state == "disabled"


def test_replaying_older_week_does_not_count_failure_or_overwrite_newer_metrics() -> None:
    service, repository, _source = _service()
    repository.states[MIXED] = _trial_state()
    metrics = _forward_metrics(median_excess_returns={"20": 0.0})
    service.evaluate_weekly(strategy_id=MIXED, as_of=NOW, metrics=metrics)
    latest = service.evaluate_weekly(
        strategy_id=MIXED, as_of=NOW + timedelta(weeks=1), metrics=metrics
    )

    replayed = service.evaluate_weekly(
        strategy_id=MIXED, as_of=NOW, metrics=_forward_metrics(rolling_excess=-0.02)
    )

    assert replayed.state == "trial"
    assert replayed.consecutive_failure_count == 2
    assert replayed.last_evaluated_at == latest.last_evaluated_at
    assert replayed.forward_metrics == metrics


def test_missing_data_does_not_consume_weeks_first_complete_failure_evaluation() -> None:
    service, repository, _source = _service()
    repository.states[MIXED] = _trial_state()
    metrics = _forward_metrics(median_excess_returns={"20": 0.0})
    service.evaluate_weekly(strategy_id=MIXED, as_of=NOW, metrics=metrics)
    service.evaluate_weekly(strategy_id=MIXED, as_of=NOW + timedelta(weeks=1), metrics=metrics)
    shortage = service.evaluate_weekly(strategy_id=MIXED, as_of=NOW + timedelta(weeks=2), metrics={})
    assert shortage.consecutive_failure_count == 2

    saved = service.evaluate_weekly(
        strategy_id=MIXED, as_of=NOW + timedelta(weeks=2, days=1), metrics=metrics
    )

    assert saved.state == "disabled"
    assert saved.consecutive_failure_count == 3


def test_validated_state_still_disables_on_new_integrity_violation_in_same_week() -> None:
    service, repository, _source = _service()
    state = _trial_state()
    state.state = "validated"
    state.last_evaluated_at = NOW
    state.forward_metrics = _forward_metrics()
    repository.states[MIXED] = state

    saved = service.evaluate_weekly(
        strategy_id=MIXED,
        as_of=NOW + timedelta(days=1),
        metrics=_forward_metrics(data_integrity_violations=["future_data_detected"]),
    )

    assert saved.state == "disabled"
    assert saved.payload["forward_validation"]["allowed"] is False
    assert "data_integrity:future_data_detected" in saved.payload["forward_validation"]["reason_codes"]


@pytest.mark.parametrize("state_name", ["trial", "validated"])
def test_missing_forward_data_keeps_state_but_refreshes_buy_block(state_name: str) -> None:
    service, repository, _source = _service()
    state = _trial_state()
    state.state = state_name
    state.forward_metrics = _forward_metrics()
    repository.states[MIXED] = state

    saved = service.evaluate_weekly(strategy_id=MIXED, as_of=NOW, metrics={})

    assert saved.state == state_name
    assert saved.consecutive_failure_count == 0
    assert saved.forward_metrics == {}
    assert not StrategyValidationGate().evaluate_runtime(saved, action="buy").allowed


def test_forward_metrics_use_recent_sixty_matured_cross_sections() -> None:
    service, repository, _source = _service()
    for index in range(90):
        signal_date = date(2026, 1, 1) + timedelta(days=index)
        excess = 0.50 if index < 30 else 0.01
        for horizon in (5, 10, 20):
            outcome_id = f"outcome:{index}:{horizon}"
            position_id = f"position:{index}"
            repository.positions[position_id] = {"strategy_id": MIXED}
            repository.outcomes[outcome_id] = {
                "outcome_id": outcome_id,
                "position_id": position_id,
                "status": "matured",
                "horizon_days": horizon,
                "gross_return": excess + 0.003,
                "net_return": excess,
                "benchmark_return": 0.0,
                "excess_return": excess,
                "payload": {"signal_date": signal_date.isoformat()},
            }

    metrics = service.build_forward_metrics(strategy_id=MIXED)

    assert metrics["sample_counts"] == {"5": 60, "10": 60, "20": 60}
    assert metrics["rolling_excess"] == pytest.approx(0.01)
    assert metrics["median_excess_returns"]["20"] == pytest.approx(0.01)
    assert "gate_passed" not in metrics


def _scheduler_with_service(monkeypatch: pytest.MonkeyPatch, service: Any) -> BaseDataScheduler:
    monkeypatch.setattr(db, "create_session_factory", lambda: object())
    monkeypatch.setattr(db, "session_scope", lambda _factory: nullcontext(object()))
    monkeypatch.setattr(observation_module, "create_strategy_observation_service", lambda _s: service)
    monkeypatch.setattr(
        StrategyObservationRepository,
        "get_trial_state",
        lambda _self, strategy_id: service.repository.get_trial_state(strategy_id),
    )
    return BaseDataScheduler(BaseDataSchedulerConfig(cache_backend="null", jobs=()))


def test_forward_settlement_job_settles_and_evaluates_all_requested_strategies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, source = _service()
    for strategy_id in (MIXED, THEME):
        repository.states[strategy_id] = _trial_state(strategy_id)
    service.capture(
        screening_id="screen:forward",
        trade_date=NOW.date() - timedelta(days=40),
        strategy_ids=(MIXED, THEME),
    )
    outcome = next(iter(repository.outcomes.values()))
    source.settlements[outcome["outcome_id"]] = {
        "status": "matured",
        "entry_date": NOW.date() - timedelta(days=39),
        "entry_price": 10.0,
        "exit_date": NOW.date() - timedelta(days=35),
        "exit_price": 10.2,
        "gross_return": 0.02,
        "net_return": 0.017,
        "benchmark_return": 0.0,
        "excess_return": 0.017,
    }

    def unexpected_history(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("前向结算任务不得运行历史回测")

    monkeypatch.setattr(strategy_walk_forward_runner, "run_strategy_walk_forward", unexpected_history)
    scheduler = _scheduler_with_service(monkeypatch, service)

    result = scheduler.run_backtest(
        strategy="strategy_forward_settlement",
        strategy_id=MIXED,
        strategy_ids=[MIXED, THEME],
        as_of=NOW,
    )

    assert result["matured_count"] == 1
    assert repository.outcomes[outcome["outcome_id"]]["status"] == "matured"
    assert repository.states[MIXED].last_evaluated_at == NOW
    assert repository.states[THEME].last_evaluated_at == NOW


def test_validation_job_persists_promotion_and_returns_runtime_buy_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, repository, _source = _service()
    repository.states[MIXED] = _trial_state()
    monkeypatch.setattr(type(service), "build_forward_metrics", lambda _self, **_kwargs: _forward_metrics())
    scheduler = _scheduler_with_service(monkeypatch, service)

    result = scheduler.run_backtest(strategy="strategy_validation_gate", strategy_id=MIXED, as_of=NOW)

    assert repository.states[MIXED].state == "validated"
    assert result["state"] == "validated"
    assert result["allow_new_buys"] is True


def test_validation_job_retries_do_not_count_same_failure_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    service, repository, _source = _service()
    repository.states[MIXED] = _trial_state()
    metrics = _forward_metrics(median_excess_returns={"20": 0.0})
    monkeypatch.setattr(type(service), "build_forward_metrics", lambda _self, **_kwargs: metrics)
    scheduler = _scheduler_with_service(monkeypatch, service)

    for _ in range(2):
        result = scheduler.run_backtest(strategy="strategy_validation_gate", strategy_id=MIXED, as_of=NOW)

    assert repository.states[MIXED].consecutive_failure_count == 1
    assert result["allow_new_buys"] is False


@pytest.mark.parametrize("dry_run", [False, True])
def test_historical_job_persists_validation_state_unless_dry_run(
    monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    service, repository, _source = _service()
    monkeypatch.setattr(
        strategy_walk_forward_runner, "run_strategy_walk_forward", lambda *_args, **_kwargs: _passing_history()
    )
    scheduler = _scheduler_with_service(monkeypatch, service)

    scheduler.run_backtest(strategy="strategy_walk_forward_v2", strategy_id=MIXED, end_at=NOW, dry_run=dry_run)

    if dry_run:
        assert MIXED not in repository.states
    else:
        assert repository.states[MIXED].state == "trial"
        assert repository.states[MIXED].historical_evidence_id == "bt:wf:v2:passed"
