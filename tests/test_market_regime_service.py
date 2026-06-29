from __future__ import annotations

from finance_agent.application.market_context_service import (
    MarketRegimeInput,
    MarketRegimeService,
    adjust_buy_percentile_threshold,
)


def test_market_regime_classifies_bear_market_with_auditable_evidence() -> None:
    result = MarketRegimeService().evaluate(
        MarketRegimeInput(
            index_trend_20d=-0.08,
            index_trend_60d=-0.12,
            volatility_20d=0.34,
            advance_decline_ratio=0.36,
            limit_up_down_ratio=0.2,
            northbound_flow_score=-0.7,
            evidence_ids=("bar:000001:20260630", "flow:northbound:20260630"),
        )
    )

    assert result.regime == "bear"
    assert result.strength == "high"
    assert result.risk_multiplier > 1
    assert result.evidence_ids == ("bar:000001:20260630", "flow:northbound:20260630")
    assert any("60日趋势为 -12.00%" in reason for reason in result.reasons)


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

    assert result.regime == "bull"
    assert result.strength == "high"
    assert result.risk_multiplier < 1


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
