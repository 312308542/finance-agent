from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from finance_agent.recommendations.service import (
    RecommendationDecisionContext,
    RecommendationService,
    decide_action,
)


def test_decide_action_uses_percentile_and_aggressive_profile_for_buy_candidate() -> None:
    action = decide_action(
        score=score(total_score=58, confidence=0.42),
        signal=signal("bullish"),
        risks=[],
        decision_context=RecommendationDecisionContext(
            rank=16,
            total=100,
            style_tendency={"value": 0.2, "theme": 0.8},
        ),
    )

    assert action == "buy_candidate"


def test_decide_action_uses_stricter_threshold_for_value_profile() -> None:
    action = decide_action(
        score=score(total_score=58, confidence=0.42),
        signal=signal("bullish"),
        risks=[],
        decision_context=RecommendationDecisionContext(
            rank=16,
            total=100,
            style_tendency={"value": 0.9, "theme": 0.1},
        ),
    )

    assert action == "watch"


def test_decide_action_absolute_floor_blocks_top_rank_buy() -> None:
    action = decide_action(
        score=score(total_score=44, confidence=0.9),
        signal=signal("bullish"),
        risks=[],
        decision_context=RecommendationDecisionContext(
            rank=1,
            total=100,
            style_tendency={"value": 0.1, "theme": 0.9},
        ),
    )

    assert action == "watch"


def test_decide_action_keeps_legacy_behavior_without_context() -> None:
    action = decide_action(
        score=score(total_score=76, confidence=0.7),
        signal=signal("bullish"),
        risks=[],
    )

    assert action == "buy_candidate"


def test_rank_from_screening_passes_percentile_context_to_recommendations() -> None:
    recommendations = _RecommendationStore()
    service = RecommendationService.__new__(RecommendationService)
    service.assets = _AssetStore()
    service.screenings = _ScreeningStore()
    service.scores = _ScoreStore()
    service.signals = _SignalStore()
    service.risks = _RiskStore()
    service.recommendations = recommendations

    service.rank_from_screening(
        screening_id="screen:percentile",
        strategy="balanced_swing_v1",
        horizon="swing",
        limit=10,
        profile_style_tendency={"theme": 0.8, "value": 0.2},
    )

    first_payload = recommendations.asset_payloads[0]["payload"]
    fourth_payload = recommendations.asset_payloads[3]["payload"]
    assert first_payload["action"] == "buy_candidate"
    assert first_payload["decision_context"]["percentile"] == 0.1
    assert first_payload["decision_context"]["buy_percentile_threshold"] == 0.2
    assert fourth_payload["action"] == "watch"


def score(*, total_score: float, confidence: float) -> SimpleNamespace:
    return SimpleNamespace(
        total_score=Decimal(str(total_score)),
        confidence=Decimal(str(confidence)),
    )


def signal(direction: str) -> SimpleNamespace:
    return SimpleNamespace(direction=direction)


class _ScreeningStore:
    def get_screening_result(self, screening_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            screening_id=screening_id,
            universe_id="universe:ashare:profile",
            market="ashare",
            passed_count=10,
        )


class _ScoreStore:
    def list_scores_for_screening(self, screening_id: str) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                score_id=f"score:ashare:{index:06d}",
                asset_id=f"ashare:{index:06d}",
                symbol=f"{index:06d}",
                market="ashare",
                horizon="swing",
                total_score=Decimal(str(59 - index * 0.1)),
                confidence=Decimal("0.42"),
                missing_penalty=Decimal("0"),
                rank=index,
                factor_frame_id=f"factor:ashare:{index:06d}",
                payload={"missing_groups": []},
            )
            for index in range(1, 11)
        ]


class _SignalStore:
    def get_latest_signal(self, *, asset_id: str, horizon: str) -> SimpleNamespace:
        return SimpleNamespace(
            signal_id=f"signal:{asset_id}",
            direction="bullish",
            score=Decimal("66"),
            status="available",
        )


class _RiskStore:
    def list_recent_risks(self, *, asset_id: str, limit: int) -> list[SimpleNamespace]:
        return []


class _AssetStore:
    def get_asset_or_none(self, asset_id: str) -> SimpleNamespace:
        return SimpleNamespace(name=f"测试标的{asset_id[-6:]}")


class _RecommendationStore:
    def __init__(self) -> None:
        self.asset_payloads: list[dict[str, Any]] = []

    def upsert_run_universe(self, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(**kwargs)

    def upsert_asset_recommendation(self, **kwargs: Any) -> SimpleNamespace:
        self.asset_payloads.append(kwargs)
        return SimpleNamespace(recommendation_id=kwargs["recommendation_id"])

    def upsert_run(self, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(**kwargs)
