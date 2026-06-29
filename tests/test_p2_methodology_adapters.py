from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from finance_agent.indicators.methodology_adapters import (
    AssetCloseSeries,
    CorrelationAdapter,
    IchimokuAdapter,
    PairTradingAdapter,
    PriceBar,
    PricePoint,
)


def test_ichimoku_adapter_computes_latest_lines_and_signal() -> None:
    bars = [
        PriceBar(
            timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index),
            open=100 + index,
            high=104 + index,
            low=96 + index,
            close=103 + index,
            volume=10000 + index,
        )
        for index in range(60)
    ]

    result = IchimokuAdapter().compute(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        timeframe="1d",
        bars=bars,
    )

    assert result.status == "available"
    assert result.lines["tenkan_sen"] == pytest.approx(155.0)
    assert result.lines["kijun_sen"] == pytest.approx(146.5)
    assert result.lines["senkou_span_a"] == pytest.approx(150.75)
    assert result.lines["senkou_span_b"] == pytest.approx(133.5)
    assert result.signals[0]["name"] == "price_above_cloud"
    assert result.evidence_id == "ichimoku:ashare:600519:1d:20260301T000000Z"
    assert result.to_indicator_payload()["schema_version"] == "ichimoku_v1"


def test_correlation_adapter_aligns_returns_and_outputs_pair_correlations() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    left_prices = [100, 110, 99, 108.9, 98.01, 107.811]
    right_prices = [price * 2 for price in left_prices]
    inverse_prices = [100, 90, 99, 89.1, 98.01, 88.209]
    left = make_close_series("ashare:600519", "600519", left_prices, start=start)
    right = make_close_series("ashare:000001", "000001", right_prices, start=start)
    inverse = make_close_series("ashare:000002", "000002", inverse_prices, start=start)

    result = CorrelationAdapter().compute(
        market="ashare",
        timeframe="1d",
        series=[left, right, inverse],
        min_observations=4,
    )

    pair_map = {
        (item["left_asset_id"], item["right_asset_id"]): item["correlation"]
        for item in result.correlations
    }
    assert pair_map[("ashare:600519", "ashare:000001")] == pytest.approx(1.0)
    assert pair_map[("ashare:600519", "ashare:000002")] < 0
    assert result.evidence_id == "correlation:ashare:1d:20260106T000000Z"
    assert result.to_indicator_payload()["schema_version"] == "correlation_v1"


def test_pair_trading_adapter_computes_hedge_ratio_and_zscore_signal() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    base = [100 + index for index in range(40)]
    paired = [5 + 1.5 * value for value in base]
    paired[-1] += 18
    left = make_close_series("ashare:PAIR_A", "PAIR_A", paired, start=start)
    right = make_close_series("ashare:PAIR_B", "PAIR_B", base, start=start)

    result = PairTradingAdapter().compute(
        left=left,
        right=right,
        timeframe="1d",
        entry_zscore=2.0,
        min_observations=30,
    )

    assert result.hedge_ratio == pytest.approx(1.5, abs=0.15)
    assert result.spread_zscore > 2.0
    assert result.signal["name"] == "short_left_long_right"
    assert result.to_indicator_payload()["schema_version"] == "pair_trading_v1"


def make_close_series(
    asset_id: str,
    symbol: str,
    closes: list[float],
    *,
    start: datetime,
) -> AssetCloseSeries:
    return AssetCloseSeries(
        asset_id=asset_id,
        symbol=symbol,
        market="ashare",
        prices=[
            PricePoint(timestamp=start + timedelta(days=index), close=float(close))
            for index, close in enumerate(closes)
        ],
    )
