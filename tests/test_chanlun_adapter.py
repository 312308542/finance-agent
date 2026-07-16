from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from finance_agent.indicators.chanlun_adapter import (
    ChanlunAdapter,
    ChanlunBar,
    ChanlunEngineUnavailable,
)


def test_chanlun_adapter_uses_injected_engine_and_outputs_structured_result() -> None:
    bars = sample_bars()
    result = ChanlunAdapter(engine=FakeChanlunEngine()).compute(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        timeframe="1d",
        bars=bars,
    )

    assert result.status == "available"
    assert result.engine == "fake-czsc"
    assert result.engine_version == "test"
    assert result.patterns["fractals"][0]["type"] == "top"
    assert result.patterns["strokes"][0]["direction"] == "up"
    assert result.patterns["centers"][0]["price_low"] == 101
    assert result.signals[0]["name"] == "third_buy"
    assert result.evidence_id == "chanlun:ashare:600519:1d:20260605T000000Z"
    assert result.to_indicator_payload()["schema_version"] == "chanlun_v1"


def test_chanlun_adapter_refuses_to_run_without_engine() -> None:
    with pytest.raises(ChanlunEngineUnavailable, match="czsc"):
        ChanlunAdapter(engine=None).compute(
            asset_id="ashare:600519",
            symbol="600519",
            market="ashare",
            timeframe="1d",
            bars=sample_bars(),
        )


def sample_bars() -> list[ChanlunBar]:
    start = datetime(2026, 6, 1, tzinfo=UTC)
    return [
        ChanlunBar(
            timestamp=start + timedelta(days=index),
            open=100 + index,
            high=102 + index,
            low=99 + index,
            close=101 + index,
            volume=10000 + index,
        )
        for index in range(5)
    ]


class FakeChanlunEngine:
    engine_name = "fake-czsc"
    engine_version = "test"

    def compute(self, bars: list[ChanlunBar]) -> dict[str, object]:
        assert len(bars) == 5
        return {
            "fractals": [{"type": "top", "timestamp": bars[2].timestamp.isoformat()}],
            "strokes": [
                {
                    "direction": "up",
                    "start": bars[0].timestamp.isoformat(),
                    "end": bars[-1].timestamp.isoformat(),
                }
            ],
            "centers": [
                {
                    "price_low": 101,
                    "price_high": 104,
                    "start": bars[1].timestamp.isoformat(),
                    "end": bars[3].timestamp.isoformat(),
                }
            ],
            "signals": [{"name": "third_buy", "confidence": 0.72, "evidence": "中枢上沿回踩确认"}],
        }
