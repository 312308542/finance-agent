from datetime import date, timedelta
from decimal import Decimal

import pandas as pd

from finance_agent.research.execution_simulator import (
    AshareExecutionSimulator,
    SimulatedOrder,
)


def _bars(*, limit_up: bool = False, suspended: bool = False) -> pd.DataFrame:
    day = date(2026, 9, 8)
    rows = []
    for i in range(3):
        current = day + timedelta(days=i)
        rows.append(
            {
                "timestamp": current,
                "open": 11.0 if limit_up else 10.0,
                "high": 11.0 if limit_up else 10.0,
                "low": 11.0 if limit_up else 10.0,
                "close": 11.0 if limit_up else 10.0,
                "volume": 100000,
                "amount": 1000000,
                "is_suspended": suspended,
                "prev_close": 10.0,
            }
        )
    return pd.DataFrame(rows)


def test_simulator_enters_next_open_and_applies_ashare_costs() -> None:
    result = AshareExecutionSimulator().simulate(
        SimulatedOrder(signal_date=date(2026, 9, 7), side="buy", quantity=1000),
        bars=_bars(),
    )
    assert result.status == "filled"
    assert result.entry_date == date(2026, 9, 8)
    assert result.entry_price == Decimal("10.00")
    assert result.total_cost_rate == Decimal("0.003")


def test_limit_up_entry_and_t1_exit_are_unexecutable() -> None:
    entry = AshareExecutionSimulator().simulate(
        SimulatedOrder(signal_date=date(2026, 9, 7), side="buy", quantity=1000),
        bars=_bars(limit_up=True),
    )
    assert entry.status == "unexecutable"
    assert entry.reason == "limit_up_no_liquidity"

    exit_result = AshareExecutionSimulator().simulate(
        SimulatedOrder(
            signal_date=date(2026, 9, 7), side="sell", quantity=1000, bought_today=True
        ),
        bars=_bars(),
    )
    assert exit_result.reason == "t1_not_sellable"


def test_simulator_rounds_buy_quantity_down_to_lot_and_charges_stamp_tax_on_sell() -> None:
    simulator = AshareExecutionSimulator()
    buy = simulator.simulate(
        SimulatedOrder(signal_date=date(2026, 9, 7), side="buy", quantity=155),
        bars=_bars(),
    )
    assert buy.filled_quantity == 100
    sell = simulator.simulate(
        SimulatedOrder(signal_date=date(2026, 9, 7), side="sell", quantity=100),
        bars=_bars(),
    )
    assert sell.total_cost_rate == Decimal("0.0015")
