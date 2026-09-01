"""补跑服务与执行器测试：幂等、过期检测、推进与分区提交。"""

from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from finance_agent.data_recovery.executor import (
    RecoveryExecutor,
    build_work_unit,
)
from finance_agent.data_recovery.models import ACTIVE_RUN_STATUSES, GapTarget, PlanStep
from finance_agent.data_recovery.service import (
    MAX_FACT_RETRY_ROUNDS,
    DataRecoveryModule,
    StalePlanError,
)
from finance_agent.data_recovery.state_machine import (
    assert_transition,
    gate_status_for_run,
)

NOW = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)


def _bar_target(asset: str = "ashare:600000", day: int = 18) -> GapTarget:
    return GapTarget(
        data_domain="market_bars",
        asset_id=asset,
        gap_start_at=date(2026, 8, day),
        gap_end_at=date(2026, 8, 21),
        granularity="1d",
        expected_count=4,
    )


class FakeRepository:
    """内存版补跑仓储，只实现被测路径。"""

    def __init__(self) -> None:
        self.runs: dict[str, SimpleNamespace] = {}
        self.steps: list[SimpleNamespace] = []
        self.targets: list[SimpleNamespace] = []
        self.transitions: list[tuple[str, str]] = []
        self.gate_updates: list[tuple[str, str]] = []
        self.blocking = False

    def add_run(self, run_id: str, status: str = "draft", plan_hash: str = "h1"):
        row = SimpleNamespace(
            run_id=run_id,
            market="ashare",
            status=status,
            gate_status=gate_status_for_run(status, has_blocking_gaps=False),
            plan_hash=plan_hash,
            cutoff_date=date(2026, 8, 21),
            gap_start_date=date(2026, 8, 18),
            universe_snapshot_hash="u1",
            universe_id="universe:base:ashare:p0:all_a",
            summary={},
            quality_result=None,
            created_at=NOW,
            approved_at=None,
            started_at=None,
            finished_at=None,
            updated_at=NOW,
        )
        self.runs[run_id] = row
        return row

    def add_step(self, phase: str, domain: str, deps: tuple[str, ...] = ()) -> SimpleNamespace:
        step = SimpleNamespace(
            step_id=f"rec:r1:{phase}:{domain}",
            phase=phase,
            data_domain=domain,
            status="pending",
            depends_on=list(deps),
            task_params={},
            target_count=0,
            completed_count=0,
            retryable_count=0,
            exception_count=0,
            attempt_round=0,
        )
        self.steps.append(step)
        return step

    # -- 查询 ----------------------------------------------------------

    def get_run(self, run_id: str):
        return self.runs.get(run_id)

    def get_active_run(self, market: str):
        for row in self.runs.values():
            if row.status in ACTIVE_RUN_STATUSES:
                return row
        return None

    def find_run_by_plan_hash(self, plan_hash: str):
        for row in self.runs.values():
            if row.plan_hash == plan_hash and row.status in ACTIVE_RUN_STATUSES:
                return row
        return None

    def list_runs(self, *, limit: int = 20):
        return list(self.runs.values())[:limit]

    def get_steps(self, run_id: str):
        return list(self.steps)

    def blocking_gap_exists(self, run_id: str) -> bool:
        return self.blocking



    # -- 写入 ----------------------------------------------------------

    def transition_run(self, run_id, target_status, **_kwargs):
        row = self.runs[run_id]
        assert_transition(row.status, target_status)
        self.transitions.append((row.status, target_status))
        row.status = target_status
        row.gate_status = gate_status_for_run(
            target_status, has_blocking_gaps=self.blocking
        )
        return row

    def set_gate_status(self, run_id, status, *, reason=None, now=None):
        self.gate_updates.append((run_id, status))
        self.runs[run_id].gate_status = status

    def mark_step_status(self, step_id, status, *, attempt_round=None, now=None):
        for step in self.steps:
            if step.step_id == step_id:
                step.status = status
                if attempt_round is not None:
                    step.attempt_round = attempt_round
                if status == "completed":
                    step.completed_count = step.target_count or 0

    def refresh_step_counters(self, step_id):
        return None

    def target_status_counts(self, *, step_id=None, run_id=None):
        counts: dict[str, int] = {}
        for target in self.targets:
            if step_id is not None and target.step_id != step_id:
                continue
            counts[target.status] = counts.get(target.status, 0) + 1
        return counts

    def list_targets(
        self, run_id, *, step_id=None, status=None, after_target_id=None, limit=500
    ):
        rows = [
            t
            for t in self.targets
            if (step_id is None or t.step_id == step_id)
            and (status is None or t.status == status)
            and (after_target_id is None or t.target_id > after_target_id)
        ]
        rows.sort(key=lambda row: row.target_id)
        return rows[:limit]

    def task_counts_for_run(self, run_id):
        return {"pending": 0}

    def replace_steps(self, run_id, steps):
        self.replaced_steps = list(steps)
        return []

    def get_step(self, step_id):
        for step in self.steps:
            if step.step_id == step_id:
                return step
        return None

    def find_latest_cancelled_run(self, market: str):
        for row in self.runs.values():
            if row.status == "cancelled":
                return row
        return None

    def bump_attempt_round(self, step_id: str, *, now=None) -> int:
        for step in self.steps:
            if step.step_id == step_id:
                step.attempt_round = int(step.attempt_round or 0) + 1
                return step.attempt_round
        raise LookupError(step_id)

    def mark_target(
        self,
        target_id,
        *,
        status,
        exception_code=None,
        evidence=None,
        last_error=None,
        next_retry_at=None,
        now=None,
    ):
        self.marked = getattr(self, "marked", [])
        self.marked.append((target_id, status))
        for target in self.targets:
            if target.target_id == target_id:
                target.status = status
                target.exception_code = exception_code
                if evidence is not None:
                    target.evidence = evidence
                    target.exception_evidence = evidence
                if last_error is not None:
                    target.last_error = last_error
                target.next_retry_at = next_retry_at


