from __future__ import annotations

from datetime import UTC, datetime, timedelta

from finance_agent.indicators.structural_methodology_adapters import (
    StructuralMethodologyAdapter,
    StructuralPriceBar,
    SwingPoint,
    detect_structure_breaks,
)


def _bar(
    day: int,
    *,
    high: float,
    low: float,
    close: float,
    open_: float | None = None,
) -> StructuralPriceBar:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day)
    return StructuralPriceBar(
        timestamp=timestamp,
        open=open_ if open_ is not None else close,
        high=high,
        low=low,
        close=close,
        volume=1000.0,
    )


def test_structure_break_ignores_unconfirmed_swing_reference() -> None:
    bars = [
        _bar(0, high=100, low=95, close=98),
        _bar(1, high=106, low=97, close=104),
        _bar(2, high=110, low=98, close=108),
        _bar(3, high=112, low=101, close=111),
        _bar(4, high=109, low=100, close=109),
        _bar(5, high=108, low=99, close=107),
    ]
    unconfirmed_until_day4 = SwingPoint(
        timestamp=bars[2].timestamp,
        bar_index=2,
        type="H",
        price=110,
        confirmed_bar_index=4,
        confirmed_at=bars[4].timestamp,
    )

    events = detect_structure_breaks(bars, [unconfirmed_until_day4])

    assert events == []


def test_structure_events_expose_confirmed_asof_metadata() -> None:
    bars = [
        _bar(0, high=106, low=101, close=103),
        _bar(1, high=104, low=98, close=99),
        _bar(2, high=110, low=102, close=108),
        _bar(3, high=105, low=101, close=102),
        _bar(4, high=116, low=111, close=112),
        _bar(5, high=114, low=107, close=108),
        _bar(6, high=109, low=96, close=97),
    ]

    payload = StructuralMethodologyAdapter(swing_window=1).compute_smc(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        timeframe="1d",
        bars=bars,
    )

    assert payload["as_of_semantics"] == "confirmed_only"
    assert payload["structure_events"]
    for event in payload["structure_events"]:
        reference = event["reference_swing"]
        assert event["confirmed_at_bar"] >= reference["confirmed_bar_index"]
        assert event["confirmed_at"] >= reference["confirmed_at"]
