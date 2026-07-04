from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from finance_agent.indicators.structural_methodology_adapters import (
    StructuralMethodologyAdapter,
    StructuralPriceBar,
)


def _random_walk_bars(seed: int, *, count: int = 250) -> list[StructuralPriceBar]:
    rng = random.Random(seed)
    bars: list[StructuralPriceBar] = []
    price = 100.0
    timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    for index in range(count):
        open_price = price
        close = max(1.0, open_price + rng.gauss(0.0, 1.0))
        spread = abs(rng.gauss(0.0, 0.4)) + 0.2
        high = max(open_price, close) + spread
        low = min(open_price, close) - spread
        bars.append(
            StructuralPriceBar(
                timestamp=timestamp + timedelta(days=index),
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=1000.0 + rng.random() * 100,
            )
        )
        price = close
    return bars


def test_structural_lite_noise_floor_under_random_walks() -> None:
    adapter = StructuralMethodologyAdapter()
    harmonic_detected = 0
    elliott_available = 0
    smc_event_counts: list[int] = []
    fvg_counts: list[int] = []

    for seed in range(30):
        bars = _random_walk_bars(seed)
        harmonic = adapter.compute_harmonic(
            asset_id=f"noise:{seed}",
            symbol=f"NOISE{seed}",
            market="ashare",
            timeframe="1d",
            bars=bars,
        )
        elliott = adapter.compute_elliott(
            asset_id=f"noise:{seed}",
            symbol=f"NOISE{seed}",
            market="ashare",
            timeframe="1d",
            bars=bars,
        )
        smc = adapter.compute_smc(
            asset_id=f"noise:{seed}",
            symbol=f"NOISE{seed}",
            market="ashare",
            timeframe="1d",
            bars=bars,
        )

        harmonic_detected += int(harmonic["status"] == "available")
        elliott_available += int(elliott["status"] == "available")
        smc_event_counts.append(len(smc["structure_events"]))
        fvg_counts.append(len(smc["fair_value_gaps"]))

    assert harmonic_detected / 30 < 0.20
    assert elliott_available / 30 < 0.30
    assert max(smc_event_counts) <= 8
    assert max(fvg_counts) <= 8
