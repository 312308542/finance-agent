from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.dialects import postgresql

from finance_agent.storage.orm import ExecutionRecordORM, OrderDraftORM
from finance_agent.storage.repositories import ActionLoopRepository


class _FakeResult:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []

    def scalars(self) -> _FakeResult:
        return self

    def one_or_none(self) -> Any | None:
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class _FakeSession:
    def __init__(self) -> None:
        self.order_drafts: dict[str, Any] = {}
        self.executions: dict[str, Any] = {}
        self.executed: list[Any] = []
        self.flushed = False

    def execute(self, statement: Any) -> _FakeResult:
        self.executed.append(statement)
        sql = str(statement)
        params = statement.compile(dialect=postgresql.dialect()).params
        if "INSERT INTO order_drafts" in sql:
            self.add(OrderDraftORM(**params))
        if "INSERT INTO execution_records" in sql:
            self.add(ExecutionRecordORM(**params))
        return _FakeResult()

    def scalars(self, statement: Any) -> _FakeResult:
        sql = str(statement)
        params = statement.compile(dialect=postgresql.dialect()).params
        if "order_drafts" in sql:
            rows = list(self.order_drafts.values())
            decision_log_id = params.get("decision_log_id_1")
            status = params.get("status_1")
            owner_id = params.get("owner_id_1")
            if decision_log_id:
                rows = [row for row in rows if row.decision_log_id == decision_log_id]
            if status:
                rows = [row for row in rows if row.status == status]
            if owner_id:
                rows = [row for row in rows if row.owner_id == owner_id]
            return _FakeResult(rows)
        if "execution_records" in sql:
            rows = list(self.executions.values())
            owner_id = params.get("owner_id_1")
            asset_id = params.get("asset_id_1")
            if owner_id:
                rows = [row for row in rows if row.owner_id == owner_id]
            if asset_id:
                rows = [row for row in rows if row.asset_id == asset_id]
            return _FakeResult(rows)
        return _FakeResult()

    def get(self, model: Any, key: str) -> Any | None:
        if model is OrderDraftORM:
            return self.order_drafts.get(key)
        if model is ExecutionRecordORM:
            return self.executions.get(key)
        return None

    def get_one(self, model: Any, key: str) -> Any:
        row = self.get(model, key)
        if row is None:
            raise LookupError(key)
        return row

    def add(self, instance: Any) -> None:
        if isinstance(instance, OrderDraftORM):
            self.order_drafts[instance.order_draft_id] = instance
        elif isinstance(instance, ExecutionRecordORM):
            self.executions[instance.execution_id] = instance

    def flush(self) -> None:
        self.flushed = True


def _compiled(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def test_order_draft_repository_writes_defaults_and_can_read() -> None:
    """订单草案只保存文档性质建议，并且必须带非空免责声明。"""

    session = _FakeSession()
    created_at = datetime(2026, 6, 13, 9, 30, tzinfo=UTC)

    draft = ActionLoopRepository(session).upsert_order_draft(
        order_draft_id="draft:owner:ashare-600519:1",
        owner_id="owner:demo",
        portfolio_id="portfolio:demo",
        asset_id="ashare:600519",
        market="ashare",
        decision_log_id="decision:600519:1",
        action="buy",
        suggested_price_range={"low": "1680.00", "high": "1720.00"},
        suggested_position_ratio=Decimal("0.1000"),
        constraints={"stop_loss": "1580.00"},
        created_at=created_at,
    )

    assert draft is session.get(OrderDraftORM, "draft:owner:ashare-600519:1")
    assert draft.status == "drafted"
    assert draft.disclaimer
    assert "非投资建议" in draft.disclaimer
    assert draft.updated_at == created_at
    assert session.flushed is True
    assert "INSERT INTO order_drafts" in _compiled(session.executed[0])


def test_supersede_active_order_drafts_for_decision_marks_previous_drafts() -> None:
    """同一决策重复生成草案时，旧草案必须作废为 superseded。"""

    session = _FakeSession()
    older = OrderDraftORM(
        order_draft_id="draft:old",
        owner_id="owner:demo",
        portfolio_id="portfolio:demo",
        asset_id="ashare:600519",
        market="ashare",
        decision_log_id="decision:600519:1",
        action="buy",
        suggested_price_range={},
        suggested_position_ratio=None,
        constraints={},
        status="drafted",
        disclaimer="非投资建议，仅用于用户自行决策前的操作草案。",
        created_at=datetime(2026, 6, 13, 9, 30, tzinfo=UTC),
        updated_at=datetime(2026, 6, 13, 9, 30, tzinfo=UTC),
    )
    cancelled = OrderDraftORM(
        order_draft_id="draft:cancelled",
        owner_id="owner:demo",
        portfolio_id="portfolio:demo",
        asset_id="ashare:600519",
        market="ashare",
        decision_log_id="decision:600519:1",
        action="buy",
        suggested_price_range={},
        suggested_position_ratio=None,
        constraints={},
        status="cancelled",
        disclaimer="非投资建议，仅用于用户自行决策前的操作草案。",
        created_at=datetime(2026, 6, 13, 9, 31, tzinfo=UTC),
        updated_at=datetime(2026, 6, 13, 9, 31, tzinfo=UTC),
    )
    session.order_drafts = {
        older.order_draft_id: older,
        cancelled.order_draft_id: cancelled,
    }

    changed = ActionLoopRepository(session).supersede_active_order_drafts(
        decision_log_id="decision:600519:1",
        superseded_at=datetime(2026, 6, 13, 10, 0, tzinfo=UTC),
    )

    assert changed == 1
    assert older.status == "superseded"
    assert cancelled.status == "cancelled"
    assert session.flushed is True


def test_execution_record_repository_writes_user_reported_source_and_lists() -> None:
    """执行记录只允许用户手工登记来源，不代表系统自动下单。"""

    session = _FakeSession()
    executed_at = datetime(2026, 6, 13, 10, 5, tzinfo=UTC)

    record = ActionLoopRepository(session).upsert_execution_record(
        execution_id="execution:owner:600519:1",
        owner_id="owner:demo",
        portfolio_id="portfolio:demo",
        asset_id="ashare:600519",
        market="ashare",
        action="buy",
        executed_price=Decimal("1699.50"),
        executed_quantity=Decimal("100"),
        executed_at=executed_at,
        order_draft_id="draft:owner:ashare-600519:1",
        decision_log_id="decision:600519:1",
        fee=Decimal("5.00"),
        note="用户在外部交易软件登记。",
    )

    rows = ActionLoopRepository(session).list_execution_records(
        owner_id="owner:demo",
        limit=10,
    )

    assert record.source == "user_reported"
    assert rows == [record]
    assert session.flushed is True
    assert "INSERT INTO execution_records" in _compiled(session.executed[0])


def test_execution_record_rejects_non_user_reported_source() -> None:
    """08 方案红线：当前不接受任何券商或交易所写入来源。"""

    session = _FakeSession()

    try:
        ActionLoopRepository(session).upsert_execution_record(
            execution_id="execution:invalid",
            owner_id="owner:demo",
            portfolio_id="portfolio:demo",
            asset_id="ashare:600519",
            market="ashare",
            action="buy",
            executed_price=Decimal("1699.50"),
            executed_quantity=Decimal("100"),
            executed_at=datetime(2026, 6, 13, 10, 5, tzinfo=UTC),
            source="broker_api",
        )
    except ValueError as exc:
        assert "user_reported" in str(exc)
    else:
        raise AssertionError("非 user_reported 来源应被拒绝")
