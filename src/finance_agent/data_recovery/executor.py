"""补跑执行器：把步骤目标分区并提交到持久任务队列。

规格 5.2 职责 3 / 9.2 / 10.2：
- 幂等键 recovery:{run_id}:{step_id}:{partition_hash}；
- payload 携带 recovery_run_id / recovery_step_id / partition_hash；
- payload 另携带规范化工作单元键 work_units（H9 / 规格 10.4 merge）：
  与普通调度共享防重边界，同一 sync_task_type + 资产 + 窗口的活动
  （pending/running）任务会让补跑提交让路；反之补跑占位时普通调度
  由水位与 only_failed_or_stale 机制天然跳过已完整数据；
- 重复确认、调度器重启或租约恢复不会产生新的逻辑任务；
- 编排步骤（P0/P1/P2/P5/P6/P8）由模块内联执行，不进队列。
"""

from __future__ import annotations

import hashlib
from typing import Any, Protocol

from finance_agent.data_recovery.models import GapTarget, PlanStep

JsonDict = dict[str, Any]

#: 补跑任务在调度器中的 job 名前缀（execute_job 据此分派）。
RECOVERY_JOB_PREFIX = "recovery."

_DOMAIN_JOB_NAMES = {
    "market_bars": "recovery.ashare.bars",
    "fundamentals": "recovery.ashare.fundamentals",
    "valuation": "recovery.ashare.valuation",
    "capital_flow": "recovery.ashare.capital_flow",
    "events": "recovery.ashare.events",
    "risk_sentiment": "recovery.ashare.risk_sentiment",
    "orchestration": "recovery.ashare.derived",
}

#: 每个分区的目标数上限：控制单任务体量并保持限频友好。
DEFAULT_PARTITION_TARGETS = 20


def domain_sync_task_type(data_domain: str) -> str:
    """补跑数据域对应的普通调度 sync_task_type；未知数据域回退为域名。

    映射的单一事实来源是 RecoverySchedulerMixin._RECOVERY_DOMAIN_TASK_TYPES；
    延迟导入避免 data_recovery 与 scheduler 包的模块级循环依赖。
    """

    from finance_agent.scheduler.recovery_bridge import RecoverySchedulerMixin

    pair = RecoverySchedulerMixin._RECOVERY_DOMAIN_TASK_TYPES.get(str(data_domain))
    return str(pair[0]) if pair else str(data_domain)


def build_work_unit(target: GapTarget, *, market: str = "ashare") -> str:
    """规范化工作单元键（H9）：f"{sync_task_type}:{资产或市场}:{日期或窗口}"。

    - 资产取归一化 asset_id（去空白、小写）；市场级目标（无资产）用
      市场标识代替资产；
    - 窗口取缺口起止自然日；单日窗口只保留一个日期。
    """

    sync_task_type = domain_sync_task_type(target.data_domain)
    owner = str(target.asset_id or "").strip().lower()
    if not owner:
        owner = str(market).strip().lower()
    start = str(target.gap_start_at)[:10]
    end = str(target.gap_end_at)[:10]
    window = start if start == end else f"{start}..{end}"
    return f"{sync_task_type}:{owner}:{window}"


def build_work_units(targets, *, market: str = "ashare") -> list[str]:
    """目标集合的去重保序工作单元键列表，用于持久队列防重查询。"""

    units: list[str] = []
    seen: set[str] = set()
    for target in targets:
        unit = build_work_unit(target, market=market)
        if unit not in seen:
            seen.add(unit)
            units.append(unit)
    return units


def partition_hash(targets):
    """分区指纹：目标 scope 排序后哈希，重复分区得到同一键。"""

    material = ";".join("|".join(target.scope_key()) for target in targets)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def partition_targets(targets, *, max_targets=DEFAULT_PARTITION_TARGETS):
    """按资产分组切分目标，避免跨资产大分区拖长单任务时长。"""

    ordered = list(targets)
    if not ordered:
        return []
    partitions = []
    current = []
    current_asset = ordered[0].asset_id
    for target in ordered:
        if current and (len(current) >= max_targets or target.asset_id != current_asset):
            partitions.append(current)
            current = []
            current_asset = target.asset_id
        current.append(target)
    if current:
        partitions.append(current)
    return partitions


class TaskQueueLike(Protocol):
    """PersistentTaskQueue 的最小结构化子集，便于测试注入。"""

    def enqueue(self, **kwargs):
        ...


class RecoveryExecutor:
    """把步骤转换为持久任务分区并幂等提交。"""

    def __init__(self, queue_factory, *, max_targets=DEFAULT_PARTITION_TARGETS) -> None:
        # queue_factory() 返回一个上下文管理器，产出已绑定 session 的 PersistentTaskQueue。
        self.queue_factory = queue_factory
        self.max_targets = max_targets

    def step_job_name(self, step: PlanStep) -> str:
        return _DOMAIN_JOB_NAMES.get(step.data_domain, "recovery.ashare.derived")

    def build_task_payload(self, *, run_id, step, targets):
        """构造任务 payload；只包含补跑必要信息，不含凭据。"""

        return {
            "recovery_run_id": run_id,
            "recovery_step_id": step.step_id(run_id),
            "recovery_phase": step.phase,
            "data_domain": step.data_domain,
            "partition_hash": partition_hash(targets),
            # H9：规范化工作单元键，供 find_active_work_unit_conflicts 做
            # 补跑与普通调度共享的活动任务防重查询（规格 10.4 merge）。
            "work_units": build_work_units(targets),
            "targets": [
                {
                    "data_domain": target.data_domain,
                    "asset_id": target.asset_id,
                    "gap_start_at": target.gap_start_at.isoformat(),
                    "gap_end_at": target.gap_end_at.isoformat(),
                    "granularity": target.granularity,
                    "expected_count": target.expected_count,
                }
                for target in targets
            ],
            "task_params": dict(step.task_params),
        }

    def submit_step(self, *, run_id, step, targets, attempt: int = 0):
        """幂等提交一个步骤的所有分区；返回提交摘要列表。

        队列 enqueue 对同一幂等键天然去重（ON CONFLICT DO NOTHING），
        因此重复调用只刷新已存在任务，不会创建新的逻辑任务。
        """

        submitted = []
        job_name = self.step_job_name(step)
        for group in partition_targets(targets, max_targets=self.max_targets):
            digest = partition_hash(group)
            idempotency_key = f"recovery:{run_id}:{step.step_id(run_id)}:{digest}"
            if attempt:
                # 重试使用独立幂等键，避免复用已终态任务而无法真正执行（H6）。
                idempotency_key = f"{idempotency_key}:r{int(attempt)}"
            payload = self.build_task_payload(run_id=run_id, step=step, targets=group)
            with self.queue_factory() as queue:
                task = queue.enqueue(
                    job_name=job_name,
                    idempotency_key=idempotency_key,
                    payload=payload,
                    max_attempts=1,
                )
            submitted.append(
                {
                    "task_id": getattr(task, "task_id", None),
                    "job_name": job_name,
                    "idempotency_key": idempotency_key,
                    "partition_hash": digest,
                    "target_count": len(group),
                }
            )
        return submitted
