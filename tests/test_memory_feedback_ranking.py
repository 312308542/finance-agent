from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from finance_agent.recommendations.service import (
    MemoryRankingAdjustment,
    apply_memory_ranking_adjustments,
    build_recommendation_payload,
)


def test_memory_feedback_adjusts_ranking_without_changing_total_score() -> None:
    scores = [
        score_row(asset_id="ashare:600519", total_score=70, rank=1),
        score_row(asset_id="ashare:000001", total_score=69, rank=2),
    ]

    ranked = apply_memory_ranking_adjustments(
        scores,
        {
            "ashare:600519": MemoryRankingAdjustment(
                asset_id="ashare:600519",
                adjustment=-8.0,
                reasons=("历史复盘：追高亏损，下调排序。",),
            ),
            "ashare:000001": MemoryRankingAdjustment(
                asset_id="ashare:000001",
                adjustment=3.0,
                reasons=("用户偏好银行板块，上调排序。",),
            ),
        },
    )

    assert [item.asset_id for item in ranked] == ["ashare:000001", "ashare:600519"]
    assert ranked[1].total_score == Decimal("70")
    assert ranked[1].rank == 2
    assert ranked[1].payload["memory_ranking_adjustment"]["adjustment"] == -8.0


def test_recommendation_payload_includes_memory_adjustment_reason() -> None:
    score = score_row(asset_id="ashare:600519", total_score=70, rank=2)
    score.payload["memory_ranking_adjustment"] = {
        "adjustment": -8.0,
        "reasons": ["历史复盘：追高亏损，下调排序。"],
    }

    payload = build_recommendation_payload(
        score=score,
        signal=SimpleNamespace(signal_id="signal:1", direction="bullish", score=75, status="available"),
        risks=[],
        asset_name="贵州茅台",
        rank=2,
        run_id="run:test",
        rule_version="test",
        backtest_evidence={"status": "available"},
    )

    assert payload["memory_ranking_adjustment"]["adjustment"] == -8.0
    assert "历史复盘" in payload["memory_ranking_adjustment"]["reasons"][0]
    assert payload["total_score"] == 70.0


def score_row(*, asset_id: str, total_score: int, rank: int) -> SimpleNamespace:
    symbol = asset_id.split(":", 1)[1]
    return SimpleNamespace(
        score_id=f"score:{symbol}",
        asset_id=asset_id,
        symbol=symbol,
        market="ashare",
        horizon="swing",
        total_score=Decimal(str(total_score)),
        confidence=Decimal("0.80"),
        missing_penalty=Decimal("0"),
        factor_frame_id=f"factor:{symbol}",
        rank=rank,
        payload={},
    )
