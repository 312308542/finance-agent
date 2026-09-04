from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from finance_agent.application.data_production_service import UniverseMergeService
from finance_agent.pipelines.recommendation import UniverseRecommendationPipeline
from finance_agent.recommendations.service import RecommendationService
from finance_agent.scoring.service import ScoringService


@dataclass(frozen=True)
class _Universe:
    universe_id: str
    market: str = "ashare"
    status: str = "available"


@dataclass(frozen=True)
class _Member:
    asset_id: str
    symbol: str
    market: str = "ashare"
    included: bool = True
    removed_reason: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _IndicatorResult:
    asset_id: str
    indicator_frame_id: str | None


@dataclass(frozen=True)
class _FactorResult:
    asset_id: str
    status: str = "available"
    total_available_groups: int = 3


@dataclass(frozen=True)
class _FactorFrame:
    asset_id: str
    symbol: str
    factor_frame_id: str
    market: str = "ashare"
    horizon: str = "swing"
    status: str = "available"
    missing_groups: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    as_of: datetime = datetime(2026, 6, 12, 9, 30, tzinfo=UTC)
    payload: dict[str, Any] = field(
        default_factory=lambda: {
            "factor_groups": [
                {"group": "technical", "score": 92, "status": "available"},
                {"group": "fundamental", "score": 54, "status": "available"},
                {"group": "risk", "score": 88, "status": "available"},
            ],
            "partial_groups": [],
        }
    )


class _Universes:
    def __init__(self, *, members: list[_Member], avoid_members: list[_Member]) -> None:
        self.members = members
        self.avoid_members = avoid_members

    def get_universe(self, universe_id: str) -> _Universe:
        return _Universe(universe_id=universe_id)

    def list_members(self, universe_id: str, *, included_only: bool = True) -> list[_Member]:
        source = self.avoid_members if universe_id == "universe:avoid:ashare:system" else self.members
        return [member for member in source if member.included or not included_only]


class _Indicators:
    def compute_for_asset(self, **kwargs: Any) -> _IndicatorResult:
        asset_id = str(kwargs["asset_id"])
        return _IndicatorResult(asset_id=asset_id, indicator_frame_id=f"indicator:{asset_id}")


class _Factors:
    def __init__(self, factor_frames: dict[str, _FactorFrame]) -> None:
        self.factor_frames = factor_frames

    def compute_for_asset(self, **kwargs: Any) -> _FactorResult:
        asset_id = str(kwargs["asset_id"])
        return _FactorResult(asset_id=asset_id)

    def get_latest_factor_frame(self, *, asset_id: str, horizon: str) -> _FactorFrame | None:
        assert horizon == "swing"
        return self.factor_frames.get(asset_id)


class _Screenings:
    def __init__(self, members: list[_Member]) -> None:
        self.members = members
        self.last_screening_id: str | None = None

    def apply_rules(self, **kwargs: Any) -> SimpleNamespace:
        self.last_screening_id = "screen:merged:ashare:short_swing"
        return SimpleNamespace(
            screening_id=self.last_screening_id,
            universe_id=kwargs["universe_id"],
            market="ashare",
            status="available",
            passed_count=len(self.members),
            removed_count=0,
        )

    def get_screening_result(self, screening_id: str) -> SimpleNamespace:
        assert screening_id == self.last_screening_id
        return SimpleNamespace(
            screening_id=screening_id,
            universe_id="universe:merged:ashare:recommendation",
            market="ashare",
            passed_count=len(self.members),
        )

    def list_items(self, *, screening_id: str, passed_only: bool = False) -> list[SimpleNamespace]:
        assert screening_id == self.last_screening_id
        assert passed_only is True
        return [
            SimpleNamespace(
                universe_id="universe:merged:ashare:recommendation",
                asset_id=member.asset_id,
                symbol=member.symbol,
                market=member.market,
            )
            for member in self.members
        ]


