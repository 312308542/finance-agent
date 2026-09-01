from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy.dialects import postgresql

from finance_agent.scheduler.persistent_task_queue import TaskClaim
from finance_agent.storage.orm import SchedulerTaskRunORM
from finance_agent.storage.repositories import SchedulerTaskRepository

NOW = datetime(2026, 7, 20, 9, 40, tzinfo=UTC)


class _Result:
    def __init__(self, rowcount: int = 1) -> None:
        self.rowcount = rowcount


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def first(self) -> Any:
        return self.value

    def all(self) -> list[Any]:
        if self.value is None:
            return []
        if isinstance(self.value, list):
            return self.value
        return [self.value]


class _Session:
    def __init__(self, *, task: Any = None, rowcount: int = 1) -> None:
        self.task = task
        self.rowcount = rowcount
        self.executed: list[Any] = []
        self.flush_count = 0

    def execute(self, statement: Any) -> _Result:
        self.executed.append(statement)
        return _Result(self.rowcount)

    def flush(self) -> None:
        self.flush_count += 1

    def get_one(self, _model: Any, _key: Any) -> Any:
        return self.task or SimpleNamespace(task_id=_key, status="pending")

    def get(self, _model: Any, _key: Any) -> Any:
        return self.task

    def scalars(self, statement: Any) -> _ScalarResult:
        self.executed.append(statement)
        return _ScalarResult(self.task)


