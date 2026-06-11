from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from finance_agent.api import routes
from finance_agent.api.schemas import DecisionFeedbackRequest


class FakeScalarResult:
    def __init__(self, items: list[Any]) -> None:
        self.items = items

    def all(self) -> list[Any]:
        return self.items


class FakeSession:
    """模拟 API 路由需要的最小 SQLAlchemy Session 行为。"""

    def __init__(self, decisions: list[Any]) -> None:
        self.decisions = {decision.decision_id: decision for decision in decisions}
        self.flushed = False

    def get(self, model: Any, decision_id: str) -> Any | None:
        return self.decisions.get(decision_id)

    def scalars(self, statement: Any) -> FakeScalarResult:
        pending = [
            decision
            for decision in self.decisions.values()
            if decision.user_action == "pending_user_confirmation"
        ]
        return FakeScalarResult(pending)

    def flush(self) -> None:
        self.flushed = True


class FakeMemoryService:
    calls: list[dict[str, Any]] = []

    def __init__(self, session: FakeSession) -> None:
        self.session = session

    def record_user_feedback(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(decision_id=kwargs["feedback_id"])


def test_submit_decision_feedback_records_memory_and_updates_decision(
    monkeypatch: Any,
) -> None:
    FakeMemoryService.calls = []
    decision = build_decision(decision_id="decision:1", user_action="pending_user_confirmation")
    session = FakeSession([decision])
    monkeypatch.setattr(routes, "MemoryService", FakeMemoryService)

    response = routes.submit_decision_feedback(
        decision_id="decision:1",
        request=DecisionFeedbackRequest(
            feedback="accepted",
            comment="确认采纳，先进入观察执行清单。",
        ),
        session=session,
    )

    assert response["status"] == "ok"
    assert response["data"]["decision_id"] == "decision:1"
    assert response["data"]["feedback_decision_id"].startswith("feedback:decision:1:")
    assert decision.user_action == "accepted"
    assert decision.payload["user_feedback"]["feedback"] == "accepted"
    assert decision.payload["user_feedback"]["comment"] == "确认采纳，先进入观察执行清单。"
    assert session.flushed is True
    call = FakeMemoryService.calls[0]
    assert call["owner_id"] == "owner:demo"
    assert call["asset_id"] == "ashare:600519"
    assert call["feedback_type"] == "decision_feedback"
    assert call["suggested_action"] == "watch"
    assert call["user_action"] == "accepted"
    assert call["payload"]["source_decision_id"] == "decision:1"


def test_submit_decision_feedback_supports_modified_action(monkeypatch: Any) -> None:
    FakeMemoryService.calls = []
    decision = build_decision(decision_id="decision:2", user_action="pending_user_confirmation")
    session = FakeSession([decision])
    monkeypatch.setattr(routes, "MemoryService", FakeMemoryService)

    response = routes.submit_decision_feedback(
        decision_id="decision:2",
        request=DecisionFeedbackRequest(
            feedback="modified",
            comment="改为继续观察，不进入执行草案。",
            modified_action="watch_only",
        ),
        session=session,
    )

    assert response["status"] == "ok"
    assert decision.user_action == "watch_only"
    assert FakeMemoryService.calls[0]["user_action"] == "watch_only"
    assert FakeMemoryService.calls[0]["payload"]["feedback"] == "modified"


def test_list_pending_confirmation_decisions_returns_pending_items() -> None:
    session = FakeSession(
        [
            build_decision(decision_id="decision:pending", user_action="pending_user_confirmation"),
            build_decision(decision_id="decision:accepted", user_action="accepted"),
        ]
    )

    response = routes.list_pending_confirmation_decisions(
        owner_id="owner:demo",
        limit=20,
        session=session,
    )

    assert response["status"] == "ok"
    assert [item["decision_id"] for item in response["data"]["items"]] == [
        "decision:pending"
    ]
    item = response["data"]["items"][0]
    assert item["asset_id"] == "ashare:600519"
    assert item["suggested_action"] == "watch"
    assert item["review_status"] == "pending_user_confirmation"


def build_decision(*, decision_id: str, user_action: str) -> Any:
    return SimpleNamespace(
        decision_id=decision_id,
        owner_id="owner:demo",
        portfolio_id="portfolio:demo",
        asset_id="ashare:600519",
        decision_type="asset_deep_analysis_watch",
        source_recommendation_id="recommendation:1",
        source_alert_id=None,
        workflow_run_id="workflow:1",
        suggested_action="watch",
        user_action=user_action,
        summary="等待用户确认。",
        reason_ids=[],
        risk_ids=["risk:1"],
        evidence_ids=["ev:1"],
        created_at=datetime(2026, 6, 12, 10, 0, tzinfo=UTC),
        payload={"review_status": "pending_user_confirmation"},
    )
