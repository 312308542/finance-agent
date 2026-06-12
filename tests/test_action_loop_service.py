from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from finance_agent.application.action_loop_service import ActionLoopService, ExecutionRegistration


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
        self.executions_by_id: dict[str, Any] = {}
        self.superseded: list[dict[str, Any]] = []
        self.drafts: list[dict[str, Any]] = []
        self.executions: list[dict[str, Any]] = []

    def supersede_active_order_drafts(self, **kwargs: Any) -> int:
        self.superseded.append(kwargs)
        return 1

    def upsert_order_draft(self, **kwargs: Any) -> Any:
        self.drafts.append(kwargs)
        return SimpleNamespace(**kwargs)

    def get_order_draft(self, order_draft_id: str) -> Any | None:
        return next(
            (
                SimpleNamespace(**draft)
                for draft in self.drafts
                if draft["order_draft_id"] == order_draft_id
            ),
            None,
        )

    def get_execution_record(self, execution_id: str) -> Any | None:
        return self.executions_by_id.get(execution_id)

    def upsert_execution_record(self, **kwargs: Any) -> Any:
        self.executions.append(kwargs)
        record = SimpleNamespace(**kwargs)
        self.executions_by_id[kwargs["execution_id"]] = record
        return record


class FakePortfolioService:
    def __init__(self, positions: list[Any] | None = None) -> None:
        self.positions = positions or []
        self.upserts: list[dict[str, Any]] = []

    def load_portfolio_snapshot(self, portfolio_id: str) -> Any:
        return SimpleNamespace(portfolio=SimpleNamespace(portfolio_id=portfolio_id), positions=tuple(self.positions))

    def upsert_position(self, **kwargs: Any) -> Any:
        self.upserts.append(kwargs)
        position = SimpleNamespace(**kwargs)
        self.positions = [
            existing
            for existing in self.positions
            if not (
                existing.portfolio_id == kwargs["portfolio_id"]
                and existing.asset_id == kwargs["asset_id"]
                and existing.side == kwargs["side"]
            )
        ]
        self.positions.append(position)
        return position


class FakeExecutionMemoryService(FakeMemoryService):
    def __init__(self) -> None:
        super().__init__()
        self.decisions: list[dict[str, Any]] = []
        self.memories: list[dict[str, Any]] = []
        self.review_tasks: list[dict[str, Any]] = []
        self.completed_reviews: list[dict[str, Any]] = []

    def record_decision(self, **kwargs: Any) -> Any:
        self.decisions.append(kwargs)
        return SimpleNamespace(decision_id=kwargs["decision_id"])

    def upsert_memory(self, **kwargs: Any) -> Any:
        self.memories.append(kwargs)
        return SimpleNamespace(memory_id=kwargs["memory_id"])

    def schedule_review(self, **kwargs: Any) -> Any:
        self.review_tasks.append(kwargs)
        return SimpleNamespace(**kwargs)

    def complete_review_task(self, **kwargs: Any) -> Any:
        self.completed_reviews.append(kwargs)
        return SimpleNamespace(**kwargs, status="completed")


class FakeReviewSession(FakeDecisionStore):
    def __init__(self, review_tasks: list[Any]) -> None:
        super().__init__([])
        self.review_tasks = review_tasks

    def scalars(self, statement: Any) -> Any:
        return iter(self.review_tasks)


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


def test_record_execution_buy_adds_weighted_average_position() -> None:
    """买入登记应写执行记录，并按加权平均成本更新已有持仓。"""

    position = build_position(quantity=Decimal("100"), avg_cost=Decimal("10"))
    repository = FakeActionRepository()
    portfolio = FakePortfolioService([position])
    memory = FakeExecutionMemoryService()

    record = ActionLoopService(
        session=FakeDecisionStore([]),
        action_repository=repository,
        portfolio_service=portfolio,
        memory_service=memory,
        now=lambda: NOW,
    ).record_execution(
        ExecutionRegistration(
            owner_id="owner:demo",
            portfolio_id="portfolio:demo",
            asset_id="ashare:600519",
            market="ashare",
            action="buy",
            executed_price=Decimal("12"),
            executed_quantity=Decimal("50"),
            executed_at=NOW,
            decision_log_id="decision:600519:1",
            order_draft_id=None,
            fee=Decimal("3"),
            note="外部软件已执行。",
        )
    )

    assert record.source == "user_reported"
    assert repository.executions[0]["execution_id"].startswith("execution:owner:demo:ashare-600519:")
    updated = portfolio.upserts[0]
    assert updated["quantity"] == Decimal("150")
    assert updated["avg_cost"] == Decimal("10.6666666667")
    assert updated["last_price"] == Decimal("12")
    assert updated["market_value"] == Decimal("1800")
    assert updated["status"] == "active"
    assert memory.memories[0]["memory_type"] == "execution_note"
    assert memory.review_tasks[0]["source_decision_id"] == "decision:600519:1"


