"""确定性决策闸门。

闸门负责判断当前事实版本是否允许建议进入下一步动作。它不调用模型、
不联网，也不执行交易；Hermes 只能补充证据，不能绕过本模块的拒绝结果。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from finance_agent.storage.snapshot_contracts import DataSnapshot, canonical_json, normalize_datetime

DecisionGateStatus = Literal[
    "approved",
    "rejected",
    "pending_review",
    "data_unavailable",
    "expired",
]

HIGH_RISK_ACTIONS = frozenset({"buy", "sell", "swap", "reduce"})


@dataclass(frozen=True)
class DecisionGateInput:
    """一次闸门评估所需的只读输入。"""

    decision_type: str
    action: str
    snapshot: DataSnapshot | None
    evaluated_at: datetime
    evidence_ids: tuple[str, ...] = ()
    require_evidence: bool = False
    requires_human_confirmation: bool = False
    human_confirmed: bool = False
    conflict_detected: bool = False


@dataclass(frozen=True)
class DecisionGateResult:
    """闸门输出，允许下游持久化和审计。"""

    decision_gate_id: str
    status: DecisionGateStatus
    decision_type: str
    action: str
    data_snapshot_id: str | None
    reason_codes: tuple[str, ...]
    reasons: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    evaluated_at: datetime
    expires_at: datetime | None
    rule_version: str

    def to_record(self) -> dict[str, object]:
        """转换为决策闸门仓储记录。"""

        return {
            "decision_gate_id": self.decision_gate_id,
            "decision_type": self.decision_type,
            "action": self.action,
            "data_snapshot_id": self.data_snapshot_id,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "reasons": list(self.reasons),
            "evidence_ids": list(self.evidence_ids),
            "evaluated_at": self.evaluated_at,
            "expires_at": self.expires_at,
            "rule_version": self.rule_version,
            "payload": {"rule_version": self.rule_version},
        }


class DecisionGateService:
    """执行无副作用、可重复的确定性闸门检查。"""

    def __init__(self, *, max_age: timedelta = timedelta(minutes=10), rule_version: str = "gate-v1") -> None:
        if max_age <= timedelta(0):
            raise ValueError("max_age 必须大于 0")
        self.max_age = max_age
        self.rule_version = rule_version

    def evaluate(self, gate_input: DecisionGateInput) -> DecisionGateResult:
        """返回放行、拒绝、等待人工复核或数据不可用状态。"""

        decision_type = str(gate_input.decision_type).strip()
        action = str(gate_input.action).strip()
        if not decision_type:
            raise ValueError("decision_type 不能为空")
        if not action:
            raise ValueError("action 不能为空")
        evaluated_at = normalize_datetime(gate_input.evaluated_at, field_name="evaluated_at")
        evidence_ids = tuple(sorted({str(item).strip() for item in gate_input.evidence_ids if str(item).strip()}))
        reasons: list[tuple[str, str]] = []
        snapshot = gate_input.snapshot
        expires_at = None

        if snapshot is None:
            reasons.append(("snapshot_missing", "缺少不可变数据快照，无法进行决策。"))
            status: DecisionGateStatus = "data_unavailable"
            snapshot_id = None
        else:
            snapshot_id = snapshot.data_snapshot_id
            expires_at = snapshot.as_of + self.max_age
            age = evaluated_at - snapshot.as_of
            if snapshot.quality_status in {"unavailable", "invalid_server_time", "clock_skew"}:
                reasons.append(("snapshot_unavailable", f"快照质量状态为 {snapshot.quality_status}。"))
                status = "data_unavailable"
            elif age < timedelta(0):
                reasons.append(("snapshot_clock_skew", "评估时间早于快照时间，时间顺序不可信。"))
                status = "rejected"
            elif snapshot.quality_status in {"stale", "after_hours_snapshot"} or age > self.max_age:
                reasons.append(("snapshot_expired", "快照已超过本次决策允许的新鲜度。"))
                status = "expired"
            elif snapshot.quality_status == "conflict" or gate_input.conflict_detected:
                reasons.append(("snapshot_conflict", "输入数据存在来源冲突，不能生成可执行建议。"))
                status = "rejected"
            elif snapshot.quality_status == "partial":
                reasons.append(("snapshot_incomplete", "输入数据不完整，不能生成可执行建议。"))
                status = "rejected"
            else:
                status = "approved"

        if gate_input.require_evidence and not evidence_ids:
            reasons.append(("evidence_missing", "本次决策要求至少一条可追溯证据。"))
            if status == "approved":
                status = "rejected"

        requires_confirmation = gate_input.requires_human_confirmation or action in HIGH_RISK_ACTIONS
        if requires_confirmation and not gate_input.human_confirmed and status == "approved":
            reasons.append(("human_confirmation_required", "高风险动作必须经过人工确认。"))
            status = "pending_review"

        reason_codes = tuple(code for code, _ in reasons)
        reason_texts = tuple(reason for _, reason in reasons)
        identity = {
            "decision_type": decision_type,
            "action": action,
            "data_snapshot_id": snapshot_id,
            "status": status,
            "reason_codes": reason_codes,
            "evidence_ids": evidence_ids,
            "evaluated_at": evaluated_at.isoformat(),
            "rule_version": self.rule_version,
        }
        gate_hash = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()[:32]
        return DecisionGateResult(
            decision_gate_id=f"gate:{decision_type}:{gate_hash}",
            status=status,
            decision_type=decision_type,
            action=action,
            data_snapshot_id=snapshot_id,
            reason_codes=reason_codes,
            reasons=reason_texts,
            evidence_ids=evidence_ids,
            evaluated_at=evaluated_at,
            expires_at=expires_at,
            rule_version=self.rule_version,
        )
