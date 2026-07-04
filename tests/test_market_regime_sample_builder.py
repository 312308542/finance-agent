from __future__ import annotations

from datetime import date
from decimal import Decimal

from scripts.data.build_market_regime_samples import (
    DailyBarSample,
    build_regime_samples,
    build_equal_weight_proxy_index_bars,
    compute_breadth_metrics,
    compute_index_regime_input,
)


def test_compute_index_regime_input_uses_20d_60d_trend_and_volatility() -> None:
    bars = [
        DailyBarSample(asset_id="ashare:000001", trade_date=date(2026, 1, day), close=Decimal("100"))
        for day in range(1, 21)
    ]
    bars.extend(
        DailyBarSample(asset_id="ashare:000001", trade_date=date(2026, 2, day), close=Decimal("100"))
        for day in range(1, 21)
    )
    bars.extend(
        DailyBarSample(
            asset_id="ashare:000001",
            trade_date=date(2026, 3, day),
            close=Decimal("80"),
        )
        for day in range(1, 21)
    )

    regime_input = compute_index_regime_input(
        bars,
        as_of=date(2026, 3, 30),
        advance_decline_ratio=0.5,
        limit_up_down_ratio=0.2,
    )

    assert regime_input.index_trend_20d < -0.10
    assert regime_input.index_trend_60d < -0.15
    assert regime_input.volatility_20d >= 0
    assert regime_input.advance_decline_ratio == 0.5
    assert regime_input.limit_up_down_ratio == 0.2


def test_compute_breadth_metrics_counts_advancers_and_limit_ratio() -> None:
    bars = [
        DailyBarSample("ashare:600001", date(2026, 7, 2), Decimal("110"), Decimal("100")),
        DailyBarSample("ashare:600002", date(2026, 7, 2), Decimal("90"), Decimal("100")),
        DailyBarSample("ashare:600003", date(2026, 7, 2), Decimal("80"), Decimal("100")),
        DailyBarSample("ashare:600004", date(2026, 7, 2), Decimal("110"), Decimal("100")),
    ]

    metrics = compute_breadth_metrics(bars)

    assert metrics["advance_decline_ratio"] == 1.0
    assert metrics["limit_up_down_ratio"] == 1.0
    assert metrics["advancers"] == 2
    assert metrics["decliners"] == 2


def test_build_equal_weight_proxy_index_bars_from_daily_returns() -> None:
    bars = [
        DailyBarSample("ashare:600001", date(2026, 7, 1), Decimal("110"), Decimal("100")),
        DailyBarSample("ashare:600002", date(2026, 7, 1), Decimal("100"), Decimal("100")),
        DailyBarSample("ashare:600001", date(2026, 7, 2), Decimal("121"), Decimal("110")),
        DailyBarSample("ashare:600002", date(2026, 7, 2), Decimal("90"), Decimal("100")),
    ]

    proxy = build_equal_weight_proxy_index_bars(bars, proxy_asset_id="ashare:proxy:equal_weight")

    assert [item.trade_date.isoformat() for item in proxy] == ["2026-07-01", "2026-07-02"]
    assert proxy[0].asset_id == "ashare:proxy:equal_weight"
    assert proxy[0].close == Decimal("105.000000")
    assert proxy[1].close == Decimal("105.000000")


def test_build_regime_samples_returns_bear_and_range_windows() -> None:
    index_bars = [
        DailyBarSample("ashare:000001", date(2026, 1, day), Decimal("100"))
        for day in range(1, 21)
    ]
    index_bars.extend(
        DailyBarSample("ashare:000001", date(2026, 2, day), Decimal("100"))
        for day in range(1, 21)
    )
    index_bars.extend(
        DailyBarSample("ashare:000001", date(2026, 3, day), Decimal("86"))
        for day in range(1, 21)
    )
    index_bars.extend(
        DailyBarSample("ashare:000001", date(2026, 4, day), Decimal("86"))
        for day in range(1, 21)
    )
    breadth_by_date = {
        date(2026, 3, 20): {"advance_decline_ratio": 0.4, "limit_up_down_ratio": 0.2},
        date(2026, 4, 20): {"advance_decline_ratio": 1.0, "limit_up_down_ratio": 1.0},
    }

    samples = build_regime_samples(index_bars, breadth_by_date=breadth_by_date, limit=1)

    assert samples["bear"][0]["as_of"] == "2026-03-20"
    assert samples["bear"][0]["regime"] == "bear"
    assert samples["range"][0]["as_of"] == "2026-04-20"
    assert samples["range"][0]["regime"] == "range"