def _compiled(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def test_enqueue_is_idempotent_by_key() -> None:
    session = _Session()

    task = SchedulerTaskRepository(session).enqueue(
        job_name="ashare.realtime_quotes",
        idempotency_key="ashare.realtime_quotes:20260720:09:40",
        payload={"data_snapshot_id": "snapshot:quotes:1"},
        now=NOW,
    )

    sql = _compiled(session.executed[0])
    assert "ON CONFLICT ON CONSTRAINT uq_scheduler_task_runs_idempotency DO NOTHING" in sql
    assert task.task_id.startswith("task:")
    assert session.flush_count == 1


def test_claim_uses_skip_locked_and_sets_lease() -> None:
    task = SimpleNamespace(
        task_id="task:1",
        status="pending",
        attempts=0,
        max_attempts=3,
        lease_owner=None,
        lease_token=None,
        lease_expires_at=None,
        started_at=None,
    )
    session = _Session(task=task)

    claimed = SchedulerTaskRepository(session).claim(worker_id="worker-1", now=NOW)

    sql = _compiled(session.executed[0])
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert claimed is task
    assert task.status == "running"
    assert task.lease_owner == "worker-1"
    assert task.lease_token
    assert task.lease_expires_at == NOW + timedelta(seconds=60)
    assert task.attempts == 1


def test_claim_can_target_one_scheduler_idempotency_key() -> None:
    """调度器精确领取自己的任务时仍保留 SKIP LOCKED。"""

    task = SimpleNamespace(
        task_id="task:1",
        status="pending",
        attempts=0,
        max_attempts=1,
        lease_owner=None,
        lease_token=None,
        lease_expires_at=None,
        started_at=None,
    )
    session = _Session(task=task)

    claimed = SchedulerTaskRepository(session).claim(
        worker_id="worker-1",
        job_name="ashare.realtime_quotes",
        idempotency_key="scheduler:ashare.realtime_quotes:2026-07-20T09:40:00+00:00",
        now=NOW,
    )

    sql = _compiled(session.executed[0])
    assert claimed is task
    assert "scheduler_task_runs.job_name" in sql
    assert "scheduler_task_runs.idempotency_key" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql


def test_heartbeat_and_complete_require_active_lease() -> None:
    session = _Session(rowcount=1)
    repo = SchedulerTaskRepository(session)

    assert repo.heartbeat(task_id="task:1", lease_token="lease-1", now=NOW) is True
    assert repo.complete(task_id="task:1", lease_token="lease-1", now=NOW) is True
    assert session.flush_count == 2
    assert "lease_token" in _compiled(session.executed[0])
    assert "status" in _compiled(session.executed[1])


def test_fail_requeues_before_max_attempts_and_terminally_fails_after_limit() -> None:
    task = SimpleNamespace(
        task_id="task:1",
        status="running",
        attempts=1,
        max_attempts=3,
        lease_token="lease-1",
        lease_expires_at=NOW + timedelta(seconds=30),
        lease_owner="worker-1",
        next_retry_at=None,
        finished_at=None,
    )
    session = _Session(task=task)
    repo = SchedulerTaskRepository(session)

    assert repo.fail(
        task_id="task:1",
        lease_token="lease-1",
        error_message="provider timeout",
        retry_after=timedelta(seconds=15),
        now=NOW,
    ) is True
    assert task.status == "pending"
    assert task.next_retry_at == NOW + timedelta(seconds=15)

    task.status = "running"
    task.attempts = 3
    task.lease_token = "lease-2"
    task.lease_expires_at = NOW + timedelta(seconds=30)
    assert repo.fail(
        task_id="task:1",
        lease_token="lease-2",
        error_message="provider timeout",
        now=NOW,
    ) is True
    assert task.status == "failed"
    assert task.finished_at == NOW


def test_recover_expired_returns_updated_row_count() -> None:
    session = _Session(rowcount=4)

    recovered = SchedulerTaskRepository(session).recover_expired(now=NOW)

    assert recovered == 4
    sql = _compiled(session.executed[0])
    assert "scheduler_task_runs.status =" in sql
    assert "lease_expires_at" in sql


def test_recover_orphaned_releases_previous_scheduler_instance_leases() -> None:
    """scheduler 重启后应回收同一服务前一实例留下的运行租约。"""
    session = _Session(rowcount=2)

    recovered = SchedulerTaskRepository(session).recover_orphaned(
        worker_prefix="base_data_scheduler",
        current_worker_id="base_data_scheduler:1:new",
        now=NOW,
    )

    assert recovered == 2
    sql = _compiled(session.executed[0])
    assert "scheduler_task_runs.lease_owner" in sql
    assert "LIKE" in sql
    assert "scheduler_restarted" in session.executed[0].compile(dialect=postgresql.dialect()).params.values()


def _event_outbox():
    """记录 outbox.append 调用的桩。"""

    appends = []
    stub = SimpleNamespace(append=lambda **kwargs: appends.append(kwargs))
    return appends, stub


class _RowsSession(_Session):
    """execute 返回可 .scalars().all() 的假会话，用于查询/取消路径。"""

    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def execute(self, statement):  # noqa: D102
        self.executed.append(statement)
        rows = list(self.rows)
        return SimpleNamespace(
            rowcount=len(rows),
            scalars=lambda: SimpleNamespace(all=lambda: rows),
        )


class _ClaimConstraintSession(_Session):
    """依次返回 running 快照和 pending 候选，验证事务内额度筛选。"""

    def __init__(self, *, running, candidates) -> None:
        super().__init__()
        self._scalar_results = [list(running), list(candidates)]

    def scalars(self, statement: Any) -> _ScalarResult:
        self.executed.append(statement)
        return _ScalarResult(self._scalar_results.pop(0))


def test_heartbeat_emits_running_event_not_completed() -> None:
    appends, outbox = _event_outbox()
    task = SimpleNamespace(task_id="task:1", attempts=2)
    session = _Session(rowcount=1, task=task)
    repo = SchedulerTaskRepository(session, outbox_repository=outbox)
    assert repo.heartbeat(task_id="task:1", lease_token="lease-1", now=NOW) is True
    assert len(appends) == 1
    assert appends[0]["event_type"] == "scheduler.task.heartbeat"
    assert appends[0]["payload"] == {"status": "running", "lease_extended": True}
    assert appends[0]["idempotency_key"] == "scheduler.task.heartbeat:task:1:2"


def test_list_tasks_filters_by_status_and_payload_key() -> None:
    session = _RowsSession([])
    repo = SchedulerTaskRepository(session)
    rows = repo.list_tasks(
        job_name="recovery.ashare.bars",
        statuses=("pending",),
        payload_key="recovery_run_id",
        payload_value="rec:r1",
        limit=50,
    )
    assert rows == []
    statement = session.executed[0]
    sql = _compiled(statement)
    assert "job_name" in sql
    assert "payload ->> " in sql.replace('\"', '"')
    params = list(statement.compile(dialect=postgresql.dialect()).params.values())
    # IN 子句的取值会整体绑定为列表，这里统一展平后断言。
    flattened = [
        item
        for value in params
        for item in (value if isinstance(value, (list, tuple)) else [value])
    ]
    assert "recovery.ashare.bars" in flattened
    assert "rec:r1" in flattened
    assert "pending" in flattened


def test_cancel_tasks_only_cancels_pending_and_emits_events() -> None:
    appends, outbox = _event_outbox()
    pending = SimpleNamespace(task_id="a", attempts=0)
    session = _RowsSession([pending])
    repo = SchedulerTaskRepository(session, outbox_repository=outbox)
    cancelled = repo.cancel_tasks(
        task_ids=["a", "b"], reason="data_recovery_cancelled:rec:r1", now=NOW
    )
    assert cancelled == 1  # b 不在待取消集合中
    update_statement = session.executed[1]
    assert "UPDATE scheduler_task_runs" in _compiled(update_statement)
    assert "cancelled" in list(
        update_statement.compile(dialect=postgresql.dialect()).params.values()
    )
    assert len(appends) == 1
    assert appends[0]["event_type"] == "scheduler.task.cancelled"
    assert appends[0]["aggregate_id"] == "a"
    assert appends[0]["payload"] == {"reason": "data_recovery_cancelled:rec:r1"}


def test_cancel_tasks_empty_ids_is_noop() -> None:
    session = _RowsSession([])
    repo = SchedulerTaskRepository(session)
    assert repo.cancel_tasks(task_ids=[], reason=None) == 0
    assert session.executed == []


def test_find_active_work_unit_conflicts_queries_active_intersection() -> None:
    """H9：只返回 pending/running 任务与请求集合实际相交的工作单元。"""

    conflict_unit = "market_bars_backfill:ashare:600000:2026-08-18..2026-08-21"
    session = _RowsSession(
        [
            [
                conflict_unit,
                "market_bars_backfill:ashare:000001:2026-08-18..2026-08-21",
            ]
        ]
    )
    repo = SchedulerTaskRepository(session)

    conflicts = repo.find_active_work_unit_conflicts(
        [
            conflict_unit,
            "event_refresh:ashare:2026-08-19",
        ]
    )

    assert conflicts == [conflict_unit]
    statement = session.executed[0]
    sql = _compiled(statement)
    # 状态限定活动任务；JSONB 包含（@>）做交集匹配，NULL 不命中。
    assert "scheduler_task_runs.status IN" in sql
    assert "@>" in sql
    assert "scheduler_task_runs.payload[" in sql
    params = list(statement.compile(dialect=postgresql.dialect()).params.values())
    flattened = [
        item
        for value in params
        for item in (value if isinstance(value, (list, tuple)) else [value])
    ]
    assert "pending" in flattened
    assert "running" in flattened
    assert "market_bars_backfill:ashare:600000:2026-08-18..2026-08-21" in flattened
    assert "event_refresh:ashare:2026-08-19" in flattened


def test_find_active_work_unit_conflicts_normalizes_and_dedupes_units() -> None:
    session = _RowsSession([])
    repo = SchedulerTaskRepository(session)

    conflicts = repo.find_active_work_unit_conflicts(
        [" market_bars_backfill:a:1 ", "market_bars_backfill:a:1", ""]
    )

    assert conflicts == []
    compiled = session.executed[0].compile(dialect=postgresql.dialect())
    flattened = [
        item
        for value in compiled.params.values()
        for item in (value if isinstance(value, (list, tuple)) else [value])
    ]
    # 归一化去重：同一单元只绑定一次，空白与空串被剔除。
    assert flattened.count("market_bars_backfill:a:1") == 1
    assert not any(item == "" or item != item.strip() for item in flattened)


def test_find_active_work_unit_conflicts_empty_input_skips_query() -> None:
    session = _RowsSession([])
    repo = SchedulerTaskRepository(session)

    assert repo.find_active_work_unit_conflicts([]) == []

    assert session.executed == []


def test_task_run_supports_full_scheduler_lifecycle_fields() -> None:
    """调度事实表必须能表达计划、阻塞、准入和配置身份。"""

    columns = SchedulerTaskRunORM.__table__.columns
    for name in (
        "schedule_type",
        "scheduled_for",
        "priority",
        "resource_pool",
        "mutex_key",
        "dependency_generation",
        "required_data_domains",
        "blocked_reason",
        "blocked_detail",
        "blocked_until",
        "config_digest",
        "coalesced_count",
        "cancel_requested_at",
    ):
        assert name in columns

    status_constraint = next(
        constraint
        for constraint in SchedulerTaskRunORM.__table__.constraints
        if constraint.name == "ck_scheduler_task_runs_status"
    )
    sql = str(status_constraint.sqltext)
    assert "scheduled" in sql
    assert "blocked" in sql


def test_task_claim_preserves_zero_priority_and_config_digest() -> None:
    """worker 领取上下文必须无损携带合法零优先级和配置摘要。"""

    task = SimpleNamespace(
        task_id="task:zero",
        job_name="maintenance.zero",
        lease_token="secret",
        payload={},
        attempts=1,
        max_attempts=3,
        scheduled_for=NOW,
        priority=0,
        resource_pool="maintenance",
        mutex_key=None,
        dependency_generation=[],
        required_data_domains=[],
        config_digest="scheduler-digest",
    )

    claim = TaskClaim.from_orm(task)

    assert claim.priority == 0
    assert claim.config_digest == "scheduler-digest"


def test_schedule_creates_scheduled_task_with_metadata() -> None:
    """逻辑运行先以 scheduled 状态落盘，不能直接伪装成 pending。"""

    session = _Session()
    task = SchedulerTaskRepository(session).schedule(
        job_name="ashare.realtime_quotes",
        idempotency_key="scheduler:quotes:2026-08-31T09:35",
        schedule_type="trading_session",
        scheduled_for=NOW,
        priority=10,
        resource_pool="realtime",
        required_data_domains=("realtime_quotes",),
        config_digest="abc123",
        now=NOW,
    )

    statement = session.executed[0]
    params = statement.compile(dialect=postgresql.dialect()).params
    assert "scheduled" in params.values()
    assert "trading_session" in params.values()
    assert "realtime" in params.values()
    assert task.task_id.startswith("task:")


def test_set_admission_persists_blocked_reason_and_pending_transition() -> None:
    """准入结论必须是数据库状态，而不是循环日志。"""

    task = SimpleNamespace(task_id="task:1", attempts=0, status="scheduled")
    session = _Session(task=task, rowcount=1)
    repo = SchedulerTaskRepository(session)

    assert repo.set_admission(
        task_id="task:1",
        allowed=False,
        reason_code="recovery_domain_blocked",
        reason_detail={"domains": ["market_bars"]},
        recheck_at=NOW + timedelta(minutes=5),
        now=NOW,
    )
    blocked_sql = _compiled(session.executed[0])
    assert "blocked_reason" in blocked_sql
    assert "blocked_detail" in blocked_sql

    assert repo.set_admission(task_id="task:1", allowed=True, now=NOW)
    pending_statement = session.executed[1]
    pending_params = pending_statement.compile(dialect=postgresql.dialect()).params
    assert "pending" in pending_params.values()


def test_claim_many_uses_priority_due_time_and_skip_locked() -> None:
    """worker 应按额度从整个 pending 队列领取，不依赖本地 due-state。"""

    tasks = [
        SimpleNamespace(
            task_id=f"task:{index}",
            status="pending",
            attempts=0,
            max_attempts=3,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            started_at=None,
            priority=index,
        )
        for index in range(2)
    ]
    session = _Session(task=tasks)

    claimed = SchedulerTaskRepository(session).claim_many(
        worker_id="worker-1",
        limit=2,
        now=NOW,
    )

    sql = _compiled(session.executed[0])
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "priority" in sql
    assert "priority DESC" in sql
    assert "scheduled_for" in sql
    assert len(claimed) == 2
    assert all(task.status == "running" for task in claimed)


def test_claim_many_rechecks_resource_pool_and_mutex_in_transaction() -> None:
    """多 scheduler 竞争时，领取事务必须复核运行占用而非只信 Admission。"""

    def task(task_id: str, *, status: str, mutex_key: str | None) -> SimpleNamespace:
        return SimpleNamespace(
            task_id=task_id,
            job_name=f"job:{task_id}",
            status=status,
            attempts=0,
            max_attempts=3,
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            started_at=None,
            priority=10,
            resource_pool="realtime",
            mutex_key=mutex_key,
        )

    active = task("active", status="running", mutex_key="quotes")
    same_mutex = task("same-mutex", status="pending", mutex_key="quotes")
    available = task("available", status="pending", mutex_key="news")
    over_limit = task("over-limit", status="pending", mutex_key="events")
    session = _ClaimConstraintSession(
        running=[active],
        candidates=[same_mutex, available, over_limit],
    )

    claimed = SchedulerTaskRepository(session).claim_many(
        worker_id="worker-1",
        limit=3,
        resource_pool_limits={"realtime": 2},
        now=NOW,
    )

    assert [item.task_id for item in claimed] == ["available"]
    assert active.status == "running"
    assert same_mutex.status == "pending"
    assert over_limit.status == "pending"
    assert available.status == "running"
    candidate_sql = _compiled(session.executed[1])
    assert "FOR UPDATE SKIP LOCKED" in candidate_sql
