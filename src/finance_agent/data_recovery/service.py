"""DataRecoveryModule：停跑恢复补跑的唯一对外门面。

规格 5.1：对外只暴露 preview / approve / control / get（外加批次列表与
启动扫描适配入口）。缺口规则、任务分区、状态持久化、事实验收和门控
细节全部隐藏在本包内；HTTP、CLI、MCP、控制台和调度器都不得绕过门面
直接操作补跑状态。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from finance_agent.data_recovery.executor import (
    RecoveryExecutor,
    build_work_unit,
)
from finance_agent.data_recovery.gap_detector import (
    DEFAULT_ASHARE_UNIVERSE_ID,
    DomainGapQueries,
    GapDetector,
)
from finance_agent.data_recovery.models import (
    DataDomain,
    GapTarget,
    RecoveryRunView,
)
from finance_agent.data_recovery.plan_builder import PlanBuilder
from finance_agent.data_recovery.repository import (
    RecoveryRepository,
)
from finance_agent.data_recovery.state_machine import InvalidRecoveryTransition
from finance_agent.data_recovery.verifier import RecoveryVerifier

JsonDict = dict[str, Any]

logger = logging.getLogger(__name__)

# 缺口扫描起点推导（规格 6.3）：各数据域最早成功水位给出真实覆盖边界；
# 真实老水位不截断（复审 H4-①），固定回看窗口仅用于完全无水位的回退。
FALLBACK_SCAN_LOOKBACK_DAYS = 366


def _scope_timestamp(value) -> str:
    """payload ISO 字符串与 ORM datetime 的统一比较形式（复审 CRITICAL）。

    executor 写 payload 用 isoformat()（含 T），ORM 读回 str() 为空格分隔，
    直接字符串比较必然失配导致整个分区被跳过；统一归一到 UTC 日期粒度
    （补跑目标均为日粒度边界）后比较，不受 timestamptz 读写偏移表示漂移影响。
    """

    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            try:
                value = date.fromisoformat(value)
            except ValueError:
                return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        else:
            value = value.astimezone(UTC)
        return value.date().isoformat()
    return value.isoformat()

# 参与扫描起点推导的数据域（交易日历 + K 线 + 全部 P4 事实域）。
SCAN_WATERMARK_DOMAINS: tuple[str, ...] = tuple(
    sorted(
        {
            DataDomain.MARKET_CALENDAR,
            DataDomain.MARKET_BARS,
            *DataDomain.PARALLEL_FACT_DOMAINS,
        }
    )
)

#: 阻塞派生链路的核心数据域（规格 13.1）。
BLOCKING_DOMAINS = frozenset(
    {DataDomain.MARKET_BARS, DataDomain.FUNDAMENTALS, DataDomain.VALUATION}
)


class StalePlanError(RuntimeError):
    """确认时资产池或交易日历版本已变化（规格 6.2）。"""

    def __init__(self, run_id: str, reason: str) -> None:
        self.run_id = run_id
        self.reason = reason
        super().__init__(f"计划已过期: {run_id} ({reason})")


def default_queue_factory():
    """默认队列工厂：每个提交动作独立事务。"""

    from contextlib import contextmanager

    from finance_agent.scheduler.persistent_task_queue import PersistentTaskQueue
    from finance_agent.storage.db import create_session_factory, session_scope

    session_factory = create_session_factory()

    @contextmanager
    def factory():
        with session_scope(session_factory) as session:
            yield PersistentTaskQueue(session)

    return factory


def default_readiness_fn() -> JsonDict:
    """读取最新推荐运行并复用现有 readiness 评估（规格 12.2）。"""

    from finance_agent.recommendations.readiness import (
        evaluate_recommendation_readiness,
    )
    from finance_agent.storage.db import create_session_factory, session_scope
    from finance_agent.storage.repositories import RecommendationRepository

    with session_scope(create_session_factory()) as session:
        repo = RecommendationRepository(session)
        runs = repo.list_available_runs_since(since=datetime.now().astimezone() - timedelta(days=7), limit=1)
        run = runs[0] if runs else None
        recommendations = []
        if run is not None:
            getter = getattr(repo, "list_recommendations", None)
            if callable(getter):
                recommendations = list(getter(run.run_id))[:1]
        result = evaluate_recommendation_readiness(run=run, recommendations=recommendations)
        return result.to_dict()




#: 事实验证未通过时的最大重提轮次（与 reconcile 上限一致，规格 11.2）。
MAX_FACT_RETRY_ROUNDS = 5

#: P7 派生链路重试上限。派生依赖前置候选池缺失属于真实阻塞，
#: 未建立时重试只会耗尽请求，达到上限后转入 attention_required。
DERIVED_MAX_RETRY_ROUNDS = 3


def _fully_completed_phases(steps) -> set[str]:
    """只有同阶段的所有并行步骤都完成，才能满足下游阶段依赖。"""

    statuses: dict[str, list[str]] = {}
    for step in steps:
        statuses.setdefault(str(step.phase), []).append(str(step.status))
    return {
        phase
        for phase, phase_statuses in statuses.items()
        if phase_statuses and all(status == "completed" for status in phase_statuses)
    }


class DataRecoveryModule:
    """补跑批次门面：preview / approve / control / get + 编排推进。"""

    def __init__(
        self,
        session,
        *,
        market: str = "ashare",
        universe_id: str = DEFAULT_ASHARE_UNIVERSE_ID,
        calendar_refresh: Callable[[], list[JsonDict]] | None = None,
        universe_refresh: Callable[[], JsonDict | None] | None = None,
        queue_factory: Callable[[], Any] | None = None,
        readiness_fn: Callable[[], JsonDict] | None = None,
        work_unit_conflict_fn: Callable[[list[str]], list[str]] | None = None,
    ) -> None:
        self.session = session
        self.market = market
        self.repository = RecoveryRepository(session)
        self.detector = GapDetector(
            session,
            market=market,
            universe_id=universe_id,
            calendar_refresh=calendar_refresh,
            universe_refresh=universe_refresh,
        )
        self.queries = DomainGapQueries(session, market=market)
        self.builder = PlanBuilder(self.queries)
        self.queue_factory = queue_factory or default_queue_factory()
        self.executor = RecoveryExecutor(self.queue_factory)
        # 验证器复用同一份生命周期窗口查询（规格 8/16.6）。
        self.verifier = RecoveryVerifier(
            session,
            lifecycle_fn=self.queries.load_asset_lifecycles,
            market=self.market,
        )
        self.readiness_fn = readiness_fn or default_readiness_fn
        # H9：工作单元冲突探针；None 时回退到队列仓储的只读查询实现。
        self.work_unit_conflict_fn = work_unit_conflict_fn
        # 最近一次采集提交摘要（step_id → {submitted, deferred, ...}），
        # deferred 为因工作单元冲突让路未提交的目标数量。
        self.last_collection_submit_summaries: dict[str, JsonDict] = {}

    # ------------------------------------------------------------------
    # 内部：检测并构建计划
    # ------------------------------------------------------------------

    def _scan_plan(self, *, now: datetime | None = None):
        checked_at = now or datetime.now().astimezone()
        cutoff = self.detector.resolve_cutoff(now=checked_at)
        if not cutoff.executable or cutoff.cutoff_date is None:
            return None, cutoff, None, []
        universe, diagnostics = self.detector.load_universe_snapshot(
            cutoff_date=cutoff.cutoff_date, now=checked_at
        )
        if universe is None:
            return None, cutoff, None, []
        scan_start = self._scan_start_date(cutoff_date=cutoff.cutoff_date)
        trading_dates = self._trading_dates_between(scan_start, cutoff.cutoff_date)
        plan = self.builder.build_plan(
            market=self.market,
            cutoff=cutoff,
            universe=universe,
            trading_dates=trading_dates,
            now=checked_at,
        )
        return plan, cutoff, universe, trading_dates

    def _trading_dates_between(self, start_date: date, end_date: date) -> list[date]:

        rows = self.session.execute(
            select_market_calendar_statement(self.market, start_date, end_date)
        ).scalars()
        return [
            row.trade_date
            for row in rows
            if row.is_trading_day and str(row.status or "") != "invalid"
        ]

    def _scan_start_date(self, *, cutoff_date: date) -> date:
        """缺口扫描起点：各数据域最早成功水位推导（规格 6.3）。

        真实老水位不截断（复审 H4-①）；单个新水位不得掩盖同域其他资产
        的旧水位，故取域内 min（复审 H4-②，domain_watermark_earliest）。
        仅当全部数据域都无水位、或水位不早于截止日时回退固定窗口。
        """

        earliest: date | None = None
        for domain in SCAN_WATERMARK_DOMAINS:
            watermark = self.queries.domain_watermark_earliest(data_domain=domain)
            if not isinstance(watermark, datetime):
                continue
            watermark_date = watermark.astimezone().date()
            if earliest is None or watermark_date < earliest:
                earliest = watermark_date
        if earliest is None or earliest >= cutoff_date:
            # 无任何可用水位：退回固定回看窗口。
            return cutoff_date - timedelta(days=FALLBACK_SCAN_LOOKBACK_DAYS)
        return min(earliest, cutoff_date)

    def _iter_targets(self, run_id: str, *, step_id: str | None = None):
        """按 target_id 键集分页遍历全部目标（复审 HIGH：1000 条上限）。"""

        after: str | None = None
        while True:
            rows = self.repository.list_targets(
                run_id, step_id=step_id, after_target_id=after, limit=500
            )
            if not rows:
                return
            yield from rows
            after = rows[-1].target_id
            if len(rows) < 500:
                return



    # ------------------------------------------------------------------
    # 对外接口（规格 5.1）
    # ------------------------------------------------------------------

    def preview(self, *, requested_by: str | None = None, now: datetime | None = None) -> JsonDict:
        """只读生成或复用草稿计划；不写入业务事实表。"""

        plan, cutoff, universe, _ = self._scan_plan(now=now)
        if plan is None:
            return {
                "executable": False,
                "blockers": ["calendar_or_universe_unavailable"],
                "calendar_source": getattr(cutoff, "source", "unknown"),
            }
        has_blocking = any(
            step.targets and step.data_domain in BLOCKING_DOMAINS for step in plan.steps
        )
        row, created = self.repository.create_or_reuse_draft(
            market=plan.market,
            cutoff_date=plan.cutoff_date,
            gap_start_date=plan.gap_start_date,
            universe_id=plan.universe.universe_id,
            universe_snapshot_at=plan.universe.snapshot_at,
            universe_snapshot_hash=plan.universe.snapshot_hash,
            plan_hash=plan.plan_hash,
            summary={
                **plan.to_dict(),
                "universe_diagnostics": {},
            },
            requested_by=requested_by,
            has_blocking_gaps=has_blocking,
            now=now,
        )
        # 步骤持久化：advance/回调全链路都依赖 steps 行存在；
        # 缺失会导致批准后空转假运行（实机验证发现）。
        self.repository.replace_steps(run_id=row.run_id, steps=plan.steps)
        self._materialize_plan_targets(run_id=row.run_id, steps=plan.steps)
        result = plan.to_dict()
        result["run_id"] = row.run_id
        result["run_status"] = row.status
        result["gate_status"] = row.gate_status
        result["created"] = created
        return result

    def approve(
        self,
        *,
        run_id: str,
        plan_hash: str,
        approved_by: str | None = None,
        now: datetime | None = None,
    ) -> RecoveryRunView:
        """携带用户所见 plan_hash 幂等确认；版本变化时抛出 StalePlanError。"""

        occurred_at = now or datetime.now().astimezone()
        row = self.repository.get_run(run_id)
        if row is None:
            raise LookupError(f"补跑批次不存在: {run_id}")
        if str(plan_hash or "") != row.plan_hash:
            raise StalePlanError(run_id, "plan_hash_mismatch")
        if row.status == "draft":
            # 规格 6.2：批准时重校验冻结依赖版本，变化即拒绝并要求重建草稿。
            resolution = self.detector.resolve_cutoff(now=occurred_at)
            if (
                not resolution.executable
                or resolution.cutoff_date != row.cutoff_date
            ):
                raise StalePlanError(run_id, "calendar_changed_since_preview")
            snapshot = self.detector.load_universe_snapshot(
                cutoff_date=row.cutoff_date, now=occurred_at
            )[0]
            if (
                snapshot is None
                or snapshot.snapshot_hash != row.universe_snapshot_hash
            ):
                raise StalePlanError(run_id, "universe_changed_since_preview")
            self._persist_frozen_snapshots(run_row=row)
            self.repository.transition_run(
                run_id,
                "approved",
                actor=approved_by,
                expected_current="draft",
                has_blocking_gaps=self.repository.blocking_gap_exists(run_id),
                now=occurred_at,
            )
            self.repository.transition_run(
                run_id,
                "running",
                actor=approved_by,
                expected_current="approved",
                has_blocking_gaps=self.repository.blocking_gap_exists(run_id),
                now=occurred_at,
            )
        elif row.status in {"running", "paused", "verifying"}:
            pass  # 重复确认幂等：直接返回当前状态。
        else:
            raise InvalidRecoveryTransition(row.status, "approved")
        # 批准即形成可运行闭环：立刻做首次推进（规格 5.1/15.6）。
        self.advance(run_id, now=occurred_at)
        return self.get(run_id)

    def control(
        self,
        run_id: str,
        action: str,
        *,
        actor: str | None = None,
        now: datetime | None = None,
    ) -> RecoveryRunView:
        """pause / resume / cancel（规格 5.1、12.3）。"""

        occurred_at = now or datetime.now().astimezone()
        action = str(action or "").strip().lower()
        if action not in {"pause", "resume", "continue", "cancel"}:
            raise ValueError(f"不支持的控制动作: {action}")
        blocking = self.repository.blocking_gap_exists(run_id)
        if action == "pause":
            self.repository.transition_run(
                run_id, "paused", actor=actor, expected_current="running",
                has_blocking_gaps=blocking, now=occurred_at,
            )
        elif action in {"resume", "continue"}:
            current = self.repository.get_run(run_id).status
            if current == "running":
                return self.get(run_id)
            if current not in {"paused", "attention_required"}:
                raise InvalidRecoveryTransition(current, "running")
            self.repository.transition_run(
                run_id, "running", actor=actor, expected_current=current,
                has_blocking_gaps=blocking, now=occurred_at,
            )
            self.advance(run_id, now=occurred_at)
        else:
            self.repository.transition_run(
                run_id, "cancelled", actor=actor,
                has_blocking_gaps=blocking, now=occurred_at,
            )
            self._cancel_pending_tasks(run_id)
            # 取消后核心缺口仍存在 → 门控保持 degraded；由新批次解除。
            if blocking:
                self.repository.set_gate_status(
                    run_id, "degraded", reason="cancelled_with_blocking_gaps", now=occurred_at
                )
        return self.get(run_id)



    def get(self, run_id: str) -> RecoveryRunView:
        """返回 PostgreSQL 稳定状态（规格 5.1）；实时进度由控制台另行叠加。"""

        row = self.repository.get_run(run_id)
        if row is None:
            raise LookupError(f"补跑批次不存在: {run_id}")
        steps = []
        exceptions = []
        for step in self.repository.get_steps(run_id):
            steps.append(
                {
                    "step_id": step.step_id,
                    "phase": step.phase,
                    "data_domain": step.data_domain,
                    "status": step.status,
                    "depends_on": list(step.depends_on or []),
                    "target_count": int(step.target_count or 0),
                    "completed_count": int(step.completed_count or 0),
                    "retryable_count": int(step.retryable_count or 0),
                    "exception_count": int(step.exception_count or 0),
                    "attempt_round": int(step.attempt_round or 0),
                }
            )
        for target in self.repository.list_targets(run_id, status="exception", limit=100):
            evidence = dict(target.exception_evidence or {})
            evidence.pop("raw_response", None)
            exceptions.append(
                {
                    "target_id": target.target_id,
                    "data_domain": target.data_domain,
                    "asset_id": target.asset_id,
                    "granularity": target.granularity,
                    "exception_code": target.exception_code,
                    "evidence": evidence,
                    "last_error": (target.last_error or "")[:300],
                }
            )
        return RecoveryRunView(
            run_id=row.run_id,
            market=row.market,
            status=str(row.status),
            gate_status=str(row.gate_status),
            cutoff_date=row.cutoff_date,
            gap_start_date=row.gap_start_date,
            universe_id=row.universe_id,
            plan_hash=row.plan_hash,
            steps=tuple(steps),
            quality_result=dict(row.quality_result or {}),
            summary=dict(row.summary or {}),
            live_progress={"tasks": self.repository.task_counts_for_run(run_id)},
            exceptions=tuple(exceptions),
            updated_at=row.updated_at,
        )

    def list_runs(self, *, limit: int = 20) -> list[JsonDict]:
        return [
            {
                "run_id": row.run_id,
                "market": row.market,
                "status": row.status,
                "gate_status": row.gate_status,
                "cutoff_date": row.cutoff_date.isoformat(),
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in self.repository.list_runs(limit=limit)
        ]

    def startup_scan(self, *, now: datetime | None = None) -> JsonDict:
        """规格 13.1：只读扫描 → 创建/复用草稿 → 设置门控。"""

        result = self.preview(now=now)
        if not result.get("executable"):
            return {"scan": "calendar_or_universe_unavailable", **result}
        if not result.get("total_targets"):
            return {"scan": "no_gap", "created_draft": False}
        return {
            "scan": "draft_ready",
            "created_draft": bool(result.get("created")),
            "run_id": result.get("run_id"),
            "gate_status": result.get("gate_status"),
            "blocking_domains_present": any(
                step.get("target_count") and step.get("data_domain") in BLOCKING_DOMAINS
                for step in result.get("steps", [])
            ),
        }



    # ------------------------------------------------------------------
    # 编排推进（advance）：重启续跑的唯一入口
    # ------------------------------------------------------------------

    def advance(self, run_id: str, *, now: datetime | None = None) -> RecoveryRunView:
        """按步骤依赖推进批次；可安全重复调用（幂等）。"""

        occurred_at = now or datetime.now().astimezone()
        run = self.repository.get_run(run_id)
        if run is None or run.status not in {"running", "verifying", "attention_required"}:
            return self.get(run_id)
        if run.status == "attention_required":
            self.repository.transition_run(
                run_id,
                "running",
                has_blocking_gaps=self.repository.blocking_gap_exists(run_id),
                now=occurred_at,
            )
        # P5-P7 是内联编排步骤（由本进程同步执行），不存在可恢复的
        # 外部任务租约；进程若在其执行中重启，需要重新置为 pending 后
        # 按依赖幂等再执行，避免 P6/P7 停在 running 阻塞批次推进。
        for step in self.repository.get_steps(run_id):
            if step.phase in {"P5", "P6", "P7"} and step.status == "running":
                self.repository.mark_step_status(step.step_id, "pending", now=occurred_at)

        progress_made = True
        while progress_made:
            progress_made = False
            current_steps = list(self.repository.get_steps(run_id))
            completed_phases = _fully_completed_phases(current_steps)
            for step in sorted(current_steps, key=lambda s: s.phase):
                if step.status == "running" and step.phase in {"P3", "P4"}:
                    # 分区回调后的收敛与到期失败重试都在这里闭环。
                    handled = self._reconcile_running_collection_step(
                        run_id, step, now=occurred_at
                    )
                    progress_made = progress_made or handled
                if step.status != "pending":
                    continue
                depends = list(step.depends_on or [])
                if any(phase not in completed_phases for phase in depends):
                    continue
                handled = self._execute_step(run_id, step, now=occurred_at)
                progress_made = progress_made or handled
        return self.get(run_id)

    def _execute_step(self, run_id: str, step, *, now: datetime) -> bool:
        """执行单个就绪步骤；返回是否有实际动作。"""

        phase = step.phase
        if phase in {"P0", "P1", "P2"}:
            self.repository.mark_step_status(step.step_id, "completed", now=now)
            return True
        if phase == "P3":
            return self._submit_collection_step(run_id, step, now=now)
        if phase == "P4":
            return self._submit_collection_step(run_id, step, now=now)
        if phase == "P5":
            return self._residual_recheck(run_id, step, now=now)
        if phase == "P6":
            return self._quality_acceptance(run_id, step, now=now)
        if phase == "P7":
            return self._submit_derived_refresh(run_id, step, now=now)
        if phase == "P8":
            return self._finalize_run(run_id, step, now=now)
        self.repository.mark_step_status(step.step_id, "skipped", now=now)
        return True



    def _queue_backed_conflict_probe(self):
        """默认工作单元冲突探针：经队列工厂做只读仓储查询。"""

        factory = self.queue_factory

        def probe(work_units: list[str]) -> list[str]:
            units = [str(unit) for unit in work_units]
            with factory() as queue:
                finder = getattr(
                    queue.repository, "find_active_work_unit_conflicts", None
                )
                if not callable(finder):
                    return []
                return [str(unit) for unit in (finder(units) or [])]

        return probe

    def _defer_active_work_unit_conflicts(self, run_id: str, step_id: str, targets):
        """H9 / 规格 10.4：与活动任务工作单元重叠的目标本轮让路。

        返回 (可提交目标, deferred 数量, 冲突单元列表)。让路目标保持
        pending，等待下一轮 advance 重试；探针异常时 fail-open 放行——
        短暂失去防重的代价低于让补跑采集整体停摆。
        """

        pairs = [
            (target, build_work_unit(target, market=self.market))
            for target in targets
        ]
        try:
            probe = self.work_unit_conflict_fn or self._queue_backed_conflict_probe()
            conflict_set = set(probe(sorted({unit for _, unit in pairs})))
        except Exception:
            logger.exception(
                "工作单元冲突查询失败，本轮放行 run=%s step=%s", run_id, step_id
            )
            return list(targets), 0, []
        runnable = [target for target, unit in pairs if unit not in conflict_set]
        conflicts = sorted({unit for _, unit in pairs if unit in conflict_set})
        deferred = len(targets) - len(runnable)
        if deferred:
            logger.warning(
                "补跑目标让路 run=%s step=%s deferred=%d conflicts=%s",
                run_id,
                step_id,
                deferred,
                conflicts,
            )
        return runnable, deferred, conflicts

    def _submit_collection_step(self, run_id: str, step, *, now: datetime) -> bool:
        """把步骤的未决目标分区提交到持久队列；全部解决后标记完成。

        H9：提交前按规范化工作单元键查询持久队列中的活动任务
        （pending/running），重叠目标不提交（保持 pending 等待下一轮
        advance 重试），deferred 数量记入提交摘要。
        """

        targets = []
        for target in self._iter_targets(run_id, step_id=step.step_id):
            if target.status in {"pending", "failed"}:
                if target.next_retry_at is not None and target.next_retry_at > now:
                    continue
                gap_target = GapTarget(
                    data_domain=target.data_domain,
                    gap_start_at=target.gap_start_at,
                    gap_end_at=target.gap_end_at,
                    granularity=target.granularity,
                    asset_id=target.asset_id,
                    expected_count=int(target.expected_count or 0),
                )
                targets.append(gap_target)
        self.repository.mark_step_status(step.step_id, "running", now=now)
        submitted = []
        deferred = 0
        conflicts: list[str] = []
        if targets:
            plan_step = _PlanStepAdapter(step)
            runnable, deferred, conflicts = self._defer_active_work_unit_conflicts(
                run_id, step.step_id, targets
            )
            if runnable:
                submitted = self.executor.submit_step(
                    run_id=run_id, step=plan_step, targets=runnable
                )
        self.last_collection_submit_summaries[step.step_id] = {
            "submitted": len(submitted),
            "deferred": deferred,
            "work_unit_conflicts": conflicts,
        }
        counts = self.repository.target_status_counts(step_id=step.step_id)
        open_targets = counts.get("pending", 0) + counts.get("failed", 0)
        self.repository.refresh_step_counters(step.step_id)
        if open_targets == 0 and counts.get("running", 0) == 0:
            self.repository.mark_step_status(step.step_id, "completed", now=now)
            return True
        return bool(submitted) or True

    def _residual_recheck(self, run_id: str, step, *, now: datetime) -> bool:
        """P5：重新检测残余缺口并有限重试（规格 11.2）。"""

        self.repository.get_run(run_id)
        self._scan_plan(now=now)
        attempt_round = int(step.attempt_round or 0) + 1
        max_rounds = int((step.task_params or {}).get("max_retry_rounds") or 3)
        for collection in self.repository.get_steps(run_id):
            if collection.phase not in {"P3", "P4"}:
                continue
            counts = self.repository.target_status_counts(step_id=collection.step_id)
            if counts.get("pending", 0) or counts.get("failed", 0):
                # 还有未决目标：冷却到期后由 advance 再次提交。
                self.repository.mark_step_status(
                    collection.step_id,
                    "pending",
                    attempt_round=attempt_round,
                    now=now,
                )
                # P5 不能在采集步骤未决时完成。
                self.repository.mark_step_status(step.step_id, "running", now=now)
                if attempt_round >= max_rounds:
                    blocking = self.repository.blocking_gap_exists(run_id)
                    self.repository.transition_run(
                        run_id,
                        "attention_required",
                        has_blocking_gaps=blocking,
                        summary_patch={"reason": "retry_rounds_exhausted"},
                        now=now,
                    )
                return True
        self.repository.mark_step_status(step.step_id, "completed", now=now)
        return True



    def _quality_acceptance(self, run_id: str, step, *, now: datetime) -> bool:
        """P6：数据质量与推荐就绪度验收。"""

        self.repository.mark_step_status(step.step_id, "running", now=now)
        run = self.repository.get_run(run_id)
        # P6 只验收数据层核心目标；推荐就绪度在 P8 派生重建后的真正终验中复核。
        check = self.verifier.final_data_gate_check(run_id=run_id)
        if check.get("executable") or run is None:
            self.repository.transition_run(
                run_id,
                "verifying",
                expected_current="running",
                has_blocking_gaps=False,
                quality_result=check,
                now=now,
            )
            self.repository.mark_step_status(step.step_id, "completed", now=now)
            return True
        blocking = self.repository.blocking_gap_exists(run_id)
        self.repository.transition_run(
            run_id,
            "attention_required",
            expected_current="running",
            has_blocking_gaps=blocking,
            quality_result=check,
            summary_patch={"attention": [str(r) for r in check.get("reasons", [])]},
            now=now,
        )
        return True

    def _submit_derived_refresh(self, run_id: str, step, *, now: datetime) -> bool:
        """P7：只提交一次冻结截止日对应的最新派生链路（规格 2 决策 8）。"""

        if int(step.attempt_round or 0) >= DERIVED_MAX_RETRY_ROUNDS:
            blocking = self.repository.blocking_gap_exists(run_id)
            self.repository.mark_step_status(step.step_id, "failed", now=now)
            self.repository.transition_run(
                run_id,
                "attention_required",
                expected_current="running",
                has_blocking_gaps=blocking,
                summary_patch={"reason": "derived_dependency_missing"},
                now=now,
            )
            return True

        counts = self.repository.task_counts_for_run(run_id)
        derived_submitted = False
        for collection in self.repository.get_steps(run_id):
            if collection.phase == "P7" and collection.status in {"running", "completed"}:
                derived_submitted = True
        if derived_submitted and counts.get("running", 0) == 0 and counts.get("pending", 0) == 0:
            self.repository.mark_step_status(step.step_id, "completed", now=now)
            return True
        payload_step = _PlanStepAdapter(step)
        # P7 派生任务失败后，固定幂等键的任务已终态，直接重提同键不会执行。
        # 检测已有失败/取消的派生任务，用下一轮 attempt 生成新幂等键重跑。
        attempt = 0
        try:
            with self.queue_factory() as queue:
                existing = queue.repository.list_tasks(
                    job_name="recovery.ashare.derived",
                    payload_key="recovery_run_id",
                    payload_value=str(run_id),
                    statuses=("pending", "running", "failed", "cancelled"),
                    limit=100,
                )
                failed_attempts = [
                    int(task.attempts or 0)
                    for task in existing
                    if task.status in {"failed", "cancelled"}
                ]
                if failed_attempts:
                    attempt = max(failed_attempts) + 1
        except Exception:
            logger.exception("读取 P7 派生任务历史失败，使用基础幂等键提交")
            attempt = 0
        submitted = self.executor.submit_step(
            run_id=run_id,
            step=payload_step,
            targets=[
                GapTarget(
                    data_domain=DataDomain.ORCHESTRATION,
                    gap_start_at=now,
                    gap_end_at=now,
                    granularity="derived",
                    asset_id=None,
                    expected_count=0,
                )
            ],
            attempt=attempt,
        )
        self.repository.mark_step_status(step.step_id, "running", now=now)
        return bool(submitted)



    def on_task_finished(self, payload, *, success, now=None):
        """调度器完成补跑任务后的回调：逐目标事实验证并推进批次。

        任务成功不等于目标完成：这里重新读取事实表逐目标判定（规格 11.3）。
        """

        occurred_at = now or datetime.now().astimezone()
        payload = payload or {}
        run_id = str(payload.get('recovery_run_id') or '')
        step_id = str(payload.get('recovery_step_id') or '')
        if not run_id:
            return None
        verified = 0
        failed = 0
        partition_scopes = {
            (
                str(item.get('data_domain') or ''),
                str(item.get('asset_id') or ''),
                _scope_timestamp(item.get('gap_start_at') or ''),
                _scope_timestamp(item.get('gap_end_at') or ''),
            )
            for item in (payload.get('targets') or [])
        }
        targets = list(self._iter_targets(run_id, step_id=step_id or None))
        for target in targets:
            if partition_scopes:
                # H7：只验证本分区 payload 声明的目标，避免并行分区互相污染。
                scope = (
                    str(target.data_domain or ''),
                    str(target.asset_id or ''),
                    _scope_timestamp(target.gap_start_at),
                    _scope_timestamp(target.gap_end_at),
                )
                if scope not in partition_scopes:
                    continue
            if target.status in {'completed', 'exception', 'excluded'}:
                continue
            # P7 编排派生 target 不是事实目标，不应用域水位验证（派生链路
            # 成功即视为该步骤成功），否则派生执行成功也会被误判失败。
            if str(target.data_domain) == DataDomain.ORCHESTRATION:
                continue
            if not success:
                self.repository.mark_target(
                    target.target_id,
                    status='failed',
                    last_error=str(payload.get('error_message') or 'task_failed')[:300],
                    next_retry_at=occurred_at + timedelta(minutes=15),
                    now=occurred_at,
                )
                failed += 1
                continue
            status_code, exception_code, evidence = self.verifier.verify_target(target)
            if status_code == 'completed':
                self.repository.mark_target(
                    target.target_id,
                    status='completed',
                    evidence=evidence,
                    now=occurred_at,
                )
                verified += 1
            elif status_code == 'exception_allowed':
                self.repository.mark_target(
                    target.target_id,
                    status='exception',
                    exception_code=exception_code or 'source_unavailable',
                    evidence=evidence,
                    now=occurred_at,
                )
            else:
                step_row_rounds = (
                    self.repository.get_step(step_id) if step_id else None
                )
                if int(getattr(step_row_rounds, 'attempt_round', 0) or 0) >= MAX_FACT_RETRY_ROUNDS:
                    # 有限重试耗尽仍未取得事实（典型：停牌日无行情且快照缺失）：
                    # 转允许例外 source_unavailable，避免无限重试阻塞批次推进。
                    self.repository.mark_target(
                        target.target_id,
                        status='exception',
                        exception_code='source_unavailable',
                        evidence=evidence,
                        last_error='fact_verification_failed',
                        now=occurred_at,
                    )
                else:
                    self.repository.mark_target(
                        target.target_id,
                        status='failed',
                        exception_code=None,
                        evidence=evidence,
                        last_error='fact_verification_failed',
                        next_retry_at=occurred_at + timedelta(minutes=15),
                        now=occurred_at,
                    )
                failed += 1
        self.repository.refresh_step_counters(step_id)
        step_row = self.repository.get_step(step_id) if step_id else None
        if step_row is not None and step_row.phase in {'P3', 'P4'}:
            counts = self.repository.target_status_counts(step_id=step_id)
            if (
                counts.get('pending', 0)
                + counts.get('failed', 0)
                + counts.get('running', 0)
                == 0
            ):
                self.repository.mark_step_status(
                    step_id, 'completed', now=occurred_at
                )
        elif step_row is not None and step_row.phase == "P7":
            # P7 派生链路为编排步骤：成功即完成；失败进入重试预算，
            # 由 _submit_derived_refresh 在达到上限后转入 attention_required。
            if success:
                self.repository.mark_step_status(
                    step_id, 'completed', now=occurred_at
                )
            else:
                if int(getattr(step_row, 'attempt_round', 0) or 0) < DERIVED_MAX_RETRY_ROUNDS:
                    self.repository.bump_attempt_round(step_id, now=occurred_at)
                self.repository.mark_step_status(
                    step_id, 'pending', now=occurred_at
                )
        run = self.repository.get_run(run_id)
        if run is not None and run.status in {'running', 'verifying'}:
            self.advance(run_id, now=occurred_at)
        return {'verified': verified, 'failed': failed}



    def _reconcile_running_collection_step(self, run_id, step, *, now) -> bool:
        """收敛 running 的 P3/P4：全部目标终态则完成；到期失败则重提。"""

        counts = self.repository.target_status_counts(step_id=step.step_id)
        open_count = (
            counts.get("pending", 0)
            + counts.get("failed", 0)
            + counts.get("running", 0)
        )
        if open_count == 0:
            self.repository.mark_step_status(step.step_id, "completed", now=now)
            return True
        if int(step.attempt_round or 0) >= MAX_FACT_RETRY_ROUNDS:
            return self._converge_exhausted_collection_step(
                run_id, step, now=now
            )
        due_rows = [
            row
            for row in self._iter_targets(run_id, step_id=step.step_id)
            if row.status == "failed"
            and (row.next_retry_at is None or row.next_retry_at <= now)
        ]
        if not due_rows:
            return False
        retry_targets = [
            GapTarget(
                data_domain=row.data_domain,
                gap_start_at=row.gap_start_at,
                gap_end_at=row.gap_end_at,
                granularity=row.granularity,
                asset_id=row.asset_id,
                expected_count=int(row.expected_count or 0),
            )
            for row in due_rows
        ]
        # H9：重提与首次提交共用工作单元防重，活动任务重叠时本轮让路。
        runnable, _deferred, conflicts = self._defer_active_work_unit_conflicts(
            run_id, step.step_id, retry_targets
        )
        if not runnable:
            logger.warning(
                "失败目标重试全部让路 run=%s step=%s conflicts=%s",
                run_id,
                step.step_id,
                conflicts,
            )
            return False
        round_no = self.repository.bump_attempt_round(step.step_id, now=now)
        for row in due_rows:
            # 重提即进入新的冷却窗口，避免同一批失败目标被反复 bump。
            self.repository.mark_target(
                row.target_id,
                status="failed",
                last_error=row.last_error,
                next_retry_at=now + timedelta(minutes=15),
                now=now,
            )
        self.executor.submit_step(
            run_id=run_id,
            step=_PlanStepAdapter(step),
            targets=runnable,
            attempt=round_no,
        )
        return True

    def _converge_exhausted_collection_step(self, run_id, step, *, now) -> bool:
        """重试耗尽后复核无活动任务的目标，并转换为明确终态。"""

        pairs = []
        for row in self._iter_targets(run_id, step_id=step.step_id):
            if row.status not in {"pending", "failed", "running"}:
                continue
            target = GapTarget(
                data_domain=row.data_domain,
                gap_start_at=row.gap_start_at,
                gap_end_at=row.gap_end_at,
                granularity=row.granularity,
                asset_id=row.asset_id,
                expected_count=int(row.expected_count or 0),
            )
            pairs.append((row, target))
        if not pairs:
            self.repository.mark_step_status(step.step_id, "completed", now=now)
            return True

        inactive, _deferred, _conflicts = self._defer_active_work_unit_conflicts(
            run_id,
            step.step_id,
            [target for _, target in pairs],
        )
        inactive_scopes = {target.scope_key() for target in inactive}
        changed = 0
        for row, target in pairs:
            if target.scope_key() not in inactive_scopes:
                continue
            status_code, exception_code, evidence = self.verifier.verify_target(row)
            if status_code == "completed":
                self.repository.mark_target(
                    row.target_id,
                    status="completed",
                    evidence=evidence,
                    now=now,
                )
            else:
                self.repository.mark_target(
                    row.target_id,
                    status="exception",
                    exception_code=(
                        exception_code
                        if status_code == "exception_allowed" and exception_code
                        else "source_unavailable"
                    ),
                    evidence=evidence,
                    last_error="retry_rounds_exhausted",
                    now=now,
                )
            changed += 1

        if changed:
            self.repository.refresh_step_counters(step.step_id)
        counts = self.repository.target_status_counts(step_id=step.step_id)
        open_count = (
            counts.get("pending", 0)
            + counts.get("failed", 0)
            + counts.get("running", 0)
        )
        if open_count == 0:
            self.repository.mark_step_status(step.step_id, "completed", now=now)
            return True
        return changed > 0

    def _finalize_run(self, run_id, step, *, now) -> bool:
        """P8：终验收收敛批次并放行门控（规格 9.4/12.1）。"""

        self.repository.mark_step_status(step.step_id, "running", now=now)
        run = self.repository.get_run(run_id)
        check = self.verifier.final_gate_check(
            run_id=run_id,
            cutoff_date=run.cutoff_date,
            readiness_fn=self.readiness_fn,
        )
        blocking = self.repository.blocking_gap_exists(run_id)
        has_exceptions = bool(
            self.repository.list_targets(run_id, status="exception", limit=1)
        )
        if not check.get("executable") or blocking:
            self.repository.transition_run(
                run_id,
                "attention_required",
                expected_current="verifying",
                has_blocking_gaps=True,
                quality_result=check,
                summary_patch={"reason": "final_gate_not_executable"},
                now=now,
            )
            return True
        terminal = (
            "completed_with_exceptions" if has_exceptions else "completed"
        )
        self.repository.transition_run(
            run_id,
            terminal,
            expected_current="verifying",
            has_blocking_gaps=False,
            quality_result=check,
            summary_patch={"finished_at": now.isoformat()},
            now=now,
        )
        self.repository.set_gate_status(
            run_id, "open", reason="recovery_completed", now=now
        )
        self.repository.mark_step_status(step.step_id, "completed", now=now)
        return True

    def _materialize_plan_targets(self, *, run_id: str, steps) -> int:
        """P2 物化：把计划目标区间幂等写入 data_recovery_targets。"""

        materialized = 0
        for step in steps:
            if not getattr(step, 'targets', None):
                continue
            if step.phase not in {'P3', 'P4'}:
                continue
            materialized += self.repository.upsert_targets(
                run_id,
                step.step_id(run_id),
                list(step.targets),
            )
        return materialized

    def _persist_frozen_snapshots(self, *, run_row) -> None:
        """P1 物化：确认时把冻结的资产池快照写入现有 Repository。"""

        from finance_agent.storage.repositories import UniverseRepository

        universe_id = run_row.universe_id
        if not universe_id:
            return
        snapshot = self.detector.load_universe_snapshot(cutoff_date=run_row.cutoff_date)[0]
        if snapshot is None:
            return
        members = list(snapshot.asset_ids)
        existing = UniverseRepository(self.session)
        existing.upsert_universe(
            universe_id=universe_id,
            name=universe_id,
            source='data_recovery',
            market=self.market,
            as_of=run_row.universe_snapshot_at or datetime.now().astimezone(),
            payload={'recovery_run_id': run_row.run_id, 'snapshot_hash': snapshot.snapshot_hash},
        )
        asset_rows = self.session.execute(
            _assets_statement(members)
        ).all()
        by_id = {row.asset_id: row for row in asset_rows}
        payloads = []
        for index, asset_id in enumerate(members):
            row = by_id.get(asset_id)
            if row is None:
                continue
            payloads.append(
                {
                    'member_id': f'{universe_id}:{asset_id}',
                    'asset_id': asset_id,
                    'symbol': row.symbol,
                    'market': row.market or self.market,
                    'as_of': run_row.universe_snapshot_at or datetime.now().astimezone(),
                    'included': True,
                    'removed_reason': None,
                    'rank_hint': index,
                    'payload': {},
                }
            )
        if payloads:
            existing.replace_members(universe_id=universe_id, members=payloads)

    def _cancel_pending_tasks(self, run_id: str) -> int:
        """取消语义：只停止未领取的分区任务，不回滚已写事实（规格 12.3）。"""

        from finance_agent.storage.repositories import (
            OutboxEventRepository,
            SchedulerTaskRepository,
        )

        repository = SchedulerTaskRepository(
            self.session, outbox_repository=OutboxEventRepository(self.session)
        )
        total = 0
        while True:
            rows = repository.list_tasks(
                statuses=("pending",),
                payload_key="recovery_run_id",
                payload_value=str(run_id),
                limit=500,
            )
            ids = [row.task_id for row in rows]
            if not ids:
                return total
            total += repository.cancel_tasks(
                task_ids=ids,
                reason=f"data_recovery_cancelled:{run_id}",
            )
            if len(rows) < 500:
                return total


class _PlanStepAdapter:
    """把步骤 ORM 行适配为 executor 需要的最小 PlanStep 接口。"""

    def __init__(self, step_row) -> None:
        self._row = step_row
        self.phase = str(step_row.phase)
        self.data_domain = str(step_row.data_domain)
        self.task_params = dict(step_row.task_params or {})

    def step_id(self, run_id: str) -> str:
        return f'{run_id}:{self.phase}:{self.data_domain}'


def select_market_calendar_statement(market: str, start_date: date, end_date: date):
    from sqlalchemy import select

    from finance_agent.storage.orm import MarketCalendarORM

    return (
        select(MarketCalendarORM)
        .where(
            MarketCalendarORM.market == market,
            MarketCalendarORM.trade_date >= start_date,
            MarketCalendarORM.trade_date <= end_date,
        )
        .order_by(MarketCalendarORM.trade_date)
    )


def _assets_statement(asset_ids):
    from sqlalchemy import select

    from finance_agent.storage.orm import AssetORM

    return select(
        AssetORM.asset_id, AssetORM.symbol, AssetORM.market
    ).where(AssetORM.asset_id.in_(list(asset_ids)))
