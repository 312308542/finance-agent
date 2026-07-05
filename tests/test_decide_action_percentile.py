from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from finance_agent.recommendations.service import (
    MemoryRankingAdjustment,
    RecommendationDecisionContext,
    RecommendationService,
    build_recommendation_payload,
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


def test_rank_from_screening_applies_memory_ranking_adjustments_without_mutating_score() -> None:
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
        memory_ranking_adjustments={
            "ashare:000001": MemoryRankingAdjustment(
                asset_id="ashare:000001",
                adjustment=-8,
                reasons=("历史复盘：追高亏损，下调排序。",),
            ),
            "ashare:000010": MemoryRankingAdjustment(
                asset_id="ashare:000010",
                adjustment=5,
                reasons=("用户偏好同类低波动资产，上调排序。",),
            ),
        },
    )

    first_payload = recommendations.asset_payloads[0]["payload"]
    last_payload = recommendations.asset_payloads[-1]["payload"]
    assert first_payload["asset_id"] == "ashare:000010"
    assert first_payload["rank"] == 1
    assert first_payload["total_score"] == 58.0
    assert first_payload["memory_ranking_adjustment"]["adjustment"] == 5
    assert last_payload["asset_id"] == "ashare:000001"
    assert last_payload["total_score"] == 58.9


def test_rank_from_screening_attaches_compact_structure_evidence_without_changing_action() -> None:
    recommendations = _RecommendationStore()
    service = RecommendationService.__new__(RecommendationService)
    service.assets = _AssetStore()
    service.screenings = _ScreeningStore()
    service.scores = _ScoreStore()
    service.signals = _SignalStore()
    service.risks = _RiskStore()
    service.indicators = _IndicatorStore.with_structure()
    service.recommendations = recommendations

    service.rank_from_screening(
        screening_id="screen:percentile",
        strategy="balanced_swing_v1",
        horizon="swing",
        limit=1,
        profile_style_tendency={"theme": 0.8, "value": 0.2},
    )

    saved_record = recommendations.asset_payloads[0]
    payload = saved_record["payload"]
    baseline = build_recommendation_payload(
        score=_ScoreStore().list_scores_for_screening("screen:percentile")[0],
        signal=_SignalStore().get_latest_signal(asset_id="ashare:000001", horizon="swing"),
        risks=[],
        asset_name="测试标的000001",
        rank=1,
        run_id=saved_record["run_id"],
        rule_version=payload["rule_version"],
        decision_context=RecommendationDecisionContext(
            rank=1,
            total=1,
            style_tendency={"theme": 0.8, "value": 0.2},
        ),
    )

    assert payload["action"] == baseline["action"]
    structure = payload["structure"]
    assert structure["library"] == "structural-lite"
    assert len(structure["structure_frames"]) == 4
    assert len(json.dumps(structure, ensure_ascii=False).encode("utf-8")) <= 4096
    smc = next(frame for frame in structure["structure_frames"] if frame["horizon"] == "smc_lite_v2")
    assert set(smc) == {"horizon", "status", "confidence", "evidence_id", "as_of", "items"}
    assert smc["items"] == [
        {"name": "bos_bullish", "direction": "bullish", "break_level": 12.3},
        {"name": "choch_bearish", "direction": "bearish", "break_level": 11.8},
        {"name": "bos_bullish_2", "direction": "bullish", "break_level": 12.7},
    ]
    harmonic = next(frame for frame in structure["structure_frames"] if frame["horizon"] == "harmonic_lite_v2")
    assert harmonic["items"] == [
        {"pattern": "Bat", "direction": "bullish", "bars_since_d": 2}
    ]
    assert "payload" not in smc


def test_rank_from_screening_marks_missing_structure_evidence() -> None:
    recommendations = _RecommendationStore()
    service = RecommendationService.__new__(RecommendationService)
    service.assets = _AssetStore()
    service.screenings = _ScreeningStore()
    service.scores = _ScoreStore()
    service.signals = _SignalStore()
    service.risks = _RiskStore()
    service.indicators = _IndicatorStore.empty()
    service.recommendations = recommendations

    service.rank_from_screening(screening_id="screen:percentile", limit=1)

    payload = recommendations.asset_payloads[0]["payload"]
    assert payload["structure"] == {"status": "no_structure_evidence"}


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


class _IndicatorStore:
    def __init__(self, frames: dict[str, SimpleNamespace]) -> None:
        self.frames = frames

    @classmethod
    def empty(cls) -> "_IndicatorStore":
        return cls({})

    @classmethod
    def with_structure(cls) -> "_IndicatorStore":
        as_of = datetime(2026, 7, 5, tzinfo=UTC)
        return cls(
            {
                "smc_lite_v2": SimpleNamespace(
                    horizon="smc_lite_v2",
                    status="available",
                    confidence=Decimal("0.68"),
                    as_of=as_of,
                    payload={
                        "schema_version": "smc_lite_v2",
                        "status": "available",
                        "confidence": 0.68,
                        "evidence_id": "smc_lite:ashare:000001:1d:20260705",
                        "structure_events": [
                            {"name": "bos_bullish", "direction": "bullish", "break_level": 12.3},
                            {"name": "choch_bearish", "direction": "bearish", "break_level": 11.8},
                            {"name": "bos_bullish_2", "direction": "bullish", "break_level": 12.7},
                            {"name": "ignored", "direction": "bullish", "break_level": 13.1},
                        ],
                    },
                ),
                "harmonic_lite_v2": SimpleNamespace(
                    horizon="harmonic_lite_v2",
                    status="available",
                    confidence=Decimal("0.72"),
                    as_of=as_of,
                    payload={
                        "schema_version": "harmonic_lite_v2",
                        "status": "available",
                        "patterns": [
                            {
                                "pattern": "Bat",
                                "direction": "bullish",
                                "bars_since_d": 2,
                                "confidence": 0.72,
                                "points": {"X": {"price": 10}, "D": {"price": 11}},
                            }
                        ],
                        "evidence_id": "harmonic_lite:ashare:000001:1d:20260705",
                    },
                ),
                "elliott_lite_v2": SimpleNamespace(
                    horizon="elliott_lite_v2",
                    status="available",
                    confidence=Decimal("0.61"),
                    as_of=as_of,
                    payload={
                        "schema_version": "elliott_lite_v2",
                        "status": "available",
                        "candidates": [
                            {"pattern": "abc_correction", "signal_hint": "wait_confirmation", "confidence": 0.61}
                        ],
                        "evidence_id": "elliott_lite:ashare:000001:1d:20260705",
                    },
                ),
                "structural_swings_v2": SimpleNamespace(
                    horizon="structural_swings_v2",
                    status="available",
                    confidence=Decimal("0.58"),
                    as_of=as_of,
                    payload={
                        "schema_version": "structural_swings_v2",
                        "status": "available",
                        "segments": [
                            {"direction": "up"},
                            {"direction": "down"},
                            {"direction": "up"},
                        ],
                        "evidence_id": "structural_swings:ashare:000001:1d:20260705",
                    },
                ),
            }
        )

    def get_latest_indicator_frame(
        self,
        *,
        asset_id: str,
        timeframe: str,
        horizon: str,
        library: str | None = None,
    ) -> SimpleNamespace | None:
        assert asset_id == "ashare:000001"
        assert timeframe == "1d"
        assert library == "structural-lite"
        return self.frames.get(horizon)
