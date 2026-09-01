"""调度器补跑桥接（RecoverySchedulerMixin）单元测试。"""

import threading
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

from finance_agent.scheduler.base_data_scheduler import BaseDataScheduler


def _scheduler(scope=None):
    sched = object.__new__(BaseDataScheduler)
    sched.config = SimpleNamespace(
        max_concurrent_jobs=2,
        job_timeout_seconds=600,
    )
    sched._persistent_task_queue_scope = scope
    sched._worker_id = "w1"
    sched._recovery_lock = threading.Lock()
    sched._recovery_inflight_count = 0
    sched.finished_tasks = []
    sched.notified = []
    sched.collect_calls = []

    def finish(claim, *, succeeded, error_message=""):
        sched.finished_tasks.append((claim.task_id, succeeded))

    def notify(claim, *, succeeded):
        sched.notified.append((claim.task_id, succeeded))

    sched._finish_persistent_task = finish  # type: ignore[method-assign]
    sched._notify_recovery_module = notify  # type: ignore[method-assign]
    return sched


def _queue_scope(claims_per_job=None):
    """claims_per_job: {job_name: [claim 或 None, ...]}；耗尽后返回 None。"""

    remaining = {
        name: list(seq or []) for name, seq in (claims_per_job or {}).items()
    }
    claimed_log = []

    class Queue:
        def claim(self, **kwargs):
            claimed_log.append(kwargs["job_name"])
            seq = remaining.get(kwargs["job_name"], [])
            return seq.pop(0) if seq else None

    @contextmanager
    def factory():
        yield Queue()

    factory.claimed_log = claimed_log  # type: ignore[attr-defined]
    return factory


def _claim(task_id="t1", job_name="recovery.ashare.bars", payload=None):
    return SimpleNamespace(
        task_id=task_id,
        job_name=job_name,
        payload=payload if payload is not None else {"recovery_run_id": "rec:r1"},
        attempts=1,
    )




def test_claim_skips_without_scope_or_free_slots() -> None:
    sched = _scheduler(scope=None)
    class _Executor:
        def submit(self, *args, **kwargs):
            raise AssertionError("不应提交")

    sched._claim_recovery_tasks(_Executor(), free_slots=0)

    # free_slots=0 时直接返回，不产生任何领取调用（M2：替换原恒真分支）。
    scope = _queue_scope({"recovery.ashare.bars": [_claim()]})
    sched2 = _scheduler(scope=scope)
    sched2._claim_recovery_tasks(_Executor(), free_slots=0)
    assert scope.claimed_log == []


def test_claim_submits_and_counts_inflight_then_throttles() -> None:
    submitted = []

    class Executor:
        def submit(self, fn, claim):
            submitted.append(claim)
            return None

    scope = _queue_scope(
        {
            "recovery.ashare.bars": [_claim("t1")],
            "recovery.ashare.derived": [_claim("t2", job_name="recovery.ashare.derived")],
        }
    )
    sched = _scheduler(scope=scope)
    sched._claim_recovery_tasks(Executor(), free_slots=5)
    assert [c.task_id for c in submitted] == ["t1", "t2"]
    assert sched._recovery_inflight() == 2
    # 5 秒节流窗口内的第二次扫描完全跳过，不产生新的领取查询。
    claims_after_first_sweep = len(scope.claimed_log)
    sched._claim_recovery_tasks(Executor(), free_slots=5)
    assert len(scope.claimed_log) == claims_after_first_sweep


def test_run_recovery_claim_success_and_failure_paths() -> None:
    sched = _scheduler()

    def ok_execute(claim):
        return {"status": "executed"}

    sched._execute_recovery_task = ok_execute  # type: ignore[method-assign]
    summary = sched._run_recovery_claim(_claim("t-ok"))
    assert summary["status"] == "executed"
    assert sched.finished_tasks == [("t-ok", True)]
    assert sched.notified == [("t-ok", True)]
    assert sched._recovery_inflight() == 0

    def bad_execute(claim):
        raise RuntimeError("boom")

    sched._execute_recovery_task = bad_execute  # type: ignore[method-assign]
    failed = sched._run_recovery_claim(_claim("t-bad"))
    assert failed["status"] == "failed"
    assert sched.finished_tasks == [("t-ok", True), ("t-bad", False)]
    assert sched.notified[-1] == ("t-bad", False)
    assert sched._recovery_inflight() == 0


