"""基于 PostgreSQL 持久任务表生成统一调度运行快照。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from math import ceil
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finance_agent.storage.orm import SchedulerTaskRunORM

JsonDict = dict[str, Any]

SCHEDULER_TASK_STATUSES = (
    "scheduled",
    "blocked",
    "pending",
    "running",
    "completed",
    "failed",
    "cancelled",
)
ACTIVE_TASK_STATUSES = SCHEDULER_TASK_STATUSES[:4]
TERMINAL_TASK_STATUSES = SCHEDULER_TASK_STATUSES[4:]


class SchedulerRuntimeTaskSource(Protocol):
    """Reporter 所需的最小持久任务读取接口。"""

    def status_counts(self) -> dict[str, int]: ...

    def active_tasks(self) -> list[SchedulerTaskRunORM]: ...

    def recent_terminal_tasks(self) -> list[SchedulerTaskRunORM]: ...

    def latest_config_digest(self) -> str | None: ...


class SqlAlchemySchedulerRuntimeTaskSource:
    """使用 SQLAlchemy Session 读取调度运行快照。"""

    def __init__(self, session: Session, *, terminal_limit: int = 120) -> None:
        self.session = session
        self.terminal_limit = max(1, min(int(terminal_limit), 500))

    def status_counts(self) -> dict[str, int]:
        statement = (
            select(SchedulerTaskRunORM.status, func.count())
            .group_by(SchedulerTaskRunORM.status)
            .order_by(SchedulerTaskRunORM.status)
        )
        return {str(status): int(count) for status, count in self.session.execute(statement).all()}

    def active_tasks(self) -> list[SchedulerTaskRunORM]:
        statement = (
            select(SchedulerTaskRunORM)
            .where(SchedulerTaskRunORM.status.in_(ACTIVE_TASK_STATUSES))
            .order_by(
                SchedulerTaskRunORM.priority.desc(),
                SchedulerTaskRunORM.scheduled_for.asc().nulls_last(),
                SchedulerTaskRunORM.created_at.asc(),
            )
        )
        return list(self.session.scalars(statement))

    def recent_terminal_tasks(self) -> list[SchedulerTaskRunORM]:
        statement = (
            select(SchedulerTaskRunORM)
            .where(SchedulerTaskRunORM.status.in_(TERMINAL_TASK_STATUSES))
            .order_by(SchedulerTaskRunORM.updated_at.desc())
            .limit(self.terminal_limit)
        )
        return list(self.session.scalars(statement))

    def latest_config_digest(self) -> str | None:
        statement = (
            select(SchedulerTaskRunORM.config_digest)
            .order_by(
                SchedulerTaskRunORM.created_at.desc(),
                SchedulerTaskRunORM.task_id.desc(),
            )
            .limit(1)
        )
        value = self.session.scalar(statement)
        return str(value) if value else None


@dataclass(frozen=True)
class SchedulerRuntimeSnapshot:
    """API 和 Web 共用的七状态快照。"""

    generated_at: datetime
    status_counts: dict[str, int]
    listed_status_counts: dict[str, int]
    task_list: JsonDict
    tasks: list[JsonDict]
    waiting_jobs: list[JsonDict]
    running_jobs: list[JsonDict]
    resource_pools: dict[str, JsonDict]
    metrics: JsonDict
    scheduler_config_digest: str | None
    api_config_digest: str | None
    config_drift: bool
    config_drift_status: str

    def to_dict(self) -> JsonDict:
        return {
            "source": "postgresql",
            "database_status": "available",
            "generated_at": self.generated_at.isoformat(),
            "status_counts": dict(self.status_counts),
            "listed_status_counts": dict(self.listed_status_counts),
            "task_list": dict(self.task_list),
            "tasks": list(self.tasks),
            "waiting": list(self.waiting_jobs),
            "waiting_jobs": list(self.waiting_jobs),
            "running_jobs": list(self.running_jobs),
            "resource_pools": dict(self.resource_pools),
            "metrics": dict(self.metrics),
            "scheduler_config_digest": self.scheduler_config_digest,
            "api_config_digest": self.api_config_digest,
            "config_drift": self.config_drift,
            "config_drift_status": self.config_drift_status,
        }


class SchedulerRuntimeReporter:
    """把数据库状态和 Redis 内部进度合成为单一运行视图。"""

    def __init__(self, task_source: SchedulerRuntimeTaskSource) -> None:
        self.task_source = task_source

    @classmethod
    def from_session(
        cls,
        session: Session,
        *,
        terminal_limit: int = 120,
    ) -> SchedulerRuntimeReporter:
        return cls(
            SqlAlchemySchedulerRuntimeTaskSource(
                session,
                terminal_limit=terminal_limit,
            )
        )

    def snapshot(
        self,
        *,
        now: datetime | None = None,
        redis_progress: JsonDict | None = None,
        api_config_digest: str | None = None,
        resource_pool_limits: dict[str, int] | None = None,
        max_concurrent_jobs: int = 4,
    ) -> SchedulerRuntimeSnapshot:
        generated_at = _as_utc(now or datetime.now(tz=UTC))
        raw_counts = self.task_source.status_counts()
        status_counts = {
            status: max(0, int(raw_counts.get(status, 0) or 0))
            for status in SCHEDULER_TASK_STATUSES
        }
        rows = _deduplicate_tasks(
            [
                *self.task_source.active_tasks(),
                *self.task_source.recent_terminal_tasks(),
            ]
        )
        redis_by_job = _running_redis_progress_by_job(redis_progress)
        tasks = [
            _serialize_task(
                row,
                now=generated_at,
                redis_progress=redis_by_job.get(str(row.job_name))
                if str(row.status) == "running"
                else None,
            )
            for row in rows
        ]
        listed_status_counts = {
            status: sum(task["status"] == status for task in tasks)
            for status in SCHEDULER_TASK_STATUSES
        }
        terminal_count = sum(
            listed_status_counts[status] for status in TERMINAL_TASK_STATUSES
        )
        terminal_total_count = sum(status_counts[status] for status in TERMINAL_TASK_STATUSES)
        terminal_limit = getattr(self.task_source, "terminal_limit", None)
        task_list = {
            "listed_count": len(tasks),
            "active_count": sum(
                listed_status_counts[status] for status in ACTIVE_TASK_STATUSES
            ),
            "terminal_count": terminal_count,
            "terminal_total_count": terminal_total_count,
            "terminal_limit": int(terminal_limit) if terminal_limit is not None else None,
            "truncated": terminal_count < terminal_total_count,
        }
        waiting_jobs = [task for task in tasks if task["status"] == "pending"]
        running_jobs = [task for task in tasks if task["status"] == "running"]
        limits = {
            str(name): max(1, int(limit))
            for name, limit in (resource_pool_limits or {}).items()
        }
        resource_pools = _build_resource_pool_state(
            tasks,
            limits=limits,
            max_concurrent_jobs=max_concurrent_jobs,
        )
        scheduler_config_digest = self.task_source.latest_config_digest()
        config_drift_status = _config_drift_status(
            scheduler_config_digest=scheduler_config_digest,
            api_config_digest=api_config_digest,
        )
        return SchedulerRuntimeSnapshot(
            generated_at=generated_at,
            status_counts=status_counts,
            listed_status_counts=listed_status_counts,
            task_list=task_list,
            tasks=tasks,
            waiting_jobs=waiting_jobs,
            running_jobs=running_jobs,
            resource_pools=resource_pools,
            metrics=_build_metrics(tasks, now=generated_at, max_concurrent_jobs=max_concurrent_jobs),
            scheduler_config_digest=scheduler_config_digest,
            api_config_digest=api_config_digest,
            config_drift=config_drift_status == "drift",
            config_drift_status=config_drift_status,
        )


def _deduplicate_tasks(tasks: list[SchedulerTaskRunORM]) -> list[SchedulerTaskRunORM]:
    seen: set[str] = set()
    result: list[SchedulerTaskRunORM] = []
    for task in tasks:
        task_id = str(task.task_id)
        if task_id in seen:
            continue
        seen.add(task_id)
        result.append(task)
    return result


def _serialize_task(
    task: SchedulerTaskRunORM,
    *,
    now: datetime,
    redis_progress: JsonDict | None,
) -> JsonDict:
    scheduled_for = _optional_datetime(getattr(task, "scheduled_for", None))
    started_at = _optional_datetime(getattr(task, "started_at", None))
    lease_expires_at = _optional_datetime(getattr(task, "lease_expires_at", None))
    start_latency = None
    if scheduled_for is not None and started_at is not None:
        start_latency = max(0.0, round((started_at - scheduled_for).total_seconds(), 3))
    status = str(task.status)
    result: JsonDict = {
        "task_id": str(task.task_id),
        "job_name": str(task.job_name),
        "status": status,
        "schedule_type": str(getattr(task, "schedule_type", "manual") or "manual"),
        "scheduled_for": _isoformat(scheduled_for),
        "priority": _int_or_default(getattr(task, "priority", 100), default=100),
        "resource_pool": str(getattr(task, "resource_pool", "default") or "default"),
        "mutex_key": getattr(task, "mutex_key", None),
        "dependency_generation": list(getattr(task, "dependency_generation", None) or []),
        "required_data_domains": list(getattr(task, "required_data_domains", None) or []),
        "blocked_reason": getattr(task, "blocked_reason", None),
        "blocked_detail": dict(getattr(task, "blocked_detail", None) or {}),
        "blocked_until": _isoformat(_optional_datetime(getattr(task, "blocked_until", None))),
        "config_digest": getattr(task, "config_digest", None),
        "coalesced_count": int(getattr(task, "coalesced_count", 0) or 0),
        "cancel_requested_at": _isoformat(
            _optional_datetime(getattr(task, "cancel_requested_at", None))
        ),
        "attempts": int(getattr(task, "attempts", 0) or 0),
        "max_attempts": int(getattr(task, "max_attempts", 0) or 0),
        "lease_owner": getattr(task, "lease_owner", None),
        "lease_expires_at": _isoformat(lease_expires_at),
        "lease_expired": bool(status == "running" and lease_expires_at and lease_expires_at <= now),
        "next_retry_at": _isoformat(_optional_datetime(getattr(task, "next_retry_at", None))),
        "error_message": getattr(task, "error_message", None),
        "started_at": _isoformat(started_at),
        "finished_at": _isoformat(_optional_datetime(getattr(task, "finished_at", None))),
        "created_at": _isoformat(_optional_datetime(getattr(task, "created_at", None))),
        "updated_at": _isoformat(_optional_datetime(getattr(task, "updated_at", None))),
        "start_latency_seconds": start_latency,
    }
    if redis_progress is not None:
        result["progress"] = _remove_lease_tokens(redis_progress)
    return result


def _running_redis_progress_by_job(response: JsonDict | None) -> dict[str, JsonDict]:
    data = response.get("data") if isinstance(response, dict) else None
    raw_tasks = data.get("tasks") if isinstance(data, dict) else None
    result: dict[str, JsonDict] = {}
    if not isinstance(raw_tasks, list):
        return result
    for item in raw_tasks:
        if not isinstance(item, dict) or str(item.get("status") or "") != "running":
            continue
        job_name = str(item.get("job_name") or "").strip()
        if job_name:
            result[job_name] = dict(item)
    return result


def _remove_lease_tokens(value: Any) -> Any:
    """递归移除 Redis 内部租约，避免经运行快照泄露。"""

    if isinstance(value, dict):
        return {
            str(key): _remove_lease_tokens(item)
            for key, item in value.items()
            if str(key) != "lease_token"
        }
    if isinstance(value, list):
        return [_remove_lease_tokens(item) for item in value]
    if isinstance(value, tuple):
        return [_remove_lease_tokens(item) for item in value]
    return value


def _int_or_default(value: Any, *, default: int) -> int:
    """保留合法的零值，仅在缺失或非法时使用默认值。"""

    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _build_resource_pool_state(
    tasks: list[JsonDict],
    *,
    limits: dict[str, int],
    max_concurrent_jobs: int,
) -> dict[str, JsonDict]:
    pool_names = set(limits)
    pool_names.update(str(task.get("resource_pool") or "default") for task in tasks)
    if not pool_names:
        pool_names.add("default")
    result: dict[str, JsonDict] = {}
    for pool_name in sorted(pool_names):
        limit = limits.get(pool_name, max(1, int(max_concurrent_jobs)))
        running = sum(
            task["status"] == "running" and task["resource_pool"] == pool_name
            for task in tasks
        )
        pending = sum(
            task["status"] == "pending" and task["resource_pool"] == pool_name
            for task in tasks
        )
        blocked = sum(
            task["status"] == "blocked" and task["resource_pool"] == pool_name
            for task in tasks
        )
        result[pool_name] = {
            "limit": limit,
            "running": running,
            "pending": pending,
            "blocked": blocked,
            "available": max(0, limit - running),
        }
    return result


def _build_metrics(
    tasks: list[JsonDict],
    *,
    now: datetime,
    max_concurrent_jobs: int,
) -> JsonDict:
    latencies = sorted(
        float(task["start_latency_seconds"])
        for task in tasks
        if task.get("start_latency_seconds") is not None
    )
    p95_index = max(0, ceil(len(latencies) * 0.95) - 1) if latencies else 0
    running = [task for task in tasks if task["status"] == "running"]
    return {
        "max_concurrent_jobs": max(1, int(max_concurrent_jobs)),
        "running_count": len(running),
        "waiting_count": sum(task["status"] == "pending" for task in tasks),
        "blocked_count": sum(task["status"] == "blocked" for task in tasks),
        "active_lease_count": sum(bool(task.get("lease_owner")) for task in running),
        "expired_lease_count": sum(bool(task.get("lease_expired")) for task in running),
        "coalesced_count": sum(int(task.get("coalesced_count") or 0) for task in tasks),
        "average_start_latency_seconds": (
            round(sum(latencies) / len(latencies), 3) if latencies else None
        ),
        "p95_start_latency_seconds": latencies[p95_index] if latencies else None,
        "max_start_latency_seconds": latencies[-1] if latencies else None,
        "oldest_pending_seconds": _oldest_pending_seconds(tasks, now=now),
    }


def _oldest_pending_seconds(tasks: list[JsonDict], *, now: datetime) -> float | None:
    due_times: list[datetime] = []
    for task in tasks:
        if task["status"] != "pending":
            continue
        parsed = _optional_datetime(task.get("scheduled_for"))
        if parsed is not None and parsed <= now:
            due_times.append(parsed)
    if not due_times:
        return None
    return max(0.0, round((now - min(due_times)).total_seconds(), 3))


def _config_drift_status(
    *,
    scheduler_config_digest: str | None,
    api_config_digest: str | None,
) -> str:
    if not scheduler_config_digest or not api_config_digest:
        return "unknown"
    return "match" if scheduler_config_digest == api_config_digest else "drift"


def _optional_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _as_utc(value)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
