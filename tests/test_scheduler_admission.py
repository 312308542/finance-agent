"""持久调度准入与恢复数据域门控测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from finance_agent.data_recovery.gate import RecoveryGate
from finance_agent.scheduler.admission import (
    AdmissionSnapshot,
    SchedulerAdmissionController,
)

NOW = datetime(2026, 8, 31, 2, 0, tzinfo=UTC)


class _RecoveryRepository:
    def __init__(self, run=None, targets=()) -> None:
        self.run = run
        self.targets = list(targets)

    def get_active_run(self, _market: str):
        return self.run

    def list_blocking_targets(self, _run_id: str):
        return list(self.targets)

    def find_latest_cancelled_run(self, _market: str):
        return SimpleNamespace(run_id="cancelled:1", status="cancelled")

    def blocking_gap_exists(self, _run_id: str) -> bool:
        return True


@pytest.mark.parametrize(
    "status",
    ["draft", "completed", "completed_with_exceptions", "cancelled"],
)
def test_non_active_recovery_status_never_blocks(status: str) -> None:
    run = SimpleNamespace(run_id=f"rec:{status}", status=status, gate_status="degraded")
    gate = RecoveryGate(_RecoveryRepository(run))

    assert gate.current_state() == (None, "open")
    assert gate.blocked_domains() == {}


def test_cancelled_recovery_never_reopens_global_gate_from_historical_gaps() -> None:
    gate = RecoveryGate(_RecoveryRepository(None))

    assert gate.current_state() == (None, "open")


def test_active_recovery_exposes_blockers_grouped_by_data_domain() -> None:
    run = SimpleNamespace(run_id="rec:1", status="running", gate_status="recovering")
    targets = [
        SimpleNamespace(
            target_id="target:1",
            step_id="step:1",
            data_domain="market_bars",
            status="running",
            exception_code=None,
        ),
        SimpleNamespace(
            target_id="target:2",
            step_id="step:2",
            data_domain="fundamentals",
            status="failed",
            exception_code="transient",
        ),
    ]
    gate = RecoveryGate(_RecoveryRepository(run, targets))

    domains = gate.blocked_domains()

    assert set(domains) == {"market_bars", "fundamentals"}
    assert domains["market_bars"][0]["run_id"] == "rec:1"
    assert domains["market_bars"][0]["step_id"] == "step:1"


def _task(**overrides):
    values = {
        "task_id": "task:1",
        "job_name": "analytics.technical_screening",
        "required_data_domains": ["market_bars"],
        "blocked_until": None,
        "mutex_key": None,
        "resource_pool": "analytics",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_admission_blocks_only_intersecting_recovery_domains() -> None:
    controller = SchedulerAdmissionController()
    snapshot = AdmissionSnapshot(
        now=NOW,
        recovery_blocked_domains={"fundamentals": ({"run_id": "rec:1"},)},
    )

    decision = controller.evaluate(_task(), snapshot)

    assert decision.allowed is True


def test_admission_reports_recovery_domain_blockers() -> None:
    controller = SchedulerAdmissionController()
    blocker = {"run_id": "rec:1", "step_id": "step:1", "target_id": "target:1"}
    snapshot = AdmissionSnapshot(
        now=NOW,
        recovery_blocked_domains={"market_bars": (blocker,)},
    )

    decision = controller.evaluate(_task(), snapshot)

    assert decision.allowed is False
    assert decision.reason_code == "recovery_domain_blocked"
    assert decision.reason_detail["domains"] == ["market_bars"]
    assert decision.blocking_task_ids == ("target:1",)


@pytest.mark.parametrize(
    ("snapshot", "task", "reason"),
    [
        (AdmissionSnapshot(now=NOW, scheduler_paused=True), _task(), "scheduler_paused"),
        (
            AdmissionSnapshot(now=NOW, unsatisfied_dependencies={"task:1": ("task:up",)}),
            _task(),
            "dependency_not_satisfied",
        ),
        (
            AdmissionSnapshot(now=NOW, trading_session_open={"task:1": False}),
            _task(),
            "outside_trading_session",
        ),
        (
            AdmissionSnapshot(now=NOW, active_mutex_keys={"quotes"}),
            _task(mutex_key="quotes"),
            "mutex_busy",
        ),
        (
            AdmissionSnapshot(
                now=NOW,
                resource_pool_limits={"analytics": 2},
                resource_pool_running={"analytics": 2},
            ),
            _task(),
            "resource_pool_full",
        ),
        (
            AdmissionSnapshot(now=NOW),
            _task(blocked_until=NOW + timedelta(minutes=1)),
            "retry_backoff",
        ),
    ],
)
def test_admission_returns_stable_reason_codes(snapshot, task, reason: str) -> None:
    decision = SchedulerAdmissionController().evaluate(task, snapshot)

    assert decision.allowed is False
    assert decision.reason_code == reason
