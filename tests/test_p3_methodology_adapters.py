from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from finance_agent.indicators.p3_methodology_adapters import (
    HarmonicAdapter,
    HarmonicEngineUnavailable,
    PricePoint,
    SeasonalAdapter,
)


def test_seasonal_adapter_computes_monthly_return_profile() -> None:
    start = datetime(2024, 1, 31, tzinfo=UTC)
    prices = []
    close = 100.0
    for index in range(30):
        timestamp = start + timedelta(days=30 * index)
        close *= 1.02 if timestamp.month in {1, 2, 3} else 0.99
        prices.append(PricePoint(timestamp=timestamp, close=close))

    result = SeasonalAdapter().compute(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        timeframe="1mo",
        prices=prices,
        min_observations=20,
    )

    assert result.status == "available"
    assert result.monthly_profile[2]["average_return"] > 0
    assert result.best_month in {1, 2, 3}
    assert result.to_indicator_payload()["schema_version"] == "seasonal_v1"


def test_harmonic_adapter_refuses_without_engine() -> None:
    with pytest.raises(HarmonicEngineUnavailable, match="pyharmonics"):
        HarmonicAdapter(engine=None).compute(
            asset_id="crypto_spot:BTCUSDT",
            symbol="BTCUSDT",
            market="crypto_spot",
            timeframe="1d",
            prices=[PricePoint(timestamp=datetime(2026, 1, 1, tzinfo=UTC), close=100.0)],
        )