class FakeExecutor:
    def __init__(self) -> None:
        self.calls = []

    def submit_step(self, *, run_id, step, targets, attempt: int = 0):
        self.calls.append(
            {
                "run_id": run_id,
                "step": step,
                "targets": list(targets),
                "attempt": attempt,
            }
        )
        return ["task-1"]


def _module(fake_repo: FakeRepository, executor=None) -> DataRecoveryModule:
    module = DataRecoveryModule(None)
    module.repository = fake_repo
    module.executor = executor or FakeExecutor()
    # H9：默认无活动工作单元冲突；冲突场景由具体测试注入 stub 覆盖。
    module.work_unit_conflict_fn = lambda units: []
    module._persist_frozen_snapshots = lambda run_row: None  # type: ignore[method-assign]
    module._cancel_pending_tasks = lambda run_id: None  # type: ignore[method-assign]

    @contextmanager
    def _empty_queue_factory():
        queue = SimpleNamespace(repository=SimpleNamespace(list_tasks=lambda **kwargs: []))
        yield queue

    module.queue_factory = _empty_queue_factory  # type: ignore[method-assign]

    def _stub_resolve(now=None):
        run = next(iter(fake_repo.runs.values()), None)
        return SimpleNamespace(
            cutoff_date=run.cutoff_date if run else None, executable=True
        )

    def _stub_snapshot(*, cutoff_date, now=None):
        run = next(iter(fake_repo.runs.values()), None)
        return (
            SimpleNamespace(snapshot_hash=run.universe_snapshot_hash if run else ""),
            {},
        )

    module.detector = SimpleNamespace(  # type: ignore[assignment]
        resolve_cutoff=_stub_resolve, load_universe_snapshot=_stub_snapshot
    )
    return module




def test_approve_rejects_stale_plan_hash() -> None:
    repo = FakeRepository()
    repo.add_run("rec:r1", status="draft", plan_hash="h-current")
    module = _module(repo)
    with pytest.raises(StalePlanError):
        module.approve(run_id="rec:r1", plan_hash="h-stale", approved_by="u")


def test_approve_draft_transitions_and_is_idempotent() -> None:
    repo = FakeRepository()
    repo.add_run("rec:r1", status="draft", plan_hash="h-current")
    module = _module(repo)
    view = module.approve(run_id="rec:r1", plan_hash="h-current", approved_by="u", now=NOW)
    assert view.status == "running"
    assert repo.transitions == [("draft", "approved"), ("approved", "running")]
    # 重复确认幂等：不再产生新转换。
    module.approve(run_id="rec:r1", plan_hash="h-current", approved_by="u", now=NOW)
    assert repo.transitions == [("draft", "approved"), ("approved", "running")]


def test_approve_unknown_run_raises_lookup_error() -> None:
    module = _module(FakeRepository())
    with pytest.raises(LookupError):
        module.approve(run_id="missing", plan_hash="h", approved_by=None)


