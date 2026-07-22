"""持久化任务队列门面。

调度器只依赖本模块的六个状态操作，不直接拼接 SQL；事务边界仍由调用方的
`session_scope` 控制。任务执行器不可把租约 token 暴露给外部 Agent。
"""

from __future__ import annotations

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
