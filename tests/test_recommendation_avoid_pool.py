from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from finance_agent.pipelines.recommendation import UniverseRecommendationPipeline
from finance_agent.recommendations.service import RecommendationService


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
    status: str = "partial"
    total_available_groups: int = 3


class _Universes:
    def __init__(
        self,
        *,
        candidate_members: list[_Member],
        avoid_members: list[_Member] | None = None,
        candidate_market: str = "ashare",
        avoid_market: str = "ashare",
    ) -> None:
        self.candidate_members = candidate_members
        self.avoid_members = avoid_members or []
        self.candidate_market = candidate_market
        self.avoid_market = avoid_market
        self.list_calls: list[tuple[str, bool]] = []

    def get_universe(self, universe_id: str) -> _Universe:
        if universe_id == "universe:avoid":
            return _Universe(universe_id=universe_id, market=self.avoid_market)
        return _Universe(universe_id=universe_id, market=self.candidate_market)

    def list_members(self, universe_id: str, *, included_only: bool = True) -> list[_Member]:
        self.list_calls.append((universe_id, included_only))
        if universe_id == "universe:avoid":
            return [member for member in self.avoid_members if member.included or not included_only]
        return [member for member in self.candidate_members if member.included or not included_only]


class _Indicators:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def compute_for_asset(self, **kwargs: Any) -> _IndicatorResult:
        asset_id = str(kwargs["asset_id"])
        self.calls.append(asset_id)
        return _IndicatorResult(asset_id=asset_id, indicator_frame_id=f"ind:{asset_id}:1d")


class _Factors:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def compute_for_asset(self, **kwargs: Any) -> _FactorResult:
        asset_id = str(kwargs["asset_id"])
        self.calls.append(asset_id)
        return _FactorResult(asset_id=asset_id)


class _Screenings:
    def apply_rules(self, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            status="available",
            screening_id="screen:balanced",
            passed_count=1,
            removed_count=0,
        )


