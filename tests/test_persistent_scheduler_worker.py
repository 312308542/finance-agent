"""数据库直领 worker 与重启恢复测试。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from finance_agent.scheduler.base_data_scheduler import BaseDataSchedulerJob
from finance_agent.scheduler.persistent_scheduler_worker import PersistentSchedulerWorker
from finance_agent.scheduler.persistent_task_queue import TaskClaim

NOW = datetime(2026, 8, 31, 2, 0, tzinfo=UTC)


class _Queue:
    def __init__(self, claims=()) -> None:
        self.claims = list(claims)
        self.claim_calls: list[dict[str, Any]] = []
        self.completed: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []
        self.scheduled: list[dict[str, Any]] = []
        self.admitted: list[dict[str, Any]] = []
        self.recovered = 0

    def claim_many(self, **kwargs: Any) -> list[TaskClaim]:
        self.claim_calls.append(kwargs)
        limit = int(kwargs["limit"])
        claimed, self.claims = self.claims[:limit], self.claims[limit:]
        return claimed

    def recover_expired(self, **_kwargs: Any) -> int:
        self.recovered += 1
        return 1

    def complete(self, **kwargs: Any) -> bool:
        self.completed.append(kwargs)
        return True

    def fail(self, **kwargs: Any) -> bool:
        self.failed.append(kwargs)
        return True

    def schedule(self, **kwargs: Any) -> SimpleNamespace:
        self.scheduled.append(kwargs)
        return SimpleNamespace(task_id="task:next")

    def set_admission(self, **kwargs: Any) -> bool:
        self.admitted.append(kwargs)
        return True


def _claim(
    *,
    task_id: str = "task:1",
    job_name: str = "ashare.news_articles",
    payload: dict[str, Any] | None = None,
) -> TaskClaim:
    return TaskClaim(
        task_id=task_id,
        job_name=job_name,
        lease_token=f"lease:{task_id}",
        payload=dict(payload or {}),
        attempts=1,
        max_attempts=3,
        scheduled_for=NOW,
        priority=10,
        resource_pool="realtime",
    )


def _worker(queue: _Queue, execute, *, clock=lambda: NOW):
    @contextmanager
    def queue_scope() -> Iterator[_Queue]:
        yield queue

    job = BaseDataSchedulerJob(
        name="ashare.news_articles",
        group="ashare-p1",
        interval_seconds=300,
        params={"scope": "default"},
    )
    return PersistentSchedulerWorker(
        queue_scope=queue_scope,
        jobs={job.name: job},
        execute_job=execute,
        worker_id="scheduler:1",
        lease_seconds=600,
        resource_pool_limits={"realtime": 2},
        clock=clock,
    )


def test_worker_claims_pending_task_without_local_due_state() -> None:
    queue = _Queue([_claim()])
    worker = _worker(queue, lambda _job: {"status": "executed"})

    claims = worker.claim(free_slots=2, now=NOW)

    assert [claim.job_name for claim in claims] == ["ashare.news_articles"]
    assert queue.claim_calls[0]["job_names"] == ("ashare.news_articles",)
    assert queue.claim_calls[0]["limit"] == 2
    assert queue.claim_calls[0]["resource_pool_limits"] == {"realtime": 2}


def test_expired_running_task_is_recovered_and_executed_after_restart() -> None:
    queue = _Queue([_claim(task_id="task:expired")])
    worker = _worker(queue, lambda _job: {"status": "executed"})

    assert worker.startup_recover(now=NOW) == 1
    result = worker.run_once(now=NOW)

    assert result.completed == 1
    assert queue.recovered == 1
    assert queue.completed == [
        {"task_id": "task:expired", "lease_token": "lease:task:expired", "now": NOW}
    ]


def test_worker_rebuilds_job_params_from_persistent_payload() -> None:
    queue = _Queue([_claim(payload={"params": {"partition_cursor": 7, "scope": "sweep"}})])
    received: list[BaseDataSchedulerJob] = []
    worker = _worker(queue, lambda job: received.append(job) or {"status": "executed"})

    worker.run_once(now=NOW)

    assert received[0].params == {"scope": "sweep", "partition_cursor": 7}


def test_worker_failure_releases_lease_through_queue() -> None:
    queue = _Queue([_claim()])

    def fail(_job):
        raise RuntimeError("provider unavailable")

    result = _worker(queue, fail).run_once(now=NOW)

    assert result.failed == 1
    assert queue.completed == []
    assert queue.failed[0]["task_id"] == "task:1"
    assert queue.failed[0]["error_message"] == "provider unavailable"


def test_worker_writes_actual_completion_time_after_execution() -> None:
    finished_at = NOW + timedelta(seconds=45)
    queue = _Queue([_claim()])

    _worker(
        queue,
        lambda _job: {"status": "executed"},
        clock=lambda: finished_at,
    ).run_once(now=NOW)

    assert queue.completed[0]["now"] == finished_at


def test_worker_writes_actual_failure_time_after_execution() -> None:
    failed_at = NOW + timedelta(seconds=30)
    queue = _Queue([_claim()])

    def fail(_job):
        raise RuntimeError("provider unavailable")

    _worker(queue, fail, clock=lambda: failed_at).run_once(now=NOW)

    assert queue.failed[0]["now"] == failed_at


def test_worker_persists_next_partition_before_completing_current() -> None:
    finished_at = NOW + timedelta(seconds=20)
    queue = _Queue([_claim(payload={"params": {"partition_cursor": 2}})])
    worker = _worker(
        queue,
        lambda _job: {
            "status": "executed",
            "next_partition_payload": {"partition_cursor": 3, "partition_count": 10},
        },
        clock=lambda: finished_at,
    )

    result = worker.run_once(now=NOW)

    assert result.completed == 1
    assert queue.scheduled[0]["payload"]["params"]["partition_cursor"] == 3
    assert queue.scheduled[0]["idempotency_key"].endswith(":partition:3")
    assert queue.scheduled[0]["now"] == finished_at
    assert queue.scheduled[0]["mutex_key"] == "scheduler.job:ashare.news_articles"
    assert queue.admitted == []
    assert queue.completed


def test_worker_propagates_config_digest_to_next_partition() -> None:
    """分区续跑必须继承当前配置摘要，避免 Reporter 回退或误报漂移。"""

    claim = _claim()
    object.__setattr__(claim, "config_digest", "scheduler-digest")
    queue = _Queue([claim])
    worker = _worker(
        queue,
        lambda _job: {
            "status": "executed",
            "next_partition_payload": {"partition_cursor": 3, "partition_count": 10},
        },
    )

    worker.run_once(now=NOW)

    assert queue.scheduled[0]["config_digest"] == "scheduler-digest"


def test_worker_persists_partition_cursor_from_collection_summary() -> None:
    """采集摘要嵌套 payload 中的游标也必须在完成当前租约前持久化。"""

    queue = _Queue([_claim(payload={"params": {"partition_cursor": 2}})])
    worker = _worker(
        queue,
        lambda _job: {
            "status": "executed",
            "summary": {
                "results": [
                    {
                        "payload": {
                            "next_partition_payload": {
                                "partition_cursor": 3,
                                "partition_count": 10,
                            }
                        }
                    }
                ]
            },
        },
    )

    worker.run_once(now=NOW)

    assert queue.scheduled[0]["payload"]["params"]["partition_cursor"] == 3