def test_record_execution_sell_rejects_oversell() -> None:
    """卖出登记不得超过当前持仓数量。"""

    position = build_position(quantity=Decimal("100"), avg_cost=Decimal("10"))
    repository = FakeActionRepository()

    with pytest.raises(ValueError, match="卖出数量不能超过当前持仓"):
        ActionLoopService(
            session=FakeDecisionStore([]),
            action_repository=repository,
            portfolio_service=FakePortfolioService([position]),
            memory_service=FakeExecutionMemoryService(),
            now=lambda: NOW,
        ).record_execution(
            ExecutionRegistration(
                owner_id="owner:demo",
                portfolio_id="portfolio:demo",
                asset_id="ashare:600519",
                market="ashare",
                action="sell",
                executed_price=Decimal("11"),
                executed_quantity=Decimal("120"),
                executed_at=NOW,
            )
        )
    assert repository.executions == []


def test_record_execution_sell_closes_position_when_quantity_zero() -> None:
    """卖出登记清空持仓后，应把持仓状态置为 closed。"""

    position = build_position(quantity=Decimal("100"), avg_cost=Decimal("10"))
    portfolio = FakePortfolioService([position])

    ActionLoopService(
        session=FakeDecisionStore([]),
        action_repository=FakeActionRepository(),
        portfolio_service=portfolio,
        memory_service=FakeExecutionMemoryService(),
        now=lambda: NOW,
    ).record_execution(
        ExecutionRegistration(
            owner_id="owner:demo",
            portfolio_id="portfolio:demo",
            asset_id="ashare:600519",
            market="ashare",
            action="sell",
            executed_price=Decimal("11"),
            executed_quantity=Decimal("100"),
            executed_at=NOW,
        )
    )

    updated = portfolio.upserts[0]
    assert updated["quantity"] == Decimal("0")
    assert updated["avg_cost"] == Decimal("10")
    assert updated["status"] == "closed"


def test_record_execution_allows_autonomous_registration_without_draft() -> None:
    """用户自主交易也可以登记，只要来源仍是 user_reported。"""

    repository = FakeActionRepository()
    portfolio = FakePortfolioService()

    record = ActionLoopService(
        session=FakeDecisionStore([]),
        action_repository=repository,
        portfolio_service=portfolio,
        memory_service=FakeExecutionMemoryService(),
        now=lambda: NOW,
    ).record_execution(
        ExecutionRegistration(
            owner_id="owner:demo",
            portfolio_id="portfolio:demo",
            asset_id="ashare:000001",
            market="ashare",
            action="buy",
            executed_price=Decimal("10"),
            executed_quantity=Decimal("100"),
            executed_at=NOW,
        )
    )

    assert record.order_draft_id is None
    assert record.decision_log_id is None
    assert portfolio.upserts[0]["position_id"] == "position:portfolio:demo:ashare-000001:long"


def test_record_execution_is_idempotent_by_execution_id() -> None:
    """同一个 execution_id 重复提交不得重复更新持仓。"""

    existing_record = SimpleNamespace(
        execution_id="execution:fixed",
        owner_id="owner:demo",
        portfolio_id="portfolio:demo",
        asset_id="ashare:600519",
        market="ashare",
        order_draft_id=None,
        decision_log_id=None,
        action="buy",
        executed_price=Decimal("10"),
        executed_quantity=Decimal("100"),
        executed_at=NOW,
        fee=None,
        note=None,
        source="user_reported",
    )
    repository = FakeActionRepository()
    repository.executions_by_id[existing_record.execution_id] = existing_record
    portfolio = FakePortfolioService()

    result = ActionLoopService(
        session=FakeDecisionStore([]),
        action_repository=repository,
        portfolio_service=portfolio,
        memory_service=FakeExecutionMemoryService(),
        now=lambda: NOW,
    ).record_execution(
        ExecutionRegistration(
            execution_id="execution:fixed",
            owner_id="owner:demo",
            portfolio_id="portfolio:demo",
            asset_id="ashare:600519",
            market="ashare",
            action="buy",
            executed_price=Decimal("10"),
            executed_quantity=Decimal("100"),
            executed_at=NOW,
        )
    )

    assert result is existing_record
    assert repository.executions == []
    assert portfolio.upserts == []


