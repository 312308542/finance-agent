from __future__ import annotations

from types import SimpleNamespace

from scripts.data.validate_market_regime_gate import (
    evaluate_actions_under_regime,
    summarize_gate_validation,
)


def test_evaluate_actions_under_regime_tightens_bear_market_buy_candidates() -> None:
    scores = [
        score_row(rank=rank, total_score=70, confidence=0.82)
        for rank in range(1, 21)
    ]
    signals = {score.asset_id: signal_row("bullish") for score in scores}

    range_summary = evaluate_actions_under_regime(
        scores=scores,
        signals_by_asset=signals,
        risks_by_asset={},
        market_regime={"regime": "range", "strength": "medium"},
        profile_style_tendency={"theme": 0.7, "timing_posture": "balanced"},
    )
    bear_summary = evaluate_actions_under_regime(
        scores=scores,
        signals_by_asset=signals,
        risks_by_asset={},
        market_regime={"regime": "bear", "strength": "medium"},
        profile_style_tendency={"theme": 0.7, "timing_posture": "balanced"},
    )

    assert range_summary["action_counts"]["buy_candidate"] == 4
    assert bear_summary["action_counts"]["buy_candidate"] == 2
    assert bear_summary["adjusted_buy_percentile_threshold"] < range_summary[
        "adjusted_buy_percentile_threshold"
    ]


def test_summarize_gate_validation_requires_bear_not_looser_than_range() -> None:
    summary = summarize_gate_validation(
        regime_summaries=[
            {"regime": "bear", "action_counts": {"buy_candidate": 2}},
            {"regime": "range", "action_counts": {"buy_candidate": 4}},
            {"regime": "bull", "action_counts": {"buy_candidate": 4}},
        ]
    )

    assert summary["passed"] is True
    assert summary["checks"]["bear_buy_not_above_range"] is True


def score_row(*, rank: int, total_score: int, confidence: float) -> SimpleNamespace:
    return SimpleNamespace(
        asset_id=f"ashare:600{rank:03d}",
        symbol=f"600{rank:03d}",
        rank=rank,
        total_score=total_score,
        confidence=confidence,
    )


def signal_row(direction: str) -> SimpleNamespace:
    return SimpleNamespace(direction=direction)
