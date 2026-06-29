"""用户投资画像与个性化建议服务。

本模块只做确定性画像读写和可审计建议推断。LLM 可以解释这些结果，但不能通过
本服务改写评分、信号方向或风险标记。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from finance_agent.storage.repositories import UserInvestmentProfileRepository

JsonDict = dict[str, Any]
DETERMINISTIC_FIELDS_UNCHANGED = [
    "asset_scores.total_score",
    "signal_snapshots.direction",
    "risk_findings.severity",
]


class InvestmentProfileRepository(Protocol):
    """画像服务依赖的最小仓储协议。"""

    def get_or_default(self, *, owner_id: str) -> Any:
        """读取画像；不存在时返回默认画像。"""

    def upsert_profile(self, **kwargs: Any) -> Any:
        """写入画像维度和审计证据。"""


class ProfileSignalStore(Protocol):
    """个性化建议服务读取的历史行为端口。"""

    def list_recent_decision_feedback(
        self,
        *,
        owner_id: str,
        limit: int = 50,
    ) -> list[JsonDict]:
        """读取近期决策反馈。"""

    def list_recent_review_outcomes(
        self,
        *,
        owner_id: str,
        limit: int = 50,
    ) -> list[JsonDict]:
        """读取近期复盘结果。"""


class UserInvestmentProfileService:
    """用户投资画像读写服务。"""

    def __init__(self, repository: InvestmentProfileRepository) -> None:
        self.repository = repository

    @classmethod
    def from_session(cls, session: Any) -> "UserInvestmentProfileService":
        """从数据库 Session 构造画像服务。"""

        return cls(UserInvestmentProfileRepository(session))

    def get_profile(self, *, owner_id: str) -> JsonDict:
        """返回画像结构化 payload。"""

        return serialize_profile(self.repository.get_or_default(owner_id=owner_id))

    def upsert_profile(
        self,
        *,
        owner_id: str,
        updates: JsonDict,
        source: JsonDict,
        evidence: Sequence[JsonDict],
        confidence_delta: Decimal = Decimal("0.25"),
    ) -> JsonDict:
        """写入画像更新，要求显式来源和证据链。"""

        if not source:
            raise ValueError("profile.upsert 写入必须提供 source。")
        if not evidence:
            raise ValueError("profile.upsert 写入必须提供 evidence。")
        if not updates:
            raise ValueError("profile.upsert 写入必须提供 updates。")
        profile = self.repository.upsert_profile(
            owner_id=owner_id,
            risk_appetite=updates.get("risk_appetite"),
            horizon=updates.get("horizon"),
            capital_scale=updates.get("capital_scale"),
            style_tendency=updates.get("style_tendency"),
            timing_posture=updates.get("timing_posture"),
            source=source,
            evidence=list(evidence),
            confidence_delta=confidence_delta,
            updated_at=updates.get("updated_at") if isinstance(updates.get("updated_at"), datetime) else None,
            payload={"source_service": "profile.upsert"},
        )
        return serialize_profile(profile)


class ProfileAdviceService:
    """从历史反馈和复盘中推断画像建议。"""

    def __init__(
        self,
        profile_service: UserInvestmentProfileService,
        store: ProfileSignalStore,
    ) -> None:
        self.profile_service = profile_service
        self.store = store

    def suggest_style(self, *, owner_id: str, limit: int = 50) -> JsonDict:
        """返回可审计的风格/择时建议，不修改确定性评分字段。"""

        profile = self.profile_service.get_profile(owner_id=owner_id)
        decisions = self.store.list_recent_decision_feedback(owner_id=owner_id, limit=limit)
        reviews = self.store.list_recent_review_outcomes(owner_id=owner_id, limit=limit)

        style = dict(profile.get("style_tendency") or {"value": 0.6, "theme": 0.4})
        risk_appetite = str(profile.get("risk_appetite") or "balanced")
        timing_posture = str(profile.get("timing_posture") or "neutral")
        evidence: list[JsonDict] = []
        reasons: list[str] = []

        theme_rejections = [
            item
            for item in decisions
            if str(item.get("action") or item.get("user_action") or "").lower()
            in {"reject", "rejected", "declined"}
            and str(item.get("style") or item.get("strategy_style") or "").lower() == "theme"
        ]
        if len(theme_rejections) >= 3:
            risk_appetite = "conservative"
            style = {"value": max(float(style.get("value", 0.6)), 0.75), "theme": 0.25}
            reasons.append("近期连续拒绝题材型建议，推断风险偏好需要更保守。")
            evidence.extend(decision_to_evidence(item) for item in theme_rejections[:5])

        chased_high_losses = [
            item
            for item in reviews
            if str(item.get("outcome") or "").lower() in {"loss", "failed", "negative"}
            and (
                "chased_high" in set(item.get("tags") or [])
                or Decimal(str(item.get("realized_return") or "0")) < 0
                and "chased_high" in str(item.get("reason") or "")
            )
        ]
        if chased_high_losses:
            timing_posture = "defensive"
            reasons.append("复盘中出现追高亏损记录，建议择时姿态转为防守。")
            evidence.extend(review_to_evidence(item) for item in chased_high_losses[:5])

        return {
            "owner_id": owner_id,
            "current_profile": profile,
            "suggested_risk_appetite": risk_appetite,
            "suggested_style_tendency": normalize_style_tendency(style),
            "suggested_timing_posture": timing_posture,
            "confidence": resolve_advice_confidence(evidence),
            "reasons": reasons,
            "evidence": evidence,
            "llm_role": "explanation_only",
            "deterministic_fields_unchanged": list(DETERMINISTIC_FIELDS_UNCHANGED),
        }


def serialize_profile(profile: Any) -> JsonDict:
    """把 ORM 或测试替身序列化为工具输出结构。"""

    updated_at = getattr(profile, "updated_at", None)
    return {
        "profile_id": getattr(profile, "profile_id"),
        "owner_id": getattr(profile, "owner_id"),
        "risk_appetite": getattr(profile, "risk_appetite"),
        "horizon": getattr(profile, "horizon"),
        "capital_scale": getattr(profile, "capital_scale"),
        "style_tendency": dict(getattr(profile, "style_tendency") or {}),
        "timing_posture": getattr(profile, "timing_posture"),
        "dimension_confidence": dict(getattr(profile, "dimension_confidence") or {}),
        "source": dict(getattr(profile, "source") or {}),
        "status": getattr(profile, "status"),
        "updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else updated_at,
        "payload": dict(getattr(profile, "payload") or {}),
    }


def decision_to_evidence(item: JsonDict) -> JsonDict:
    """把决策反馈转换为画像建议证据。"""

    return {
        "type": "decision",
        "id": str(item.get("decision_id") or item.get("id")),
        "signal": "theme_rejected",
        "payload": dict(item),
    }


def review_to_evidence(item: JsonDict) -> JsonDict:
    """把复盘记录转换为画像建议证据。"""

    return {
        "type": "review",
        "id": str(item.get("review_task_id") or item.get("id")),
        "signal": "chased_high_loss",
        "payload": dict(item),
    }


def normalize_style_tendency(style: JsonDict) -> JsonDict:
    """把 value/theme 风格权重归一到 0~1。"""

    value = max(float(style.get("value", 0.6)), 0.0)
    theme = max(float(style.get("theme", 0.4)), 0.0)
    total = value + theme
    if total <= 0:
        return {"value": 0.6, "theme": 0.4}
    return {"value": round(value / total, 6), "theme": round(theme / total, 6)}


def resolve_advice_confidence(evidence: Sequence[JsonDict]) -> float:
    """根据证据数量给出保守置信度。"""

    if not evidence:
        return 0.1
    return round(min(0.35 + len(evidence) * 0.08, 0.85), 6)
