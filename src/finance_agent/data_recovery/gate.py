"""RecoveryGate：补跑期间对普通调度任务的分层放行。

规格 10.4 / 12：
- always：实时行情、事件列表等始终允许运行。
- merge：与补跑相同数据域的采集任务允许运行，但依赖既有互斥键和幂等键
  与补跑分区自然去重，不产生双份 Provider 请求。
- requires_open：质量、指标、因子、筛选、候选池、推荐等派生任务只在
  门控为 open 时执行；被拦截时返回 blocked_by_recovery，不计失败、
  不消耗任务重试次数。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from finance_agent.data_recovery.models import TaskMergePolicy

JsonDict = dict[str, Any]

#: 被门控拦截的标准原因码（持久调度规格 6.3）。
BLOCKED_BY_RECOVERY = "recovery_domain_blocked"

_GATING_RECOVERY_STATUSES = {
    "approved",
    "running",
    "paused",
    "verifying",
    "attention_required",
}

#: 派生链路任务名前缀（规格 7.2）：要求门控 open。
_REQUIRES_OPEN_PREFIXES = (
    "quality.",
    "analytics.",
)

#: 始终放行的实时/事件采集任务名前缀。
_ALWAYS_JOB_PREFIXES = (
    "ashare.realtime_quotes",
    "ashare.events",
    "ashare.news_articles",
    "ashare.news_retention",
    "ashare.risk_sentiment",
    "crypto_future.derivatives",
)


@dataclass(frozen=True)
class GateDecision:
    """单个任务在当前门控状态下的执行决定。"""

    policy: TaskMergePolicy
    allowed: bool
    reason: str = ""
    blocked_domains: tuple[str, ...] = ()
    blockers: tuple[JsonDict, ...] = ()

    @property
    def blocked_by_recovery(self) -> bool:
        return self.reason == BLOCKED_BY_RECOVERY


def classify_recovery_policy(job_name: str) -> TaskMergePolicy:
    """按任务名归类补跑共存策略；未知任务默认 merge 保持保守。"""

    name = str(job_name or "")
    if name.startswith(_REQUIRES_OPEN_PREFIXES):
        return "requires_open"
    if name.startswith(_ALWAYS_JOB_PREFIXES):
        return "always"
    return "merge"


def evaluate_policy(policy: TaskMergePolicy, gate_status: str) -> GateDecision:
    """给定策略与门控状态输出决定；纯函数便于单元测试。"""

    if gate_status == "open":
        return GateDecision(policy=policy, allowed=True)
    if policy == "always":
        return GateDecision(policy=policy, allowed=True)
    if policy == "requires_open":
        return GateDecision(
            policy=policy,
            allowed=False,
            reason=BLOCKED_BY_RECOVERY,
        )
    return GateDecision(policy=policy, allowed=True)


class RecoveryGate:
    """读取活动批次的门控状态，并对具体任务给出执行决定。

    无活动批次时视同 open，调度器零开销直通。
    """

    def __init__(self, repository, *, market: str = "ashare") -> None:
        self.repository = repository
        self.market = market

    def current_state(self) -> tuple[str | None, str]:
        """返回 (run_id, gate_status)；无活动批次时 run_id 为 None。"""

        run = self.repository.get_active_run(self.market)
        if run is not None and str(run.status) in _GATING_RECOVERY_STATUSES:
            gate_status = str(run.gate_status)
            if str(run.status) in {"paused", "attention_required"}:
                gate_status = "degraded"
            elif gate_status == "open":
                gate_status = "recovering"
            return run.run_id, gate_status
        return None, "open"

    def blocked_domains(self) -> dict[str, tuple[JsonDict, ...]]:
        """返回活动批次仍未解决的阻塞目标，并按数据域分组。"""

        run_id, gate_status = self.current_state()
        if run_id is None or gate_status == "open":
            return {}
        finder = getattr(self.repository, "list_blocking_targets", None)
        if not callable(finder):
            return {}
        grouped: dict[str, list[JsonDict]] = {}
        for target in finder(run_id):
            domain = str(target.data_domain)
            grouped.setdefault(domain, []).append(
                {
                    "run_id": run_id,
                    "target_id": str(target.target_id),
                    "step_id": str(target.step_id),
                    "data_domain": domain,
                    "status": str(target.status),
                    "exception_code": getattr(target, "exception_code", None),
                }
            )
        return {domain: tuple(items) for domain, items in grouped.items()}

    def decide(
        self,
        job_name: str,
        *,
        required_data_domains: tuple[str, ...] = (),
    ) -> GateDecision:
        _, gate_status = self.current_state()
        policy = classify_recovery_policy(job_name)
        if gate_status == "open":
            return GateDecision(policy=policy, allowed=True)
        blocked = self.blocked_domains()
        intersections = tuple(
            sorted(
                {
                    str(domain)
                    for domain in required_data_domains
                    if str(domain) in blocked
                }
            )
        )
        if not intersections:
            return GateDecision(policy=policy, allowed=True)
        blockers = tuple(item for domain in intersections for item in blocked[domain])
        return GateDecision(
            policy="requires_open",
            allowed=False,
            reason=BLOCKED_BY_RECOVERY,
            blocked_domains=intersections,
            blockers=blockers,
        )

    def filter_due_states(self, due_states):
        """仿照调度器现有过滤器的 (runnable, blocked) 接口。

        blocked 元素为 (state, decision)，调用方可据此记录
        blocked_by_recovery 而不推进失败计数。
        """

        runnable = []
        blocked = []
        for state in due_states:
            params = getattr(state.job, "params", {}) or {}
            required_domains = tuple(
                str(item)
                for item in params.get("requires_data_domains", ())
                if str(item).strip()
            )
            decision = self.decide(
                state.job.name,
                required_data_domains=required_domains,
            )
            if decision.allowed:
                runnable.append(state)
            else:
                blocked.append((state, decision))
        return runnable, blocked
