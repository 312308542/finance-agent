"""结构方向、入场确认和失效硬门槛测试。"""

from __future__ import annotations

from decimal import Decimal

import pytest

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
