from __future__ import annotations

from finance_agent.application.market_context_service import (
    MarketRegimeInput,
    MarketRegimeService,
    adjust_buy_percentile_threshold,
)


def test_market_regime_classifies_risk_off_with_auditable_evidence() -> None:
    result = MarketRegimeService().evaluate(
        MarketRegimeInput(
            index_trend_20d=-0.09,
            index_trend_60d=-0.14,
            volatility_20d=0.36,
            advance_decline_ratio=0.25,
            limit_up_down_ratio=0.15,
            northbound_flow_score=-0.7,
            evidence_ids=("bar:000001:20260630", "flow:northbound:20260630"),
        )
    )

    assert result.regime == "risk_off"
    assert result.legacy_regime == "bear"
    assert result.strength == "high"
    assert result.risk_multiplier > 1
    assert result.risk_budget.total_exposure == 0
    assert result.risk_budget.allow_new_buys is False
    assert result.risk_budget.allow_sector_override is False
    assert result.evidence_ids == ("bar:000001:20260630", "flow:northbound:20260630")
    assert any("60日趋势为 -14.00%" in reason for reason in result.reasons)


def test_market_regime_classifies_bull_market() -> None:
    result = MarketRegimeService().evaluate(
        MarketRegimeInput(
            index_trend_20d=0.07,
            index_trend_60d=0.14,
            volatility_20d=0.16,
            advance_decline_ratio=1.35,
            limit_up_down_ratio=2.4,
            northbound_flow_score=0.8,
        )
    )

    assert result.regime == "trend_up"
    assert result.legacy_regime == "bull"
    assert result.strength == "high"
    assert result.risk_multiplier < 1
    assert result.risk_budget.total_exposure == 1.0
    assert result.risk_budget.allow_new_buys is True


def test_market_regime_keeps_range_when_signals_conflict() -> None:
    result = MarketRegimeService().evaluate(
        MarketRegimeInput(
            index_trend_20d=0.01,
            index_trend_60d=-0.01,
            volatility_20d=0.2,
            advance_decline_ratio=0.98,
            limit_up_down_ratio=1.1,
            northbound_flow_score=0.0,
        )
    )

    assert result.regime == "range"
    assert result.strength == "medium"
    assert result.risk_multiplier == 1.0
    assert result.risk_budget.total_exposure == 0.7


def test_trend_down_allows_only_reduced_sector_override_budget() -> None:
    result = MarketRegimeService().evaluate(
        MarketRegimeInput(
            index_trend_20d=-0.06,
            index_trend_60d=-0.09,
            volatility_20d=0.24,
            advance_decline_ratio=0.65,
            limit_up_down_ratio=0.45,
        )
    )

    assert result.regime == "trend_down"
    assert result.legacy_regime == "bear"
    assert result.risk_budget.total_exposure == 0.35
    assert result.risk_budget.allow_sector_override is True


def test_market_result_serializes_new_and_legacy_regime_with_budget() -> None:
    result = MarketRegimeService().evaluate(
        MarketRegimeInput(
            index_trend_20d=0.01,
            index_trend_60d=0.01,
            volatility_20d=0.22,
            advance_decline_ratio=1.0,
            limit_up_down_ratio=1.0,
        )
    ).to_dict()

    assert result["regime"] == "range"
    assert result["legacy_regime"] == "range"
    assert result["risk_budget"] == {
        "total_exposure": 0.7,
        "per_position_risk": 0.008,
        "allow_new_buys": True,
        "allow_sector_override": True,
    }


def test_adjust_buy_percentile_threshold_respects_profile_timing_posture() -> None:
    assert adjust_buy_percentile_threshold(
        base_threshold=0.12,
        regime="bear",
        timing_posture="defensive",
    ) == 0.048
    assert adjust_buy_percentile_threshold(
        base_threshold=0.12,
        regime="bear",
        timing_posture="opportunistic",
    ) == 0.084
    assert adjust_buy_percentile_threshold(
        base_threshold=0.12,
        regime="bull",
        timing_posture="opportunistic",
    ) == 0.144
    assert adjust_buy_percentile_threshold(
        base_threshold=0.12,
        regime="trend_down",
        timing_posture="defensive",
    ) == 0.048
    assert adjust_buy_percentile_threshold(
        base_threshold=0.12,
        regime="trend_up",
        timing_posture="opportunistic",
    ) == 0.144