def test_control_pause_resume_and_invalid() -> None:
    repo = FakeRepository()
    repo.add_run("rec:r1", status="running", plan_hash="h1")
    module = _module(repo)
    assert module.control("rec:r1", "pause", actor="u", now=NOW).status == "paused"
    assert module.control("rec:r1", "resume", actor="u", now=NOW).status == "running"
    with pytest.raises(ValueError):
        module.control("rec:r1", "explode", actor="u", now=NOW)


def test_control_cancel_keeps_gate_degraded_with_blocking_gaps() -> None:
    repo = FakeRepository()
    repo.add_run("rec:r1", status="running", plan_hash="h1")
    repo.blocking = True
    module = _module(repo)
    view = module.control("rec:r1", "cancel", actor="u", now=NOW)
    assert view.status == "cancelled"
    assert ("rec:r1", "degraded") in repo.gate_updates


def test_advance_completes_freeze_phases_and_submits_bars() -> None:
    repo = FakeRepository()
    repo.add_run("rec:r1", status="running", plan_hash="h1")
    repo.add_step("P0", "orchestration")
    repo.add_step("P1", "market_calendar", deps=("P0",))
    repo.add_step("P2", "orchestration", deps=("P1",))
    step3 = repo.add_step("P3", "market_bars", deps=("P2",))
    target = _bar_target()
    repo.targets.append(
        SimpleNamespace(
            target_id="rt:bars:600000",
            step_id=step3.step_id,
            data_domain="market_bars",
            asset_id=target.asset_id,
            gap_start_at=target.gap_start_at,
            gap_end_at=target.gap_end_at,
            granularity="1d",
            expected_count=4,
            status="pending",
            next_retry_at=None,
        )
    )
    step3.target_count = 1
    executor = FakeExecutor()
    module = _module(repo, executor)
    view = module.advance("rec:r1", now=NOW)
    statuses = {step.phase: step.status for step in repo.steps}
    assert statuses["P0"] == "completed"
    assert statuses["P1"] == "completed"
    assert statuses["P2"] == "completed"
    assert statuses["P3"] == "running"
    assert len(executor.calls) == 1
    call = executor.calls[0]
    assert call["run_id"] == "rec:r1"
    assert [t.asset_id for t in call["targets"]] == ["ashare:600000"]
    assert view.status == "running"


def test_recovery_executor_partitions_and_idempotency_key() -> None:
    class RecordingQueue:
        def __init__(self) -> None:
            self.enqueued = []

        def enqueue(self, **kwargs):
            self.enqueued.append(kwargs)
            return SimpleNamespace(task_id="t")

    queue = RecordingQueue()

    @contextmanager
    def factory():
        yield queue

    executor = RecoveryExecutor(factory)
    # 同一资产的多段窗口才会触发单资产分区的 20 目标上限。
    step = PlanStep(
        phase="P3",
        data_domain="market_bars",
        targets=tuple(_bar_target(day=1 + i) for i in range(25)),
    )
    submitted = executor.submit_step(
        run_id="rec:r1", step=step, targets=list(step.targets)
    )
    assert len(submitted) == 2  # 25 个目标按 20 分区
    assert len(queue.enqueued) == 2
    first = queue.enqueued[0]
    assert first["job_name"] == "recovery.ashare.bars"
    assert first["max_attempts"] == 1
    assert first["idempotency_key"].startswith("recovery:rec:r1:")
    payload = first["payload"]
    assert payload["recovery_run_id"] == "rec:r1"
    assert payload["data_domain"] == "market_bars"
    assert len(payload["targets"]) == 20
    # H9：payload 携带与目标一一对应的规范化工作单元键。
    units = payload["work_units"]
    assert len(units) == 20
    assert units[0] == "market_bars_backfill:ashare:600000:2026-08-01..2026-08-21"
    assert len(set(units)) == 20
    assert len(queue.enqueued[1]["payload"]["targets"]) == 5


def test_build_work_unit_normalizes_asset_market_and_window() -> None:
    """H9：资产归一化、市场级目标用市场标识、单日窗口折叠为单日期。"""

    asset_target = GapTarget(
        data_domain="market_bars",
        asset_id=" AShare:600000 ",
        gap_start_at=date(2026, 8, 18),
        gap_end_at=date(2026, 8, 21),
    )
    assert (
        build_work_unit(asset_target)
        == "market_bars_backfill:ashare:600000:2026-08-18..2026-08-21"
    )
    market_target = GapTarget(
        data_domain="events",
        asset_id=None,
        gap_start_at=date(2026, 8, 18),
        gap_end_at=date(2026, 8, 18),
    )
    assert build_work_unit(market_target) == "event_refresh:ashare:2026-08-18"



