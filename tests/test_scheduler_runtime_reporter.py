from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy.dialects import postgresql

from finance_agent.scheduler.runtime_reporter import (
    SchedulerRuntimeReporter,
    SqlAlchemySchedulerRuntimeTaskSource,
)

NOW = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)


def _task(
    name: str,
    status: str,
    *,
    task_id: str | None = None,
    resource_pool: str = "default",
    scheduled_for: datetime | None = None,
    started_at: datetime | None = None,
    blocked_reason: str | None = None,
    blocked_detail: dict[str, Any] | None = None,
    config_digest: str | None = "scheduler-digest",
    coalesced_count: int = 0,
    priority: int = 100,
) -> SimpleNamespace:
    return SimpleNamespace(
        task_id=task_id or f"task:{name}",
        job_name=name,
        status=status,
        payload={},
        schedule_type="fixed_interval",
        scheduled_for=scheduled_for,
        priority=priority,
        resource_pool=resource_pool,
        mutex_key=None,
        dependency_generation=[],
        required_data_domains=[],
        blocked_reason=blocked_reason,
        blocked_detail=blocked_detail or {},
        blocked_until=None,
        config_digest=config_digest,
        coalesced_count=coalesced_count,
        cancel_requested_at=None,
        attempts=1,
        max_attempts=3,
        lease_owner="worker-1" if status == "running" else None,
        lease_token="secret-token" if status == "running" else None,
        lease_expires_at=NOW + timedelta(seconds=60) if status == "running" else None,
        next_retry_at=None,
        error_message="boom" if status == "failed" else None,
        started_at=started_at,
        finished_at=NOW if status in {"completed", "failed", "cancelled"} else None,
        created_at=NOW - timedelta(minutes=5),
        updated_at=NOW,
    )


class _TaskSource:
    def __init__(
        self,
        tasks: list[SimpleNamespace],
        *,
        status_counts: dict[str, int] | None = None,
        terminal_limit: int = 120,
    ) -> None:
        self.tasks = tasks
        self._status_counts = status_counts
        self.terminal_limit = terminal_limit

    def status_counts(self) -> dict[str, int]:
        if self._status_counts is not None:
            return dict(self._status_counts)
        return {
            status: sum(task.status == status for task in self.tasks)
            for status in {
                "scheduled",
                "blocked",
                "pending",
                "running",
                "completed",
                "failed",
                "cancelled",
            }
        }

    def active_tasks(self) -> list[SimpleNamespace]:
        return [
            task
            for task in self.tasks
            if task.status in {"scheduled", "blocked", "pending", "running"}
        ]

    def recent_terminal_tasks(self) -> list[SimpleNamespace]:
        return [
            task
            for task in self.tasks
            if task.status in {"completed", "failed", "cancelled"}
        ]

    def latest_config_digest(self) -> str | None:
        return "scheduler-digest"


