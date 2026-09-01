"""持久化任务队列门面。

调度器只依赖本模块的六个状态操作，不直接拼接 SQL；事务边界仍由调用方的
`session_scope` 控制。任务执行器不可把租约 token 暴露给外部 Agent。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.storage.orm import SchedulerTaskRunORM
from finance_agent.storage.repositories import OutboxEventRepository, SchedulerTaskRepository

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class TaskClaim:
    """分配给 worker 的最小任务上下文。"""

    task_id: str
    job_name: str
    lease_token: str
    payload: JsonDict
    attempts: int
    max_attempts: int
    scheduled_for: datetime | None = None
    priority: int = 100
    resource_pool: str = "default"
    mutex_key: str | None = None
    dependency_generation: tuple[str, ...] = ()
    required_data_domains: tuple[str, ...] = ()
    config_digest: str | None = None

    @classmethod
    def from_orm(cls, task: SchedulerTaskRunORM) -> TaskClaim:
        """从 ORM 任务提取不可变执行上下文。"""

        if not task.lease_token:
            raise ValueError("running 任务缺少 lease_token")
        return cls(
            task_id=task.task_id,
            job_name=task.job_name,
            lease_token=task.lease_token,
            payload=dict(task.payload or {}),
            attempts=int(task.attempts or 0),
            max_attempts=int(task.max_attempts or 0),
            scheduled_for=getattr(task, "scheduled_for", None),
            priority=(
                100
                if getattr(task, "priority", None) is None
                else int(task.priority)
            ),
            resource_pool=str(getattr(task, "resource_pool", "default") or "default"),
            mutex_key=getattr(task, "mutex_key", None),
            dependency_generation=tuple(getattr(task, "dependency_generation", None) or ()),
            required_data_domains=tuple(getattr(task, "required_data_domains", None) or ()),
            config_digest=getattr(task, "config_digest", None),
        )


class PersistentTaskQueue:
    """调度器使用的持久化任务队列。"""

    def __init__(
        self,
        session: Session,
        *,
        outbox_repository: OutboxEventRepository | None = None,
    ) -> None:
        self.repository = SchedulerTaskRepository(
            session,
            outbox_repository=outbox_repository or OutboxEventRepository(session),
        )

    def enqueue(
        self,
        *,
        job_name: str,
        idempotency_key: str,
        payload: JsonDict | None = None,
        max_attempts: int = 3,
        now: datetime | None = None,
    ) -> SchedulerTaskRunORM:
        """创建或读取同一幂等键对应的任务。"""

        return self.repository.enqueue(
            job_name=job_name,
            idempotency_key=idempotency_key,
            payload=payload,
            max_attempts=max_attempts,
            now=now,
        )

    def claim(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 60,
        job_name: str | None = None,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> TaskClaim | None:
        """领取一个任务并返回最小执行上下文。"""

        task = self.repository.claim(
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            job_name=job_name,
            idempotency_key=idempotency_key,
            now=now,
        )
        return TaskClaim.from_orm(task) if task is not None else None

    def schedule(self, **kwargs: Any) -> SchedulerTaskRunORM:
        """持久化尚未准入的逻辑运行。"""

        return self.repository.schedule(**kwargs)

    def set_admission(self, **kwargs: Any) -> bool:
        """持久化准入结论。"""

        return self.repository.set_admission(**kwargs)

    def coalesce_task(self, **kwargs: Any) -> bool:
        """合并一个尚未执行的固定节拍运行。"""

        return self.repository.coalesce_task(**kwargs)

    def claim_many(
        self,
        *,
        worker_id: str,
        limit: int,
        lease_seconds: int = 60,
        resource_pool: str | None = None,
        job_names: Sequence[str] | None = None,
        resource_pool_limits: Mapping[str, int] | None = None,
        now: datetime | None = None,
    ) -> list[TaskClaim]:
        """批量领取所有已到期且已准入的任务。"""

        tasks = self.repository.claim_many(
            worker_id=worker_id,
            limit=limit,
            lease_seconds=lease_seconds,
            resource_pool=resource_pool,
            job_names=job_names,
            resource_pool_limits=resource_pool_limits,
            now=now,
        )
        return [TaskClaim.from_orm(task) for task in tasks]

    def heartbeat(
        self,
        *,
        task_id: str,
        lease_token: str,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> bool:
        """续租当前任务。"""

        return self.repository.heartbeat(
            task_id=task_id,
            lease_token=lease_token,
            lease_seconds=lease_seconds,
            now=now,
        )

    def complete(self, *, task_id: str, lease_token: str, now: datetime | None = None) -> bool:
        """以当前租约完成任务。"""

        return self.repository.complete(task_id=task_id, lease_token=lease_token, now=now)

    def fail(
        self,
        *,
        task_id: str,
        lease_token: str,
        error_message: str,
        retry_after: timedelta = timedelta(seconds=30),
        now: datetime | None = None,
    ) -> bool:
        """记录任务失败并安排重试或终态。"""

        return self.repository.fail(
            task_id=task_id,
            lease_token=lease_token,
            error_message=error_message,
            retry_after=retry_after,
            now=now,
        )

    def recover_expired(self, *, now: datetime | None = None) -> int:
        """恢复所有过期租约。"""

        return self.repository.recover_expired(now=now)

    def recover_orphaned(
        self,
        *,
        worker_prefix: str,
        current_worker_id: str | None = None,
        now: datetime | None = None,
    ) -> int:
        """回收同一 scheduler 前一实例留下的运行租约。"""

        return self.repository.recover_orphaned(
            worker_prefix=worker_prefix,
            current_worker_id=current_worker_id,
            now=now,
        )

    def list_tasks(
        self,
        *,
        job_name: str | None = None,
        statuses: Sequence[str] = ("pending",),
        payload_key: str | None = None,
        payload_value: str | None = None,
        limit: int = 200,
    ) -> list[SchedulerTaskRunORM]:
        """按任务名/状态/负载键值查询持久任务（任务观测 API）。"""

        return self.repository.list_tasks(
            job_name=job_name,
            statuses=statuses,
            payload_key=payload_key,
            payload_value=payload_value,
            limit=limit,
        )

    def cancel_tasks(
        self,
        *,
        task_ids: Sequence[str],
        reason: str | None = None,
        now: datetime | None = None,
    ) -> int:
        """取消未领取的 pending 任务；返回实际取消数（规格 12.3）。"""

        return self.repository.cancel_tasks(
            task_ids=task_ids,
            reason=reason,
            now=now,
        )