def _full_plan(repo, *, p3_status="pending"):
    repo.add_run("rec:r1", status="running", plan_hash="h1")
    repo.add_step("P0", "orchestration")
    repo.add_step("P1", "market_calendar", deps=("P0",))
    repo.add_step("P2", "orchestration", deps=("P1",))
    p3 = repo.add_step("P3", "market_bars", deps=("P2",))
    p3.status = p3_status
    return p3


def _target_row(step_id, asset="ashare:600000", status="pending", retry=None):
    return SimpleNamespace(
        target_id=f"rt:{asset}",
        step_id=step_id,
        data_domain="market_bars",
        asset_id=asset,
        gap_start_at=date(2026, 8, 18),
        gap_end_at=date(2026, 8, 21),
        granularity="1d",
        expected_count=4,
        status=status,
        next_retry_at=retry,
        exception_code=None,
        evidence=None,
        exception_evidence={},
        last_error=None,
    )


def test_approve_triggers_first_advance_and_submits_collection() -> None:
    repo = FakeRepository()
    repo.add_run("rec:r1", status="draft", plan_hash="h1")
    _full_plan(repo)
    p3 = repo.steps[-1]
    repo.targets.append(_target_row(p3.step_id))
    p3.target_count = 1
    executor = FakeExecutor()
    module = _module(repo, executor)
    module.approve(run_id="rec:r1", plan_hash="h1", approved_by="u", now=NOW)
    statuses = {s.phase: s.status for s in repo.steps}
    assert statuses["P0"] == "completed"
    assert statuses["P2"] == "completed"
    assert statuses["P3"] == "running"
    assert len(executor.calls) == 1  # 批准即提交首批分区（C1）


def test_advance_completes_running_step_when_all_targets_terminal() -> None:
    repo = FakeRepository()
    p3 = _full_plan(repo, p3_status="running")
    for asset in ("ashare:600000", "ashare:000001"):
        row = _target_row(p3.step_id, asset=asset, status="completed")
        repo.targets.append(row)
    p4 = repo.add_step("P4", "fundamentals", deps=("P2", "P3"))
    p4_target = _target_row(p4.step_id, asset="ashare:600000", status="pending")
    p4_target.data_domain = "fundamentals"
    repo.targets.append(p4_target)
    p4.target_count = 1
    executor = FakeExecutor()
    module = _module(repo, executor)
    module.advance("rec:r1", now=NOW)
    statuses = {s.phase: s.status for s in repo.steps}
    assert statuses["P3"] == "completed"  # 全目标终态后收敛（C1b）
    assert statuses["P4"] == "running"  # 下游继续推进并提交自己的分区
    assert len(executor.calls) == 1
    assert executor.calls[0]["attempt"] == 0
    assert [
        t.asset_id for t in executor.calls[0]["targets"]
    ] == ["ashare:600000"]


def test_advance_retries_due_failed_targets_with_retry_key() -> None:
    repo = FakeRepository()
    p3 = _full_plan(repo, p3_status="running")
    done = _target_row(p3.step_id, status="completed")
    failed = _target_row(
        p3.step_id, asset="ashare:000001", status="failed", retry=NOW - timedelta(minutes=1)
    )
    repo.targets.extend([done, failed])
    executor = FakeExecutor()
    module = _module(repo, executor)
    module.advance("rec:r1", now=NOW)
    assert p3.attempt_round == 1  # 轮次自增一次，冷却推迟防止同批重复重提
    assert executor.calls and executor.calls[0]["attempt"] == 1
    assert [t.asset_id for t in executor.calls[0]["targets"]] == ["ashare:000001"]
    # 未到冷却期的失败目标不重提。
    module.advance("rec:r1", now=NOW)  # 重试已进入新冷却窗口，不再重复提交
    assert len(executor.calls) == 1
    assert p3.attempt_round == 1


def test_p5_waits_for_every_parallel_p4_step_and_rearms_after_restart() -> None:
    repo = FakeRepository()
    repo.add_run("rec:r1", status="running", plan_hash="h1")
    for phase, domain in (
        ("P0", "orchestration"),
        ("P1", "market_calendar"),
        ("P2", "orchestration"),
        ("P3", "market_bars"),
    ):
        row = repo.add_step(phase, domain)
        row.status = "completed"
    events = repo.add_step("P4", "events", deps=("P3",))
    events.status = "completed"
    valuation = repo.add_step("P4", "valuation", deps=("P3",))
    valuation.status = "running"
    target = _target_row(
        valuation.step_id,
        asset="ashare:600000",
        status="pending",
        retry=NOW + timedelta(hours=1),
    )
    target.data_domain = "valuation"
    repo.targets.append(target)
    p5 = repo.add_step("P5", "orchestration", deps=("P4",))
    p5.status = "running"  # 模拟进程在内联 P5 中重启。

    module = _module(repo)
    module.advance("rec:r1", now=NOW)

    assert p5.status == "pending"
    assert valuation.status == "running"


