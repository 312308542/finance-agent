from __future__ import annotations

from datetime import UTC, datetime, timedelta

from finance_agent.indicators.structural_methodology_adapters import (
    StructuralMethodologyAdapter,
    StructuralPriceBar,
)


def _bar(
    day: int,
    *,
    high: float,
    low: float,
    close: float | None = None,
    open_: float | None = None,
    volume: float = 1000.0,
) -> StructuralPriceBar:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day)
    parsed_close = close if close is not None else (high + low) / 2
    parsed_open = open_ if open_ is not None else parsed_close
    return StructuralPriceBar(
        timestamp=timestamp,
        open=parsed_open,
        high=high,
        low=low,
        close=parsed_close,
        volume=volume,
    )


def _interpolated_bars(points: list[tuple[int, float]]) -> list[StructuralPriceBar]:
    prices: dict[int, float] = {}
    for (left_day, left_price), (right_day, right_price) in zip(points, points[1:], strict=False):
        span = right_day - left_day
        for offset in range(span + 1):
            ratio = offset / span if span else 0.0
            prices[left_day + offset] = left_price + (right_price - left_price) * ratio
    return [
        _bar(day, high=price, low=price, close=price, open_=price)
        for day, price in sorted(prices.items())
    ]


def test_swing_detector_returns_alternating_points_and_payload_metadata() -> None:
    bars = [
        _bar(0, high=10, low=9),
        _bar(1, high=14, low=10),
        _bar(2, high=11, low=7),
        _bar(3, high=15, low=9),
        _bar(4, high=12, low=6),
        _bar(5, high=16, low=10),
        _bar(6, high=13, low=8),
        _bar(7, high=17, low=11),
        _bar(8, high=14, low=9),
    ]

    payload = StructuralMethodologyAdapter(swing_window=1).compute_swings(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        timeframe="1d",
        bars=bars,
    )

    assert payload["schema_version"] == "structural_swings_v2"
    assert payload["status"] == "available"
    assert payload["engine"] == "finance-agent-structural-lite"
    assert payload["engine_version"] == "2026.07.04"
    assert payload["confidence"] > 0
    assert payload["bar_count"] == len(bars)
    assert payload["evidence_id"].startswith("structural_swings:ashare:600519:1d:")
    assert [point["type"] for point in payload["swings"]] == ["H", "L", "H", "L", "H", "L", "H"]
    assert payload["swings"][0]["price"] == 14.0
    assert payload["swings"][1]["price"] == 7.0
    assert payload["segments"][-1]["direction"] == "up"
    assert any("LLM" in line for line in payload["red_lines"])


def test_harmonic_lite_detects_bullish_gartley_candidate() -> None:
    bars = [
        _bar(0, high=110, low=105),
        _bar(1, high=106, low=100),  # X
        _bar(2, high=120, low=110),  # A
        _bar(3, high=112, low=108),  # B
        _bar(4, high=116, low=111),  # C
        _bar(5, high=109, low=104),  # D
        _bar(6, high=112, low=107),
    ]

    payload = StructuralMethodologyAdapter(swing_window=1).compute_harmonic(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        timeframe="1d",
        bars=bars,
    )

    assert payload["schema_version"] == "harmonic_lite_v2"
    assert payload["status"] == "available"
    assert payload["patterns"][0]["pattern"] == "Gartley"
    assert payload["patterns"][0]["direction"] == "bullish"
    assert payload["patterns"][0]["points"]["X"]["price"] == 100.0
    assert 0.55 <= payload["patterns"][0]["ratios"]["b_retrace"] <= 0.68
    assert 0.7 <= payload["patterns"][0]["confidence"] <= 0.95
    assert payload["patterns"][0]["invalidation_price"] < 104.0
    assert "pyharmonics" not in payload["engine"]


def test_default_swing_window_detects_fresh_harmonic_structure() -> None:
    bars = _interpolated_bars(
        [
            (0, 110.0),
            (10, 100.0),
            (30, 120.0),
            (50, 110.0),
            (70, 115.0),
            (90, 102.4),
            (100, 105.0),
        ]
    )

    adapter = StructuralMethodologyAdapter()
    assert adapter.swing_window == 10

    payload = adapter.compute_harmonic(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        timeframe="1d",
        bars=bars,
    )

    assert payload["status"] == "available"
    assert payload["patterns"][0]["pattern"] == "Bat"
    assert payload["patterns"][0]["bars_since_d"] == 10