def test_waiting_contains_only_persistent_pending_and_preserves_seven_statuses() -> None:
    """waiting 必须只来自真实 pending，不能由配置或 Redis 推导。"""

    tasks = [
        _task("scheduled.job", "scheduled"),
        _task(
            "blocked.job",
            "blocked",
            blocked_reason="recovery_data_domain",
            blocked_detail={"domains": ["ashare_quotes"]},
        ),
        _task("pending.job", "pending", resource_pool="network_io"),
        _task(
            "running.job",
            "running",
            resource_pool="network_io",
            scheduled_for=NOW - timedelta(seconds=45),
            started_at=NOW - timedelta(seconds=15),
            coalesced_count=2,
        ),
        _task("completed.job", "completed"),
        _task("failed.job", "failed"),
        _task("cancelled.job", "cancelled", priority=0),
    ]
    reporter = SchedulerRuntimeReporter(_TaskSource(tasks))

    snapshot = reporter.snapshot(
        now=NOW,
        redis_progress={
            "status": "ok",
            "data": {
                "tasks": [
                    {
                        "job_name": "running.job",
                        "status": "running",
                        "lease_token": "top-secret",
                        "summary": {
                            "progress_ratio": 0.4,
                            "lease_token": "nested-secret",
                            "items": [{"lease_token": "list-secret"}],
                        },
                    },
                    {"job_name": "completed.job", "status": "running", "summary": {"progress_ratio": 0.8}},
                ]
            },
        },
        api_config_digest="api-digest",
        resource_pool_limits={"default": 2, "network_io": 3},
        max_concurrent_jobs=4,
    )

    assert [task["job_name"] for task in snapshot.waiting_jobs] == ["pending.job"]
    assert snapshot.status_counts == {
        "scheduled": 1,
        "blocked": 1,
        "pending": 1,
        "running": 1,
        "completed": 1,
        "failed": 1,
        "cancelled": 1,
    }
    by_name = {task["job_name"]: task for task in snapshot.tasks}
    assert by_name["blocked.job"]["blocked_detail"] == {"domains": ["ashare_quotes"]}
    assert by_name["running.job"]["progress"]["summary"]["progress_ratio"] == 0.4
    assert "lease_token" not in repr(by_name["running.job"]["progress"])
    assert "progress" not in by_name["completed.job"]
    assert "lease_token" not in by_name["running.job"]
    assert by_name["cancelled.job"]["priority"] == 0
    assert snapshot.resource_pools["network_io"] == {
        "limit": 3,
        "running": 1,
        "pending": 1,
        "blocked": 0,
        "available": 2,
    }
    assert snapshot.metrics["max_start_latency_seconds"] == 30.0
    assert snapshot.metrics["coalesced_count"] == 2
    assert snapshot.scheduler_config_digest == "scheduler-digest"
    assert snapshot.api_config_digest == "api-digest"
    assert snapshot.config_drift is True


def test_snapshot_distinguishes_cumulative_counts_from_listed_rows() -> None:
    """历史累计数与有限任务列表必须分别报告，避免筛选计数误导。"""

    source = _TaskSource(
        [_task("completed.latest", "completed")],
        status_counts={
            "scheduled": 0,
            "blocked": 0,
            "pending": 0,
            "running": 0,
            "completed": 8,
            "failed": 2,
            "cancelled": 1,
        },
        terminal_limit=1,
    )

    payload = SchedulerRuntimeReporter(source).snapshot(now=NOW).to_dict()

    assert payload["status_counts"]["completed"] == 8
    assert payload["listed_status_counts"]["completed"] == 1
    assert payload["listed_status_counts"]["failed"] == 0
    assert payload["task_list"] == {
        "listed_count": 1,
        "active_count": 0,
        "terminal_count": 1,
        "terminal_total_count": 11,
        "terminal_limit": 1,
        "truncated": True,
    }


def test_latest_config_digest_uses_latest_created_task_even_when_digest_is_null() -> None:
    """最新任务摘要为空时必须返回 unknown，不能回退到旧任务摘要。"""

    class _ScalarSession:
        statement: Any | None = None

        def scalar(self, statement: Any) -> None:
            self.statement = statement
            return None

    session = _ScalarSession()

    assert SqlAlchemySchedulerRuntimeTaskSource(session).latest_config_digest() is None  # type: ignore[arg-type]
    sql = str(session.statement.compile(dialect=postgresql.dialect()))
    assert "config_digest IS NOT NULL" not in sql
    assert "ORDER BY scheduler_task_runs.created_at DESC" in sql
    assert "scheduler_task_runs.task_id DESC" in sql


def test_matching_or_unknown_digest_does_not_raise_false_drift() -> None:
    """缺少任一摘要时应标记 unknown，而不是误报配置漂移。"""

    source = _TaskSource([_task("pending.job", "pending", config_digest=None)])
    source.latest_config_digest = lambda: None  # type: ignore[method-assign]
    snapshot = SchedulerRuntimeReporter(source).snapshot(
        now=NOW,
        api_config_digest="api-digest",
    )

    assert snapshot.config_drift is False
    assert snapshot.config_drift_status == "unknown"
