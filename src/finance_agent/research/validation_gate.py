"""策略历史、前向和运行时准入门控。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

JsonDict = dict[str, Any]

MINIMUM_CROSS_SECTIONS = 120
MINIMUM_T20_SAMPLES = 60
MAX_DRAWDOWN_GAP = 0.05
MINIMUM_RANK_IC = 0.03
MAX_WEEKLY_TURNOVER = 0.35
MAX_UNEXECUTABLE_RATE = 0.05


@dataclass(frozen=True)
class ValidationDecision:
    """一次策略验证门控决定。"""

    allowed: bool
    current_state: str
    next_state: str
    evidence_id: str | None
    reason_codes: tuple[str, ...]
    metrics: JsonDict


class StrategyValidationGate:
    """只消费已持久化指标，不直接读取行情或重新运行回测。"""

    def evaluate_history(self, result: Any) -> ValidationDecision:
        """判断历史回测结果是否具备策略准入资格。"""

        payload = dict(_value(result, "payload", {}) or {})
        metrics = dict(_value(result, "metrics", {}) or {})
        schema_version = str(payload.get("schema_version") or metrics.get("schema_version") or "")
        evidence_id = _optional_text(_value(result, "backtest_id", None))
        if schema_version != "strategy_walk_forward_v2":
            return ValidationDecision(
                allowed=False,
                current_state="research",
                next_state="research",
                evidence_id=evidence_id,
                reason_codes=("legacy_backtest_not_gating_eligible",),
                metrics=metrics,
            )

        reasons: list[str] = []
        if str(_value(result, "status", "")) not in {"available", "completed"}:
            reasons.append("historical_result_not_available")
        cross_sections = int(
            metrics.get("valid_cross_sections")
            or metrics.get("total_cross_sections")
            or 0
        )
        if cross_sections < MINIMUM_CROSS_SECTIONS:
            reasons.append("valid_cross_sections_below_minimum")
        if metrics.get("gate_passed") is False:
            reasons.append("historical_gate_failed")
        horizon_means = metrics.get("horizon_mean_excess_returns") or metrics.get("returns_by_horizon") or {}
        for horizon in ("5", "10", "20"):
            value = _number(horizon_means.get(horizon, horizon_means.get(int(horizon))))
            if value is None or value <= 0:
                reasons.append(f"t{horizon}_excess_not_positive")
        bootstrap = metrics.get("t10_block_bootstrap") or {}
        ci = bootstrap.get("ci_95") or bootstrap.get("ci95") or ()
        lower = _number(ci[0]) if isinstance(ci, Sequence) and not isinstance(ci, (str, bytes)) and ci else None
        if lower is not None and lower <= 0:
            reasons.append("t10_bootstrap_lower_bound_not_positive")
        if int(metrics.get("positive_t10_phase_count") or 0) < 2:
            reasons.append("fewer_than_two_positive_t10_phases")
        drawdown_gap = _number(metrics.get("drawdown_gap"))
        if drawdown_gap is not None and drawdown_gap > MAX_DRAWDOWN_GAP:
            reasons.append("drawdown_gap_above_limit")
        rank_ic = metrics.get("rank_ic") or {}
        rank_ic_mean = _number(rank_ic.get("mean")) if isinstance(rank_ic, Mapping) else None
        if rank_ic_mean is not None and rank_ic_mean < MINIMUM_RANK_IC:
            reasons.append("rank_ic_below_minimum")
        turnover = metrics.get("turnover") or {}
        weekly_turnover = _number(turnover.get("weekly_mean")) if isinstance(turnover, Mapping) else None
        if weekly_turnover is not None and weekly_turnover > MAX_WEEKLY_TURNOVER:
            reasons.append("turnover_above_limit")
        execution = metrics.get("execution") or {}
        unexecutable_rate = _number(execution.get("unexecutable_rate")) if isinstance(execution, Mapping) else None
        if unexecutable_rate is not None and unexecutable_rate > MAX_UNEXECUTABLE_RATE:
            reasons.append("unexecutable_rate_above_limit")

        normalized_reasons = tuple(dict.fromkeys(reasons))
        return ValidationDecision(
            allowed=not normalized_reasons,
            current_state="research",
            next_state="historical_passed" if not normalized_reasons else "research",
            evidence_id=evidence_id if not normalized_reasons else None,
            reason_codes=normalized_reasons,
            metrics=metrics,
        )

    def evaluate_forward(self, *, state: Any, outcomes: Any) -> ValidationDecision:
        """按滚动前向结果决定保持、晋级或停用。"""

        current_state = str(_value(state, "state", "research"))
        metrics = _normalize_forward_metrics(outcomes)
        reasons: list[str] = []
        t20_count = int(metrics.get("t20_count") or 0)
        if t20_count < MINIMUM_T20_SAMPLES:
            reasons.append("t20_samples_below_60")
        rolling_excess = _number(metrics.get("rolling_excess"))
        if rolling_excess is not None and rolling_excess < 0:
            reasons.append("rolling_excess_negative")
        median_excess = _number(metrics.get("median_excess"))
        if median_excess is not None and median_excess <= 0:
            reasons.append("median_excess_not_positive")

        failure_count = int(_value(state, "consecutive_failure_count", 0) or 0)
        hard_failure = bool(
            "rolling_excess_negative" in reasons
            or "median_excess_not_positive" in reasons
        )
        if "rolling_excess_negative" in reasons:
            next_state = "disabled"
        elif hard_failure and failure_count + 1 >= 3:
            next_state = "disabled"
        elif t20_count >= MINIMUM_T20_SAMPLES and not reasons:
            next_state = "validated" if current_state in {"trial", "validated"} else current_state
        else:
            next_state = current_state
        allowed = next_state == "validated"
        if current_state == "disabled":
            next_state = "disabled"
            allowed = False
        return ValidationDecision(
            allowed=allowed,
            current_state=current_state,
            next_state=next_state,
            evidence_id=_optional_text(_value(state, "historical_evidence_id", None)),
            reason_codes=tuple(dict.fromkeys(reasons)),
            metrics=metrics,
        )

    def evaluate_runtime(self, trial_state: Any, *, action: str) -> ValidationDecision:
        """判断运行时动作是否允许进入新增买入链路。"""

        current_state = str(_value(trial_state, "state", "research"))
        normalized_action = str(action or "").lower()
        exit_actions = {"hold", "watch", "reduce", "sell", "exit", "unexecutable", "add_blocked"}
        buy_actions = {"buy", "buy_candidate", "strong_buy", "buy_ready", "add"}
        if current_state == "validated":
            allowed = True
            reasons: tuple[str, ...] = ()
        elif current_state == "disabled":
            allowed = normalized_action in {"reduce", "sell", "exit", "unexecutable", "hold", "watch"}
            reasons = () if allowed else ("strategy_disabled_new_buy_blocked",)
        elif normalized_action in buy_actions:
            allowed = False
            reasons = ("strategy_not_validated_new_buy_blocked",)
        else:
            allowed = normalized_action in exit_actions or not normalized_action
            reasons = () if allowed else ("strategy_state_action_blocked",)
        return ValidationDecision(
            allowed=allowed,
            current_state=current_state,
            next_state=current_state,
            evidence_id=_optional_text(_value(trial_state, "historical_evidence_id", None)),
            reason_codes=reasons,
            metrics={},
        )


def _normalize_forward_metrics(outcomes: Any) -> JsonDict:
    if isinstance(outcomes, Mapping):
        metrics = dict(outcomes)
        sample_counts = metrics.get("sample_counts") or {}
        medians = metrics.get("median_excess_returns") or {}
        metrics.setdefault("t20_count", sample_counts.get("20", sample_counts.get(20, 0)))
        metrics.setdefault("median_excess", medians.get("20", medians.get(20)))
        if metrics.get("rolling_excess") is None:
            metrics["rolling_excess"] = metrics.get("rolling_median_excess")
        return metrics
    rows = list(outcomes or ()) if isinstance(outcomes, Sequence) and not isinstance(outcomes, (str, bytes)) else []
    t20 = [row for row in rows if int(_value(row, "horizon_days", 0) or 0) == 20]
    values = [_number(_value(row, "excess_return", None)) for row in t20]
    values = [value for value in values if value is not None]
    return {
        "t20_count": len(values),
        "median_excess": sorted(values)[len(values) // 2] if values else None,
        "rolling_excess": sum(values) / len(values) if values else None,
    }


def _value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _number(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError, ArithmeticError):
        return None