def test_reconcile_exhausted_targets_waits_for_active_work_then_terminalizes() -> None:
    repo = FakeRepository()
    p3 = _full_plan(repo, p3_status="running")
    p3.attempt_round = MAX_FACT_RETRY_ROUNDS
    completed = _target_row(
        p3.step_id,
        asset="ashare:600000",
        status="failed",
        retry=NOW - timedelta(minutes=1),
    )
    unavailable = _target_row(
        p3.step_id,
        asset="ashare:000001",
        status="failed",
        retry=NOW - timedelta(minutes=1),
    )
    active = _target_row(
        p3.step_id,
        asset="ashare:000002",
        status="pending",
    )
    repo.targets.extend([completed, unavailable, active])
    p3.target_count = 3
    module = _module(repo)
    module.verifier = SimpleNamespace(  # type: ignore[assignment]
        verify_target=lambda row: (
            ("completed", None, {"verified": True})
            if row.asset_id == "ashare:600000"
            else ("failed_transient", None, {"missing": True})
        )
    )
    module.work_unit_conflict_fn = lambda units: [
        unit for unit in units if "ashare:000002" in unit
    ]

    module.advance("rec:r1", now=NOW)

    assert completed.status == "completed"
    assert unavailable.status == "exception"
    assert unavailable.exception_code == "source_unavailable"
    assert active.status == "pending"
    assert p3.status == "running"

    module.work_unit_conflict_fn = lambda units: []
    module.advance("rec:r1", now=NOW)

    assert active.status == "exception"
    assert active.exception_code == "source_unavailable"
    assert p3.status == "completed"


def test_quality_acceptance_ignores_recommendation_readiness_and_advances() -> None:
    repo = FakeRepository()
    repo.add_run("rec:r1", status="running", plan_hash="h1")
    p5 = repo.add_step("P5", "orchestration", deps=("P4",))
    p6 = repo.add_step("P6", "orchestration", deps=("P5",))
    p5.status = "completed"
    module = _module(repo)
    module.verifier = SimpleNamespace(  # type: ignore[assignment]
        final_data_gate_check=lambda **kwargs: {
            "executable": True,
            "reasons": [],
            "target_counts": {"completed": 1, "exception": 0},
        }
    )

    view = module.advance("rec:r1", now=NOW)

    assert view.status == "verifying"
    assert p6.status == "completed"


def test_control_resume_from_attention_required_advances() -> None:
    repo = FakeRepository()
    repo.add_run("rec:r1", status="attention_required", plan_hash="h1")
    p5 = repo.add_step("P5", "orchestration", deps=("P4",))
    p6 = repo.add_step("P6", "orchestration", deps=("P5",))
    p5.status = "completed"
    p6.status = "running"
    module = _module(repo)
    module.verifier = SimpleNamespace(  # type: ignore[assignment]
        final_data_gate_check=lambda **kwargs: {
            "executable": True,
            "reasons": [],
            "target_counts": {"completed": 1, "exception": 0},
        }
    )

    view = module.control("rec:r1", "resume", actor="u", now=NOW)

    assert view.status == "verifying"
    assert p6.status == "completed"
    assert ("running", "verifying") in repo.transitions


def test_advance_reenters_from_attention_required() -> None:
    repo = FakeRepository()
    repo.add_run("rec:r1", status="attention_required", plan_hash="h1")
    p5 = repo.add_step("P5", "orchestration", deps=("P4",))
    p6 = repo.add_step("P6", "orchestration", deps=("P5",))
    p5.status = "completed"
    module = _module(repo)
    module.verifier = SimpleNamespace(  # type: ignore[assignment]
        final_data_gate_check=lambda **kwargs: {
            "executable": True,
            "reasons": [],
            "target_counts": {"completed": 1, "exception": 0},
        }
    )

    view = module.advance("rec:r1", now=NOW)

    assert view.status == "verifying"
    assert p6.status == "completed"


