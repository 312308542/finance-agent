"""推荐结果可执行性闸门。

本模块只判断推荐运行是否能被聊天/报告称为“可执行候选清单”，不改变推荐分数、
动作或排序。模型只能读取这里的结论并解释，不能覆盖闸门结果。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from finance_agent.storage.repositories import is_smoke_recommendation_run

JsonDict = dict[str, Any]
DEFAULT_MAX_RECOMMENDATION_AGE = timedelta(days=7)


@dataclass(frozen=True)
class RecommendationReadiness:
    """推荐运行可执行性判断结果。"""

    status: str
    executable: bool
    reasons: list[str]
    checked_at: datetime

    def to_dict(self) -> JsonDict:
        """转换为 JSON 友好结构。"""

        return {
            "status": self.status,
            "executable": self.executable,
            "reasons": list(self.reasons),
            "checked_at": self.checked_at.isoformat(),
        }


def evaluate_recommendation_readiness(
    *,
    run: Any | None,
    recommendations: list[Any],
    as_of: datetime | None = None,
    max_age: timedelta = DEFAULT_MAX_RECOMMENDATION_AGE,
) -> RecommendationReadiness:
    """评估推荐运行是否可作为可执行候选清单。"""

    checked_at = normalize_datetime(as_of or datetime.now(UTC))
    reasons: list[str] = []
    if run is None:
        reasons.append("missing_run")
    else:
        if getattr(run, "status", None) != "available":
            reasons.append("run_unavailable")
        if is_smoke_recommendation_run(run):
            reasons.append("smoke")
        finished_at = normalize_optional_datetime(
            getattr(run, "finished_at", None) or getattr(run, "started_at", None)
        )
        if finished_at is None:
            reasons.append("missing_finished_at")
        elif finished_at > checked_at + timedelta(minutes=5):
            reasons.append("future_timestamp")
        elif checked_at - finished_at > max_age:
            reasons.append("stale")
        reasons.extend(payload_quality_reasons(getattr(run, "payload", None)))

    if not recommendations:
        reasons.append("empty_recommendations")
    for recommendation in recommendations:
        reasons.extend(
            payload_quality_reasons(
                getattr(recommendation, "payload", None),
                require_backtest=True,
            )
        )

    unique_reasons = sorted(set(reasons))
    executable = not unique_reasons
    return RecommendationReadiness(
        status="ready" if executable else "blocked",
        executable=executable,
        reasons=unique_reasons,
        checked_at=checked_at,
    )


def payload_quality_reasons(payload: Any, *, require_backtest: bool = False) -> list[str]:
    """从推荐 payload 中提取数据质量阻断原因。"""

    if not isinstance(payload, dict):
        return []
    reasons: list[str] = []
    if truthy_flag(payload.get("sandbox")) or truthy_flag(payload.get("is_sandbox")):
        reasons.append("sandbox")
    if truthy_flag(payload.get("smoke")) or str(payload.get("source") or "").lower() == "smoke":
        reasons.append("smoke")

    for key in ("data_quality", "readiness", "quality_gate"):
        item = payload.get(key)
        if isinstance(item, dict):
            status = str(item.get("status") or item.get("freshness_status") or "").lower()
            if status in {"partial", "stale", "missing", "unavailable", "failed", "blocked"}:
                reasons.append("data_quality")
            nested_reasons = item.get("reasons")
            if isinstance(nested_reasons, list):
                reasons.extend(str(reason) for reason in nested_reasons if reason)
    backtest = payload.get("backtest_evidence")
    if require_backtest and "backtest_evidence" not in payload:
        reasons.append("missing_backtest_evidence")
    elif isinstance(backtest, dict) and str(backtest.get("status") or "").lower() == "missing":
        reasons.append("missing_backtest_evidence")
    return reasons


def truthy_flag(value: Any) -> bool:
    """解析 payload 中的布尔标记。"""

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return False


def normalize_optional_datetime(value: Any) -> datetime | None:
    """把常见时间表示转换为带时区 datetime。"""

    if value is None:
        return None
    if isinstance(value, datetime):
        return normalize_datetime(value)
    if isinstance(value, str) and value.strip():
        try:
            return normalize_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def normalize_datetime(value: datetime) -> datetime:
    """统一时间比较口径。"""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
