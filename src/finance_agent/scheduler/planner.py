"""持久化调度计划器。

本模块只回答“哪些逻辑运行应存在”，不执行准入、领取或业务采集。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

TASK_STATES = (
    "scheduled",
    "blocked",
    "pending",
    "running",
    "completed",
    "failed",
    "cancelled",
)
QUEUED_STATES = ("scheduled", "blocked", "pending")


class PlannerRepository(Protocol):
    """Planner 所需的最小持久化 interface。"""

    def list_tasks(self, **kwargs: Any) -> list[Any]: ...

    def schedule(self, **kwargs: Any) -> Any: ...

    def coalesce_task(self, **kwargs: Any) -> bool: ...


@dataclass(frozen=True)
class PlanningSummary:
    """一次 reconcile 的持久化变更摘要。"""

    created: int = 0
    existing: int = 0
    coalesced: int = 0
    dependency_created: int = 0
    skipped: int = 0

    def plus(self, **changes: int) -> PlanningSummary:
        values = {
            "created": self.created,
            "existing": self.existing,
            "coalesced": self.coalesced,
            "dependency_created": self.dependency_created,
            "skipped": self.skipped,
        }
        for key, value in changes.items():
            values[key] += int(value)
        return PlanningSummary(**values)


class SchedulerPlanner:
    """把配置与完成代次幂等收敛为 PostgreSQL 逻辑运行。"""

    def __init__(
        self,
        repository: PlannerRepository,
        *,
        config_digest: str | None = None,
    ) -> None:
        self.repository = repository
        self.config_digest = str(config_digest).strip() if config_digest else None

    def reconcile(self, *, now: datetime, config: Any) -> PlanningSummary:
        """确保当前时刻所有应存在的逻辑运行都已持久化。"""

        normalized_now = _as_utc(now)
        summary = PlanningSummary()
        for job in config.jobs:
            if not job.enabled or job.schedule_type == "manual":
                summary = summary.plus(skipped=1)
                continue
            if job.schedule_type == "after_success":
                created = self._reconcile_dependency(job, now=normalized_now)
                summary = summary.plus(
                    created=created,
                    dependency_created=created,
                    existing=0 if created else 1,
                )
                continue
            scheduled_for = _latest_due_tick(job, now=normalized_now)
            if scheduled_for is None:
                summary = summary.plus(skipped=1)
                continue
            change = self._reconcile_time_job(job, scheduled_for=scheduled_for)
            summary = summary.plus(**change)
        return summary

    def _reconcile_time_job(
        self,
        job: Any,
        *,
        scheduled_for: datetime,
    ) -> dict[str, int]:
        active = self.repository.list_tasks(
            job_name=job.name,
            statuses=(*QUEUED_STATES, "running"),
            limit=200,
        )
        queued = [row for row in active if row.status in QUEUED_STATES]
        if queued:
            task = max(
                queued,
                key=lambda row: _as_utc(row.scheduled_for or datetime.min.replace(tzinfo=UTC)),
            )
            previous_tick = _as_utc(task.scheduled_for or scheduled_for)
            if scheduled_for <= previous_tick:
                return {"existing": 1}
            interval = max(1, int(job.interval_seconds or 1))
            missed_ticks = max(1, int((scheduled_for - previous_tick).total_seconds()) // interval)
            payload = _task_payload(job, scheduled_for=scheduled_for)
            self.repository.coalesce_task(
                task_id=task.task_id,
                scheduled_for=scheduled_for,
                coalesced_count_delta=missed_ticks,
                payload=payload,
                now=scheduled_for,
            )
            return {"coalesced": missed_ticks}

        idempotency_key = _time_idempotency_key(job.name, scheduled_for)
        existing = self.repository.list_tasks(
            job_name=job.name,
            statuses=TASK_STATES,
            limit=200,
        )
        if any(row.idempotency_key == idempotency_key for row in existing):
            return {"existing": 1}

        self.repository.schedule(
            job_name=job.name,
            idempotency_key=idempotency_key,
            schedule_type=_normalized_schedule_type(job.schedule_type),
            scheduled_for=scheduled_for,
            payload=_task_payload(job, scheduled_for=scheduled_for),
            priority=int(job.priority),
            resource_pool=str(job.resource_pool),
            mutex_key=job.mutex_key,
            required_data_domains=tuple(
                str(item)
                for item in job.params.get("requires_data_domains", ())
                if str(item).strip()
            ),
            config_digest=self.config_digest,
            max_attempts=max(1, int(job.max_retries or 0) + 1),
            now=scheduled_for,
        )
        return {"created": 1}

    def _reconcile_dependency(self, job: Any, *, now: datetime) -> int:
        downstream = self.repository.list_tasks(
            job_name=job.name,
            statuses=TASK_STATES,
            limit=1000,
        )
        consumed = {
            str(generation)
            for row in downstream
            for generation in (getattr(row, "dependency_generation", None) or ())
        }
        candidates: dict[str, list[Any]] = {}
        for dependency in job.depends_on:
            completed = self.repository.list_tasks(
                job_name=dependency,
                statuses=("completed",),
                limit=1000,
            )
            candidates[dependency] = [
                row for row in completed if str(row.task_id) not in consumed
            ]

        generations = _select_dependency_generations(
            job.dependency_mode,
            job.depends_on,
            candidates,
        )
        if not generations:
            return 0
        generation_ids = tuple(sorted(str(row.task_id) for row in generations))
        scheduled_for = max(
            (_as_utc(getattr(row, "scheduled_for", None) or now) for row in generations),
            default=now,
        )
        idempotency_key = _dependency_idempotency_key(
            job.name,
            job.dependency_mode,
            generation_ids,
        )
        if any(row.idempotency_key == idempotency_key for row in downstream):
            return 0
        self.repository.schedule(
            job_name=job.name,
            idempotency_key=idempotency_key,
            schedule_type="after_success",
            scheduled_for=scheduled_for,
            payload=_task_payload(job, scheduled_for=scheduled_for),
            priority=int(job.priority),
            resource_pool=str(job.resource_pool),
            mutex_key=job.mutex_key,
            dependency_generation=generation_ids,
            required_data_domains=tuple(
                str(item)
                for item in job.params.get("requires_data_domains", ())
                if str(item).strip()
            ),
            config_digest=self.config_digest,
            max_attempts=max(1, int(job.max_retries or 0) + 1),
            now=now,
        )
        return 1


def _select_dependency_generations(
    mode: str,
    dependencies: tuple[str, ...],
    candidates: dict[str, list[Any]],
) -> tuple[Any, ...]:
    if mode == "any_of":
        rows = [row for dependency in dependencies for row in candidates[dependency]]
        return tuple(rows)
    if mode == "barrier":
        by_dependency: dict[str, dict[str, Any]] = {}
        for dependency in dependencies:
            by_dependency[dependency] = {
                _as_utc(row.scheduled_for).isoformat(): row
                for row in candidates[dependency]
                if getattr(row, "scheduled_for", None) is not None
            }
        common_windows = set.intersection(
            *(set(rows) for rows in by_dependency.values())
        ) if by_dependency else set()
        if not common_windows:
            return ()
        window = max(common_windows)
        return tuple(by_dependency[dependency][window] for dependency in dependencies)
    if any(not candidates[dependency] for dependency in dependencies):
        return ()
    return tuple(
        max(
            candidates[dependency],
            key=lambda row: _as_utc(row.scheduled_for or row.created_at),
        )
        for dependency in dependencies
    )


def _latest_due_tick(job: Any, *, now: datetime) -> datetime | None:
    schedule_type = _normalized_schedule_type(job.schedule_type)
    if schedule_type == "fixed_interval":
        interval = int(job.interval_seconds or 0)
        if interval <= 0:
            return None
        timestamp = int(now.timestamp()) // interval * interval
        return datetime.fromtimestamp(timestamp, tz=UTC)
    zone = ZoneInfo(job.timezone)
    local_now = now.astimezone(zone)
    if schedule_type == "daily_time":
        candidates = [
            datetime.combine(local_now.date(), _parse_local_time(value), tzinfo=zone)
            for value in job.run_at
        ]
        due = [candidate for candidate in candidates if candidate <= local_now]
        return max(due).astimezone(UTC) if due else None
    if schedule_type == "trading_session":
        for raw_window in job.session_windows:
            start_text, end_text = raw_window.split("-", 1)
            start = datetime.combine(local_now.date(), _parse_local_time(start_text), tzinfo=zone)
            end = datetime.combine(local_now.date(), _parse_local_time(end_text), tzinfo=zone)
            if start <= local_now <= end:
                interval = max(1, int(job.interval_seconds or 1))
                elapsed = int((local_now - start).total_seconds())
                return (start + timedelta(seconds=elapsed // interval * interval)).astimezone(UTC)
        return None
    return None


def _task_payload(job: Any, *, scheduled_for: datetime) -> dict[str, Any]:
    return {
        "job_name": job.name,
        "job_type": job.job_type,
        "market": job.market,
        "limit": job.limit,
        "scheduled_for": scheduled_for.isoformat(),
        "params": json.loads(json.dumps(job.params, default=str)),
    }


def _normalized_schedule_type(value: str) -> str:
    return "fixed_interval" if value == "interval" else str(value)


def _time_idempotency_key(job_name: str, scheduled_for: datetime) -> str:
    return f"scheduler:{job_name}:{scheduled_for.astimezone(UTC).isoformat()}"


def _dependency_idempotency_key(
    job_name: str,
    mode: str,
    generation_ids: tuple[str, ...],
) -> str:
    raw = json.dumps([job_name, mode, generation_ids], separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"scheduler:{job_name}:dependency:{digest}"


def _parse_local_time(value: str) -> Any:
    return datetime.strptime(str(value).strip(), "%H:%M").time()


def _as_utc(value: datetime) -> datetime:
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC)
