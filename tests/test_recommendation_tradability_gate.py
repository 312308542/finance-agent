from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from finance_agent.recommendations.portfolio_construction import (
    PortfolioCandidate,
    PortfolioConstructionEngine,
    PortfolioRiskBudget,
)
from finance_agent.recommendations.service import (
    RecommendationDecisionContext,
    build_recommendation_payload,
    decide_action,
)


def test_unbuyable_asset_is_downgraded_to_watch_even_when_top_ranked() -> None:
    action = decide_action(
        score=score_row(total_score=82),
        signal=signal_row(direction="bullish"),
        risks=[],
        decision_context=RecommendationDecisionContext(
            rank=1,
            total=50,
            style_tendency={"theme": 0.7},
            tradability={"tradable": False, "blocking_level": "blocked", "reasons": ["one_word_limit_up"]},
        ),
    )

    assert action == "watch"


def test_recommendation_payload_includes_tradability_context_and_watch_reason() -> None:
    payload = build_recommendation_payload(
        score=score_row(total_score=82),
        signal=signal_row(direction="bullish"),
        risks=[],
        asset_name="测试股票",
        rank=1,
        run_id="run:test",
        rule_version="test",
        backtest_evidence={"status": "available"},
        decision_context=RecommendationDecisionContext(
            rank=1,
            total=50,
            style_tendency={"theme": 0.7},
            tradability={"tradable": False, "blocking_level": "blocked", "reasons": ["one_word_limit_up"]},
        ),
    )

    assert payload["action"] == "watch"
    assert payload["tradability"]["blocking_level"] == "blocked"
    assert "当前可买入性受限：one_word_limit_up。" in payload["watch_conditions"]["conditions"]


def test_portfolio_plan_does_not_buy_one_word_limit_up_candidate() -> None:
    candidate = PortfolioCandidate(
        asset_id="ashare:600519",
        setup_id="setup:1",
        sector_id="liquor",
        price=Decimal("10"),
        invalidation_price=Decimal("9.5"),
        expected_net_return=0.08,
        downside_risk=0.03,
        confidence=0.85,
        tradable=False,
        tradability_reasons=("one_word_limit_up",),
    )

    plan = PortfolioConstructionEngine().allocate(
        candidates=(candidate,),
        positions=(),
        budget=PortfolioRiskBudget(
            equity=Decimal("100000"),
            total_exposure=1.0,
            per_position_risk=0.01,
        ),
    )

    assert plan.orders == ()
    assert plan.blocked_candidates[0].reason_codes == ("one_word_limit_up",)


def score_row(*, total_score: int) -> SimpleNamespace:
    return SimpleNamespace(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        horizon="swing",
        total_score=total_score,
        confidence=0.86,
        score_id="score:1",
        factor_frame_id="factor:1",
        missing_penalty=0,
        rank=1,
        payload={},
    )


def signal_row(*, direction: str) -> SimpleNamespace:
    return SimpleNamespace(
        signal_id="signal:1",
        direction=direction,
        score=80,
        status="available",
    )
