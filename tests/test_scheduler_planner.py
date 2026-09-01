"""持久化调度 Planner 行为测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from finance_agent.scheduler.base_data_scheduler import (
    BaseDataSchedulerConfig,
    BaseDataSchedulerJob,
)
from finance_agent.scheduler.planner import SchedulerPlanner


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


NOW = _dt("2026-08-31T09:35:42+08:00")


class _PlannerRepository:
    """测试用持久 adapter；按幂等键模拟数据库唯一约束。"""

    def __init__(self) -> None:
        self.rows: list[SimpleNamespace] = []

    def add(
        self,
        *,
        job_name: str,
        status: str,
        scheduled_for: datetime,
        task_id: str | None = None,
        dependency_generation: tuple[str, ...] = (),
        coalesced_count: int = 0,
        mutex_key: str | None = None,
    ) -> SimpleNamespace:
        row = SimpleNamespace(
            task_id=task_id or f"task:{len(self.rows) + 1}",
            job_name=job_name,
            status=status,
            scheduled_for=scheduled_for,
            idempotency_key=f"seed:{len(self.rows) + 1}",
            dependency_generation=list(dependency_generation),
            coalesced_count=coalesced_count,
            payload={},
            created_at=scheduled_for,
            mutex_key=mutex_key,
        )
        self.rows.append(row)
        return row

    def list_tasks(
        self,
        *,
        job_name: str | None = None,
        statuses: tuple[str, ...],
        limit: int = 200,
        **_kwargs: Any,
    ) -> list[SimpleNamespace]:
        rows = [row for row in self.rows if row.status in statuses]
        if job_name is not None:
            rows = [row for row in rows if row.job_name == job_name]
        return sorted(rows, key=lambda row: row.created_at, reverse=True)[:limit]

    def schedule(self, **kwargs: Any) -> SimpleNamespace:
        for row in self.rows:
            if row.idempotency_key == kwargs["idempotency_key"]:
                return row
        row = self.add(
            job_name=kwargs["job_name"],
            status="scheduled",
            scheduled_for=kwargs["scheduled_for"],
            task_id=f"task:planned:{len(self.rows) + 1}",
            dependency_generation=tuple(kwargs.get("dependency_generation") or ()),
            coalesced_count=int(kwargs.get("coalesced_count") or 0),
            mutex_key=kwargs.get("mutex_key"),
        )
        row.idempotency_key = kwargs["idempotency_key"]
        row.payload = dict(kwargs.get("payload") or {})
        return row

    def coalesce_task(
        self,
        *,
        task_id: str,
        scheduled_for: datetime,
        coalesced_count_delta: int,
        payload: dict[str, Any],
        **_kwargs: Any,
    ) -> bool:
        row = next(row for row in self.rows if row.task_id == task_id)
        row.scheduled_for = scheduled_for
        row.coalesced_count += coalesced_count_delta
        row.payload = dict(payload)
        return True


def _config(*jobs: BaseDataSchedulerJob) -> BaseDataSchedulerConfig:
    return BaseDataSchedulerConfig(jobs=jobs)


def _interval_job(**overrides: Any) -> BaseDataSchedulerJob:
    job = BaseDataSchedulerJob(
        name="ashare.realtime_quotes",
        group="ashare-p0",
        interval_seconds=60,
        schedule_type="interval",
        resource_pool="realtime",
        params={"requires_data_domains": ["realtime_quotes"]},
    )
    return replace(job, **overrides)


def test_fixed_interval_anchors_to_planned_time() -> None:
    repo = _PlannerRepository()
    planner = SchedulerPlanner(repo, config_digest="digest-1")

    summary = planner.reconcile(now=NOW, config=_config(_interval_job()))

    assert summary.created == 1
    assert repo.rows[0].scheduled_for == _dt("2026-08-31T09:35:00+08:00")
    assert repo.rows[0].payload["scheduled_for"] == "2026-08-31T01:35:00+00:00"


def test_fixed_interval_coalesces_missed_ticks_into_one_pending_run() -> None:
    repo = _PlannerRepository()
    pending = repo.add(
        job_name="ashare.realtime_quotes",
        status="pending",
        scheduled_for=_dt("2026-08-31T09:30:00+08:00"),
    )

    summary = SchedulerPlanner(repo).reconcile(
        now=NOW,
        config=_config(_interval_job()),
    )

    assert summary.created == 0
    assert summary.coalesced == 5
    assert pending.scheduled_for == _dt("2026-08-31T09:35:00+08:00")
    assert pending.coalesced_count == 5
    assert len(repo.rows) == 1


def test_running_tick_keeps_only_one_new_pending_tick() -> None:
    repo = _PlannerRepository()
    repo.add(
        job_name="ashare.realtime_quotes",
        status="running",
        scheduled_for=_dt("2026-08-31T09:30:00+08:00"),
    )
    planner = SchedulerPlanner(repo)

    first = planner.reconcile(now=NOW, config=_config(_interval_job()))
    second = planner.reconcile(now=NOW + timedelta(seconds=20), config=_config(_interval_job()))

    assert first.created == 1
    assert second.created == 0
    assert len([row for row in repo.rows if row.status != "running"]) == 1


def test_planned_task_uses_stable_job_mutex_by_default() -> None:
    repo = _PlannerRepository()

    SchedulerPlanner(repo).reconcile(now=NOW, config=_config(_interval_job()))

    assert repo.rows[0].mutex_key == "scheduler.job:ashare.realtime_quotes"


def test_planned_task_preserves_explicit_mutex() -> None:
    repo = _PlannerRepository()

    SchedulerPlanner(repo).reconcile(
        now=NOW,
        config=_config(_interval_job(mutex_key="ashare.realtime_quotes.explicit")),
    )

    assert repo.rows[0].mutex_key == "ashare.realtime_quotes.explicit"


def _dependency_job(mode: str = "all_of") -> BaseDataSchedulerJob:
    return BaseDataSchedulerJob(
        name="consumer",
        group="analytics",
        interval_seconds=0,
        schedule_type="after_success",
        depends_on=("source-a", "source-b"),
        dependency_mode=mode,
    )


def test_after_success_generation_survives_new_planner_instance() -> None:
    repo = _PlannerRepository()
    repo.add(job_name="source-a", status="completed", scheduled_for=NOW, task_id="a:1")
    repo.add(job_name="source-b", status="completed", scheduled_for=NOW, task_id="b:1")
    config = _config(_dependency_job())

    first = SchedulerPlanner(repo).reconcile(now=NOW, config=config)
    second = SchedulerPlanner(repo).reconcile(now=NOW, config=config)

    assert first.dependency_created == 1
    assert second.dependency_created == 0
    consumers = [row for row in repo.rows if row.job_name == "consumer"]
    assert len(consumers) == 1
    assert consumers[0].dependency_generation == ["a:1", "b:1"]


@pytest.mark.parametrize("active_status", ["scheduled", "blocked", "pending", "running"])
def test_after_success_waits_while_same_job_has_active_task(active_status: str) -> None:
    repo = _PlannerRepository()
    repo.add(job_name="source-a", status="completed", scheduled_for=NOW, task_id="a:1")
    repo.add(job_name="source-b", status="completed", scheduled_for=NOW, task_id="b:1")
    repo.add(
        job_name="consumer",
        status=active_status,
        scheduled_for=NOW,
        task_id="consumer:active",
        dependency_generation=("a:1", "b:1"),
    )
    repo.add(
        job_name="source-a",
        status="completed",
        scheduled_for=NOW + timedelta(minutes=1),
        task_id="a:2",
    )
    repo.add(
        job_name="source-b",
        status="completed",
        scheduled_for=NOW + timedelta(minutes=1),
        task_id="b:2",
    )

    summary = SchedulerPlanner(repo).reconcile(
        now=NOW + timedelta(minutes=1),
        config=_config(_dependency_job()),
    )

    assert summary.dependency_created == 0
    assert [row.task_id for row in repo.rows if row.job_name == "consumer"] == ["consumer:active"]


def test_all_of_waits_until_each_dependency_has_an_unconsumed_generation() -> None:
    repo = _PlannerRepository()
    repo.add(job_name="source-a", status="completed", scheduled_for=NOW, task_id="a:1")
    repo.add(job_name="source-b", status="completed", scheduled_for=NOW, task_id="b:1")
    planner = SchedulerPlanner(repo)
    config = _config(_dependency_job())
    planner.reconcile(now=NOW, config=config)

    repo.add(
        job_name="source-a",
        status="completed",
        scheduled_for=NOW + timedelta(minutes=1),
        task_id="a:2",
    )
    assert planner.reconcile(now=NOW + timedelta(minutes=1), config=config).dependency_created == 0

    repo.add(
        job_name="source-b",
        status="completed",
        scheduled_for=NOW + timedelta(minutes=2),
        task_id="b:2",
    )
    next(row for row in repo.rows if row.job_name == "consumer").status = "completed"
    assert planner.reconcile(now=NOW + timedelta(minutes=2), config=config).dependency_created == 1


def test_any_of_consumes_each_new_generation_once() -> None:
    repo = _PlannerRepository()
    repo.add(job_name="source-a", status="completed", scheduled_for=NOW, task_id="a:1")
    config = _config(_dependency_job("any_of"))

    first = SchedulerPlanner(repo).reconcile(now=NOW, config=config)
    second = SchedulerPlanner(repo).reconcile(now=NOW, config=config)

    assert first.dependency_created == 1
    assert second.dependency_created == 0


def test_barrier_requires_same_scheduled_window() -> None:
    repo = _PlannerRepository()
    repo.add(job_name="source-a", status="completed", scheduled_for=NOW, task_id="a:1")
    repo.add(
        job_name="source-b",
        status="completed",
        scheduled_for=NOW + timedelta(minutes=1),
        task_id="b:1",
    )
    config = _config(_dependency_job("barrier"))

    assert SchedulerPlanner(repo).reconcile(now=NOW, config=config).dependency_created == 0

    repo.add(job_name="source-b", status="completed", scheduled_for=NOW, task_id="b:2")
    assert SchedulerPlanner(repo).reconcile(now=NOW, config=config).dependency_created == 1
