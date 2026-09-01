"""停跑恢复补跑的领域常量与只读数据结构。

本模块不依赖数据库、Redis 或 Provider，保证状态机与计划语义可以在纯单元
测试中验证。持久化行结构与 ORM 解耦，统一通过这里的 dataclass 流转。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

JsonDict = dict[str, Any]

#: 补跑批次状态机（规格 9.4）。
RunStatus = Literal[
    "draft",
    "approved",
    "running",
    "paused",
    "verifying",
    "attention_required",
    "completed",
    "completed_with_exceptions",
    "cancelled",
]

#: 终态集合：进入后不允许再流转。
TERMINAL_RUN_STATUSES: tuple[str, ...] = (
    "completed",
    "completed_with_exceptions",
    "cancelled",
)

#: 活动状态集合：同一市场同一时间只允许一个批次处于这些状态。
ACTIVE_RUN_STATUSES: tuple[str, ...] = (
    "draft",
    "approved",
    "running",
    "paused",
    "verifying",
    "attention_required",
)

#: RecoveryGate 三态（规格 12.1）。
GateState = Literal["recovering", "degraded", "open"]

#: 步骤状态：沿用持久任务队列的语义并补充 skipped。
StepStatus = Literal["pending", "running", "completed", "failed", "skipped", "cancelled"]

#: 目标区间状态；exception 表示已归类例外，excluded 表示从应补分母排除。
TargetStatus = Literal[
    "pending", "running", "completed", "failed", "exception", "excluded"
]

#: 例外分类（规格 11.1）。transient/unknown_gap/data_conflict 属于核心阻塞项。
ExceptionCode = Literal[
    "transient", "source_unavailable", "not_applicable", "data_conflict", "unknown_gap"
]

BLOCKING_EXCEPTION_CODES: tuple[str, ...] = ("transient", "data_conflict", "unknown_gap")
ALLOWED_EXCEPTION_CODES: tuple[str, ...] = ("source_unavailable", "not_applicable")

#: 调度任务与补跑共存的合并策略（规格 10.4）。
TaskMergePolicy = Literal["always", "merge", "requires_open"]

#: 补跑阶段（规格 7.1）。
StepPhase = Literal["P0", "P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8"]


class DataDomain:
    """补跑涉及的数据域标识，与现有采集任务的 data_packages 对齐。"""

    MARKET_CALENDAR = "market_calendar"
    UNIVERSE = "universe"
    MARKET_BARS = "market_bars"
    FUNDAMENTALS = "fundamentals"
    VALUATION = "valuation"
    CAPITAL_FLOW = "capital_flow"
    EVENTS = "events"
    RISK_SENTIMENT = "risk_sentiment"

    #: P4 中可并行恢复的事实数据域。
    PARALLEL_FACT_DOMAINS: tuple[str, ...] = (
        FUNDAMENTALS,
        VALUATION,
        CAPITAL_FLOW,
        EVENTS,
        RISK_SENTIMENT,
    )

    #: 编排步骤使用的数据域占位（不属于任何真实采集域）。
    ORCHESTRATION = "orchestration"


RECOVERY_STRATEGY_VERSION = "1"


@dataclass(frozen=True)
class UniverseSnapshot:
    """生产资产池快照：approve 时幂等物化到现有 Repository。"""

    universe_id: str
    snapshot_at: datetime
    snapshot_hash: str
    asset_ids: tuple[str, ...]
    source: str = "production_config"

    def to_dict(self) -> JsonDict:
        """转换为可写入计划摘要的字典。"""

        return {
            "universe_id": self.universe_id,
            "snapshot_at": self.snapshot_at.isoformat(),
            "snapshot_hash": self.snapshot_hash,
            "asset_ids": list(self.asset_ids),
            "source": self.source,
        }


@dataclass(frozen=True)
class GapTarget:
    """一个压缩后的缺口目标区间。"""

    data_domain: str
    gap_start_at: datetime
    gap_end_at: datetime
    granularity: str = "1d"
    asset_id: str | None = None
    expected_count: int = 0
    exception_code: str | None = None
    exception_evidence: JsonDict = field(default_factory=dict)

    def scope_key(self) -> tuple[str, str, str, str, str]:
        """目标唯一约束的应用层等价键（asset 归一为空串）。"""

        return (
            self.data_domain,
            self.asset_id or "",
            self.gap_start_at.isoformat(),
            self.gap_end_at.isoformat(),
            self.granularity,
        )


@dataclass(frozen=True)
class PlanStep:
    """计划中的一个逻辑步骤：phase+数据域唯一。"""

    phase: str
    data_domain: str
    depends_on_phases: tuple[str, ...] = ()
    targets: tuple[GapTarget, ...] = ()
    estimated_requests: int = 0
    task_params: JsonDict = field(default_factory=dict)
    parallelizable: bool = False

    def step_id(self, run_id: str) -> str:
        """确定性步骤 ID：重复确认不会产生第二份逻辑步骤。"""

        return f"{run_id}:{self.phase}:{self.data_domain}"


@dataclass(frozen=True)
class RecoveryPlan:
    """preview 输出的补跑计划草稿。"""

    market: str
    cutoff_date: date
    gap_start_date: date | None
    universe: UniverseSnapshot
    plan_hash: str
    steps: tuple[PlanStep, ...]
    created_at: datetime
    executable: bool
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    calendar_source: str = "database"
    total_targets: int = 0
    total_estimated_requests: int = 0
    reused_draft: bool = False

    def to_dict(self) -> JsonDict:
        """序列化为 HTTP/CLI 可直接返回的结构。"""

        return {
            "market": self.market,
            "cutoff_date": self.cutoff_date.isoformat(),
            "gap_start_date": self.gap_start_date.isoformat() if self.gap_start_date else None,
            "universe": self.universe.to_dict(),
            "plan_hash": self.plan_hash,
            "executable": self.executable,
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "calendar_source": self.calendar_source,
            "total_targets": self.total_targets,
            "total_estimated_requests": self.total_estimated_requests,
            "created_at": self.created_at.isoformat(),
            "reused_draft": self.reused_draft,
            "steps": [
                {
                    "phase": step.phase,
                    "data_domain": step.data_domain,
                    "depends_on_phases": list(step.depends_on_phases),
                    "target_count": len(step.targets),
                    "estimated_requests": step.estimated_requests,
                    "parallelizable": step.parallelizable,
                }
                for step in self.steps
            ],
        }


@dataclass(frozen=True)
class RecoveryRunView:
    """get(run_id) 返回的批次稳定状态视图（PostgreSQL 事实 + 实时进度叠加）。"""

    run_id: str
    market: str
    status: str
    gate_status: str
    cutoff_date: date
    gap_start_date: date | None
    universe_id: str | None
    plan_hash: str
    steps: tuple[JsonDict, ...] = ()
    quality_result: JsonDict = field(default_factory=dict)
    summary: JsonDict = field(default_factory=dict)
    live_progress: JsonDict = field(default_factory=dict)
    exceptions: tuple[JsonDict, ...] = ()
    updated_at: datetime | None = None

    def to_dict(self) -> JsonDict:
        """序列化为控制台/API 结构。"""

        return {
            "run_id": self.run_id,
            "market": self.market,
            "status": self.status,
            "gate_status": self.gate_status,
            "cutoff_date": self.cutoff_date.isoformat(),
            "gap_start_date": self.gap_start_date.isoformat() if self.gap_start_date else None,
            "universe_id": self.universe_id,
            "plan_hash": self.plan_hash,
            "steps": [dict(step) for step in self.steps],
            "quality_result": dict(self.quality_result),
            "summary": dict(self.summary),
            "live_progress": dict(self.live_progress),
            "exceptions": [dict(item) for item in self.exceptions],
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
