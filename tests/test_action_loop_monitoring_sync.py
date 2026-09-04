from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from finance_agent.application.action_loop_service import ActionLoopService, ExecutionRegistration
from tests.test_action_loop_service import (
    FakeActionRepository,
    FakeDecisionStore,
    FakeExecutionMemoryService,
    FakePortfolioService,
    build_position,
)


def test_buy_execution_syncs_t1_quantity_to_monitoring_repository() -> None:
    """买入登记后应同步监控状态，并在当日锁定新增 T+1 数量。"""

    calls: list[dict[str, object]] = []

    class MonitoringRepository:
        def apply_execution(self, **kwargs):
            calls.append(kwargs)

    service = ActionLoopService(
        session=FakeDecisionStore([]),
        action_repository=FakeActionRepository(),
        portfolio_service=FakePortfolioService([build_position(quantity=Decimal("100"), avg_cost=Decimal("10"))]),
        memory_service=FakeExecutionMemoryService(),
        monitoring_repository=MonitoringRepository(),
        now=lambda: datetime(2026, 9, 7, 10, 0, tzinfo=UTC),
    )

    service.record_execution(
        ExecutionRegistration(
            owner_id="owner:demo",
            portfolio_id="portfolio:demo",
            asset_id="ashare:600519",
            market="ashare",
            action="buy",
            executed_price=Decimal("12"),
            executed_quantity=Decimal("50"),
            executed_at=datetime(2026, 9, 7, 10, 0, tzinfo=UTC),
        )
    )

    assert calls[0]["action"] == "buy"
    assert calls[0]["total_quantity"] == Decimal("150")
    assert calls[0]["sellable_quantity"] == Decimal("100")