class _Scoring:
    def score_screening(self, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(scored_count=1)


class _Signals:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def compute_for_asset(self, **kwargs: Any) -> SimpleNamespace:
        asset_id = str(kwargs["asset_id"])
        self.calls.append(asset_id)
        return SimpleNamespace(signal_id=f"signal:{asset_id}")


class _Recommendations:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None

    def rank_from_screening(self, **kwargs: Any) -> SimpleNamespace:
        self.kwargs = kwargs
        return SimpleNamespace(
            recommendation_count=1,
            run_id="rec-run:1",
            top_recommendation_id="rec:ashare:000001",
        )


def _build_pipeline(
    *,
    candidate_members: list[_Member],
    avoid_members: list[_Member] | None = None,
    avoid_market: str = "ashare",
) -> tuple[UniverseRecommendationPipeline, _Universes, _Indicators, _Factors, _Signals, _Recommendations]:
    pipeline = UniverseRecommendationPipeline.__new__(UniverseRecommendationPipeline)
    universes = _Universes(
        candidate_members=candidate_members,
        avoid_members=avoid_members,
        avoid_market=avoid_market,
    )
    indicators = _Indicators()
    factors = _Factors()
    signals = _Signals()
    recommendations = _Recommendations()
    pipeline.universes = universes
    pipeline.indicators = indicators
    pipeline.factors = factors
    pipeline.screenings = _Screenings()
    pipeline.scoring = _Scoring()
    pipeline.signals = signals
    pipeline.recommendations = recommendations
    pipeline.screening_repository = None
    return pipeline, universes, indicators, factors, signals, recommendations


def test_recommendation_pipeline_excludes_avoid_pool_before_recommendation() -> None:
    """推荐入口应在指标/因子计算前剔除回避池成员。"""

    pipeline, universes, indicators, factors, signals, recommendations = _build_pipeline(
        candidate_members=[
            _Member(asset_id="ashare:000001", symbol="000001"),
            _Member(asset_id="ashare:600519", symbol="600519"),
        ],
        avoid_members=[
            _Member(
                asset_id="ashare:600519",
                symbol="600519",
                included=False,
                removed_reason="st_stock",
                payload={"reason": "ST 或高风险状态"},
            )
        ],
    )

    result = pipeline.run_for_universe(
        universe_id="universe:candidate",
        avoid_universe_id="universe:avoid",
        horizon="swing",
        timeframe="1d",
        min_indicator_coverage_ratio=0.5,
        min_factor_coverage_ratio=0.0,
    )

    assert result.status == "available"
    assert result.member_count == 1
    assert indicators.calls == ["ashare:000001"]
    assert factors.calls == ["ashare:000001"]
    assert signals.calls == ["ashare:000001"]
    assert ("universe:avoid", False) in universes.list_calls
    assert recommendations.kwargs is not None
    assert recommendations.kwargs["audit_payload"]["avoid_pool_excluded"] == {
        "count": 1,
        "assets": [
            {
                "asset_id": "ashare:600519",
                "symbol": "600519",
                "reason": "ST 或高风险状态",
                "source_universe_id": "universe:avoid",
            }
        ],
    }


def test_recommendation_pipeline_keeps_auditable_removed_reason_when_payload_missing() -> None:
    """回避池成员 payload 没有原因时，应回退到 removed_reason 作为审计说明。"""

    pipeline, _, _, _, _, recommendations = _build_pipeline(
        candidate_members=[
            _Member(asset_id="ashare:000001", symbol="000001"),
            _Member(asset_id="ashare:600519", symbol="600519"),
        ],
        avoid_members=[
            _Member(
                asset_id="ashare:600519",
                symbol="600519",
                included=False,
                removed_reason="delisting_risk",
            )
        ],
    )

    pipeline.run_for_universe(
        universe_id="universe:candidate",
        avoid_universe_id="universe:avoid",
        horizon="swing",
        timeframe="1d",
        min_indicator_coverage_ratio=0.5,
        min_factor_coverage_ratio=0.0,
    )

    assert recommendations.kwargs is not None
    excluded = recommendations.kwargs["audit_payload"]["avoid_pool_excluded"]["assets"]
    assert excluded == [
        {
            "asset_id": "ashare:600519",
            "symbol": "600519",
            "reason": "delisting_risk",
            "source_universe_id": "universe:avoid",
        }
    ]


def test_recommendation_pipeline_rejects_cross_market_avoid_pool() -> None:
    """候选池和回避池市场不一致时，应直接拒绝运行。"""

    pipeline, _, indicators, factors, _, _ = _build_pipeline(
        candidate_members=[_Member(asset_id="ashare:000001", symbol="000001")],
        avoid_members=[
            _Member(
                asset_id="crypto_spot:BTCUSDT",
                symbol="BTCUSDT",
                market="crypto_spot",
                included=False,
                removed_reason="risk",
            )
        ],
        avoid_market="crypto_spot",
    )

    with pytest.raises(ValueError, match="回避池.*市场.*候选池"):
        pipeline.run_for_universe(
            universe_id="universe:candidate",
            avoid_universe_id="universe:avoid",
            horizon="swing",
            timeframe="1d",
        )

    assert indicators.calls == []
    assert factors.calls == []


class _ScreeningStore:
    def get_screening_result(self, screening_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            screening_id=screening_id,
            universe_id="universe:candidate",
            market="ashare",
            passed_count=1,
        )


class _ScoreStore:
    def list_scores_for_screening(self, screening_id: str) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                score_id="score:ashare:000001",
                asset_id="ashare:000001",
                symbol="000001",
                market="ashare",
                horizon="swing",
                total_score=Decimal("72"),
                confidence=Decimal("0.68"),
                missing_penalty=Decimal("0"),
                rank=1,
                factor_frame_id="factor:ashare:000001",
                payload={},
            )
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
        return SimpleNamespace(name="平安银行")


class _RecommendationStore:
    def __init__(self) -> None:
        self.run_payload: dict[str, Any] | None = None

    def upsert_run_universe(self, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(**kwargs)

    def upsert_asset_recommendation(self, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(recommendation_id=kwargs["recommendation_id"])

    def upsert_run(self, **kwargs: Any) -> SimpleNamespace:
        self.run_payload = kwargs["payload"]
        return SimpleNamespace(**kwargs)


def test_recommendation_service_writes_avoid_pool_audit_to_run_payload() -> None:
    """推荐服务应把流水线审计信息合并写入 recommendation_runs.payload。"""

    service = RecommendationService.__new__(RecommendationService)
    recommendation_store = _RecommendationStore()
    service.assets = _AssetStore()
    service.screenings = _ScreeningStore()
    service.scores = _ScoreStore()
    service.signals = _SignalStore()
    service.risks = _RiskStore()
    service.recommendations = recommendation_store

    service.rank_from_screening(
        screening_id="screen:balanced",
        strategy="balanced_swing_v1",
        horizon="swing",
        limit=20,
        audit_payload={
            "avoid_pool_excluded": {
                "count": 1,
                "assets": [
                    {
                        "asset_id": "ashare:600519",
                        "symbol": "600519",
                        "reason": "ST 或高风险状态",
                        "source_universe_id": "universe:avoid",
                    }
                ],
            }
        },
    )

    assert recommendation_store.run_payload is not None
    assert recommendation_store.run_payload["avoid_pool_excluded"] == {
        "count": 1,
        "assets": [
            {
                "asset_id": "ashare:600519",
                "symbol": "600519",
                "reason": "ST 或高风险状态",
                "source_universe_id": "universe:avoid",
            }
        ],
    }