def test_submit_derived_refresh_reuses_retry_attempt_after_failure() -> None:
    repo = FakeRepository()
    repo.add_run("rec:r1", status="running", plan_hash="h1")
    p7 = repo.add_step("P7", "orchestration")
    p7.status = "pending"
    p7.task_params = {"pipeline": ["data_quality_refresh"]}

    def _queue_with_failed_task(status):
        task = SimpleNamespace(attempts=1, status=status)

        @contextmanager
        def factory():
            yield SimpleNamespace(
                repository=SimpleNamespace(list_tasks=lambda **kwargs: [task])
            )

        return factory

    module = _module(repo)
    module.verifier = SimpleNamespace(  # type: ignore[assignment]
        final_data_gate_check=lambda **kwargs: {"executable": True, "reasons": []}
    )

    # 首次无历史失败任务：attempt=0
    module.queue_factory = _queue_with_failed_task("completed")
    module.advance("rec:r1", now=NOW)
    assert any(call["attempt"] == 0 for call in module.executor.calls)

    # 失败后重提：attempt 前进到 1，生成新幂等键重新执行
    module.queue_factory = _queue_with_failed_task("failed")
    module.executor.calls.clear()
    module.advance("rec:r1", now=NOW)
    assert any(call["attempt"] >= 1 for call in module.executor.calls)


def test_finalize_run_completes_batch_and_opens_gate() -> None:
    repo = FakeRepository()
    repo.add_run("rec:r1", status="verifying", plan_hash="h1")
    repo.add_step("P8", "orchestration")
    module = _module(repo)
    module.verifier = SimpleNamespace(  # type: ignore[assignment]
        final_gate_check=lambda **kwargs: {"executable": True, "reasons": []}
    )
    view = module.advance("rec:r1", now=NOW)
    assert view.status == "completed"  # C2：P8 真实收敛
    assert ("rec:r1", "open") in repo.gate_updates
    assert repo.steps[0].status == "completed"


def test_finalize_with_blocking_gaps_goes_attention() -> None:
    repo = FakeRepository()
    repo.add_run("rec:r1", status="verifying", plan_hash="h1")
    repo.add_step("P8", "orchestration")
    repo.blocking = True
    module = _module(repo)
    module.verifier = SimpleNamespace(  # type: ignore[assignment]
        final_gate_check=lambda **kwargs: {"executable": True, "reasons": []}
    )
    view = module.advance("rec:r1", now=NOW)
    assert view.status == "attention_required"
    assert view.gate_status == "degraded"


def test_on_task_finished_verifies_only_partition_scope() -> None:
    repo = FakeRepository()
    repo.add_run("rec:r1", status="running", plan_hash="h1")
    p3 = repo.add_step("P3", "market_bars", deps=("P2",))
    p3.status = "running"
    mine = _target_row(p3.step_id, asset="ashare:600000", status="pending")
    other = _target_row(p3.step_id, asset="ashare:999999", status="pending")
    repo.targets.extend([mine, other])
    module = _module(repo)
    module.verifier = SimpleNamespace(  # type: ignore[assignment]
        verify_target=lambda row: ("completed", None, {})
    )
    payload = {
        "recovery_run_id": "rec:r1",
        "recovery_step_id": p3.step_id,
        "data_domain": "market_bars",
        "targets": [
            {
                "data_domain": "market_bars",
                "asset_id": "ashare:600000",
                "gap_start_at": "2026-08-18",
                "gap_end_at": "2026-08-21",
            }
        ],
    }
    result = module.on_task_finished(payload, success=True, now=NOW)
    assert result == {"verified": 1, "failed": 0}
    marked = dict(getattr(repo, "marked", []))
    assert marked.get(mine.target_id) == "completed"
    assert other.target_id not in marked  # 其他分区不被本次回调污染（H7）
    assert other.status == "pending"
    assert p3.status == "running"  # 还有未决目标，步骤不能完成




def test_approve_rejects_when_calendar_changed_since_preview() -> None:
    repo = FakeRepository()
    repo.add_run("rec:r1", status="draft", plan_hash="h1")
    module = _module(repo)
    module.detector = SimpleNamespace(  # type: ignore[assignment]
        resolve_cutoff=lambda now=None: SimpleNamespace(
            cutoff_date=date(2026, 8, 20), executable=True
        )
    )
    with pytest.raises(StalePlanError):
        module.approve(run_id="rec:r1", plan_hash="h1", approved_by="u", now=NOW)


def test_approve_rejects_when_universe_changed_since_preview() -> None:
    repo = FakeRepository()
    repo.add_run("rec:r1", status="draft", plan_hash="h1")
    module = _module(repo)
    module.detector = SimpleNamespace(  # type: ignore[assignment]
        resolve_cutoff=lambda now=None: SimpleNamespace(
            cutoff_date=date(2026, 8, 21), executable=True
        ),
        load_universe_snapshot=lambda *, cutoff_date, now=None: (
            SimpleNamespace(snapshot_hash="u-changed"),
            {},
        ),
    )
    with pytest.raises(StalePlanError):
        module.approve(run_id="rec:r1", plan_hash="h1", approved_by="u", now=NOW)


