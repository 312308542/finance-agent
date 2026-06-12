from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from finance_agent.application.action_loop_service import ActionLoopService


NOW = datetime(2026, 6, 13, 10, 30, tzinfo=UTC)


class FakeDecisionStore:
    def __init__(self, decisions: list[Any]) -> None:
        self.decisions = {decision.decision_id: decision for decision in decisions}
        self.flushed = False

    def get(self, model: Any, decision_id: str) -> Any | None:
        return self.decisions.get(decision_id)

    def flush(self) -> None:
        self.flushed = True


class FakeMemoryService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def record_user_feedback(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(decision_id=kwargs["feedback_id"])


class FakeActionRepository:
    def __init__(self) -> None:
        self.superseded: list[dict[str, Any]] = []
        self.drafts: list[dict[str, Any]] = []

    def supersede_active_order_drafts(self, **kwargs: Any) -> int:
        self.superseded.append(kwargs)
        return 1

    def upsert_order_draft(self, **kwargs: Any) -> Any:
        self.drafts.append(kwargs)
        return SimpleNamespace(**kwargs)


def test_confirm_decision_records_feedback_and_marks_executable() -> None:
    """accepted 反馈应记录为 Finance Memory，并返回可生成草案标记。"""

    decision = build_decision(user_action="pending_user_confirmation", suggested_action="buy")
    memory = FakeMemoryService()

    result = ActionLoopService(
        session=FakeDecisionStore([decision]),
        memory_service=memory,
        now=lambda: NOW,
    ).confirm_decision(
        decision_log_id=decision.decision_id,
        feedback="accepted",
        comment="确认采纳，生成外部执行前的草案。",
    )

    assert result.status == "accepted"
    assert result.can_create_order_draft is True
    assert decision.user_action == "accepted"
    assert decision.payload["user_feedback"]["feedback"] == "accepted"
    assert memory.calls[0]["suggested_action"] == "buy"
    assert memory.calls[0]["user_action"] == "accepted"


def test_confirm_decision_modified_action_is_not_executable_without_trade_action() -> None:
    """modified 反馈可沉淀记忆，但非交易动作不应提示生成草案。"""

    decision = build_decision(user_action="pending_user_confirmation", suggested_action="buy")

    result = ActionLoopService(
        session=FakeDecisionStore([decision]),
        memory_service=FakeMemoryService(),
        now=lambda: NOW,
    ).confirm_decision(
        decision_log_id=decision.decision_id,
        feedback="modified",
        modified_action="watch_only",
        comment="改为继续观察。",
    )

    assert result.status == "watch_only"
    assert result.can_create_order_draft is False
    assert decision.user_action == "watch_only"


def test_create_order_draft_rejects_review_rejected_decision() -> None:
    """高风险复核驳回的决策不能生成订单草案。"""

    decision = build_decision(
        user_action="accepted",
        suggested_action="sell",
        payload={
            "review_status": "rejected_by_review",
            "review_result": {"blocking_risks": ["风险证据强于原建议"]},
        },
    )

    with pytest.raises(ValueError, match="复核已驳回"):
        ActionLoopService(
            session=FakeDecisionStore([decision]),
            action_repository=FakeActionRepository(),
            now=lambda: NOW,
        ).create_order_draft(decision.decision_id)


def test_create_order_draft_requires_high_risk_review_before_sell() -> None:
    """卖出/减仓类高风险动作缺少复核结论时，应先阻断草案生成。"""

    decision = build_decision(
        user_action="accepted",
        suggested_action="sell",
        payload={"review_status": "requires_model_review"},
    )

    with pytest.raises(ValueError, match="高风险动作需要先完成复核"):
        ActionLoopService(
            session=FakeDecisionStore([decision]),
            action_repository=FakeActionRepository(),
            now=lambda: NOW,
        ).create_order_draft(decision.decision_id)


def test_create_order_draft_supersedes_old_draft_and_builds_new_one() -> None:
    """同一决策再次生成草案时，应先 superseded 旧草案再生成新草案。"""

    decision = build_decision(
        user_action="accepted",
        suggested_action="buy",
        payload={
            "suggested_price_range": {"low": "10.00", "high": "10.50"},
            "suggested_position_ratio": "0.08",
            "constraints": {"stop_loss": "9.20"},
        },
    )
    repository = FakeActionRepository()

    draft = ActionLoopService(
        session=FakeDecisionStore([decision]),
        action_repository=repository,
        now=lambda: NOW,
    ).create_order_draft(decision.decision_id)

    assert repository.superseded[0]["decision_log_id"] == decision.decision_id
    assert repository.drafts[0]["decision_log_id"] == decision.decision_id
    assert repository.drafts[0]["action"] == "buy"
    assert repository.drafts[0]["suggested_position_ratio"] == Decimal("0.08")
    assert "非投资建议" in draft.disclaimer


def build_decision(
    *,
    user_action: str,
    suggested_action: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    return SimpleNamespace(
        decision_id="decision:600519:1",
        owner_id="owner:demo",
        portfolio_id="portfolio:demo",
        asset_id="ashare:600519",
        market="ashare",
        decision_type="asset_deep_analysis",
        suggested_action=suggested_action,
        user_action=user_action,
        summary="等待用户确认。",
        workflow_run_id="workflow:1",
        payload=payload or {"review_status": "pending_user_confirmation"},
    )
