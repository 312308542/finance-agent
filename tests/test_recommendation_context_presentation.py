from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from finance_agent.recommendations.service import (
    RecommendationDecisionContext,
    build_recommendation_payload,
)


def test_recommendation_payload_presents_market_tradability_and_memory_context() -> None:
    score = score_row()
    score.payload["memory_ranking_adjustment"] = {
        "adjustment": -6.0,
        "reasons": ["历史复盘显示同类追高亏损，排序下调。"],
    }

    payload = build_recommendation_payload(
        score=score,
        signal=SimpleNamespace(signal_id="signal:1", direction="bullish", score=76, status="available"),
        risks=[],
        asset_name="测试股票",
        rank=3,
        run_id="run:test",
        rule_version="test",
        backtest_evidence={"status": "available"},
        decision_context=RecommendationDecisionContext(
            rank=3,
            total=50,
            market_regime={"regime": "bear", "strength": "high"},
            tradability={"tradable": False, "blocking_level": "blocked", "reasons": ["one_word_limit_up"]},
        ),
    )

    assert any("大盘环境 bear/high" in reason for reason in payload["reasons"])
    assert any("当前买入受限" in rebuttal for rebuttal in payload["risk_rebuttals"])
    assert any("历史复盘显示同类追高亏损" in rebuttal for rebuttal in payload["risk_rebuttals"])


def score_row() -> SimpleNamespace:
    return SimpleNamespace(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        horizon="swing",
        total_score=Decimal("72"),
        confidence=Decimal("0.82"),
        score_id="score:1",
        factor_frame_id="factor:1",
        missing_penalty=Decimal("0"),
        rank=3,
        payload={},
    )
