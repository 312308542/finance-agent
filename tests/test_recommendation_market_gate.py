from __future__ import annotations

from types import SimpleNamespace

from finance_agent.recommendations.service import (
    RecommendationDecisionContext,
    decide_action,
)


def test_bear_market_tightens_buy_candidate_threshold() -> None:
    score = score_row(total_score=68)
    signal = signal_row(direction="bullish")

    action = decide_action(
        score=score,
        signal=signal,
        risks=[],
        decision_context=RecommendationDecisionContext(
            rank=12,
            total=100,
            style_tendency={"theme": 0.7, "timing_posture": "balanced"},
            market_regime={"regime": "bear"},
        ),
    )

    assert action == "watch"


def test_defensive_profile_is_stricter_than_opportunistic_in_bear_market() -> None:
    score = score_row(total_score=68)
    signal = signal_row(direction="bullish")

    defensive_action = decide_action(
        score=score,
        signal=signal,
        risks=[],
            decision_context=RecommendationDecisionContext(
                rank=9,
                total=100,
                style_tendency={"theme": 0.7, "timing_posture": "defensive"},
                market_regime={"regime": "bear"},
        ),
    )
    opportunistic_action = decide_action(
        score=score,
        signal=signal,
        risks=[],
            decision_context=RecommendationDecisionContext(
                rank=9,
                total=100,
                style_tendency={"theme": 0.7, "timing_posture": "opportunistic"},
                market_regime={"regime": "bear"},
        ),
    )

    assert defensive_action == "watch"
    assert opportunistic_action == "buy_candidate"


def test_market_gate_keeps_score_value_unchanged_in_audit_context() -> None:
    context = RecommendationDecisionContext(
        rank=6,
        total=100,
        style_tendency={"theme": 0.7, "timing_posture": "defensive"},
        market_regime={"regime": "bear", "risk_multiplier": 1.35},
    )

    assert context.adjusted_buy_percentile_threshold == 0.08
    assert context.market_regime == {"regime": "bear", "risk_multiplier": 1.35}


def score_row(*, total_score: int) -> SimpleNamespace:
    return SimpleNamespace(total_score=total_score, confidence=0.82)


def signal_row(*, direction: str) -> SimpleNamespace:
    return SimpleNamespace(direction=direction)