def test_executor_retry_attempt_appends_suffix_to_key() -> None:
    """H6 真实路径：attempt>0 时幂等键带 :r{n}，与首轮键不同。"""

    class RecordingQueue:
        def __init__(self) -> None:
            self.enqueued = []

        def enqueue(self, **kwargs):
            self.enqueued.append(kwargs)
            return SimpleNamespace(task_id="t")

    queue = RecordingQueue()

    @contextmanager
    def factory():
        yield queue

    executor = RecoveryExecutor(factory)
    step = PlanStep(
        phase="P3",
        data_domain="market_bars",
        targets=(_bar_target(),),
    )
    first = executor.submit_step(run_id="rec:r1", step=step, targets=list(step.targets))
    retry = executor.submit_step(
        run_id="rec:r1", step=step, targets=list(step.targets), attempt=1
    )
    assert not first[0]["idempotency_key"].endswith(":r1")
    assert retry[0]["idempotency_key"].endswith(":r1")
    assert (
        first[0]["idempotency_key"] != retry[0]["idempotency_key"]
    )  # 不同键 → 队列不会把重试折叠成首轮终态任务
    assert len(queue.enqueued) == 2


def test_advance_defers_conflicting_targets_and_records_deferred_count() -> None:
    """H9：与活动任务工作单元重叠的目标不提交，deferred 计数入摘要。"""

    repo = FakeRepository()
    p3 = _full_plan(repo)
    blocked = _target_row(p3.step_id, asset="ashare:600000")
    clean = _target_row(p3.step_id, asset="ashare:000001")
    repo.targets.extend([blocked, clean])
    p3.target_count = 2
    executor = FakeExecutor()
    module = _module(repo, executor)
    conflict_unit = build_work_unit(
        GapTarget(
            data_domain="market_bars",
            asset_id="ashare:600000",
            gap_start_at=date(2026, 8, 18),
            gap_end_at=date(2026, 8, 21),
            granularity="1d",
        )
    )

    def conflict_stub(work_units):
        matched = [unit for unit in work_units if unit == conflict_unit]
        assert len(matched) == 1  # 查询只应收到归一化去重后的工作单元键
        return matched

    module.work_unit_conflict_fn = conflict_stub

    module.advance("rec:r1", now=NOW)

    # 冲突目标让路，非冲突目标照常提交。
    assert len(executor.calls) == 1
    assert [t.asset_id for t in executor.calls[0]["targets"]] == ["ashare:000001"]
    # 提交摘要记录 deferred 数量与冲突单元。
    summary = module.last_collection_submit_summaries[p3.step_id]
    assert summary == {
        "submitted": 1,
        "deferred": 1,
        "work_unit_conflicts": [conflict_unit],
    }
    # 让路目标保持 pending，等待下一轮 advance 重试。
    assert blocked.status == "pending"


def test_work_unit_probe_failure_fails_open_and_submits_all() -> None:
    """H9：冲突探针异常时 fail-open，不阻塞补跑采集。"""

    repo = FakeRepository()
    p3 = _full_plan(repo)
    repo.targets.extend(
        [
            _target_row(p3.step_id, asset="ashare:600000"),
            _target_row(p3.step_id, asset="ashare:000001"),
        ]
    )
    p3.target_count = 2
    executor = FakeExecutor()
    module = _module(repo, executor)

    def broken_probe(work_units):
        raise RuntimeError("probe down")

    module.work_unit_conflict_fn = broken_probe

    module.advance("rec:r1", now=NOW)

    assert len(executor.calls) == 1
    assert len(executor.calls[0]["targets"]) == 2
    summary = module.last_collection_submit_summaries[p3.step_id]
    assert summary["submitted"] == 1
    assert summary["deferred"] == 0
    assert summary["work_unit_conflicts"] == []



def test_reconcile_defers_retry_targets_conflicting_with_active_tasks() -> None:
    """H9×重试：活动任务占用同一工作单元时，失败目标本轮让路不 bump。"""

    repo = FakeRepository()
    p3 = _full_plan(repo, p3_status="running")
    done = _target_row(p3.step_id, status="completed")
    failed = _target_row(
        p3.step_id, asset="ashare:000001", status="failed", retry=NOW - timedelta(minutes=1)
    )
    repo.targets.extend([done, failed])
    executor = FakeExecutor()
    module = _module(repo, executor)
    module.work_unit_conflict_fn = lambda units: list(units)  # 全部冲突
    module.advance("rec:r1", now=NOW)
    assert p3.attempt_round == 0  # 未自增轮次
    assert executor.calls == []  # 未提交任何任务
    assert p3.status == "running"  # 保持等待下一轮 advance 重试




