"""基础数据调度器接入持久化任务队列的行为测试。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

from finance_agent.scheduler import (
    BaseDataScheduler,
    BaseDataSchedulerConfig,
    BaseDataSchedulerJob,
    TaskClaim,
)


class _FakeQueue:
    def __init__(
        self,
        *,
        claim: TaskClaim | None,
        recovered_claim: TaskClaim | None = None,
    ) -> None:
        self.claim_result = claim
        self.recovered_claim = recovered_claim
        self.enqueued: list[dict[str, Any]] = []
        self.claim_calls: list[dict[str, Any]] = []
        self.completed: list[dict[str, str]] = []
        self.failed: list[dict[str, str]] = []
        self.recovered_count = 0

    def enqueue(self, **kwargs: Any) -> SimpleNamespace:
        self.enqueued.append(kwargs)
        return SimpleNamespace(task_id="task:fake")

    def claim(self, **kwargs: Any) -> TaskClaim | None:
        # 忠实模拟真实队列的 job_name 过滤：本夹具不含补跑分区任务。
        if str(kwargs.get("job_name") or "").startswith("recovery."):
            self.claim_calls.append(kwargs)
            return None
        self.claim_calls.append(kwargs)
        if kwargs.get("idempotency_key") is None:
            return self.recovered_claim
        return self.claim_result

    def recover_expired(self, **_kwargs: Any) -> int:
        self.recovered_count += 1
        return 1

    def complete(self, **kwargs: str) -> bool:
        self.completed.append(kwargs)
        return True

    def fail(self, **kwargs: str) -> bool:
        self.failed.append(kwargs)
        return True


def _config() -> BaseDataSchedulerConfig:
    return BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        max_concurrent_jobs=1,
        loop_idle_seconds=0.01,
        jobs=(
            BaseDataSchedulerJob(
                name="ashare.realtime_quotes",
                group="ashare-p0",
                interval_seconds=60,
                market="ashare",
                params={"name": "ashare.realtime_quotes", "sync_task_type": "realtime"},
            ),
        ),
    )


def test_scheduler_persistent_queue_claims_and_completes_job() -> None:
    """常驻调度器应先持久化入队和领取，再在成功后回写完成状态。"""

    queue = _FakeQueue(
        claim=TaskClaim(
            task_id="task:fake",
            job_name="ashare.realtime_quotes",
            lease_token="lease:fake",
            payload={},
            attempts=1,
            max_attempts=1,
        )
    )

    @contextmanager
    def queue_scope() -> Iterator[_FakeQueue]:
        yield queue

    started: list[str] = []

    def collect(args: Any) -> dict[str, Any]:
        started.append(args.name)
        return {"status": "ok"}

    scheduler = BaseDataScheduler(
        _config(),
        collect_base_data_func=collect,
        default_collection_args_func=lambda **kwargs: SimpleNamespace(**kwargs),
        persistent_task_queue_scope=queue_scope,
        sleep_func=lambda _: None,
    )

    result = scheduler.run_loop(max_cycles=1)

    assert result["cycles"] == 1
    assert started == ["ashare.realtime_quotes"]
    assert len(queue.enqueued) == 1
    assert queue.enqueued[0]["job_name"] == "ashare.realtime_quotes"
    assert queue.enqueued[0]["idempotency_key"].startswith("scheduler:ashare.realtime_quotes:")
    # 补跑分区任务（recovery.*）会额外轮询领取；普通任务的领取行为不变。
    normal_claims = [
        call
        for call in queue.claim_calls
        if not str(call.get("job_name") or "").startswith("recovery.")
    ]
    recovery_claims = [
        call for call in queue.claim_calls if str(call.get("job_name") or "").startswith("recovery.")
    ]
    assert len(normal_claims) == 2
    assert normal_claims[0].get("idempotency_key") is None
    assert normal_claims[1]["idempotency_key"].startswith("scheduler:ashare.realtime_quotes:")
    assert all(call.get("idempotency_key") is None for call in recovery_claims)
    assert queue.recovered_count == 1
    assert queue.completed == [{"task_id": "task:fake", "lease_token": "lease:fake"}]
    assert queue.failed == []


def test_scheduler_without_persistent_queue_keeps_in_memory_path() -> None:
    """未注入持久化队列时，现有进程内调度行为保持不变。"""

    started: list[str] = []

    def collect(args: Any) -> dict[str, Any]:
        started.append(args.name)
        return {"status": "ok"}

    scheduler = BaseDataScheduler(
        _config(),
        collect_base_data_func=collect,
        default_collection_args_func=lambda **kwargs: SimpleNamespace(**kwargs),
        sleep_func=lambda _: None,
    )

    result = scheduler.run_loop(max_cycles=1)

    assert result["cycles"] == 1
    assert started == ["ashare.realtime_quotes"]


def test_scheduler_persistent_queue_marks_failed_job() -> None:
    """任务执行最终失败时，当前租约必须回写失败状态。"""

    queue = _FakeQueue(
        claim=TaskClaim(
            task_id="task:failed",
            job_name="ashare.realtime_quotes",
            lease_token="lease:failed",
            payload={},
            attempts=1,
            max_attempts=1,
        )
    )

    @contextmanager
    def queue_scope() -> Iterator[_FakeQueue]:
        yield queue

    def collect(_: Any) -> dict[str, Any]:
        raise RuntimeError("provider unavailable")

    scheduler = BaseDataScheduler(
        _config(),
        collect_base_data_func=collect,
        default_collection_args_func=lambda **kwargs: SimpleNamespace(**kwargs),
        persistent_task_queue_scope=queue_scope,
        sleep_func=lambda _: None,
    )

    result = scheduler.run_loop(max_cycles=1)

    assert result["jobs"][0]["status"] == "failed"
    assert queue.completed == []
    assert queue.failed[0]["task_id"] == "task:failed"
    assert queue.failed[0]["lease_token"] == "lease:failed"


def test_scheduler_reuses_recovered_pending_task_before_new_enqueue() -> None:
    """重启后恢复的同名 pending 任务应优先被接管，避免产生孤儿实例。"""

    queue = _FakeQueue(
        claim=None,
        recovered_claim=TaskClaim(
            task_id="task:recovered",
            job_name="ashare.realtime_quotes",
            lease_token="lease:recovered",
            payload={"scheduled_at": "2026-07-20T09:40:00+00:00"},
            attempts=2,
            max_attempts=3,
        ),
    )

    @contextmanager
    def queue_scope() -> Iterator[_FakeQueue]:
        yield queue

    started: list[str] = []

    def collect(args: Any) -> dict[str, Any]:
        started.append(args.name)
        return {"status": "ok"}

    scheduler = BaseDataScheduler(
        _config(),
        collect_base_data_func=collect,
        default_collection_args_func=lambda **kwargs: SimpleNamespace(**kwargs),
        persistent_task_queue_scope=queue_scope,
        sleep_func=lambda _: None,
    )

    result = scheduler.run_loop(max_cycles=1)

    assert result["cycles"] == 1
    assert started == ["ashare.realtime_quotes"]
    assert queue.enqueued == []
    assert queue.completed == [{"task_id": "task:recovered", "lease_token": "lease:recovered"}]


def test_scheduler_recovers_expired_tasks_periodically(monkeypatch) -> None:
    queue = _FakeQueue(claim=None)

    @contextmanager
    def queue_scope() -> Iterator[_FakeQueue]:
        yield queue

    scheduler = BaseDataScheduler(
        _config(),
        collect_base_data_func=lambda args: {"status": "ok"},
        default_collection_args_func=lambda **kwargs: SimpleNamespace(**kwargs),
        persistent_task_queue_scope=queue_scope,
        sleep_func=lambda _: None,
    )
    ticks = iter([10.0, 20.0, 41.0])
    monkeypatch.setattr(
        "finance_agent.scheduler.base_data_scheduler.time.monotonic",
        lambda: next(ticks),
    )

    assert scheduler._recover_expired_persistent_tasks(force=True) == 1
    assert scheduler._recover_expired_persistent_tasks() == 0
    assert scheduler._recover_expired_persistent_tasks() == 1
    assert queue.recovered_count == 2
