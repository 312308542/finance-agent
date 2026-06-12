"""人工确认、订单草案和外部执行登记服务。

本服务只编排用户确认后的闭环状态，不连接券商或交易所，也不执行真实下单。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy.orm import Session

from finance_agent.application.memory_service import MemoryService
from finance_agent.storage.orm import DecisionLogORM, OrderDraftORM
from finance_agent.storage.repositories import (
    ACTION_LOOP_DISCLAIMER,
    ActionLoopRepository,
)

JsonDict = dict[str, Any]
EXECUTABLE_ACTIONS = {"buy", "sell", "add", "reduce"}
HIGH_RISK_ACTIONS = {"sell", "reduce"}
APPROVED_REVIEW_STATUSES = {"approved_by_review", "pending_user_confirmation"}


class FeedbackMemoryService(Protocol):
    """确认决策时需要的用户反馈记忆端口。"""

    def record_user_feedback(self, **kwargs: Any) -> Any:
        """记录用户反馈并反写 Finance Memory。"""


class ActionRepository(Protocol):
    """订单草案仓储端口。"""

    def supersede_active_order_drafts(
        self,
        *,
        decision_log_id: str,
        superseded_at: datetime | None = None,
    ) -> int:
        """把同一决策下仍有效的旧草案置为 superseded。"""

    def upsert_order_draft(self, **kwargs: Any) -> OrderDraftORM:
        """写入订单草案。"""


@dataclass(frozen=True)
class DecisionConfirmationResult:
    """用户确认后的推进结果。"""

    decision_id: str
    feedback_decision_id: str
    status: str
    can_create_order_draft: bool
    suggested_action: str


class ActionLoopService:
    """人工确认与订单草案服务。"""

    def __init__(
        self,
        session: Session,
        *,
        memory_service: FeedbackMemoryService | None = None,
        action_repository: ActionRepository | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.session = session
        self.memory_service = memory_service or MemoryService(session)
        self.action_repository = action_repository or ActionLoopRepository(session)
        self.now = now or (lambda: datetime.now().astimezone())

    def confirm_decision(
        self,
        *,
        decision_log_id: str,
        feedback: str,
        comment: str | None = None,
        modified_action: str | None = None,
    ) -> DecisionConfirmationResult:
        """记录用户确认/反馈，并判断是否可以继续生成订单草案。"""

        decision = self._get_decision(decision_log_id)
        user_action = resolve_feedback_user_action(
            feedback=feedback,
            modified_action=modified_action,
        )
        feedback_payload = build_action_loop_feedback_payload(
            decision=decision,
            feedback=feedback,
            comment=comment,
            modified_action=modified_action,
            user_action=user_action,
        )
        update_decision_feedback_state(
            decision=decision,
            feedback_payload=feedback_payload,
            user_action=user_action,
        )
        feedback_decision = self.memory_service.record_user_feedback(
            feedback_id=build_feedback_decision_id(decision_id=decision_log_id, as_of=self.now()),
            owner_id=decision.owner_id,
            feedback_type="decision_confirmation",
            suggested_action=decision.suggested_action,
            user_action=user_action,
            summary=build_action_loop_feedback_summary(
                decision=decision,
                feedback=feedback,
                comment=comment,
                user_action=user_action,
            ),
            as_of=self.now(),
            asset_id=decision.asset_id,
            portfolio_id=decision.portfolio_id,
            payload=feedback_payload,
        )
        self.session.flush()
        return DecisionConfirmationResult(
            decision_id=decision.decision_id,
            feedback_decision_id=feedback_decision.decision_id,
            status=user_action,
            can_create_order_draft=user_action == "accepted"
            and normalize_action(decision.suggested_action) in EXECUTABLE_ACTIONS,
            suggested_action=decision.suggested_action,
        )

    def create_order_draft(self, decision_log_id: str) -> OrderDraftORM:
        """基于已接受的决策生成订单草案。"""

        decision = self._get_decision(decision_log_id)
        action = normalize_action(decision.suggested_action)
        if decision.user_action != "accepted":
            raise ValueError(f"决策尚未被用户接受，不能生成订单草案：{decision_log_id}")
        if action not in EXECUTABLE_ACTIONS:
            raise ValueError(f"决策动作不可生成订单草案：{decision.suggested_action}")
        ensure_review_gate_allows_draft(decision=decision, action=action)

        created_at = self.now()
        self.action_repository.supersede_active_order_drafts(
            decision_log_id=decision.decision_id,
            superseded_at=created_at,
        )
        return self.action_repository.upsert_order_draft(
            order_draft_id=build_order_draft_id(decision=decision, as_of=created_at),
            owner_id=decision.owner_id,
            portfolio_id=decision.portfolio_id,
            asset_id=decision.asset_id,
            market=resolve_decision_market(decision),
            decision_log_id=decision.decision_id,
            action=action,
            suggested_price_range=extract_json_object(decision.payload, "suggested_price_range"),
            suggested_position_ratio=extract_decimal(decision.payload, "suggested_position_ratio"),
            constraints=extract_json_object(decision.payload, "constraints"),
            disclaimer=ACTION_LOOP_DISCLAIMER,
            created_at=created_at,
            updated_at=created_at,
        )

    def _get_decision(self, decision_log_id: str) -> DecisionLogORM:
        decision = self.session.get(DecisionLogORM, decision_log_id)
        if decision is None:
            raise ValueError(f"决策不存在：{decision_log_id}")
        return decision


def resolve_feedback_user_action(*, feedback: str, modified_action: str | None = None) -> str:
    """把用户反馈类型转换为决策日志动作。"""

    if feedback == "modified":
        action = (modified_action or "").strip()
        if not action:
            raise ValueError("modified 反馈必须提供 modified_action")
        return action
    if feedback not in {"accepted", "rejected", "deferred"}:
        raise ValueError(f"不支持的反馈类型：{feedback}")
    return feedback


def update_decision_feedback_state(
    *,
    decision: DecisionLogORM,
    feedback_payload: JsonDict,
    user_action: str,
) -> None:
    """把用户反馈写回原决策日志。"""

    payload = dict(decision.payload or {})
    payload["user_feedback"] = feedback_payload
    decision.payload = payload
    decision.user_action = user_action


def build_action_loop_feedback_payload(
    *,
    decision: DecisionLogORM,
    feedback: str,
    comment: str | None,
    modified_action: str | None,
    user_action: str,
) -> JsonDict:
    """构造人工确认反馈 payload。"""

    return {
        "source_decision_id": decision.decision_id,
        "feedback": feedback,
        "comment": comment,
        "modified_action": modified_action,
        "resolved_user_action": user_action,
        "original_user_action": decision.user_action,
        "suggested_action": decision.suggested_action,
        "workflow_run_id": decision.workflow_run_id,
        "closed_loop_stage": "confirmed",
    }


def build_action_loop_feedback_summary(
    *,
    decision: DecisionLogORM,
    feedback: str,
    comment: str | None,
    user_action: str,
) -> str:
    """生成可沉淀为 Finance Memory 的确认摘要。"""

    note = f"备注：{comment}" if comment else "无备注。"
    return (
        f"用户对决策 {decision.decision_id} 做出 {feedback} 反馈，"
        f"系统建议动作为 {decision.suggested_action}，最终用户动作为 {user_action}。{note}"
    )


def build_feedback_decision_id(*, decision_id: str, as_of: datetime) -> str:
    """生成用户确认反馈决策日志 ID。"""

    stamp = as_of.astimezone(UTC).strftime("%Y%m%d%H%M%S%f")
    return f"feedback:{decision_id}:{stamp}"


def build_order_draft_id(*, decision: DecisionLogORM, as_of: datetime) -> str:
    """生成订单草案 ID。"""

    asset_part = str(decision.asset_id or "asset").replace(":", "-")
    stamp = as_of.astimezone(UTC).strftime("%Y%m%d%H%M%S%f")
    return f"draft:{decision.owner_id}:{asset_part}:{stamp}"


def normalize_action(action: str) -> str:
    """将推荐动作归一到订单草案动作集合。"""

    normalized = action.strip().lower()
    mapping = {
        "buy_more": "add",
        "increase": "add",
        "trim": "reduce",
        "decrease": "reduce",
    }
    return mapping.get(normalized, normalized)


def ensure_review_gate_allows_draft(*, decision: DecisionLogORM, action: str) -> None:
    """高风险动作生成草案前必须通过复核闸门。"""

    payload = dict(decision.payload or {})
    review_status = str(payload.get("review_status") or "")
    if review_status == "rejected_by_review":
        review_result = payload.get("review_result") or {}
        blocking = review_result.get("blocking_risks") if isinstance(review_result, dict) else None
        reason = "；".join(str(item) for item in blocking or []) or "高风险复核已驳回。"
        raise ValueError(f"复核已驳回，不能生成订单草案：{reason}")
    if action in HIGH_RISK_ACTIONS and review_status not in APPROVED_REVIEW_STATUSES:
        raise ValueError("高风险动作需要先完成复核后才能生成订单草案。")


def extract_json_object(payload: JsonDict | None, key: str) -> JsonDict:
    """从决策 payload 中读取 JSON 对象字段。"""

    value = (payload or {}).get(key)
    return dict(value) if isinstance(value, dict) else {}


def extract_decimal(payload: JsonDict | None, key: str) -> Decimal | None:
    """从决策 payload 中读取十进制字段。"""

    value = (payload or {}).get(key)
    if value is None or value == "":
        return None
    return Decimal(str(value))


def resolve_decision_market(decision: DecisionLogORM) -> str:
    """解析决策所属市场，优先使用显式字段，回退到 asset_id 前缀。"""

    market = getattr(decision, "market", None)
    if market:
        return str(market)
    asset_id = str(decision.asset_id or "")
    if ":" in asset_id:
        return asset_id.split(":", 1)[0]
    return "unknown"
