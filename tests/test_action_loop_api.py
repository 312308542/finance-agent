from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from finance_agent.api import routes
from finance_agent.api.schemas import (
    DecisionConfirmationRequest,
    ExecutionRecordRequest,
)


class FakeActionLoopService:
    """模拟人工确认闭环服务，验证 API 层只做请求编排和序列化。"""

    calls: list[dict[str, Any]] = []

    def __init__(self, session: Any) -> None:
        self.session = session

    def confirm_decision(self, **kwargs: Any) -> Any:
        self.calls.append({"method": "confirm_decision", **kwargs})
        return SimpleNamespace(
            decision_id=kwargs["decision_log_id"],
            feedback_decision_id=f"feedback:{kwargs['decision_log_id']}:1",
            status=kwargs["feedback"],
            can_create_order_draft=True,
            suggested_action="buy",
        )

    def create_order_draft(self, decision_log_id: str) -> Any:
        self.calls.append(
            {"method": "create_order_draft", "decision_log_id": decision_log_id}
        )
        return build_order_draft(order_draft_id="draft:1", decision_log_id=decision_log_id)

    def record_execution(self, registration: Any) -> Any:
        self.calls.append({"method": "record_execution", "registration": registration})
        return build_execution_record(execution_id="execution:1")


class FakeActionLoopRepository:
    def __init__(self, session: Any) -> None:
        self.session = session

    def list_order_drafts(
        self,
        *,
        owner_id: str,
        status: str | None = None,
        limit: int = 50,
    ) -> list[Any]:
        _ = owner_id, status, limit
        return [build_order_draft(order_draft_id="draft:listed")]

    def list_execution_records(
        self,
        *,
        owner_id: str,
        asset_id: str | None = None,
        limit: int = 50,
    ) -> list[Any]:
        _ = owner_id, asset_id, limit
        return [build_execution_record(execution_id="execution:listed")]


class FakeScalarResult:
    def __init__(self, items: list[Any]) -> None:
        self.items = items

    def __iter__(self):
        return iter(self.items)

    def all(self) -> list[Any]:
        return self.items


class FakeSession:
    """模拟 API 列表查询需要的最小 Session 行为。"""

    def __init__(self, review_tasks: list[Any] | None = None) -> None:
        self.review_tasks = review_tasks or []
        self.flushed = False

    def scalars(self, statement: Any) -> FakeScalarResult:
        _ = statement
        return FakeScalarResult(self.review_tasks)

    def flush(self) -> None:
        self.flushed = True


def test_confirm_decision_endpoint_wraps_action_loop_service(monkeypatch: Any) -> None:
    FakeActionLoopService.calls = []
    monkeypatch.setattr(routes, "ActionLoopService", FakeActionLoopService)
    session = FakeSession()

    response = routes.confirm_decision(
        decision_id="decision:1",
        request=DecisionConfirmationRequest(
            feedback="accepted",
            comment="确认采纳，准备生成草案。",
        ),
        session=session,
    )

    assert response["status"] == "ok"
    assert response["data"]["decision_id"] == "decision:1"
    assert response["data"]["feedback_decision_id"] == "feedback:decision:1:1"
    assert response["data"]["can_create_order_draft"] is True
    assert FakeActionLoopService.calls == [
        {
            "method": "confirm_decision",
            "decision_log_id": "decision:1",
            "feedback": "accepted",
            "comment": "确认采纳，准备生成草案。",
            "modified_action": None,
        }
    ]


def test_create_order_draft_endpoint_returns_disclaimer(monkeypatch: Any) -> None:
    FakeActionLoopService.calls = []
    monkeypatch.setattr(routes, "ActionLoopService", FakeActionLoopService)

    response = routes.create_order_draft(
        decision_id="decision:1",
        session=FakeSession(),
    )

    assert response["status"] == "ok"
    assert response["data"]["order_draft_id"] == "draft:1"
    assert response["data"]["decision_log_id"] == "decision:1"
    assert "非投资建议" in response["data"]["disclaimer"]
    assert FakeActionLoopService.calls == [
        {"method": "create_order_draft", "decision_log_id": "decision:1"}
    ]


def test_list_order_drafts_endpoint_serializes_items(monkeypatch: Any) -> None:
    monkeypatch.setattr(routes, "ActionLoopRepository", FakeActionLoopRepository)

    response = routes.list_order_drafts(
        owner_id="owner:demo",
        status="drafted",
        limit=10,
        session=FakeSession(),
    )

    assert response["status"] == "ok"
    assert response["data"]["items"][0]["order_draft_id"] == "draft:listed"
    assert response["data"]["items"][0]["suggested_position_ratio"] == "0.100000"


