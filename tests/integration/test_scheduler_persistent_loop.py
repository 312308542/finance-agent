"""PostgreSQL/Redis 持久调度闭环集成测试。"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from redis import Redis
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

from finance_agent.data_recovery.gate import RecoveryGate
from finance_agent.data_recovery.repository import RecoveryRepository
from finance_agent.scheduler.base_data_scheduler import BaseDataSchedulerConfig, BaseDataSchedulerJob
from finance_agent.scheduler.persistent_task_queue import PersistentTaskQueue, TaskClaim
from finance_agent.scheduler.planner import SchedulerPlanner
from finance_agent.storage.db import DEFAULT_DATABASE_URL, create_session_factory
from finance_agent.storage.orm import DataRecoveryRunORM, SchedulerTaskRunORM

BASE_TIME = datetime(2000, 1, 1, 1, 0, tzinfo=UTC)
ALL_STATES = (
    "scheduled",
    "blocked",
    "pending",
    "running",
    "completed",
    "failed",
    "cancelled",
)


@pytest.fixture
def postgres_queue() -> Iterator[tuple[PersistentTaskQueue, str]]:
    """连接真实 PostgreSQL，并用事务回滚隔离每个集成用例。"""

    database_url = os.getenv("FINANCE_AGENT_TEST_DATABASE_URL", DEFAULT_DATABASE_URL)
    session_factory = create_session_factory(database_url)
    session = session_factory()
    namespace = f"it.{uuid4().hex[:8]}"
    try:
        session.execute(text("SELECT 1"))
    except OperationalError as exc:
        session.close()
        pytest.skip(f"PostgreSQL 集成环境不可达：{exc}")

    try:
        yield PersistentTaskQueue(session), namespace
    finally:
        session.rollback()
        session.close()
        with session_factory() as verification_session:
            remaining = verification_session.scalar(
                select(func.count())
                .select_from(SchedulerTaskRunORM)
                .where(SchedulerTaskRunORM.job_name.like(f"{namespace}.%"))
            )
        assert remaining == 0, "集成测试事务回滚后不应残留调度任务"


@pytest.fixture
def isolated_redis() -> Iterator[Redis]:
    """使用 Redis DB 15；存在非测试键时拒绝执行 flushdb。"""

    configured_url = os.getenv("FINANCE_AGENT_TEST_REDIS_URL") or os.getenv(
        "FINANCE_AGENT_REDIS_URL",
        "redis://localhost:6379/0",
    )
    parsed = urlsplit(configured_url)
    redis_url = urlunsplit((parsed.scheme, parsed.netloc, "/15", parsed.query, parsed.fragment))
    client = Redis.from_url(redis_url, decode_responses=True)
    try:
        client.ping()
    except Exception as exc:
        client.close()
        pytest.skip(f"Redis 集成环境不可达：{exc}")

    existing_keys = [str(key) for key in client.scan_iter(match="*")]
    foreign_keys = [key for key in existing_keys if not key.startswith("finance-agent-it:")]
    if foreign_keys:
        client.close()
        pytest.skip("Redis DB 15 包含非集成测试键，拒绝执行 flushdb")

    client.flushdb()
    try:
        yield client
    finally:
        client.flushdb()
        client.close()


def _schedule(
    queue: PersistentTaskQueue,
    *,
    namespace: str,
    suffix: str,
    resource_pool: str,
    mutex_key: str | None = None,
) -> SchedulerTaskRunORM:
    job_name = f"{namespace}.{suffix}"
    task = queue.schedule(
        job_name=job_name,
        idempotency_key=f"{job_name}:generation:1",
        schedule_type="manual",
        scheduled_for=BASE_TIME,
        payload={"params": {"name": job_name}},
        priority=100,
        resource_pool=resource_pool,
        mutex_key=mutex_key,
        config_digest="integration-digest",
        max_attempts=2,
        now=BASE_TIME,
    )
    return task


def _admit(queue: PersistentTaskQueue, task_id: str) -> None:
    assert queue.set_admission(task_id=task_id, allowed=True, now=BASE_TIME)


def _complete_one(
    queue: PersistentTaskQueue,
    *,
    job_name: str,
    resource_pool: str,
) -> TaskClaim:
    claims = queue.claim_many(
        worker_id="integration-worker",
        limit=1,
        lease_seconds=60,
        job_names=(job_name,),
        resource_pool_limits={resource_pool: 1},
        now=BASE_TIME,
    )
    assert len(claims) == 1
    claim = claims[0]
    assert queue.complete(task_id=claim.task_id, lease_token=claim.lease_token, now=BASE_TIME)
    return claim


def test_postgres_task_survives_redis_flush_and_recovers_expired_lease(
    postgres_queue: tuple[PersistentTaskQueue, str],
    isolated_redis: Redis,
) -> None:
    """Redis 清空不能丢任务，过期租约必须重新回到 pending 并可完成。"""

    queue, namespace = postgres_queue
    pool = f"{namespace}.lease-pool"
    task = _schedule(queue, namespace=namespace, suffix="lease", resource_pool=pool)
    duplicate = _schedule(queue, namespace=namespace, suffix="lease", resource_pool=pool)
    assert duplicate.task_id == task.task_id

    assert queue.set_admission(
        task_id=task.task_id,
        allowed=False,
        reason_code="integration_block",
        reason_detail={"source": "postgresql"},
        recheck_at=BASE_TIME + timedelta(seconds=1),
        now=BASE_TIME,
    )
    blocked = queue.list_tasks(job_name=task.job_name, statuses=ALL_STATES)
    assert len(blocked) == 1
    assert blocked[0].status == "blocked"
    assert blocked[0].blocked_reason == "integration_block"

    _admit(queue, task.task_id)
    isolated_redis.set("finance-agent-it:scheduler-progress", "ephemeral")
    isolated_redis.flushdb()
    assert isolated_redis.dbsize() == 0
    pending = queue.list_tasks(job_name=task.job_name, statuses=("pending",))
    assert [row.task_id for row in pending] == [task.task_id]

    first_claim = queue.claim_many(
        worker_id="integration-worker-before-restart",
        limit=1,
        lease_seconds=1,
        job_names=(task.job_name,),
        resource_pool_limits={pool: 1},
        now=BASE_TIME,
    )
    assert len(first_claim) == 1
    assert queue.recover_expired(now=BASE_TIME + timedelta(seconds=2)) == 1

    second_claim = queue.claim_many(
        worker_id="integration-worker-after-restart",
        limit=1,
        lease_seconds=60,
        job_names=(task.job_name,),
        resource_pool_limits={pool: 1},
        now=BASE_TIME + timedelta(seconds=2),
    )
    assert len(second_claim) == 1
    assert second_claim[0].task_id == task.task_id
    assert second_claim[0].lease_token != first_claim[0].lease_token
    assert queue.complete(
        task_id=second_claim[0].task_id,
        lease_token=second_claim[0].lease_token,
        now=BASE_TIME + timedelta(seconds=3),
    )
    assert queue.list_tasks(job_name=task.job_name, statuses=("completed",))[0].task_id == task.task_id


def test_postgres_claim_many_enforces_resource_pool_limit(
    postgres_queue: tuple[PersistentTaskQueue, str],
) -> None:
    """数据库直领必须在同一事务内限制资源池并发数。"""

    queue, namespace = postgres_queue
    pool = f"{namespace}.pool"
    tasks = [
        _schedule(queue, namespace=namespace, suffix=f"pool-{index}", resource_pool=pool)
        for index in range(3)
    ]
    for task in tasks:
        _admit(queue, task.task_id)

    claims = queue.claim_many(
        worker_id="integration-pool-worker",
        limit=3,
        lease_seconds=60,
        job_names=tuple(task.job_name for task in tasks),
        resource_pool_limits={pool: 2},
        now=BASE_TIME,
    )

    assert len(claims) == 2
    assert {claim.resource_pool for claim in claims} == {pool}
    for claim in claims:
        assert queue.complete(task_id=claim.task_id, lease_token=claim.lease_token, now=BASE_TIME)
    remaining = [
        row
        for task in tasks
        for row in queue.list_tasks(job_name=task.job_name, statuses=("pending",), limit=10)
    ]
    assert len(remaining) == 1


def test_dependency_modes_are_idempotent_and_draft_recovery_does_not_block(
    postgres_queue: tuple[PersistentTaskQueue, str],
) -> None:
    """三种依赖代次重建不重复，draft 恢复批次不关闭调度门。"""

    queue, namespace = postgres_queue
    source_pool = f"{namespace}.source-pool"
    source_names = (f"{namespace}.source-a", f"{namespace}.source-b")
    for index, source_name in enumerate(source_names):
        task = _schedule(
            queue,
            namespace=namespace,
            suffix=f"source-{'a' if index == 0 else 'b'}",
            resource_pool=source_pool,
        )
        _admit(queue, task.task_id)
        _complete_one(queue, job_name=source_name, resource_pool=source_pool)

    downstream_jobs = tuple(
        BaseDataSchedulerJob(
            name=f"{namespace}.downstream-{mode}",
            group="analytics",
            interval_seconds=0,
            schedule_type="after_success",
            depends_on=source_names,
            dependency_mode=mode,
            resource_pool=f"{namespace}.analytics-pool",
            params={"name": f"{namespace}.downstream-{mode}"},
        )
        for mode in ("all_of", "any_of", "barrier")
    )
    config = BaseDataSchedulerConfig(cache_backend="null", jobs=downstream_jobs)

    first = SchedulerPlanner(queue, config_digest="integration-digest").reconcile(
        now=BASE_TIME,
        config=config,
    )
    second = SchedulerPlanner(queue, config_digest="integration-digest").reconcile(
        now=BASE_TIME,
        config=config,
    )

    assert first.dependency_created == 3
    assert second.dependency_created == 0
    source_task_ids = {
        row.task_id
        for source_name in source_names
        for row in queue.list_tasks(job_name=source_name, statuses=("completed",))
    }
    for job in downstream_jobs:
        rows = queue.list_tasks(job_name=job.name, statuses=("scheduled",))
        assert len(rows) == 1
        assert set(rows[0].dependency_generation) == source_task_ids

    recovery_market = f"it-{uuid4().hex[:12]}"
    queue.repository.session.add(
        DataRecoveryRunORM(
            run_id=f"rec:{recovery_market}:{uuid4().hex}",
            market=recovery_market,
            cutoff_date=BASE_TIME.date(),
            plan_hash=uuid4().hex,
            status="draft",
            gate_status="recovering",
        )
    )
    queue.repository.session.flush()

    assert RecoveryGate(
        RecoveryRepository(queue.repository.session),
        market=recovery_market,
    ).current_state() == (None, "open")