def test_compare_execution_outcome_completes_review_with_structured_payload() -> None:
    """到期执行复盘应比较建议价、执行价和后续收盘价，并完成复盘任务。"""

    record = SimpleNamespace(
        execution_id="execution:600519:1",
        owner_id="owner:demo",
        portfolio_id="portfolio:demo",
        asset_id="ashare:600519",
        market="ashare",
        order_draft_id=None,
        decision_log_id="decision:600519:1",
        action="buy",
        executed_price=Decimal("10"),
        executed_quantity=Decimal("100"),
        executed_at=NOW,
        fee=Decimal("2"),
        note=None,
        source="user_reported",
    )
    repository = FakeActionRepository()
    repository.executions_by_id[record.execution_id] = record
    memory = FakeExecutionMemoryService()
    review_task = build_review_task(
        payload={
            "execution_id": record.execution_id,
            "suggested_price": "9.80",
        }
    )

    result = ActionLoopService(
        session=FakeDecisionStore([]),
        action_repository=repository,
        memory_service=memory,
        latest_price_loader=lambda **_: {
            "price": Decimal("12.50"),
            "as_of": datetime(2026, 7, 3, tzinfo=UTC),
            "source": "market_bars",
        },
        now=lambda: datetime(2026, 7, 4, tzinfo=UTC),
    ).compare_execution_outcome(review_task)

    assert result.outcome == "confirmed"
    completed = memory.completed_reviews[0]
    assert completed["review_task_id"] == review_task.review_task_id
    assert completed["owner_id"] == "owner:demo"
    assert completed["outcome"] == "confirmed"
    outcome_payload = completed["payload"]["execution_outcome"]
    assert outcome_payload["execution_id"] == "execution:600519:1"
    assert outcome_payload["suggested_price"] == "9.80"
    assert outcome_payload["executed_price"] == "10"
    assert outcome_payload["latest_price"] == "12.50"
    assert outcome_payload["price_slippage_pct"] == "0.020408"
    assert outcome_payload["holding_return_pct"] == "0.250000"
    assert "后续表现为正" in completed["result_summary"]


def test_compare_execution_outcome_marks_partial_when_latest_price_missing() -> None:
    """复盘缺少后续行情时必须标记 partial，不能编造收益结论。"""

    record = SimpleNamespace(
        execution_id="execution:000001:1",
        owner_id="owner:demo",
        portfolio_id="portfolio:demo",
        asset_id="ashare:000001",
        market="ashare",
        order_draft_id=None,
        decision_log_id=None,
        action="buy",
        executed_price=Decimal("10"),
        executed_quantity=Decimal("100"),
        executed_at=NOW,
        fee=None,
        note=None,
        source="user_reported",
    )
    repository = FakeActionRepository()
    repository.executions_by_id[record.execution_id] = record
    memory = FakeExecutionMemoryService()

    result = ActionLoopService(
        session=FakeDecisionStore([]),
        action_repository=repository,
        memory_service=memory,
        latest_price_loader=lambda **_: None,
        now=lambda: datetime(2026, 7, 4, tzinfo=UTC),
    ).compare_execution_outcome(build_review_task(payload={"execution_id": record.execution_id}))

    assert result.outcome == "partial"
    completed = memory.completed_reviews[0]
    assert completed["outcome"] == "partial"
    assert "行情缺失" in completed["result_summary"]
    assert completed["payload"]["execution_outcome"]["missing_fields"] == [
        "suggested_price",
        "latest_price",
    ]


def test_run_due_reviews_processes_pending_execution_review_tasks() -> None:
    """调度入口应扫描到期 execution_outcome 任务并逐条复盘。"""

    record = SimpleNamespace(
        execution_id="execution:600519:2",
        owner_id="owner:demo",
        portfolio_id="portfolio:demo",
        asset_id="ashare:600519",
        market="ashare",
        order_draft_id=None,
        decision_log_id="decision:600519:2",
        action="sell",
        executed_price=Decimal("12"),
        executed_quantity=Decimal("100"),
        executed_at=NOW,
        fee=None,
        note=None,
        source="user_reported",
    )
    repository = FakeActionRepository()
    repository.executions_by_id[record.execution_id] = record
    memory = FakeExecutionMemoryService()

    summary = ActionLoopService(
        session=FakeReviewSession(
            [build_review_task(payload={"execution_id": record.execution_id})]
        ),
        action_repository=repository,
        memory_service=memory,
        latest_price_loader=lambda **_: {
            "price": Decimal("10"),
            "as_of": datetime(2026, 7, 3, tzinfo=UTC),
            "source": "market_bars",
        },
        now=lambda: datetime(2026, 7, 4, tzinfo=UTC),
    ).run_due_reviews(owner_id="owner:demo", limit=5, due_at=datetime(2026, 7, 4, tzinfo=UTC))

    assert summary["status"] == "available"
    assert summary["processed_count"] == 1
    assert summary["completed_count"] == 1
    assert summary["partial_count"] == 1
    assert summary["failed_count"] == 0
    assert memory.completed_reviews[0]["review_task_id"] == "review:execution:outcome"


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


def build_position(
    *,
    quantity: Decimal,
    avg_cost: Decimal,
    status: str = "active",
) -> Any:
    return SimpleNamespace(
        position_id="position:portfolio:demo:ashare-600519:long",
        portfolio_id="portfolio:demo",
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        side="long",
        quantity=quantity,
        avg_cost=avg_cost,
        last_price=avg_cost,
        market_value=quantity * avg_cost,
        unrealized_pnl=None,
        unrealized_pnl_pct=None,
        portfolio_weight=None,
        leverage=None,
        liquidation_price=None,
        status=status,
        payload={},
    )


def build_review_task(*, payload: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(
        review_task_id="review:execution:outcome",
        owner_id="owner:demo",
        asset_id="ashare:600519",
        source_decision_id="decision:600519:1",
        review_type="execution_outcome",
        due_at=datetime(2026, 7, 3, tzinfo=UTC),
        status="pending",
        review_questions=[],
        payload=payload or {},
    )