def test_record_execution_endpoint_builds_registration(monkeypatch: Any) -> None:
    FakeActionLoopService.calls = []
    monkeypatch.setattr(routes, "ActionLoopService", FakeActionLoopService)

    response = routes.record_execution(
        request=ExecutionRecordRequest(
            owner_id="owner:demo",
            portfolio_id="portfolio:demo",
            asset_id="ashare:600519",
            market="ashare",
            action="buy",
            executed_price=Decimal("1699.50"),
            executed_quantity=Decimal("100"),
            executed_at=datetime(2026, 6, 13, 10, 5, tzinfo=UTC),
            order_draft_id="draft:1",
            note="已在外部交易软件手工执行。",
        ),
        session=FakeSession(),
    )

    assert response["status"] == "ok"
    assert response["data"]["execution_id"] == "execution:1"
    registration = FakeActionLoopService.calls[0]["registration"]
    assert registration.owner_id == "owner:demo"
    assert registration.order_draft_id == "draft:1"
    assert registration.source == "user_reported"


def test_list_execution_records_endpoint_serializes_items(monkeypatch: Any) -> None:
    monkeypatch.setattr(routes, "ActionLoopRepository", FakeActionLoopRepository)

    response = routes.list_execution_records(
        owner_id="owner:demo",
        asset_id="ashare:600519",
        limit=10,
        session=FakeSession(),
    )

    assert response["status"] == "ok"
    assert response["data"]["items"][0]["execution_id"] == "execution:listed"
    assert response["data"]["items"][0]["executed_price"] == "1699.5000000000"


def test_list_upcoming_reviews_endpoint_returns_pending_execution_reviews() -> None:
    review_task = SimpleNamespace(
        review_task_id="review:execution:1:outcome",
        owner_id="owner:demo",
        asset_id="ashare:600519",
        source_decision_id="decision:1",
        review_type="execution_outcome",
        due_at=datetime(2026, 7, 3, 10, 5, tzinfo=UTC),
        status="pending",
        review_questions=[{"question": "比较执行表现。"}],
        result_summary=None,
        finished_at=None,
        payload={"execution_id": "execution:1"},
    )

    response = routes.list_upcoming_reviews(
        owner_id="owner:demo",
        limit=10,
        session=FakeSession(review_tasks=[review_task]),
    )

    assert response["status"] == "ok"
    assert response["data"]["items"] == [
        {
            "review_task_id": "review:execution:1:outcome",
            "owner_id": "owner:demo",
            "asset_id": "ashare:600519",
            "source_decision_id": "decision:1",
            "review_type": "execution_outcome",
            "due_at": "2026-07-03T10:05:00+00:00",
            "status": "pending",
            "review_questions": [{"question": "比较执行表现。"}],
            "result_summary": None,
            "finished_at": None,
            "payload": {"execution_id": "execution:1"},
        }
    ]


def build_order_draft(*, order_draft_id: str, decision_log_id: str = "decision:1") -> Any:
    return SimpleNamespace(
        order_draft_id=order_draft_id,
        owner_id="owner:demo",
        portfolio_id="portfolio:demo",
        asset_id="ashare:600519",
        market="ashare",
        decision_log_id=decision_log_id,
        action="buy",
        suggested_price_range={"low": "1680.00", "high": "1720.00"},
        suggested_position_ratio=Decimal("0.100000"),
        constraints={"stop_loss": "1580.00"},
        status="drafted",
        disclaimer="非投资建议，仅用于用户自行决策前的订单草案。",
        created_at=datetime(2026, 6, 13, 10, 0, tzinfo=UTC),
        updated_at=datetime(2026, 6, 13, 10, 0, tzinfo=UTC),
    )


def build_execution_record(*, execution_id: str) -> Any:
    return SimpleNamespace(
        execution_id=execution_id,
        owner_id="owner:demo",
        portfolio_id="portfolio:demo",
        asset_id="ashare:600519",
        market="ashare",
        order_draft_id="draft:1",
        decision_log_id="decision:1",
        action="buy",
        executed_price=Decimal("1699.5000000000"),
        executed_quantity=Decimal("100.0000000000"),
        executed_at=datetime(2026, 6, 13, 10, 5, tzinfo=UTC),
        fee=Decimal("5.0000000000"),
        note="已在外部交易软件手工执行。",
        source="user_reported",
        created_at=datetime(2026, 6, 13, 10, 6, tzinfo=UTC),
    )
