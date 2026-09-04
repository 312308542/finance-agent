"""截面标准化、自适应权重和收益风险门槛测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from finance_agent.scoring.adaptive import (
    AdaptiveAlphaEngine,
    AdaptiveAssetInput,
    adaptive_group_weights,
    normalize_cross_section,
    normalize_macd_signal,
)

NOW = datetime(2026, 9, 4, 7, 0, tzinfo=UTC)
FULL_GROUPS = {
    "trend": 80.0,
    "structure": 75.0,
    "sector_leadership": 78.0,
    "capital_flow": 70.0,
    "fundamental_valuation": 65.0,
    "tradability_return_risk": 72.0,
}


def _asset(asset_id: str, **overrides: object) -> AdaptiveAssetInput:
    values: dict[str, object] = {
        "asset_id": asset_id,
        "as_of": NOW,
        "group_scores": dict(FULL_GROUPS),
        "factor_as_of": {group: NOW for group in FULL_GROUPS},
        "data_quality": "available",
        "missing_groups": (),
        "partial_groups": (),
        "expected_return_hint": 0.05,
        "downside_risk": 0.02,
    }
    values.update(overrides)
    return AdaptiveAssetInput(**values)  # type: ignore[arg-type]


def test_constant_cross_section_factor_has_zero_discriminating_weight() -> None:
    assert normalize_cross_section({"a": 80.0, "b": 80.0, "c": 80.0}) == {
        "a": 50.0,
        "b": 50.0,
        "c": 50.0,
    }


def test_cross_section_normalization_preserves_rank_without_price_scale() -> None:
    assert normalize_cross_section({"a": 1.0, "b": 10.0, "c": 100.0}) == {
        "a": 0.0,
        "b": 50.0,
        "c": 100.0,
    }


def test_macd_is_normalized_by_atr_before_cross_asset_comparison() -> None:
    assert normalize_macd_signal(macd=2.0, atr=4.0, price=100.0) == pytest.approx(0.5)
    assert normalize_macd_signal(macd=0.2, atr=0.4, price=10.0) == pytest.approx(0.5)


def test_future_factor_data_is_rejected() -> None:
    future = _asset(
        "ashare:600519",
        factor_as_of={**{group: NOW for group in FULL_GROUPS}, "trend": NOW + timedelta(seconds=1)},
    )

    result = AdaptiveAlphaEngine().score((future,), market_regime="trend_up")[0]

    assert result.eligible_for_buy is False
    assert "future_factor_data" in result.reason_codes


def test_missing_risk_group_reduces_confidence_and_partial_cannot_buy() -> None:
    complete, missing, partial = AdaptiveAlphaEngine().score(
        (
            _asset("ashare:600001"),
            _asset(
                "ashare:600002",
                group_scores={
                    key: value
                    for key, value in FULL_GROUPS.items()
                    if key != "tradability_return_risk"
                },
                missing_groups=("tradability_return_risk",),
            ),
            _asset("ashare:600003", data_quality="partial", partial_groups=("capital_flow",)),
        ),
        market_regime="trend_up",
    )

    assert missing.confidence < complete.confidence
    assert missing.eligible_for_buy is False
    assert partial.eligible_for_buy is False
    assert "data_quality_partial" in partial.reason_codes


def test_market_regime_weights_always_sum_to_one() -> None:
    for regime in ("trend_up", "range", "trend_down", "risk_off"):
        weights = adaptive_group_weights(regime)
        assert set(weights) == set(FULL_GROUPS)
        assert sum(weights.values()) == pytest.approx(1.0)


def test_available_high_confidence_asset_exposes_return_risk_output() -> None:
    results = AdaptiveAlphaEngine().score(
        (_asset("ashare:600001"), _asset("ashare:600002")),
        market_regime="trend_up",
    )

    assert results[0].expected_net_return == pytest.approx(0.05)
    assert results[0].downside_risk == pytest.approx(0.02)
    assert results[0].confidence >= 0.75
    assert results[0].eligible_for_buy is True
    assert len(results[0].contributions) == 6