def test_on_task_finished_matches_real_orm_datetime_scope() -> None:
    """复审 CRITICAL：ORM datetime(str 含空格) 与 payload isoformat(含 T)
    必须经统一规范化后匹配，否则整个分区被跳过。"""

    repo = FakeRepository()
    repo.add_run("rec:r1", status="running", plan_hash="h1")
    p3 = repo.add_step("P3", "market_bars", deps=("P2",))
    p3.status = "running"
    mine = _target_row(p3.step_id, asset="ashare:600000", status="pending")
    # 模拟真实 ORM 行：timestamptz 读回的 datetime 而非 date
    mine.gap_start_at = datetime(2026, 8, 18, 0, 0, tzinfo=UTC)
    mine.gap_end_at = datetime(2026, 8, 21, 0, 0, tzinfo=UTC)
    other = _target_row(p3.step_id, asset="ashare:999999", status="pending")
    repo.targets.extend([mine, other])
    module = _module(repo)
    module.verifier = SimpleNamespace(  # type: ignore[assignment]
        verify_target=lambda row: ("completed", None, {})
    )
    payload = {
        "recovery_run_id": "rec:r1",
        "recovery_step_id": p3.step_id,
        "data_domain": "market_bars",
        # executor 写入格式：isoformat() 含 T
        "targets": [
            {
                "data_domain": "market_bars",
                "asset_id": "ashare:600000",
                "gap_start_at": "2026-08-18T00:00:00+00:00",
                "gap_end_at": "2026-08-21T00:00:00+00:00",
            }
        ],
    }
    result = module.on_task_finished(payload, success=True, now=NOW)
    assert result == {"verified": 1, "failed": 0}  # 不再因格式失配跳过分区
    assert dict(getattr(repo, "marked", [])).get(mine.target_id) == "completed"


def test_on_task_finished_completes_p7_derived_step_on_success() -> None:
    repo = FakeRepository()
    repo.add_run("rec:r1", status="running", plan_hash="h1")
    p7 = repo.add_step("P7", "orchestration")
    p7.status = "running"
    derived = _target_row(p7.step_id, asset=None, status="pending")
    derived.data_domain = "orchestration"
    derived.asset_id = None
    repo.targets.append(derived)
    module = _module(repo)

    result = module.on_task_finished(
        {
            "recovery_run_id": "rec:r1",
            "recovery_step_id": p7.step_id,
            "data_domain": "orchestration",
            "targets": [
                {
                    "data_domain": "orchestration",
                    "asset_id": None,
                    "gap_start_at": "2026-08-25",
                    "gap_end_at": "2026-08-25",
                }
            ],
        },
        success=True,
        now=NOW,
    )

    assert result == {"verified": 0, "failed": 0}
    assert p7.status == "completed"


def test_on_task_finished_keeps_p7_pending_on_failure() -> None:
    repo = FakeRepository()
    repo.add_run("rec:r1", status="running", plan_hash="h1")
    p7 = repo.add_step("P7", "orchestration")
    p7.status = "running"
    module = _module(repo)

    module.on_task_finished(
        {
            "recovery_run_id": "rec:r1",
            "recovery_step_id": p7.step_id,
            "data_domain": "orchestration",
            "targets": [],
        },
        success=False,
        now=NOW,
    )

    # 失败后 advance() 立即用新 attempt 重提派生任务，P7 回到 running。
    assert p7.status == "running"


def test_iter_targets_pages_beyond_single_limit() -> None:
    """复审 HIGH：目标超过单页上限时全部可遍历，不再截断在 1000 条。"""

    repo = FakeRepository()
    repo.add_run("rec:r1", status="running", plan_hash="h1")
    p3 = repo.add_step("P3", "market_bars", deps=("P2",))
    for index in range(1200):
        row = _target_row(p3.step_id, asset=f"ashare:{index:06d}", status="pending")
        repo.targets.append(row)
    module = _module(repo)
    seen = [row.target_id for row in module._iter_targets("rec:r1", step_id=p3.step_id)]
    assert len(seen) == 1200  # 单页 500，键集分页取全量
    assert len(set(seen)) == 1200  # 无重复无遗漏
