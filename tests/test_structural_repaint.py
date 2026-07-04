from __future__ import annotations

from datetime import UTC, datetime, timedelta

from finance_agent.indicators.structural_methodology_adapters import (
    StructuralMethodologyAdapter,
    StructuralPriceBar,
)


def _wave_bars(count: int) -> list[StructuralPriceBar]:
    bars: list[StructuralPriceBar] = []
    timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    for index in range(count):
        cycle = index % 40
        if cycle <= 20:
            price = 100 + cycle
        else:
            price = 120 - (cycle - 20)
        bars.append(
            StructuralPriceBar(
                timestamp=timestamp + timedelta(days=index),
                open=price,
                high=price + 1,
                low=price - 1,
                close=price,
                volume=1000.0,
            )
        )
    return bars


def _stable_smc_event_keys(payload: dict, *, max_confirmed_bar: int) -> set[tuple[str, int, float]]:
    return {
        (
            event["name"],
            event["confirmed_at_bar"],
            event["break_level"],
        )
        for event in payload["structure_events"]
        if event["confirmed_at_bar"] <= max_confirmed_bar
    }


def test_structural_events_do_not_repaint_outside_tail_confirmation_window() -> None:
    adapter = StructuralMethodologyAdapter()
    first = adapter.compute_smc(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        timeframe="1d",
        bars=_wave_bars(250),
    )
    extended = adapter.compute_smc(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        timeframe="1d",
        bars=_wave_bars(260),
    )

    first_stable = _stable_smc_event_keys(first, max_confirmed_bar=239)
    extended_stable = _stable_smc_event_keys(extended, max_confirmed_bar=239)
    assert first_stable == extended_stable
