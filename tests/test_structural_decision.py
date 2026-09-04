"""结构方向、入场确认和失效硬门槛测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from finance_agent.indicators.structural_methodology_adapters import (
    StructuralMethodologyAdapter,
    StructuralPriceBar,
)
from finance_agent.recommendations.service import compact_decision_structure_frames
from finance_agent.recommendations.structural_decision import StructuralDecisionEngine


def _frame(
    horizon: str,
    *,
    timeframe: str = "1d",
    direction: str = "bullish",
    setup: str | None = None,
    invalidation: str | None = None,
    target: str | None = None,
) -> dict[str, object]:
    return {
        "horizon": horizon,
        "timeframe": timeframe,
        "status": "available",
        "direction": direction,
        "setup": setup,
        "entry_zone": {"low": "9.90", "high": "10.10"},
        "invalidation_price": invalidation,
        "target_price": target,
        "evidence_id": f"evidence:{horizon}:{timeframe}",
    }


def test_missing_primary_structure_blocks_buy() -> None:
    verdict = StructuralDecisionEngine().evaluate(
        frames=(_frame("harmonic_lite_v2"),),
        current_price=Decimal("10"),
    )

    assert verdict.status == "blocked"
    assert verdict.buy_allowed is False
    assert verdict.reason_codes == ("primary_structure_missing",)


def test_daily_bullish_and_hourly_retest_confirms_entry() -> None:
    verdict = StructuralDecisionEngine().evaluate(
        frames=(
            _frame(
                "structural_swings_v2",
                direction="bullish",
                invalidation="9.40",
                target="11.20",
            ),
            _frame("smc_lite_v2", timeframe="60m", setup="retest_holds"),
        ),
        current_price=Decimal("10.00"),
    )

    assert verdict.status == "confirmed"
    assert verdict.direction == "bullish"
    assert verdict.buy_allowed is True
    assert verdict.reward_risk_ratio == pytest.approx(2.0)


def test_conflicting_primary_directions_can_only_wait() -> None:
    verdict = StructuralDecisionEngine().evaluate(
        frames=(
            _frame("structural_swings_v2", direction="bullish"),
            _frame("ichimoku_v1", direction="bearish"),
        ),
        current_price=Decimal("10"),
    )

    assert verdict.status == "waiting"
    assert verdict.buy_allowed is False
    assert "primary_direction_conflict" in verdict.reason_codes


def test_reward_risk_below_two_blocks_buy() -> None:
    verdict = StructuralDecisionEngine().evaluate(
        frames=(
            _frame(
                "structural_swings_v2",
                direction="bullish",
                invalidation="9.40",
                target="11.00",
            ),
            _frame("smc_lite_v2", timeframe="60m", setup="retest_holds"),
        ),
        current_price=Decimal("10"),
    )

    assert verdict.status == "blocked"
    assert verdict.buy_allowed is False
    assert verdict.reason_codes == ("reward_risk_below_two",)


def test_ichimoku_is_primary_but_harmonic_and_elliott_are_auxiliary() -> None:
    primary = StructuralDecisionEngine().evaluate(
        frames=(_frame("ichimoku_v1", direction="bullish"),),
        current_price=Decimal("10"),
    )
    auxiliary = StructuralDecisionEngine().evaluate(
        frames=(_frame("harmonic_lite_v2"), _frame("elliott_lite_v2")),
        current_price=Decimal("10"),
    )

    assert primary.status == "waiting"
    assert primary.reason_codes == ("entry_confirmation_missing",)
    assert auxiliary.reason_codes == ("primary_structure_missing",)


def test_price_below_invalidation_returns_invalidated() -> None:
    verdict = StructuralDecisionEngine().evaluate(
        frames=(
            _frame(
                "structural_swings_v2",
                direction="bullish",
                invalidation="9.40",
                target="11.20",
            ),
        ),
        current_price=Decimal("9.20"),
    )

    assert verdict.status == "invalidated"
    assert verdict.buy_allowed is False
    assert verdict.reason_codes == ("invalidation_price_breached",)


def test_real_swing_and_smc_payloads_produce_auditable_entry_and_risk_levels() -> None:
    prices = [10.0, 15.0, 12.0, 18.0, 15.0, 21.0, 18.0, 24.0, 22.0, 24.5]
    bars = [
        StructuralPriceBar(
            timestamp=datetime(2026, 8, 1, tzinfo=UTC) + timedelta(hours=index),
            open=price - 0.1,
            high=price + 0.2,
            low=price - 0.2,
            close=price,
            volume=1000 + index * 10,
        )
        for index, price in enumerate(prices)
    ]
    adapter = StructuralMethodologyAdapter(swing_window=1)
    daily_swings = adapter.compute_swings(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        timeframe="1d",
        bars=bars,
    )
    hourly_smc = adapter.compute_smc(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        timeframe="60m",
        bars=bars,
    )

    verdict = StructuralDecisionEngine().evaluate(
        frames=(daily_swings, hourly_smc),
        current_price=Decimal("24.5"),
    )

    assert verdict.status == "confirmed"
    assert verdict.direction == "bullish"
    assert verdict.entry_zone is not None
    assert verdict.invalidation_price == Decimal("21.8")
    assert verdict.target_price is not None
    assert verdict.reward_risk_ratio is not None
    assert verdict.reward_risk_ratio >= 2

    compacted = compact_decision_structure_frames(
        (
            SimpleNamespace(
                horizon=daily_swings["schema_version"],
                timeframe="1d",
                status=daily_swings["status"],
                as_of=datetime.fromisoformat(daily_swings["input_end_at"]),
                payload=daily_swings,
            ),
            SimpleNamespace(
                horizon=hourly_smc["schema_version"],
                timeframe="60m",
                status=hourly_smc["status"],
                as_of=datetime.fromisoformat(hourly_smc["input_end_at"]),
                payload=hourly_smc,
            ),
        )
    )
    compacted_verdict = StructuralDecisionEngine().evaluate(
        frames=compacted,
        current_price=Decimal("24.5"),
    )
    assert compacted_verdict.status == "confirmed"

    extended_bars = [
        *bars,
        StructuralPriceBar(
            timestamp=bars[-1].timestamp + timedelta(hours=1),
            open=24.4,
            high=24.8,
            low=24.3,
            close=24.6,
            volume=1200,
        ),
    ]
    next_daily = adapter.compute_swings(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        timeframe="1d",
        bars=extended_bars,
    )
    next_hourly = adapter.compute_smc(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        timeframe="60m",
        bars=extended_bars,
    )
    next_verdict = StructuralDecisionEngine().evaluate(
        frames=(next_daily, next_hourly),
        current_price=Decimal("24.6"),
    )
    assert next_verdict.status == "confirmed"
    assert next_verdict.invalidation_price == verdict.invalidation_price
