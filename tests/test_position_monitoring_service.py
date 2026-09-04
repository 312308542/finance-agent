from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from finance_agent.monitoring.models import PositionAction, PositionMonitoringState
from finance_agent.monitoring.repository import PositionMonitoringRepository
from finance_agent.monitoring.service import PositionMonitoringService

NOW = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)


def position(asset_id: str, *, quantity: str = "1000", payload=None):
    return SimpleNamespace(
        position_id=f"position:{asset_id}",
        portfolio_id="portfolio:1",
        asset_id=asset_id,
        symbol=asset_id.split(":")[-1],
        market="ashare",
        quantity=Decimal(quantity),
        payload=payload or {"sellable_quantity": quantity},
    )


def test_service_keeps_previous_state_when_quote_is_missing() -> None:
    states = PositionMonitoringRepository()
    states.save(
        PositionAction(
            position_id="position:ashare:600519",
            action="hold",
            evaluated_at=NOW,
            reason_codes=("initial",),
        ),
        state=PositionMonitoringState(
            position_id="position:ashare:600519",
            asset_id="ashare:600519",
            total_quantity=Decimal("1000"),
            sellable_quantity=Decimal("1000"),
        ),
    )
    service = PositionMonitoringService(
        portfolio_repository=SimpleNamespace(list_active_positions_by_owner=lambda **_: [position("ashare:600519")]),
        asset_repository=SimpleNamespace(list_intraday_quote_latest=lambda **_: []),
        state_repository=states,
    )

    result = service.evaluate_owner("default-owner", as_of=NOW)

    assert result.actions[0].action == "unexecutable"
    assert result.actions[0].reason_codes == ("quote_missing",)
    assert states.get_state("position:ashare:600519").previous_valid_action == "hold"


def test_one_position_failure_does_not_block_other_positions() -> None:
    class BrokenEngine:
        def evaluate(self, state, snapshot):
            if state.asset_id.endswith("600519"):
                raise RuntimeError("结构事实损坏")
            from finance_agent.monitoring.models import PositionAction

            return PositionAction(
                position_id=state.position_id,
                action="hold",
                evaluated_at=NOW,
                quote_snapshot_id="quote:1",
            )

    positions = [position("ashare:600519"), position("ashare:000001")]
    quotes = [
        SimpleNamespace(
            asset_id=item.asset_id,
            last_price=Decimal("10"),
            as_of=NOW,
            quality_status="available",
            payload={},
        )
        for item in positions
    ]
    service = PositionMonitoringService(
        portfolio_repository=SimpleNamespace(list_active_positions_by_owner=lambda **_: positions),
        asset_repository=SimpleNamespace(list_intraday_quote_latest=lambda **_: quotes),
        state_repository=PositionMonitoringRepository(),
        engine=BrokenEngine(),
    )

    result = service.evaluate_owner("default-owner", as_of=NOW)

    assert len(result.actions) == 2
    assert result.error_count == 1
    assert result.actions[1].action == "hold"
