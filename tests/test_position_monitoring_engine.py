from datetime import UTC, datetime
from decimal import Decimal

from finance_agent.monitoring.models import IntradayPositionSnapshot, PositionMonitoringState
from finance_agent.monitoring.position_engine import PositionMonitoringEngine

NOW = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)


def _state(**overrides):
    values = {
        "position_id": "position:owner:600519",
        "asset_id": "ashare:600519",
        "total_quantity": Decimal("1000"),
        "sellable_quantity": Decimal("1000"),
        "invalidation_price": Decimal("9.40"),
        "protective_price": Decimal("9.50"),
    }
    values.update(overrides)
    return PositionMonitoringState(**values)


def _snapshot(**overrides):
    values = {
        "position_id": "position:owner:600519",
        "asset_id": "ashare:600519",
        "price": Decimal("9.80"),
        "quote_snapshot_id": "quote:1",
        "as_of": NOW,
        "quality_status": "available",
        "daily_structure": "bullish",
        "sector_regime": "diffusion",
    }
    values.update(overrides)
    return IntradayPositionSnapshot(**values)


def test_normal_pullback_keeps_holding_when_thesis_and_structure_hold() -> None:
    result = PositionMonitoringEngine().evaluate(_state(), _snapshot())
    assert result.action == "hold"
    assert result.severity == "low"


def test_structure_break_on_t1_returns_unexecutable_exit() -> None:
    result = PositionMonitoringEngine().evaluate(
        _state(sellable_quantity=Decimal("0")),
        _snapshot(price=Decimal("9.20"), daily_structure="bearish", structure_invalidated=True),
    )
    assert result.action == "unexecutable"
    assert result.intended_action == "exit"
    assert "t1_not_sellable" in result.reason_codes


def test_stale_quote_is_unexecutable_and_does_not_claim_exit_filled() -> None:
    result = PositionMonitoringEngine().evaluate(
        _state(),
        _snapshot(quote_age_seconds=4.0),
    )
    assert result.action == "unexecutable"
    assert result.intended_action == "exit"
    assert "quote_stale" in result.reason_codes


def test_protective_price_only_moves_up() -> None:
    result = PositionMonitoringEngine().evaluate(
        _state(protective_price=Decimal("9.50")),
        _snapshot(new_protective_price=Decimal("9.70")),
    )
    assert result.protective_price == Decimal("9.70")
