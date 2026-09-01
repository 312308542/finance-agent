"""停跑恢复批次的 PostgreSQL 持久化仓储。

批次、步骤和目标区间全部落库；Redis 只作实时进度展示，不是事实来源。
所有写入都按规格第 10 节设计为幂等：重复 preview/approve/提交不会产生
第二份逻辑批次、步骤或目标。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from finance_agent.data_recovery.models import (
    ACTIVE_RUN_STATUSES,
    BLOCKING_EXCEPTION_CODES,
    TERMINAL_RUN_STATUSES,
    GapTarget,
    PlanStep,
)
from finance_agent.data_recovery.state_machine import (
    InvalidRecoveryTransition,
    assert_transition,
    gate_status_for_run,
)
from finance_agent.storage.orm import (
    DataRecoveryRunORM,
    DataRecoveryStepORM,
    DataRecoveryTargetORM,
    SchedulerTaskRunORM,
)
from finance_agent.storage.repositories import OutboxEventRepository

JsonDict = dict[str, Any]

#: 批次互斥范围前缀（规格 10.1）。
MUTEX_SCOPE_PREFIX = "data_recovery"

_STATUS_EVENT_TYPES = {
    "approved": "recovery_run_approved",
    "paused": "recovery_run_paused",
    "cancelled": "recovery_run_cancelled",
    "attention_required": "recovery_attention_required",
    "completed": "recovery_run_completed",
    "completed_with_exceptions": "recovery_run_completed_with_exceptions",
}


class ActiveRecoveryRunExists(RuntimeError):
    """同市场已存在非草稿活动批次，不允许再创建新的补跑计划。"""

    def __init__(self, run_id: str, status: str) -> None:
        self.run_id = run_id
        self.status = status
        super().__init__(f"市场已存在活动补跑批次 {run_id}（status={status}）")



def plan_fingerprint(
    *,
    market: str,
    universe_snapshot_hash: str,
    cutoff_date: date,
    gap_scope_keys: Sequence[Sequence[str]],
    strategy_version: str,
) -> str:
    """计划指纹 = 市场 + 资产池快照 + 冻结截止日 + 缺口集合 + 策略版本。"""

    normalized_scopes = sorted("|".join(key) for key in gap_scope_keys)
    material = "|".join(
        [
            str(market),
            str(universe_snapshot_hash),
            cutoff_date.isoformat(),
            ";".join(normalized_scopes),
            str(strategy_version),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def build_run_id(*, market: str, cutoff_date: date, plan_hash: str) -> str:
    """确定性批次 ID：同一指纹永远得到同一个 run_id。"""

    return f"rec:{market}:{cutoff_date.isoformat()}:{plan_hash[:12]}"


def build_target_id(*, run_id: str, target: GapTarget) -> str:
    """目标区间的确定性 ID（应用层等价键与唯一索引保持一致）。"""

    scope = "|".join(target.scope_key())
    digest = hashlib.sha256(f"{run_id}|{scope}".encode()).hexdigest()[:48]
    return f"rt:{digest}"


class RecoveryRepository:
    """data_recovery_runs / steps / targets 的幂等读写仓储。"""

    def __init__(
        self,
        session: Session,
        *,
        outbox_repository: OutboxEventRepository | None = None,
    ) -> None:
        self.session = session
        self.outbox = outbox_repository or OutboxEventRepository(session)

    def _emit(
        self,
        *,
        event_type: str,
        run_id: str,
        payload: JsonDict | None = None,
        now: datetime | None = None,
    ) -> None:
        """追加结构化事件；不包含任何 Provider 私密正文或凭据。"""

        occurred_at = now or datetime.now().astimezone()
        detail = "|".join(f"{key}={value}" for key, value in sorted((payload or {}).items()))
        self.outbox.append(
            event_type=event_type,
            aggregate_type="data_recovery_run",
            aggregate_id=run_id,
            idempotency_key=(
                f"{event_type}:{run_id}:"
                f"{hashlib.sha256(detail.encode('utf-8')).hexdigest()[:16]}"
            ),
            payload={"run_id": run_id, **(payload or {})},
            occurred_at=occurred_at,
        )



    # ------------------------------------------------------------------
    # 批次
    # ------------------------------------------------------------------

    def get_run(self, run_id: str) -> DataRecoveryRunORM | None:
        return self.session.get(DataRecoveryRunORM, run_id)

    def get_run_locked(self, run_id: str) -> DataRecoveryRunORM | None:
        """带行锁读取批次，供状态流转使用。"""

        statement = (
            select(DataRecoveryRunORM)
            .where(DataRecoveryRunORM.run_id == run_id)
            .with_for_update()
        )
        return self.session.execute(statement).scalar_one_or_none()

    def get_active_run(self, market: str) -> DataRecoveryRunORM | None:
        statement = (
            select(DataRecoveryRunORM)
            .where(
                DataRecoveryRunORM.market == market,
                DataRecoveryRunORM.status.in_(ACTIVE_RUN_STATUSES),
            )
            .order_by(DataRecoveryRunORM.created_at.desc())
            .limit(1)
        )
        return self.session.execute(statement).scalar_one_or_none()

    def find_run_by_plan_hash(self, plan_hash: str) -> DataRecoveryRunORM | None:
        statement = (
            select(DataRecoveryRunORM)
            .where(DataRecoveryRunORM.plan_hash == plan_hash)
            .order_by(DataRecoveryRunORM.created_at.desc())
            .limit(1)
        )
        return self.session.execute(statement).scalar_one_or_none()

    def list_runs(self, *, limit: int = 20) -> list[DataRecoveryRunORM]:
        statement = (
            select(DataRecoveryRunORM)
            .order_by(DataRecoveryRunORM.created_at.desc())
            .limit(max(1, int(limit)))
        )
        return list(self.session.execute(statement).scalars())



    def create_or_reuse_draft(
        self,
        *,
        market: str,
        cutoff_date: date,
        gap_start_date: date | None,
        universe_id: str | None,
        universe_snapshot_at: datetime | None,
        universe_snapshot_hash: str | None,
        plan_hash: str,
        summary: JsonDict,
        requested_by: str | None = None,
        has_blocking_gaps: bool = True,
        now: datetime | None = None,
    ) -> tuple[DataRecoveryRunORM, bool]:
        """创建草稿或复用相同指纹的现有批次（规格 10.1）。

        返回 (row, created)。旧草稿指纹不同时标记取消并让位；
        已确认的活动批次存在时抛出 ActiveRecoveryRunExists。
        """

        occurred_at = now or datetime.now().astimezone()
        existing = self.get_active_run(market)
        if existing is not None:
            if existing.plan_hash == plan_hash:
                return existing, False
            if existing.status != "draft":
                raise ActiveRecoveryRunExists(existing.run_id, existing.status)
            existing.status = "cancelled"
            existing.finished_at = occurred_at
            existing.updated_at = occurred_at
            self._emit(
                event_type="recovery_plan_stale",
                run_id=existing.run_id,
                payload={"reason": "superseded_by_new_plan", "new_plan_hash": plan_hash},
                now=occurred_at,
            )
        run_id = build_run_id(market=market, cutoff_date=cutoff_date, plan_hash=plan_hash)
        self.session.add(
            DataRecoveryRunORM(
                run_id=run_id,
                market=market,
                universe_id=universe_id,
                universe_snapshot_at=universe_snapshot_at,
                universe_snapshot_hash=universe_snapshot_hash,
                gap_start_date=gap_start_date,
                cutoff_date=cutoff_date,
                plan_hash=plan_hash,
                status="draft",
                gate_status=gate_status_for_run("draft", has_blocking_gaps=has_blocking_gaps),
                requested_by=requested_by,
                summary=summary,
                quality_result={},
                created_at=occurred_at,
                updated_at=occurred_at,
            )
        )
        self.session.flush()
        row = self.session.get_one(DataRecoveryRunORM, run_id)
        self._emit(
            event_type="recovery_plan_created",
            run_id=run_id,
            payload={
                "plan_hash": plan_hash,
                "cutoff_date": cutoff_date.isoformat(),
                "has_blocking_gaps": has_blocking_gaps,
            },
            now=occurred_at,
        )
        return row, True



    def transition_run(
        self,
        run_id: str,
        next_status: str,
        *,
        actor: str | None = None,
        expected_current: str | None = None,
        gate_status: str | None = None,
        has_blocking_gaps: bool = True,
        summary_patch: JsonDict | None = None,
        quality_result: JsonDict | None = None,
        now: datetime | None = None,
    ) -> DataRecoveryRunORM:
        """推进批次状态机并同步门控；非法跳转抛出 InvalidRecoveryTransition。"""

        occurred_at = now or datetime.now().astimezone()
        row = self.get_run_locked(run_id)
        if row is None:
            raise LookupError(f"补跑批次不存在: {run_id}")
        current = row.status
        if expected_current is not None and current != expected_current:
            raise InvalidRecoveryTransition(current, next_status)
        assert_transition(current, next_status)
        row.status = next_status
        row.updated_at = occurred_at
        if next_status == "approved":
            row.approved_at = occurred_at
            if actor:
                row.approved_by = actor
        elif next_status == "running":
            row.started_at = row.started_at or occurred_at
        if next_status in TERMINAL_RUN_STATUSES:
            row.finished_at = occurred_at
        if gate_status is not None:
            row.gate_status = gate_status
        else:
            row.gate_status = gate_status_for_run(
                next_status, has_blocking_gaps=has_blocking_gaps
            )
        if summary_patch:
            merged = dict(row.summary or {})
            merged.update(summary_patch)
            row.summary = merged
        if quality_result is not None:
            row.quality_result = quality_result
        self.session.flush()
        event_type = _STATUS_EVENT_TYPES.get(next_status)
        if event_type:
            payload: JsonDict = {"status": next_status}
            if actor:
                payload["actor"] = actor
            self._emit(event_type=event_type, run_id=run_id, payload=payload, now=occurred_at)
        return row

    def set_gate_status(
        self,
        run_id: str,
        gate_status: str,
        *,
        reason: str = "",
        now: datetime | None = None,
    ) -> DataRecoveryRunORM | None:
        """直接更新门控状态并发出 recovery_gate_changed 事件。"""

        occurred_at = now or datetime.now().astimezone()
        row = self.get_run_locked(run_id)
        if row is None:
            return None
        changed = row.gate_status != gate_status
        row.gate_status = gate_status
        row.updated_at = occurred_at
        self.session.flush()
        if changed:
            self._emit(
                event_type="recovery_gate_changed",
                run_id=run_id,
                payload={"gate_status": gate_status, "reason": reason},
                now=occurred_at,
            )
        return row



    # ------------------------------------------------------------------
    # 步骤
    # ------------------------------------------------------------------

    def replace_steps(self, run_id: str, steps: Sequence[PlanStep]) -> list[DataRecoveryStepORM]:
        """按 phase+数据域确定性写入步骤；重复调用不重置已有进度。"""

        occurred_at = datetime.now().astimezone()
        for step in steps:
            values = {
                "step_id": step.step_id(run_id),
                "run_id": run_id,
                "phase": step.phase,
                "data_domain": step.data_domain,
                "depends_on": list(step.depends_on_phases),
                "target_count": len(step.targets),
                "task_params": dict(step.task_params),
                "updated_at": occurred_at,
            }
            statement = (
                insert(DataRecoveryStepORM)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[DataRecoveryStepORM.__table__.c.step_id],
                    set_={
                        "depends_on": values["depends_on"],
                        "target_count": values["target_count"],
                        "task_params": values["task_params"],
                        "updated_at": occurred_at,
                    },
                )
            )
            self.session.execute(statement)
        self.session.flush()
        return self.get_steps(run_id)

    def get_steps(self, run_id: str) -> list[DataRecoveryStepORM]:
        statement = (
            select(DataRecoveryStepORM)
            .where(DataRecoveryStepORM.run_id == run_id)
            .order_by(DataRecoveryStepORM.phase, DataRecoveryStepORM.data_domain)
        )
        return list(self.session.execute(statement).scalars())

    def get_step(self, step_id: str) -> DataRecoveryStepORM | None:
        return self.session.get(DataRecoveryStepORM, step_id)

    def find_latest_cancelled_run(self, market: str) -> DataRecoveryRunORM | None:
        """最近一个已取消批次；供门控做阻塞缺口保持判定（规格 12.1）。"""

        return (
            self.session.execute(
                select(DataRecoveryRunORM)
                .where(
                    DataRecoveryRunORM.market == market,
                    DataRecoveryRunORM.status == "cancelled",
                )
                .order_by(DataRecoveryRunORM.updated_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )

    def bump_attempt_round(self, step_id: str, *, now: datetime | None = None) -> int:
        """重提轮次自增并返回新值；重试分区用独立幂等键（规格 11.2）。"""

        occurred_at = now or datetime.now().astimezone()
        row = self.session.get(DataRecoveryStepORM, step_id)
        if row is None:
            raise LookupError(f"补跑步骤不存在: {step_id}")
        row.attempt_round = int(row.attempt_round or 0) + 1
        row.updated_at = occurred_at
        self.session.flush()
        return int(row.attempt_round)

    def mark_step_status(
        self,
        step_id: str,
        status: str,
        *,
        attempt_round: int | None = None,
        now: datetime | None = None,
    ) -> DataRecoveryStepORM | None:
        occurred_at = now or datetime.now().astimezone()
        row = self.session.get(DataRecoveryStepORM, step_id)
        if row is None:
            return None
        row.status = status
        row.updated_at = occurred_at
        if attempt_round is not None:
            row.attempt_round = attempt_round
        if status == "running":
            row.started_at = row.started_at or occurred_at
        if status in {"completed", "failed", "skipped", "cancelled"}:
            row.finished_at = occurred_at
        self.session.flush()
        return row

    def refresh_step_counters(self, step_id: str) -> DataRecoveryStepORM | None:
        """从目标区间表重新聚合步骤计数（任务成功不等于目标完成）。"""

        row = self.session.get(DataRecoveryStepORM, step_id)
        if row is None:
            return None
        counts = self.target_status_counts(step_id=step_id)
        row.completed_count = counts.get("completed", 0)
        row.exception_count = counts.get("exception", 0) + counts.get("excluded", 0)
        row.retryable_count = counts.get("pending", 0) + counts.get("failed", 0)
        row.updated_at = datetime.now().astimezone()
        self.session.flush()
        return row



    # ------------------------------------------------------------------
    # 目标区间
    # ------------------------------------------------------------------

    def upsert_targets(self, run_id: str, step_id: str, targets: Sequence[GapTarget]) -> int:
        """幂等写入目标区间；返回本次新插入的数量。"""

        inserted = 0
        occurred_at = datetime.now().astimezone()
        for target in targets:
            target_id = build_target_id(run_id=run_id, target=target)
            values = {
                "target_id": target_id,
                "run_id": run_id,
                "step_id": step_id,
                "data_domain": target.data_domain,
                "asset_id": target.asset_id,
                "gap_start_at": target.gap_start_at,
                "gap_end_at": target.gap_end_at,
                "granularity": target.granularity,
                "expected_count": max(0, int(target.expected_count)),
                "exception_code": target.exception_code,
                "exception_evidence": dict(target.exception_evidence or {}),
                "updated_at": occurred_at,
            }
            statement = (
                insert(DataRecoveryTargetORM)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[DataRecoveryTargetORM.__table__.c.target_id],
                    set_={
                        "expected_count": values["expected_count"],
                        "updated_at": occurred_at,
                    },
                )
            )
            result = self.session.execute(statement)
            inserted += result.rowcount or 0
        self.session.flush()
        return inserted

    def list_targets(
        self,
        run_id: str,
        *,
        step_id: str | None = None,
        status: str | None = None,
        after_target_id: str | None = None,
        limit: int = 500,
    ) -> list[DataRecoveryTargetORM]:
        statement = select(DataRecoveryTargetORM).where(DataRecoveryTargetORM.run_id == run_id)
        if step_id is not None:
            statement = statement.where(DataRecoveryTargetORM.step_id == step_id)
        if status is not None:
            statement = statement.where(DataRecoveryTargetORM.status == status)
        if after_target_id is not None:
            statement = statement.where(DataRecoveryTargetORM.target_id > after_target_id)
        statement = statement.order_by(DataRecoveryTargetORM.target_id).limit(max(1, int(limit)))
        return list(self.session.execute(statement).scalars())

    def list_blocking_targets(self, run_id: str) -> list[DataRecoveryTargetORM]:
        """列出活动批次中仍会阻塞对应数据域消费者的目标。"""

        statement = (
            select(DataRecoveryTargetORM)
            .where(
                DataRecoveryTargetORM.run_id == str(run_id),
                or_(
                    DataRecoveryTargetORM.status.in_(["pending", "running", "failed"]),
                    and_(
                        DataRecoveryTargetORM.status == "exception",
                        DataRecoveryTargetORM.exception_code.in_(BLOCKING_EXCEPTION_CODES),
                    ),
                ),
            )
            .order_by(
                DataRecoveryTargetORM.data_domain,
                DataRecoveryTargetORM.step_id,
                DataRecoveryTargetORM.target_id,
            )
        )
        return list(self.session.execute(statement).scalars())



    def target_status_counts(
        self, *, run_id: str | None = None, step_id: str | None = None
    ) -> dict[str, int]:
        conditions = []
        if run_id is not None:
            conditions.append(DataRecoveryTargetORM.run_id == run_id)
        if step_id is not None:
            conditions.append(DataRecoveryTargetORM.step_id == step_id)
        statement = select(
            DataRecoveryTargetORM.status, func.count(DataRecoveryTargetORM.target_id)
        ).group_by(DataRecoveryTargetORM.status)
        if conditions:
            statement = statement.where(*conditions)
        return {
            status: int(count)
            for status, count in self.session.execute(statement).all()
        }

    def mark_target(
        self,
        target_id: str,
        *,
        status: str,
        exception_code: str | None = None,
        evidence: JsonDict | None = None,
        last_error: str | None = None,
        next_retry_at: datetime | None = None,
        now: datetime | None = None,
    ) -> DataRecoveryTargetORM | None:
        occurred_at = now or datetime.now().astimezone()
        row = self.session.get(DataRecoveryTargetORM, target_id)
        if row is None:
            return None
        row.status = status
        row.updated_at = occurred_at
        if exception_code is not None:
            row.exception_code = exception_code
        if evidence is not None:
            row.exception_evidence = evidence
        if last_error is not None:
            row.last_error = last_error
        row.next_retry_at = next_retry_at
        self.session.flush()
        return row

    def blocking_gap_exists(self, run_id: str) -> bool:
        """是否存在未解决的核心阻塞目标（transient/unknown/data_conflict 或未决缺口）。"""

        counts = self.target_status_counts(run_id=run_id)
        if counts.get("pending", 0) > 0 or counts.get("failed", 0) > 0:
            return True
        statement = select(func.count(DataRecoveryTargetORM.target_id)).where(
            DataRecoveryTargetORM.run_id == run_id,
            DataRecoveryTargetORM.status == "exception",
            DataRecoveryTargetORM.exception_code.in_(BLOCKING_EXCEPTION_CODES),
        )
        return int(self.session.execute(statement).scalar_one()) > 0

    # ------------------------------------------------------------------
    # 与持久任务表的关联查询
    # ------------------------------------------------------------------

    def task_counts_for_run(self, run_id: str) -> dict[str, int]:
        """按 payload 内的 recovery_run_id 统计调度任务状态分布。"""

        ref = SchedulerTaskRunORM.payload["recovery_run_id"].astext
        statement = (
            select(SchedulerTaskRunORM.status, func.count(SchedulerTaskRunORM.task_id))
            .where(ref == run_id)
            .group_by(SchedulerTaskRunORM.status)
        )
        return {status: int(count) for status, count in self.session.execute(statement).all()}