def test_build_collect_args_manual_symbol_mapping() -> None:
    sched = _scheduler()
    captured = {}

    def default_args(**overrides):
        captured.update(overrides)
        return SimpleNamespace(**overrides)

    sched._default_collection_args = default_args  # type: ignore[method-assign]
    args = sched._build_recovery_collect_args(
        "market_bars",
        {
            "asset_id": "ashare:600000",
            "gap_start_at": "2026-08-18",
            "gap_end_at": "2026-08-21",
        },
    )
    assert args is not None
    assert captured["sync_task_type"] == "market_bars_backfill"
    assert captured["symbol_source"] == "manual"
    assert captured["ashare_symbol"] == "600000"
    # K 线向两侧扩 7 天，扩窗终点不超过当天。
    assert captured["ashare_start"] == "20260811"  # 08-18 前扩 7 天
    expected_end = min(date(2026, 8, 28), date.today()).strftime("%Y%m%d")
    assert captured["ashare_end"] == expected_end
    assert captured["is_closed"] is True

    for domain, task_type, group in (
        ("valuation", "valuation_backfill", "ashare-p2"),
        ("capital_flow", "capital_flow_backfill", "ashare-p1"),
    ):
        captured.clear()
        args_fact = sched._build_recovery_collect_args(
            domain,
            {
                "asset_id": "ashare:600000",
                "gap_start_at": "2026-08-19",
                "gap_end_at": "2026-08-21",
            },
        )
        assert args_fact is not None
        assert captured["sync_task_type"] == task_type
        assert captured["group"] == group
        assert captured["ashare_start"] == "20260819"
        assert captured["ashare_end"] == "20260821"
        assert captured["limit"] == 30

    captured.clear()
    args_market = sched._build_recovery_collect_args("events", {"asset_id": None})
    assert args_market is not None
    assert captured["sync_task_type"] == "event_refresh"
    assert captured["symbol_source"] == "market_assets"


def test_filter_gate_passthrough_on_missing_scope() -> None:
    sched = _scheduler(scope=None)
    due = [SimpleNamespace(job=SimpleNamespace(name="quality.ashare"))]
    runnable, blocked = sched._filter_by_recovery_gate(due)
    assert runnable is due and blocked == []



def test_execute_recovery_derived_uses_public_methods_when_jobs_present() -> None:
    sched = _scheduler()
    sched.config.jobs = []

    quality_job = SimpleNamespace(
        name="quality.ashare",
        job_type="data_quality_refresh",
        market="ashare",
        limit=200,
        params={
            "market": "ashare",
            "timeframe": "1d",
            "min_bars": 60,
            "stale_after_seconds": 86400,
            "data_domains": ["market_bars"],
        },
    )
    sched.config.jobs.append(quality_job)

    def fake_build_data_quality(job):
        return {"market": job.market}

    def fake_run_quality(**kwargs):
        return {"status": "executed"}

    sched.build_data_quality_refresh_kwargs = fake_build_data_quality  # type: ignore[method-assign]
    sched.run_data_quality_refresh = fake_run_quality  # type: ignore[method-assign]

    result = sched._execute_recovery_derived(
        {
            "task_params": {
                "derived_refresh_for": "2026-08-21",
                "pipeline": ["data_quality_refresh"],
            }
        }
    )
    assert result["status"] == "executed"
    assert result["pipeline"] == ["data_quality_refresh"]


def test_execute_recovery_derived_fails_when_runner_missing() -> None:
    sched = _scheduler()
    sched.config.jobs = []  # 生产未注入任何配置任务 → 必需阶段缺能力
    result = sched._execute_recovery_derived(
        {
            "task_params": {
                "derived_refresh_for": "2026-08-21",
                "pipeline": ["data_quality_refresh"],
            }
        }
    )
    assert result["status"] == "failed"  # H1：不得静默跳过
    assert result["error_message"] == "missing_runner:data_quality_refresh"
    assert result["cutoff"] == "2026-08-21"
