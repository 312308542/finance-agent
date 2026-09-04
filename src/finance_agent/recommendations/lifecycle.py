"""推荐设置、当前状态和状态迁移的数据契约。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

JsonDict = dict[str, Any]
RecommendationStateName = Literal[
    "discovered",
    "watch",
    "setup_confirming",
    "buy_ready",
    "active",
    "weakening",
    "exit_pending",
    "exited",
    "cooldown",
]
RECOMMENDATION_STATES: tuple[RecommendationStateName, ...] = (
    "discovered",
    "watch",
    "setup_confirming",
    "buy_ready",
    "active",
    "weakening",
    "exit_pending",
    "exited",
    "cooldown",
)
LEGAL_STATE_TRANSITIONS: Mapping[
    RecommendationStateName, frozenset[RecommendationStateName]
] = {
    "discovered": frozenset({"discovered", "watch", "setup_confirming", "buy_ready"}),
    "watch": frozenset({"watch", "setup_confirming", "buy_ready"}),
    "setup_confirming": frozenset({"watch", "setup_confirming", "buy_ready"}),
    "buy_ready": frozenset({"watch", "buy_ready", "active", "exit_pending", "cooldown"}),
    "active": frozenset({"active", "weakening", "exit_pending", "cooldown"}),
    "weakening": frozenset({"active", "weakening", "exit_pending", "cooldown"}),
    "exit_pending": frozenset({"exit_pending", "exited", "cooldown"}),
    "exited": frozenset({"exited", "cooldown"}),
    "cooldown": frozenset({"cooldown", "watch", "setup_confirming", "buy_ready"}),
}


@dataclass(frozen=True)
class StockSetup:
    """绑定统一决策快照的单股交易设置。"""

    setup_id: str
    owner_id: str
    decision_snapshot_id: str
    asset_id: str
    strategy_id: str
    setup_type: str
    planned_horizon_days: int
    entry_zone: JsonDict
    invalidation_price: Decimal | None
    target_zone: JsonDict
    expected_net_return: Decimal | None
    downside_risk: Decimal | None
    confidence: Decimal
    as_of: datetime
    payload: JsonDict


@dataclass(frozen=True)
class RecommendationState:
    """一个用户、策略和资产唯一的一份当前生命周期状态。"""

    state_id: str
    owner_id: str
    strategy_id: str
    asset_id: str
    setup_id: str | None
    current_state: RecommendationStateName
    previous_state: RecommendationStateName | None
    decision_snapshot_id: str
    state_changed_at: datetime
    consecutive_valid_closes: int
    active_days: int
    cooldown_until: date | None
    payload: JsonDict


@dataclass(frozen=True)
class LifecycleEvidence:
    """单个交易截面的生命周期证据。"""

    owner_id: str
    strategy_id: str
    asset_id: str
    setup_id: str | None
    decision_snapshot_id: str
    as_of: datetime
    trade_date: date
    eligible: bool
    alpha_score: float
    entry_threshold: float
    retention_threshold: float
    structure_invalidated: bool
    high_quality_intraday_breakout: bool
    ordinary_volatility: bool
    held: bool
    data_stale: bool
    sold: bool
    cooldown_until: date | None
    new_independent_catalyst: bool
    new_structure_setup: bool
    reason_codes: tuple[str, ...]
    payload: JsonDict


@dataclass(frozen=True)
class RecommendationTransition:
    """一次可审计且可幂等重放的生命周期迁移。"""

    event_id: str
    state_id: str
    owner_id: str
    strategy_id: str
    asset_id: str
    setup_id: str | None
    from_state: RecommendationStateName | None
    to_state: RecommendationStateName
    reason_codes: tuple[str, ...]
    decision_snapshot_id: str
    occurred_at: datetime
    consecutive_valid_closes: int
    active_days: int
    cooldown_until: date | None
    payload: JsonDict

    @property
    def state(self) -> RecommendationState:
        """把迁移结果投影为下一份当前状态。"""

        return RecommendationState(
            state_id=self.state_id,
            owner_id=self.owner_id,
            strategy_id=self.strategy_id,
            asset_id=self.asset_id,
            setup_id=self.setup_id,
            current_state=self.to_state,
            previous_state=self.from_state,
            decision_snapshot_id=self.decision_snapshot_id,
            state_changed_at=self.occurred_at,
            consecutive_valid_closes=self.consecutive_valid_closes,
            active_days=self.active_days,
            cooldown_until=self.cooldown_until,
            payload=dict(self.payload),
        )


class RecommendationLifecycleEngine:
    """依据显式点时证据计算唯一、可重放的推荐状态迁移。"""

    def transition(
        self,
        previous: RecommendationState | None,
        evidence: LifecycleEvidence,
    ) -> RecommendationTransition:
        """计算下一状态，不读取数据库或隐式获取当前时间。"""

        self._validate_identity(previous, evidence)
        from_state = previous.current_state if previous is not None else None
        to_state, reasons, valid_closes, active_days, cooldown_until = self._decide(
            previous,
            evidence,
        )
        self.ensure_legal_transition(
            from_state,
            to_state,
            execution_registered=evidence.held,
        )

        reason_codes = _unique_codes((*reasons, *evidence.reason_codes))
        state_id = previous.state_id if previous is not None else _stable_id(
            "recommendation-state",
            evidence.owner_id,
            evidence.strategy_id,
            evidence.asset_id,
        )
        event_id = _stable_id(
            "recommendation-event",
            state_id,
            from_state,
            to_state,
            reason_codes,
            evidence.decision_snapshot_id,
            evidence.as_of.isoformat(),
        )
        transition_payload = dict(evidence.payload)
        if to_state in {"setup_confirming", "buy_ready"} and evidence.eligible:
            transition_payload["last_valid_trade_date"] = evidence.trade_date.isoformat()
        if to_state in {"active", "weakening"}:
            transition_payload["last_active_trade_date"] = evidence.trade_date.isoformat()
        return RecommendationTransition(
            event_id=event_id,
            state_id=state_id,
            owner_id=evidence.owner_id,
            strategy_id=evidence.strategy_id,
            asset_id=evidence.asset_id,
            setup_id=evidence.setup_id,
            from_state=from_state,
            to_state=to_state,
            reason_codes=reason_codes,
            decision_snapshot_id=evidence.decision_snapshot_id,
            occurred_at=evidence.as_of,
            consecutive_valid_closes=valid_closes,
            active_days=active_days,
            cooldown_until=cooldown_until,
            payload=transition_payload,
        )

    @staticmethod
    def ensure_legal_transition(
        from_state: RecommendationStateName | None,
        to_state: RecommendationStateName,
        *,
        execution_registered: bool = False,
    ) -> None:
        """拒绝绕过生命周期约束的状态跳转。"""

        if from_state is None:
            allowed = frozenset({"discovered", "watch", "setup_confirming", "buy_ready"})
        else:
            allowed = LEGAL_STATE_TRANSITIONS[from_state]
        if (
            execution_registered
            and to_state == "active"
            and from_state in {None, "discovered", "watch", "setup_confirming", "buy_ready"}
        ):
            return
        if to_state not in allowed:
            raise ValueError(f"非法的推荐生命周期迁移: {from_state!r} -> {to_state!r}")

    @staticmethod
    def _validate_identity(
        previous: RecommendationState | None,
        evidence: LifecycleEvidence,
    ) -> None:
        if previous is None:
            return
        previous_identity = (
            previous.owner_id,
            previous.strategy_id,
            previous.asset_id,
        )
        evidence_identity = (
            evidence.owner_id,
            evidence.strategy_id,
            evidence.asset_id,
        )
        if previous_identity != evidence_identity:
            raise ValueError("生命周期证据与上一状态的用户、策略或资产不一致。")

    def _decide(
        self,
        previous: RecommendationState | None,
        evidence: LifecycleEvidence,
    ) -> tuple[
        RecommendationStateName,
        tuple[str, ...],
        int,
        int,
        date | None,
    ]:
        current = previous.current_state if previous is not None else None
        previous_valid_closes = previous.consecutive_valid_closes if previous else 0
        previous_active_days = previous.active_days if previous else 0
        previous_cooldown = previous.cooldown_until if previous else None
        previous_active_date = str(
            (previous.payload if previous is not None else {}).get(
                "last_active_trade_date",
                "",
            )
        )
        next_active_days = previous_active_days + (
            0 if previous_active_date == evidence.trade_date.isoformat() else 1
        )
        entry_eligible = evidence.eligible and evidence.alpha_score >= evidence.entry_threshold
        retained = evidence.alpha_score >= evidence.retention_threshold

        if evidence.sold:
            return (
                "cooldown",
                ("sold_entered_cooldown",),
                0,
                previous_active_days,
                evidence.cooldown_until or previous_cooldown,
            )

        if current == "cooldown":
            new_setup = (
                entry_eligible
                and evidence.new_independent_catalyst
                and evidence.new_structure_setup
                and evidence.setup_id != previous.setup_id
            )
            if new_setup:
                return (
                    "setup_confirming",
                    ("cooldown_broken_by_new_setup",),
                    1,
                    0,
                    None,
                )
            if previous_cooldown is None or evidence.trade_date <= previous_cooldown:
                return (
                    "cooldown",
                    ("cooldown_active",),
                    0,
                    previous_active_days,
                    previous_cooldown,
                )
            if entry_eligible and evidence.high_quality_intraday_breakout:
                return "buy_ready", ("high_quality_intraday_breakout",), 1, 0, None
            if entry_eligible:
                return "setup_confirming", ("cooldown_completed",), 1, 0, None
            return "watch", ("cooldown_completed",), 0, 0, None

        if evidence.structure_invalidated:
            if current in {"buy_ready", "active", "weakening", "exit_pending"}:
                return (
                    "exit_pending",
                    ("structure_invalidated",),
                    previous_valid_closes,
                    previous_active_days,
                    previous_cooldown,
                )
            return "watch", ("structure_invalidated",), 0, 0, None

        if evidence.held and current not in {"active", "weakening", "exit_pending"}:
            return (
                "active",
                ("position_execution_registered",),
                previous_valid_closes,
                max(previous_active_days, 1),
                None,
            )

        if evidence.data_stale and current is not None:
            return (
                current,
                ("stale_evidence_state_retained",),
                previous_valid_closes,
                previous_active_days,
                previous_cooldown,
            )

        if current == "active":
            if retained:
                reasons = ["retention_threshold_met"]
                if evidence.ordinary_volatility:
                    reasons.append("ordinary_volatility_tolerated")
                return (
                    "active",
                    tuple(reasons),
                    previous_valid_closes,
                    next_active_days,
                    None,
                )
            return (
                "weakening",
                ("retention_threshold_missed",),
                previous_valid_closes,
                next_active_days,
                None,
            )

        if current == "weakening":
            if retained:
                return (
                    "active",
                    ("retention_threshold_recovered",),
                    previous_valid_closes,
                    next_active_days,
                    None,
                )
            return (
                "weakening",
                ("retention_threshold_missed",),
                previous_valid_closes,
                next_active_days,
                None,
            )

        if current == "exit_pending":
            return (
                "exit_pending",
                ("exit_execution_pending",),
                previous_valid_closes,
                previous_active_days,
                previous_cooldown,
            )
        if current == "exited":
            return (
                "cooldown",
                ("post_exit_cooldown",),
                0,
                previous_active_days,
                evidence.cooldown_until,
            )

        if entry_eligible and evidence.high_quality_intraday_breakout:
            return "buy_ready", ("high_quality_intraday_breakout",), 1, 0, None

        same_setup = previous is not None and evidence.setup_id == previous.setup_id
        if entry_eligible:
            previous_valid_date = str(
                (previous.payload if previous is not None else {}).get(
                    "last_valid_trade_date",
                    "",
                )
            )
            if (
                current == "setup_confirming"
                and same_setup
                and previous_valid_date == evidence.trade_date.isoformat()
            ):
                return (
                    "setup_confirming",
                    ("same_trade_date_confirmation_retained",),
                    previous_valid_closes,
                    0,
                    None,
                )
            valid_closes = previous_valid_closes + 1 if same_setup else 1
            if current == "buy_ready" or valid_closes >= 2:
                return "buy_ready", ("two_valid_closes_confirmed",), valid_closes, 0, None
            return "setup_confirming", ("setup_confirmation_started",), valid_closes, 0, None

        return "watch", ("entry_threshold_not_met",), 0, 0, None


def _unique_codes(codes: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(code for code in codes if code))


def _stable_id(prefix: str, *parts: object) -> str:
    canonical = json.dumps(parts, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}:{digest}"
