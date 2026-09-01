"""补跑状态机与门控单元测试。"""

from types import SimpleNamespace

import pytest

from finance_agent.data_recovery.gate import (
    BLOCKED_BY_RECOVERY,
    RecoveryGate,
    evaluate_policy,
)
from finance_agent.data_recovery.state_machine import (
    InvalidRecoveryTransition,
    assert_transition,
    can_transition,
    gate_status_for_run,
)


def test_legal_transitions() -> None:
    for source, target in [
        ("draft", "approved"),
        ("draft", "cancelled"),
        ("approved", "running"),
        ("running", "paused"),
        ("paused", "running"),
        ("running", "verifying"),
        ("running", "attention_required"),
        ("attention_required", "running"),
        ("verifying", "completed"),
        ("verifying", "completed_with_exceptions"),
    ]:
        assert can_transition(source, target), (source, target)
        assert_transition(source, target)


def test_illegal_transitions_rejected() -> None:
    with pytest.raises(InvalidRecoveryTransition):
        assert_transition("draft", "completed")
    with pytest.raises(InvalidRecoveryTransition):
        assert_transition("completed", "running")
    with pytest.raises(InvalidRecoveryTransition):
        assert_transition("paused", "verifying")
    assert not can_transition("cancelled", "running")


def test_gate_status_for_run_three_states() -> None:
    assert gate_status_for_run("running", has_blocking_gaps=False) == "recovering"
    assert gate_status_for_run("verifying", has_blocking_gaps=True) == "recovering"
    assert gate_status_for_run("completed", has_blocking_gaps=False) == "open"
    assert gate_status_for_run("completed_with_exceptions", has_blocking_gaps=False) == "open"
    assert gate_status_for_run("draft", has_blocking_gaps=False) == "open"
    assert gate_status_for_run("paused", has_blocking_gaps=True) == "degraded"
    assert gate_status_for_run("attention_required", has_blocking_gaps=True) == "degraded"
    assert gate_status_for_run("cancelled", has_blocking_gaps=True) == "degraded"
    # 仅有非阻塞缺口的取消批次不阻止派生任务（规格 12.1）。
    assert gate_status_for_run("cancelled", has_blocking_gaps=False) == "open"


class _FakeRepo:
    def __init__(self, run, targets=()) -> None:
        self._run = run
        self._targets = list(targets)

    def get_active_run(self, market: str):
        return self._run

    def list_blocking_targets(self, run_id: str):
        return list(self._targets)


def test_gate_decide_blocks_requires_open_only() -> None:
    run = SimpleNamespace(status="running", run_id="rec:1", gate_status="recovering")
    target = SimpleNamespace(
        target_id="target:1",
        step_id="step:1",
        data_domain="market_bars",
        status="running",
        exception_code=None,
    )
    gate = RecoveryGate(_FakeRepo(run, [target]))
    assert gate.current_state() == ("rec:1", "recovering")
    blocked = gate.decide(
        "quality.ashare",
        required_data_domains=("market_bars",),
    )
    assert not blocked.allowed and blocked.reason == BLOCKED_BY_RECOVERY
    allowed = gate.decide("ashare.realtime_quotes")
    assert allowed.allowed
    merge_allowed = gate.decide("ashare.market_bars")
    assert merge_allowed.allowed


def test_gate_open_when_no_active_run() -> None:
    gate = RecoveryGate(_FakeRepo(None))
    assert gate.current_state() == (None, "open")
    assert gate.decide("quality.ashare").allowed
    assert gate.decide("analytics.recommendations").allowed


def test_filter_due_states_partitions() -> None:
    run = SimpleNamespace(status="running", run_id="rec:1", gate_status="recovering")
    target = SimpleNamespace(
        target_id="target:1",
        step_id="step:1",
        data_domain="market_bars",
        status="running",
        exception_code=None,
    )
    gate = RecoveryGate(_FakeRepo(run, [target]))
    quality = SimpleNamespace(
        job=SimpleNamespace(
            name="quality.ashare",
            params={"requires_data_domains": ["market_bars"]},
        )
    )
    realtime = SimpleNamespace(
        job=SimpleNamespace(name="ashare.realtime_quotes", params={})
    )
    runnable, blocked = gate.filter_due_states([quality, realtime])
    assert [s.job.name for s in runnable] == ["ashare.realtime_quotes"]
    assert [(s.job.name, d.reason) for s, d in blocked] == [
        ("quality.ashare", BLOCKED_BY_RECOVERY)
    ]


def test_evaluate_policy_open_passes_everything() -> None:
    from finance_agent.data_recovery.models import TaskMergePolicy

    for policy in TaskMergePolicy:
        decision = evaluate_policy(policy, "open")
        assert decision.allowed