def test_default_adapter_returns_insufficient_data_before_minimum_window() -> None:
    bars = [_bar(day, high=100 + day, low=99 + day, close=99.5 + day) for day in range(39)]
    adapter = StructuralMethodologyAdapter()

    swings = adapter.compute_swings(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        timeframe="1d",
        bars=bars,
    )
    harmonic = adapter.compute_harmonic(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        timeframe="1d",
        bars=bars,
    )
    smc = adapter.compute_smc(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        timeframe="1d",
        bars=bars,
    )
    elliott = adapter.compute_elliott(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        timeframe="1d",
        bars=bars,
    )

    assert swings["status"] == "insufficient_data"
    assert swings["swings"] == []
    assert harmonic["status"] == "insufficient_data"
    assert harmonic["patterns"] == []
    assert smc["status"] == "insufficient_data"
    assert smc["structure_events"] == []
    assert smc["fair_value_gaps"] == []
    assert elliott["status"] == "insufficient_data"
    assert elliott["candidates"] == []


def test_harmonic_lite_uses_four_ratios_to_prefer_bat_over_gartley() -> None:
    bars = [
        _bar(0, high=108, low=105),
        _bar(1, high=106, low=100.0),  # X
        _bar(2, high=120.0, low=114),
        _bar(3, high=112, low=110.0),  # B retrace = 0.5
        _bar(4, high=115.0, low=112),  # BC/AB = 0.5
        _bar(5, high=107, low=102.4),  # D retrace = 0.88, CD/BC = 2.52
        _bar(6, high=106, low=103),
    ]

    payload = StructuralMethodologyAdapter(swing_window=1).compute_harmonic(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        timeframe="1d",
        bars=bars,
    )

    assert payload["status"] == "available"
    assert payload["patterns"][0]["pattern"] == "Bat"
    assert payload["patterns"][0]["ratios"]["cd_ratio"] > 2.0
    assert payload["patterns"][0]["bars_since_d"] == 1
    assert payload["patterns"][0]["completion"] == "complete"


def test_harmonic_lite_filters_stale_patterns_by_default() -> None:
    bars = [
        _bar(0, high=108, low=105),
        _bar(1, high=106, low=100.0),
        _bar(2, high=120.0, low=114),
        _bar(3, high=112, low=110.0),
        _bar(4, high=115.0, low=112),
        _bar(5, high=107, low=102.4),
        _bar(6, high=106, low=103),
    ]
    bars.extend(_bar(day, high=106, low=103, close=104) for day in range(7, 70))

    payload = StructuralMethodologyAdapter(swing_window=1).compute_harmonic(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        timeframe="1d",
        bars=bars,
    )

    assert payload["status"] == "no_pattern"
    assert payload["patterns"] == []


def test_smc_lite_detects_structure_breaks_and_fvg() -> None:
    bars = [
        _bar(0, high=106, low=101, close=103),
        _bar(1, high=104, low=98, close=99),
        _bar(2, high=110, low=102, close=108),
        _bar(3, high=105, low=101, close=102),
        _bar(4, high=116, low=111, close=112),
        _bar(5, high=114, low=107, close=108),
        _bar(6, high=109, low=96, close=97),
    ]

    payload = StructuralMethodologyAdapter(
        swing_window=1,
        fvg_min_atr_ratio=0.0,
        fvg_include_mitigated=True,
    ).compute_smc(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        timeframe="1d",
        bars=bars,
    )

    event_names = {event["name"] for event in payload["structure_events"]}
    gap_names = {gap["name"] for gap in payload["fair_value_gaps"]}
    assert payload["schema_version"] == "smc_lite_v2"
    assert "bos_bullish" in event_names
    assert "choch_bearish" in event_names
    assert "fvg_bullish" in gap_names
    assert payload["confidence"] >= 0.55
    assert any("订单块" in caveat for caveat in payload["caveats"])


def test_smc_lite_confidence_uses_event_strength_not_event_count() -> None:
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

    event_confidences = [
        item["confidence"]
        for item in payload["structure_events"] + payload["fair_value_gaps"]
    ]
    assert payload["confidence_method"] == "max_event_or_gap_confidence"
    assert payload["confidence"] == max(event_confidences)
    assert payload["confidence"] < 0.85


def test_smc_lite_filters_small_fvg_by_atr_floor() -> None:
    bars = [
        _bar(0, high=120, low=100, close=110),
        _bar(1, high=119, low=101, close=110),
        _bar(2, high=123, low=121, close=122),  # width=1, ATR 显著更大
        _bar(3, high=124, low=119, close=120),
    ]

    payload = StructuralMethodologyAdapter(swing_window=1).compute_smc(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        timeframe="1d",
        bars=bars,
    )

    assert payload["fair_value_gaps"] == []


