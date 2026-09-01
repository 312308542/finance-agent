"""BaseDataScheduler 持久调度闭环集成测试。"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from finance_agent.scheduler import (
    BaseDataScheduler,
    BaseDataSchedulerConfig,
    BaseDataSchedulerJob,
    TaskClaim,
)

NOW = datetime.now(tz=UTC).replace(microsecond=0)
ALL_STATES = (
    "scheduled",
    "blocked",
    "pending",
    "running",
    "completed",
    "failed",
    "cancelled",
)


class _PersistentQueue:
    """覆盖 planner、admission、worker 所需 interface 的内存 adapter。"""

    def __init__(self) -> None:
        self.rows: list[SimpleNamespace] = []
        self.completed_ids: list[str] = []
        self.recovered = 0
        self.admission_calls: list[dict[str, Any]] = []

    def add(
        self,
        *,
        job_name: str,
        status: str,
        task_id: str,
        scheduled_for: datetime = NOW,
        dependency_generation=(),
        payload: dict[str, Any] | None = None,
    ) -> SimpleNamespace:
        row = SimpleNamespace(
            task_id=task_id,
            job_name=job_name,
            idempotency_key=f"key:{task_id}",
            status=status,
            payload=dict(payload or {}),
            attempts=0,
            max_attempts=3,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=(NOW - timedelta(seconds=1) if status == "running" else None),
            started_at=None,
            scheduled_for=scheduled_for,
            priority=10,
            resource_pool="default",
            mutex_key=None,
            dependency_generation=list(dependency_generation),
            required_data_domains=[],
            blocked_reason=None,
            blocked_detail={},
            blocked_until=None,
            coalesced_count=0,
            created_at=scheduled_for,
        )
        self.rows.append(row)
        return row

    def list_tasks(self, *, job_name=None, statuses=ALL_STATES, limit=200, **_kwargs):
        rows = [row for row in self.rows if row.status in statuses]
        if job_name is not None:
            rows = [row for row in rows if row.job_name == job_name]
        return sorted(rows, key=lambda row: row.created_at, reverse=True)[:limit]

    def schedule(self, **kwargs):
        for row in self.rows:
            if row.idempotency_key == kwargs["idempotency_key"]:
                return row
        row = self.add(
            job_name=kwargs["job_name"],
            status="scheduled",
            task_id=f"task:planned:{len(self.rows) + 1}",
            scheduled_for=kwargs["scheduled_for"],
            dependency_generation=kwargs.get("dependency_generation") or (),
            payload=kwargs.get("payload"),
        )
        row.idempotency_key = kwargs["idempotency_key"]
        row.priority = kwargs.get("priority", 100)
        row.resource_pool = kwargs.get("resource_pool", "default")
        row.mutex_key = kwargs.get("mutex_key")
        row.required_data_domains = list(kwargs.get("required_data_domains") or ())
        return row

    def coalesce_task(self, **kwargs) -> bool:
        row = next(row for row in self.rows if row.task_id == kwargs["task_id"])
        row.scheduled_for = kwargs["scheduled_for"]
        row.coalesced_count += kwargs["coalesced_count_delta"]
        row.payload = kwargs["payload"]
        return True

    def set_admission(self, *, task_id: str, allowed: bool, reason_code=None, **kwargs) -> bool:
        self.admission_calls.append(
            {
                "task_id": task_id,
                "allowed": allowed,
                "reason_code": reason_code,
                "reason_detail": kwargs.get("reason_detail") or {},
                "recheck_at": kwargs.get("recheck_at"),
            }
        )
        row = next(row for row in self.rows if row.task_id == task_id)
        row.status = "pending" if allowed else "blocked"
        row.blocked_reason = None if allowed else reason_code
        row.blocked_detail = kwargs.get("reason_detail") or {}
        row.blocked_until = kwargs.get("recheck_at")
        return True

    def claim_many(self, *, worker_id: str, limit: int, job_names=(), **_kwargs):
        rows = [
            row
            for row in self.rows
            if row.status == "pending" and row.job_name in set(job_names)
        ][:limit]
        claims = []
        for row in rows:
            row.status = "running"
            row.attempts += 1
            row.lease_owner = worker_id
            row.lease_token = f"lease:{row.task_id}:{row.attempts}"
            row.lease_expires_at = NOW + timedelta(minutes=10)
            claims.append(TaskClaim.from_orm(row))
        return claims

    def claim(self, **_kwargs):
        return None

    def recover_expired(self, **_kwargs) -> int:
        recovered = 0
        for row in self.rows:
            if row.status == "running" and row.lease_expires_at <= NOW:
                row.status = "pending"
                row.lease_token = None
                row.lease_owner = None
                row.lease_expires_at = None
                recovered += 1
        self.recovered += recovered
        return recovered

    def complete(self, *, task_id: str, **_kwargs) -> bool:
        row = next(row for row in self.rows if row.task_id == task_id)
        row.status = "completed"
        self.completed_ids.append(task_id)
        return True

    def fail(self, *, task_id: str, **_kwargs) -> bool:
        row = next(row for row in self.rows if row.task_id == task_id)
        row.status = "failed"
        return True

    def recovery_blocked_domains(self):
        return {}


def _scope(queue: _PersistentQueue):
    @contextmanager
    def queue_scope() -> Iterator[_PersistentQueue]:
        yield queue

    return queue_scope


def _manual_job(name: str = "ashare.news_articles") -> BaseDataSchedulerJob:
    return BaseDataSchedulerJob(
        name=name,
        group="ashare-p1",
        interval_seconds=0,
        schedule_type="manual",
        params={"name": name},
    )


def _scheduler(queue: _PersistentQueue, jobs, started: list[str]) -> BaseDataScheduler:
    return BaseDataScheduler(
        BaseDataSchedulerConfig(
            cache_backend="null",
            loop_idle_seconds=0.01,
            job_timeout_seconds=0,
            max_concurrent_jobs=4,
            jobs=tuple(jobs),
        ),
        collect_base_data_func=lambda args: started.append(args.name) or {"status": "ok"},
        default_collection_args_func=lambda **kwargs: SimpleNamespace(**kwargs),
        persistent_task_queue_scope=_scope(queue),
        sleep_func=lambda _seconds: None,
    )


def test_loop_executes_orphan_pending_task_without_local_due_state() -> None:
    queue = _PersistentQueue()
    queue.add(
        job_name="ashare.news_articles",
        status="pending",
        task_id="task:orphan",
        payload={"params": {"name": "ashare.news_articles"}},
    )
    started: list[str] = []

    result = _scheduler(queue, [_manual_job()], started).run_loop(max_cycles=1)

    assert result["cycles"] == 1
    assert started == ["ashare.news_articles"]
    assert queue.completed_ids == ["task:orphan"]


def test_loop_recovers_expired_task_before_claiming() -> None:
    queue = _PersistentQueue()
    queue.add(
        job_name="ashare.news_articles",
        status="running",
        task_id="task:expired",
        payload={"params": {"name": "ashare.news_articles"}},
    )
    started: list[str] = []

    _scheduler(queue, [_manual_job()], started).run_loop(max_cycles=1)

    assert queue.recovered == 1
    assert queue.completed_ids == ["task:expired"]


def test_after_success_generation_is_not_reexecuted_after_restart() -> None:
    queue = _PersistentQueue()
    queue.add(job_name="source", status="completed", task_id="task:source")
    dependent = BaseDataSchedulerJob(
        name="consumer",
        group="analytics",
        interval_seconds=0,
        schedule_type="after_success",
        depends_on=("source",),
        params={"name": "consumer"},
    )
    started: list[str] = []

    _scheduler(queue, [dependent], started).run_loop(max_cycles=1)
    _scheduler(queue, [dependent], started).run_loop(max_cycles=1)

    assert started == ["consumer"]
    consumers = [row for row in queue.rows if row.job_name == "consumer"]
    assert len(consumers) == 1
    assert consumers[0].dependency_generation == ["task:source"]


def test_persistent_loop_refills_completed_slots_before_long_task_finishes() -> None:
    """长任务占用一个槽位时，短任务完成后应立即领取后续待执行任务。"""

    queue = _PersistentQueue()
    queue.add(
        job_name="long",
        status="pending",
        task_id="task:long",
        payload={"params": {"name": "long"}},
    )
    for index in range(4):
        name = f"short-{index}"
        queue.add(
            job_name=name,
            status="pending",
            task_id=f"task:{name}",
            payload={"params": {"name": name}},
        )

    started_at: dict[str, float] = {}
    finished_at: dict[str, float] = {}

    def collect(args: Any) -> dict[str, str]:
        name = str(args.name)
        started_at[name] = time.monotonic()
        if name == "long":
            time.sleep(0.35)
        else:
            time.sleep(0.01)
        finished_at[name] = time.monotonic()
        return {"status": "ok"}

    jobs = [
        BaseDataSchedulerJob(
            name="long",
            group="collection",
            interval_seconds=0,
            schedule_type="manual",
            params={"name": "long"},
            resource_pool="default",
            priority=100,
        ),
        *[
            BaseDataSchedulerJob(
                name=f"short-{index}",
                group="analytics",
                interval_seconds=0,
                schedule_type="manual",
                params={"name": f"short-{index}"},
                resource_pool="analytics",
                priority=10,
            )
            for index in range(4)
        ],
    ]
    scheduler = BaseDataScheduler(
        BaseDataSchedulerConfig(
            cache_backend="null",
            loop_idle_seconds=0.01,
            job_timeout_seconds=0,
            max_concurrent_jobs=4,
            jobs=tuple(jobs),
        ),
        collect_base_data_func=collect,
        default_collection_args_func=lambda **kwargs: SimpleNamespace(**kwargs),
        persistent_task_queue_scope=_scope(queue),
        sleep_func=lambda _seconds: None,
    )

    scheduler.run_loop(max_cycles=2)

    assert set(started_at) == {"long", "short-0", "short-1", "short-2", "short-3"}
    assert started_at["short-3"] < finished_at["long"]


def test_admission_skips_unchanged_blocked_task() -> None:
    """阻塞原因未变化时，准入扫描不得重复写数据库。"""

    queue = _PersistentQueue()
    running = queue.add(job_name="running", status="running", task_id="task:running")
    running.resource_pool = "analytics"
    blocked = queue.add(job_name="blocked", status="blocked", task_id="task:blocked")
    blocked.resource_pool = "analytics"
    blocked.blocked_reason = "resource_pool_full"
    blocked.blocked_detail = {"resource_pool": "analytics", "running": 1, "limit": 1}
    job = BaseDataSchedulerJob(
        name="blocked",
        group="analytics",
        interval_seconds=0,
        schedule_type="manual",
        resource_pool="analytics",
    )
    scheduler = BaseDataScheduler(
        BaseDataSchedulerConfig(
            cache_backend="null",
            max_concurrent_jobs=4,
            resource_pools={"analytics": {"max_concurrent_jobs": 1}},
            jobs=(job,),
        ),
        collect_base_data_func=lambda _args: {"status": "ok"},
        default_collection_args_func=lambda **kwargs: SimpleNamespace(**kwargs),
        persistent_task_queue_scope=_scope(queue),
    )

    result = scheduler._admit_persistent_tasks(now=NOW)

    assert result == {"admitted": 0, "blocked": 1}
    assert queue.admission_calls == []
