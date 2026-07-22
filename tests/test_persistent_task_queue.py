from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy.dialects import postgresql

from finance_agent.storage.repositories import SchedulerTaskRepository

NOW = datetime(2026, 7, 20, 9, 40, tzinfo=UTC)


class _Result:
    def __init__(self, rowcount: int = 1) -> None:
        self.rowcount = rowcount


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def first(self) -> Any:
        return self.value


class _Session:
    def __init__(self, *, task: Any = None, rowcount: int = 1) -> None:
        self.task = task
        self.rowcount = rowcount
        self.executed: list[Any] = []
        self.flush_count = 0

    def execute(self, statement: Any) -> _Result:
        self.executed.append(statement)
        return _Result(self.rowcount)

    def flush(self) -> None:
        self.flush_count += 1

    def get_one(self, _model: Any, _key: Any) -> Any:
        return self.task or SimpleNamespace(task_id=_key, status="pending")

    def get(self, _model: Any, _key: Any) -> Any:
        return self.task

    def scalars(self, statement: Any) -> _ScalarResult:
        self.executed.append(statement)
        return _ScalarResult(self.task)


def _compiled(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def test_enqueue_is_idempotent_by_key() -> None:
    session = _Session()

    task = SchedulerTaskRepository(session).enqueue(
        job_name="ashare.realtime_quotes",
        idempotency_key="ashare.realtime_quotes:20260720:09:40",
        payload={"data_snapshot_id": "snapshot:quotes:1"},
        now=NOW,
    )

    sql = _compiled(session.executed[0])
    assert "ON CONFLICT ON CONSTRAINT uq_scheduler_task_runs_idempotency DO NOTHING" in sql
    assert task.task_id.startswith("task:")
    assert session.flush_count == 1


def test_claim_uses_skip_locked_and_sets_lease() -> None:
    task = SimpleNamespace(
        task_id="task:1",
        status="pending",
        attempts=0,
        max_attempts=3,
        lease_owner=None,
        lease_token=None,
        lease_expires_at=None,
        started_at=None,
    )
    session = _Session(task=task)

    claimed = SchedulerTaskRepository(session).claim(worker_id="worker-1", now=NOW)

    sql = _compiled(session.executed[0])
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert claimed is task
    assert task.status == "running"
    assert task.lease_owner == "worker-1"
    assert task.lease_token
    assert task.lease_expires_at == NOW + timedelta(seconds=60)
    assert task.attempts == 1


def test_claim_can_target_one_scheduler_idempotency_key() -> None:
    """调度器精确领取自己的任务时仍保留 SKIP LOCKED。"""

    task = SimpleNamespace(
        task_id="task:1",
        status="pending",
        attempts=0,
        max_attempts=1,
        lease_owner=None,
        lease_token=None,
        lease_expires_at=None,
        started_at=None,
    )
    session = _Session(task=task)

    claimed = SchedulerTaskRepository(session).claim(
        worker_id="worker-1",
        job_name="ashare.realtime_quotes",
        idempotency_key="scheduler:ashare.realtime_quotes:2026-07-20T09:40:00+00:00",
        now=NOW,
    )

    sql = _compiled(session.executed[0])
    assert claimed is task
    assert "scheduler_task_runs.job_name" in sql
    assert "scheduler_task_runs.idempotency_key" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql


def test_heartbeat_and_complete_require_active_lease() -> None:
    session = _Session(rowcount=1)
    repo = SchedulerTaskRepository(session)

    assert repo.heartbeat(task_id="task:1", lease_token="lease-1", now=NOW) is True
    assert repo.complete(task_id="task:1", lease_token="lease-1", now=NOW) is True
    assert session.flush_count == 2
    assert "lease_token" in _compiled(session.executed[0])
    assert "status" in _compiled(session.executed[1])


def test_fail_requeues_before_max_attempts_and_terminally_fails_after_limit() -> None:
    task = SimpleNamespace(
        task_id="task:1",
        status="running",
        attempts=1,
        max_attempts=3,
        lease_token="lease-1",
        lease_expires_at=NOW + timedelta(seconds=30),
        lease_owner="worker-1",
        next_retry_at=None,
        finished_at=None,
    )
    session = _Session(task=task)
    repo = SchedulerTaskRepository(session)

    assert repo.fail(
        task_id="task:1",
        lease_token="lease-1",
        error_message="provider timeout",
        retry_after=timedelta(seconds=15),
        now=NOW,
    ) is True
    assert task.status == "pending"
    assert task.next_retry_at == NOW + timedelta(seconds=15)

    task.status = "running"
    task.attempts = 3
    task.lease_token = "lease-2"
    task.lease_expires_at = NOW + timedelta(seconds=30)
    assert repo.fail(
        task_id="task:1",
        lease_token="lease-2",
        error_message="provider timeout",
        now=NOW,
    ) is True
    assert task.status == "failed"
    assert task.finished_at == NOW


def test_recover_expired_returns_updated_row_count() -> None:
    session = _Session(rowcount=4)

    recovered = SchedulerTaskRepository(session).recover_expired(now=NOW)

    assert recovered == 4
    sql = _compiled(session.executed[0])
    assert "scheduler_task_runs.status =" in sql
    assert "lease_expires_at" in sql
