"""持久调度准入控制器。

所有阻塞条件统一转换为稳定原因码，调用方负责将结论持久化。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class AdmissionDecision:
    """单个计划任务的准入结论。"""

    allowed: bool
    reason_code: str | None = None
    reason_detail: JsonDict = field(default_factory=dict)
    recheck_at: datetime | None = None
    required_data_domains: tuple[str, ...] = ()
    blocking_task_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdmissionSnapshot:
    """一次准入扫描使用的一致状态快照。"""

    now: datetime
    scheduler_paused: bool = False
    unsatisfied_dependencies: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    recovery_blocked_domains: Mapping[str, tuple[JsonDict, ...]] = field(
        default_factory=dict
    )
    trading_session_open: Mapping[str, bool] = field(default_factory=dict)
    active_mutex_keys: set[str] = field(default_factory=set)
    resource_pool_limits: Mapping[str, int] = field(default_factory=dict)
    resource_pool_running: Mapping[str, int] = field(default_factory=dict)


class SchedulerAdmissionController:
    """在一个深 interface 后集中执行所有调度准入规则。"""

    def evaluate(self, task: Any, snapshot: AdmissionSnapshot) -> AdmissionDecision:
        """按稳定顺序返回首个阻塞原因，全部通过时允许领取。"""

        required_domains = tuple(
            sorted(
                {
                    str(domain).strip()
                    for domain in (getattr(task, "required_data_domains", None) or ())
                    if str(domain).strip()
                }
            )
        )
        if snapshot.scheduler_paused:
            return self._blocked("scheduler_paused", required_domains=required_domains)
        if not bool(getattr(task, "config_enabled", True)):
            return self._blocked("config_disabled", required_domains=required_domains)

        blocked_until = getattr(task, "blocked_until", None)
        if blocked_until is not None and _as_utc(blocked_until) > _as_utc(snapshot.now):
            return self._blocked(
                "retry_backoff",
                required_domains=required_domains,
                recheck_at=blocked_until,
            )

        dependency_blockers = tuple(
            snapshot.unsatisfied_dependencies.get(str(task.task_id), ())
        )
        if dependency_blockers:
            return self._blocked(
                "dependency_not_satisfied",
                required_domains=required_domains,
                blocking_task_ids=dependency_blockers,
                detail={"blocking_task_ids": list(dependency_blockers)},
            )

        if snapshot.trading_session_open.get(str(task.task_id), True) is False:
            return self._blocked(
                "outside_trading_session",
                required_domains=required_domains,
            )

        blocked_domains = sorted(
            domain
            for domain in required_domains
            if domain in snapshot.recovery_blocked_domains
        )
        if blocked_domains:
            blockers = tuple(
                blocker
                for domain in blocked_domains
                for blocker in snapshot.recovery_blocked_domains[domain]
            )
            blocker_ids = tuple(
                str(blocker.get("target_id"))
                for blocker in blockers
                if blocker.get("target_id")
            )
            return self._blocked(
                "recovery_domain_blocked",
                required_domains=required_domains,
                blocking_task_ids=blocker_ids,
                detail={"domains": blocked_domains, "blockers": list(blockers)},
            )

        mutex_key = str(getattr(task, "mutex_key", None) or "").strip()
        if mutex_key and mutex_key in snapshot.active_mutex_keys:
            return self._blocked(
                "mutex_busy",
                required_domains=required_domains,
                detail={"mutex_key": mutex_key},
            )

        pool = str(getattr(task, "resource_pool", "default") or "default")
        pool_limit = snapshot.resource_pool_limits.get(pool)
        pool_running = int(snapshot.resource_pool_running.get(pool, 0))
        if pool_limit is not None and pool_running >= int(pool_limit):
            return self._blocked(
                "resource_pool_full",
                required_domains=required_domains,
                detail={
                    "resource_pool": pool,
                    "running": pool_running,
                    "limit": int(pool_limit),
                },
            )

        return AdmissionDecision(
            allowed=True,
            required_data_domains=required_domains,
        )

    @staticmethod
    def _blocked(
        reason_code: str,
        *,
        required_domains: tuple[str, ...],
        detail: JsonDict | None = None,
        recheck_at: datetime | None = None,
        blocking_task_ids: tuple[str, ...] = (),
    ) -> AdmissionDecision:
        return AdmissionDecision(
            allowed=False,
            reason_code=reason_code,
            reason_detail=dict(detail or {}),
            recheck_at=recheck_at,
            required_data_domains=required_domains,
            blocking_task_ids=blocking_task_ids,
        )


def _as_utc(value: datetime) -> datetime:
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(UTC)