def test_smc_lite_filters_mitigated_fvg_by_default() -> None:
    bars = [
        _bar(0, high=100, low=95, close=98),
        _bar(1, high=103, low=97, close=101),
        _bar(2, high=112, low=110, close=111),  # bullish FVG: 100~110
        _bar(3, high=111, low=99, close=100),  # 回补缺口
        _bar(4, high=110, low=98, close=99),
    ]

    payload = StructuralMethodologyAdapter(
        swing_window=1,
        fvg_min_atr_ratio=0.0,
    ).compute_smc(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        timeframe="1d",
        bars=bars,
    )

    assert payload["fair_value_gaps"] == []


def test_elliott_lite_outputs_candidate_only_for_clear_structure() -> None:
    clear_bars = [
        _bar(0, high=106, low=104),
        _bar(1, high=105, low=100),  # start L
        _bar(2, high=110, low=106),  # wave 1 H
        _bar(3, high=106, low=104.5),  # wave 2 L
        _bar(4, high=121.5, low=116),  # wave 3 H
        _bar(5, high=117, low=114.5),  # wave 4 L
        _bar(6, high=126.5, low=120),  # wave 5 H
        _bar(7, high=124, low=118),
    ]
    weak_bars = [
        _bar(0, high=101, low=99),
        _bar(1, high=102, low=100),
        _bar(2, high=103, low=101),
        _bar(3, high=104, low=102),
        _bar(4, high=105, low=103),
        _bar(5, high=106, low=104),
    ]

    adapter = StructuralMethodologyAdapter(swing_window=1, min_bars_per_wave=1)
    clear_payload = adapter.compute_elliott(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        timeframe="1d",
        bars=clear_bars,
    )
    weak_payload = adapter.compute_elliott(
        asset_id="ashare:000001",
        symbol="000001",
        market="ashare",
        timeframe="1d",
        bars=weak_bars,
    )

    assert clear_payload["schema_version"] == "elliott_lite_v2"
    assert clear_payload["status"] == "available"
    impulse = next(
        candidate
        for candidate in clear_payload["candidates"]
        if candidate["pattern"] == "bullish_impulse_complete"
    )
    assert impulse["confidence"] >= 0.7
    assert impulse["signal_hint"] == "uptrend_exhaustion_risk"
    assert impulse["thesis_confirmation_price"] == 114.5
    assert impulse["thesis_invalidation_price"] == 126.5
    assert "invalidation_price" not in impulse
    assert weak_payload["status"] == "insufficient_structure"
    assert weak_payload["candidates"] == []
    assert weak_payload["confidence"] < 0.6


def test_elliott_lite_filters_each_candidate_below_threshold() -> None:
    bars = _interpolated_bars(
        [
            (0, 105.0),
            (1, 100.0),
            (2, 110.0),
            (3, 109.0),
            (4, 117.0),
            (5, 116.0),
            (6, 117.5),  # 低质量推动候选，当前旧实现会随高分候选一起放行
            (7, 100.0),
            (8, 110.0),
            (9, 105.0),
            (10, 122.0),
            (11, 116.0),
            (12, 128.0),
            (13, 126.0),
        ]
    )

    payload = StructuralMethodologyAdapter(
        swing_window=1,
        min_bars_per_wave=1,
    ).compute_elliott(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        timeframe="1d",
        bars=bars,
    )

    assert payload["status"] == "available"
    assert payload["candidates"]
    assert all(candidate["confidence"] >= 0.6 for candidate in payload["candidates"])


def test_swing_detector_keeps_first_equal_extreme() -> None:
    bars = [
        _bar(0, high=10, low=9),
        _bar(1, high=15, low=12),
        _bar(2, high=15, low=11),
        _bar(3, high=14, low=10),
        _bar(4, high=13, low=8),
        _bar(5, high=12, low=9),
    ]

    payload = StructuralMethodologyAdapter(swing_window=1).compute_swings(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        timeframe="1d",
        bars=bars,
    )

    assert any(point["type"] == "H" and point["bar_index"] == 1 for point in payload["swings"])


def test_normalize_bars_deduplicates_same_timestamp_and_counts_warnings() -> None:
    duplicate_timestamp = datetime(2026, 1, 2, tzinfo=UTC)
    bars = [
        StructuralPriceBar(duplicate_timestamp - timedelta(days=1), 100, 101, 99, 100, 1000),
        StructuralPriceBar(duplicate_timestamp, 100, 110, 95, 100, 1000),
        StructuralPriceBar(duplicate_timestamp, 100, 105, 102, 104, 1000),
        StructuralPriceBar(duplicate_timestamp + timedelta(days=1), 104, 106, 103, 105, 1000),
    ]

    payload = StructuralMethodologyAdapter(swing_window=1).compute_swings(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        timeframe="1d",
        bars=bars,
    )

    assert payload["bar_count"] == 3
    assert payload["data_warnings"]["duplicate_timestamp_count"] == 1
