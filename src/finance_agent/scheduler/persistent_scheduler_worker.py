"""PostgreSQL 直领调度 worker。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from finance_agent.scheduler.persistent_task_queue import PersistentTaskQueue, TaskClaim

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class WorkerRunSummary:
    """一次批量领取和执行的结果。"""

    claimed: int = 0
    completed: int = 0
    failed: int = 0
    results: tuple[JsonDict, ...] = ()


class PersistentSchedulerWorker:
    """只从 PostgreSQL pending 队列领取并执行已准入任务。"""

    def __init__(
        self,
        *,
        queue_scope: Callable[[], AbstractContextManager[PersistentTaskQueue]],
        jobs: Mapping[str, Any],
        execute_job: Callable[[Any], JsonDict],
        worker_id: str,
        lease_seconds: int,
        retry_backoff_seconds: int = 30,
        resource_pool_limits: Mapping[str, int] | None = None,
    ) -> None:
        self.queue_scope = queue_scope
        self.jobs = dict(jobs)
        self.execute_job = execute_job
        self.worker_id = str(worker_id)
        self.lease_seconds = max(1, int(lease_seconds))
        self.retry_backoff_seconds = max(0, int(retry_backoff_seconds))
        self.resource_pool_limits = {
            str(pool): max(1, int(limit))
            for pool, limit in (resource_pool_limits or {}).items()
        }

    def startup_recover(self, *, now: datetime | None = None) -> int:
        """启动时恢复全部过期租约。"""

        with self.queue_scope() as queue:
            return queue.recover_expired(now=now)

    def claim(
        self,
        *,
        free_slots: int,
        now: datetime | None = None,
    ) -> list[TaskClaim]:
        """从整个持久队列领取任务，不依赖本地到期状态。"""

        if int(free_slots) <= 0 or not self.jobs:
            return []
        with self.queue_scope() as queue:
            return queue.claim_many(
                worker_id=self.worker_id,
                limit=int(free_slots),
                lease_seconds=self.lease_seconds,
                job_names=tuple(sorted(self.jobs)),
                resource_pool_limits=self.resource_pool_limits,
                now=now,
            )

    def execute(
        self,
        claim: TaskClaim,
        *,
        now: datetime | None = None,
    ) -> JsonDict:
        """执行一个租约，并在独立事务中完成或失败回写。"""

        occurred_at = now or datetime.now(tz=UTC)
        base_job = self.jobs.get(claim.job_name)
        if base_job is None:
            error = f"持久任务对应配置不存在或已禁用：{claim.job_name}"
            self._fail(claim, error_message=error, now=occurred_at)
            return {"status": "failed", "job": claim.job_name, "error_message": error}
        persisted_params = claim.payload.get("params")
        job = (
            replace(
                base_job,
                params={**dict(base_job.params), **dict(persisted_params)},
            )
            if isinstance(persisted_params, dict)
            else base_job
        )
        try:
            result = self.execute_job(job)
        except Exception as exc:
            self._fail(claim, error_message=str(exc), now=occurred_at)
            return {
                "status": "failed",
                "job": claim.job_name,
                "persistent_task_id": claim.task_id,
                "error_message": str(exc),
            }
        succeeded = str(result.get("status") or "") in {"executed", "completed", "ok"}
        if not succeeded:
            error = str(result.get("error_message") or "scheduler_job_failed")
            self._fail(claim, error_message=error, now=occurred_at)
            return dict(result) | {
                "persistent_task_id": claim.task_id,
                "status": "failed",
                "error_message": error,
            }
        next_partition = next_partition_payload_from_result(result)
        if isinstance(next_partition, dict):
            self._schedule_next_partition(
                claim,
                payload=next_partition,
                now=occurred_at,
            )
        with self.queue_scope() as queue:
            queue.complete(
                task_id=claim.task_id,
                lease_token=claim.lease_token,
                now=occurred_at,
            )
        return dict(result) | {
            "persistent_task_id": claim.task_id,
            "persistent_task_attempt": claim.attempts,
        }

    def run_once(
        self,
        *,
        now: datetime | None = None,
        free_slots: int | None = None,
    ) -> WorkerRunSummary:
        """同步领取并执行一批任务，主要用于恢复和集成测试。"""

        occurred_at = now or datetime.now(tz=UTC)
        claims = self.claim(
            free_slots=free_slots if free_slots is not None else max(1, len(self.jobs)),
            now=occurred_at,
        )
        results = tuple(self.execute(claim, now=occurred_at) for claim in claims)
        return WorkerRunSummary(
            claimed=len(claims),
            completed=sum(result.get("status") != "failed" for result in results),
            failed=sum(result.get("status") == "failed" for result in results),
            results=results,
        )

    def _fail(self, claim: TaskClaim, *, error_message: str, now: datetime) -> None:
        with self.queue_scope() as queue:
            queue.fail(
                task_id=claim.task_id,
                lease_token=claim.lease_token,
                error_message=error_message,
                retry_after=timedelta(seconds=self.retry_backoff_seconds),
                now=now,
            )

    def _schedule_next_partition(
        self,
        claim: TaskClaim,
        *,
        payload: JsonDict,
        now: datetime,
    ) -> None:
        cursor = payload.get("partition_cursor")
        if cursor is None:
            raise ValueError("next_partition_payload 缺少 partition_cursor")
        digest = hashlib.sha256(f"{claim.task_id}|{cursor}".encode()).hexdigest()[:16]
        next_payload = dict(claim.payload)
        next_payload["params"] = {
            **dict(claim.payload.get("params") or {}),
            **dict(payload),
        }
        with self.queue_scope() as queue:
            next_task = queue.schedule(
                job_name=claim.job_name,
                idempotency_key=f"{claim.task_id}:{digest}:partition:{cursor}",
                schedule_type="manual",
                scheduled_for=now,
                payload=next_payload,
                priority=claim.priority,
                resource_pool=claim.resource_pool,
                mutex_key=claim.mutex_key,
                dependency_generation=claim.dependency_generation,
                required_data_domains=claim.required_data_domains,
                config_digest=claim.config_digest,
                max_attempts=max(1, claim.max_attempts),
                now=now,
            )
            queue.set_admission(task_id=next_task.task_id, allowed=True, now=now)


def next_partition_payload_from_result(result: Mapping[str, Any]) -> JsonDict | None:
    """从调度结果顶层或采集摘要中提取持久分区游标。"""

    direct = result.get("next_partition_payload")
    if isinstance(direct, Mapping):
        return dict(direct)
    summary = result.get("summary")
    if not isinstance(summary, Mapping):
        return None
    items = summary.get("results")
    if not isinstance(items, (list, tuple)):
        return None
    for item in items:
        if not isinstance(item, Mapping):
            continue
        payload = item.get("payload")
        if not isinstance(payload, Mapping):
            continue
        nested = payload.get("next_partition_payload")
        if isinstance(nested, Mapping):
            return dict(nested)
    return None