class _Scores:
    def __init__(self) -> None:
        self.records: list[SimpleNamespace] = []

    def upsert_asset_score(self, **kwargs: Any) -> SimpleNamespace:
        record = SimpleNamespace(**kwargs)
        self.records.append(record)
        return SimpleNamespace(score_id=kwargs["score_id"])

    def list_scores_for_screening(
        self,
        screening_id: str,
        *,
        strategy_id: str | None = None,
    ) -> list[SimpleNamespace]:
        records = [
            item
            for item in self.records
            if strategy_id is None or item.strategy_id == strategy_id
        ]
        return sorted(records, key=lambda item: item.rank)


class _Strategies:
    def get_active_strategy(self, strategy_id: str) -> SimpleNamespace:
        assert strategy_id == "strategy:ashare:short_swing"
        return SimpleNamespace(
            strategy_id=strategy_id,
            market="ashare",
            name="A 股短线波段",
            description="提高技术和资金流权重。",
            group_weights={"technical": 0.8, "fundamental": 0.2},
            missing_penalty={"per_missing_group": 2, "per_partial_group": 1},
            status="active",
        )


class _Signals:
    def compute_for_asset(self, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(signal_id=f"signal:{kwargs['asset_id']}")

    def get_latest_signal(self, *, asset_id: str, horizon: str) -> SimpleNamespace:
        return SimpleNamespace(
            signal_id=f"signal:{asset_id}",
            direction="bullish",
            score=Decimal("72"),
            status="available",
        )


class _Risks:
    def list_recent_risks(self, *, asset_id: str, limit: int) -> list[SimpleNamespace]:
        return []


class _Assets:
    def get_asset_or_none(self, asset_id: str) -> SimpleNamespace:
        return SimpleNamespace(name={"ashare:000001": "平安银行"}.get(asset_id, asset_id))


class _Recommendations:
    def __init__(self) -> None:
        self.run_universe_payloads: list[dict[str, Any]] = []
        self.recommendation_payloads: list[dict[str, Any]] = []
        self.run_payload: dict[str, Any] | None = None

    def upsert_run_universe(self, **kwargs: Any) -> SimpleNamespace:
        self.run_universe_payloads.append(kwargs)
        return SimpleNamespace(**kwargs)

    def upsert_asset_recommendation(self, **kwargs: Any) -> SimpleNamespace:
        self.recommendation_payloads.append(kwargs)
        return SimpleNamespace(recommendation_id=kwargs["recommendation_id"])

    def upsert_run(self, **kwargs: Any) -> SimpleNamespace:
        self.run_payload = kwargs["payload"]
        return SimpleNamespace(**kwargs)


def test_merged_avoid_strategy_recommendation_pipeline_smoke() -> None:
    """合并池、回避池、策略评分和推荐落库应能离线串成可审计链路。"""

    as_of = datetime(2026, 6, 12, 9, 30, tzinfo=UTC)
    merged_plans = UniverseMergeService().merge_members(
        target_universe_id="universe:merged:ashare:recommendation",
        market="ashare",
        sources=[
            {
                "universe_id": "universe:technical:ashare:main_board",
                "source": "technical_screening_pool",
                "market": "ashare",
                "weight": 2.0,
                "members": [
                    {"asset_id": "ashare:000001", "symbol": "000001", "market": "ashare", "rank_hint": 1},
                    {"asset_id": "ashare:600519", "symbol": "600519", "market": "ashare", "rank_hint": 2},
                ],
            },
            {
                "universe_id": "universe:manual:ashare:focus",
                "source": "manual_focus_pool",
                "market": "ashare",
                "weight": 1.0,
                "members": [
                    {"asset_id": "ashare:000001", "symbol": "000001", "market": "ashare", "rank_hint": 3},
                ],
            },
        ],
        as_of=as_of,
    )
    merged_members = [
        _Member(
            asset_id=plan.asset_id,
            symbol=plan.symbol,
            market=plan.market,
            payload=plan.payload,
        )
        for plan in merged_plans
    ]
    avoid_members = [
        _Member(
            asset_id="ashare:600519",
            symbol="600519",
            included=False,
            removed_reason="ST 或高风险状态",
            payload={"avoid_reasons": ["ST 或高风险状态"]},
        )
    ]
    active_members = [member for member in merged_members if member.asset_id != "ashare:600519"]
    scores = _Scores()
    screenings = _Screenings(active_members)
    recommendations = _Recommendations()
    factor_frames = {
        "ashare:000001": _FactorFrame(
            asset_id="ashare:000001",
            symbol="000001",
            factor_frame_id="factor:ashare:000001:swing",
        )
    }

    pipeline = UniverseRecommendationPipeline.__new__(UniverseRecommendationPipeline)
    pipeline.universes = _Universes(members=merged_members, avoid_members=avoid_members)
    pipeline.indicators = _Indicators()
    pipeline.factors = _Factors(factor_frames)
    pipeline.screenings = screenings
    pipeline.screening_repository = None
    scoring = ScoringService.__new__(ScoringService)
    scoring.screenings = screenings
    scoring.factors = pipeline.factors
    scoring.scores = scores
    scoring.strategies = _Strategies()
    pipeline.scoring = scoring
    pipeline.signals = _Signals()
    recommendation_service = RecommendationService.__new__(RecommendationService)
    recommendation_service.assets = _Assets()
    recommendation_service.screenings = screenings
    recommendation_service.scores = scores
    recommendation_service.signals = pipeline.signals
    recommendation_service.risks = _Risks()
    recommendation_service.recommendations = recommendations
    pipeline.recommendations = recommendation_service

    result = pipeline.run_for_universe(
        universe_id="universe:merged:ashare:recommendation",
        avoid_universe_id="universe:avoid:ashare:system",
        strategy="balanced_swing_v1",
        horizon="swing",
        timeframe="1d",
        min_indicator_coverage_ratio=0.5,
        min_factor_coverage_ratio=0.0,
        strategy_id="strategy:ashare:short_swing",
        limit=5,
    )

    assert result.status == "partial"
    assert result.member_count == 1
    assert result.recommendation_count == 0
    assert result.buy_ready_count == 0
    assert result.decision_snapshot_id is None
    assert recommendations.run_payload is None
    assert recommendations.recommendation_payloads == []

    trial_result = recommendation_service.rank_from_screening(
        screening_id="screen:merged:ashare:short_swing",
        strategy="balanced_swing_v1",
        horizon="swing",
        score_strategy_id="strategy:ashare:short_swing",
        trial_state="trial",
        validation_evidence_id="bt:wf:short-passed",
        limit=5,
    )

    assert trial_result.status == "available"
    assert recommendations.run_payload is not None
    assert recommendations.run_payload["trial"] is True
    assert recommendations.run_payload["validation_state"] == "trial"
    assert recommendations.run_payload["validation_evidence_id"] == "bt:wf:short-passed"
    assert recommendations.run_payload["source"]["trial"] is True
    trial_payload = recommendations.recommendation_payloads[-1]["payload"]
    assert trial_payload["trial"] is True
    assert trial_payload["validation_state"] == "trial"
    assert trial_payload["validation_evidence_id"] == "bt:wf:short-passed"

    validated_result = recommendation_service.rank_from_screening(
        screening_id="screen:merged:ashare:short_swing",
        strategy="balanced_swing_v1",
        horizon="swing",
        score_strategy_id="strategy:ashare:short_swing",
        trial_state="validated",
        validation_evidence_id="bt:wf:short-validated",
        limit=5,
    )
    assert validated_result.status == "available"
    assert len(
        {
            result.recommendation_run_id,
            trial_result.run_id,
            validated_result.run_id,
        }
    ) == 3
    assert recommendations.run_payload is not None
    assert recommendations.run_payload["trial"] is False
    assert recommendations.run_payload["validation_state"] == "validated"

    with pytest.raises(ValueError, match="validation_evidence_id"):
        recommendation_service.rank_from_screening(
            screening_id="screen:merged:ashare:short_swing",
            score_strategy_id="strategy:ashare:short_swing",
            trial_state="trial",
            validation_evidence_id=None,
        )
