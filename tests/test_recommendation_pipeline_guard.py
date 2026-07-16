from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from finance_agent.pipelines.recommendation import UniverseRecommendationPipeline


@dataclass(frozen=True)
class _Universe:
    universe_id: str = "universe:test:ashare"
    market: str = "ashare"
    status: str = "available"


@dataclass(frozen=True)
class _Member:
    asset_id: str
    symbol: str
    market: str = "ashare"
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _IndicatorResult:
    asset_id: str
    indicator_frame_id: str | None = None
    status: str = "unavailable"


@dataclass(frozen=True)
class _FactorResult:
    asset_id: str
    status: str = "partial"
    total_available_groups: int = 3


class _Universes:
    def __init__(self, members: list[_Member], *, market: str = "ashare") -> None:
        self.members = members
        self.market = market

    def get_universe(self, universe_id: str) -> _Universe:
        return _Universe(universe_id=universe_id, market=self.market)

    def list_members(self, universe_id: str) -> list[_Member]:
        return self.members


class _Indicators:
    def __init__(self, available_asset_ids: set[str] | None = None) -> None:
        self.available_asset_ids = available_asset_ids or set()

    def compute_for_asset(self, **kwargs: Any) -> _IndicatorResult:
        asset_id = str(kwargs["asset_id"])
        frame_id = f"ind:{asset_id}:1d" if asset_id in self.available_asset_ids else None
        return _IndicatorResult(asset_id=asset_id, indicator_frame_id=frame_id)


class _Factors:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.kwargs: list[dict[str, Any]] = []

    def compute_for_asset(self, **kwargs: Any) -> _FactorResult:
        asset_id = str(kwargs["asset_id"])
        self.calls.append(asset_id)
        self.kwargs.append(kwargs)
        return _FactorResult(asset_id=asset_id)


class _TechnicalScreeningRepository:
    def get_latest_screening_result(self, *, market: str, strategy: str) -> SimpleNamespace:
        assert market == "ashare"
        assert strategy == "technical_screening_v1"
        return SimpleNamespace(screening_id="screen:technical:ashare:main_board:latest")

    def list_items(self, *, screening_id: str, passed_only: bool = False) -> list[SimpleNamespace]:
        assert screening_id == "screen:technical:ashare:main_board:latest"
        assert passed_only is True
        return [SimpleNamespace(asset_id="ashare:000001", passed=True)]


class _Screenings:
    def apply_rules(self, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            status="available",
            screening_id="screen:balanced",
            passed_count=1,
            removed_count=0,
        )


class _Scoring:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def score_screening(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(scored_count=1)


class _Signals:
    def compute_for_asset(self, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(signal_id=f"signal:{kwargs['asset_id']}")


class _Recommendations:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def rank_from_screening(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(
            recommendation_count=0,
            run_id=None,
            top_recommendation_id=None,
        )


class _ThemeContexts:
    def __init__(self, context_by_asset_id: dict[str, dict[str, Any]]) -> None:
        self.context_by_asset_id = context_by_asset_id
        self.calls: list[list[str]] = []

    def build_for_members(self, members: list[_Member]) -> dict[str, dict[str, Any]]:
        self.calls.append([member.asset_id for member in members])
        return self.context_by_asset_id


class _ForbiddenStage:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError("覆盖率不足时不应继续执行后续推荐阶段")


def test_recommendation_pipeline_stops_before_factor_when_indicators_have_low_coverage() -> None:
    """行情指标覆盖率过低时，流水线不应继续写入因子、筛选、评分、信号或推荐。"""

    pipeline = UniverseRecommendationPipeline.__new__(UniverseRecommendationPipeline)
    pipeline.universes = _Universes(
        [
            _Member(asset_id="ashare:000001", symbol="000001"),
            _Member(asset_id="ashare:600519", symbol="600519"),
        ]
    )
    factors = _Factors()
    pipeline.indicators = _Indicators()
    pipeline.factors = factors
    pipeline.screenings = _ForbiddenStage()
    pipeline.scoring = _ForbiddenStage()
    pipeline.signals = _ForbiddenStage()
    pipeline.recommendations = _ForbiddenStage()

    result = pipeline.run_for_universe(
        universe_id="universe:test:ashare",
        horizon="swing",
        timeframe="1d",
        min_indicator_coverage_ratio=0.5,
    )

    assert result.status == "unavailable"
    assert result.indicator_count == 0
    assert result.factor_count == 0
    assert result.recommendation_count == 0
    assert result.recommendation_run_id is None
    assert factors.calls == []
    assert result.errors == (
        {
            "stage": "indicator_coverage",
            "market": "ashare",
            "successful_indicators": 0,
            "member_count": 2,
            "coverage_ratio": 0.0,
            "required_coverage_ratio": 0.5,
            "message": "行情指标覆盖率不足，跳过本次因子、筛选、评分和推荐，等待 K 线补齐。",
        },
    )


def test_recommendation_pipeline_only_computes_factors_for_indicator_backed_members() -> None:
    """覆盖率达标后，只对已有指标快照的成员计算因子，避免给缺 K 线成员写 partial 噪声。"""

    pipeline = UniverseRecommendationPipeline.__new__(UniverseRecommendationPipeline)
    pipeline.universes = _Universes(
        [
            _Member(asset_id="ashare:000001", symbol="000001"),
            _Member(asset_id="ashare:600519", symbol="600519"),
        ]
    )
    factors = _Factors()
    pipeline.indicators = _Indicators(available_asset_ids={"ashare:000001"})
    pipeline.factors = factors
    pipeline.screenings = _ForbiddenStage()
    pipeline.scoring = _ForbiddenStage()
    pipeline.signals = _ForbiddenStage()
    pipeline.recommendations = _ForbiddenStage()

    result = pipeline.run_for_universe(
        universe_id="universe:test:ashare",
        horizon="swing",
        timeframe="1d",
        min_indicator_coverage_ratio=0.8,
    )

    assert result.status == "unavailable"
    assert result.indicator_count == 1
    assert factors.calls == []

    pipeline.run_for_universe(
        universe_id="universe:test:ashare",
        horizon="swing",
        timeframe="1d",
        min_indicator_coverage_ratio=0.5,
        min_factor_coverage_ratio=2.0,
    )

    assert factors.calls == ["ashare:000001"]


def test_recommendation_pipeline_prefers_latest_technical_screening_pool() -> None:
    """推荐流水线配置技术初筛来源后，应优先处理技术初筛通过项。"""

    pipeline = UniverseRecommendationPipeline.__new__(UniverseRecommendationPipeline)
    pipeline.universes = _Universes(
        [
            _Member(asset_id="ashare:000001", symbol="000001"),
            _Member(asset_id="ashare:600519", symbol="600519"),
        ]
    )
    factors = _Factors()
    pipeline.indicators = _Indicators(available_asset_ids={"ashare:000001", "ashare:600519"})
    pipeline.factors = factors
    pipeline.screening_repository = _TechnicalScreeningRepository()
    pipeline.screenings = _Screenings()
    pipeline.scoring = _Scoring()
    pipeline.signals = _Signals()
    pipeline.recommendations = _Recommendations()

    result = pipeline.run_for_universe(
        universe_id="universe:test:ashare",
        horizon="swing",
        timeframe="1d",
        candidate_source="technical_screening_pool",
        min_indicator_coverage_ratio=0.5,
        min_factor_coverage_ratio=0.0,
    )

    assert result.member_count == 1
    assert result.indicator_count == 1
    assert factors.calls == ["ashare:000001"]


def test_recommendation_pipeline_passes_strategy_id_to_scoring_service() -> None:
    """候选池流水线应把调度层传入的评分策略 ID 透传到评分服务。"""

    pipeline = UniverseRecommendationPipeline.__new__(UniverseRecommendationPipeline)
    pipeline.universes = _Universes([_Member(asset_id="ashare:000001", symbol="000001")])
    pipeline.indicators = _Indicators(available_asset_ids={"ashare:000001"})
    pipeline.factors = _Factors()
    pipeline.screening_repository = _TechnicalScreeningRepository()
    pipeline.screenings = _Screenings()
    scoring = _Scoring()
    pipeline.scoring = scoring
    pipeline.signals = _Signals()
    pipeline.recommendations = _Recommendations()

    result = pipeline.run_for_universe(
        universe_id="universe:test:ashare",
        horizon="swing",
        timeframe="1d",
        strategy_id="strategy:ashare:short_swing",
        min_indicator_coverage_ratio=0.5,
        min_factor_coverage_ratio=0.0,
    )

    assert result.screening_id == "screen:balanced"
    assert scoring.calls == [
        {
            "screening_id": "screen:balanced",
            "horizon": "swing",
            "strategy_id": "strategy:ashare:short_swing",
        }
    ]
    assert pipeline.recommendations.calls[0]["score_strategy_id"] == (
        "strategy:ashare:short_swing"
    )


def test_recommendation_pipeline_passes_market_regime_to_recommendation_service() -> None:
    """流水线应把大盘环境上下文传入推荐裁决，但不改变评分结果。"""

    pipeline = UniverseRecommendationPipeline.__new__(UniverseRecommendationPipeline)
    pipeline.universes = _Universes([_Member(asset_id="ashare:000001", symbol="000001")])
    pipeline.indicators = _Indicators(available_asset_ids={"ashare:000001"})
    pipeline.factors = _Factors()
    pipeline.screenings = _Screenings()
    pipeline.scoring = _Scoring()
    pipeline.signals = _Signals()
    recommendations = _Recommendations()
    pipeline.recommendations = recommendations

    market_regime = {"regime": "bear", "strength": "high"}
    pipeline.run_for_universe(
        universe_id="universe:test:ashare",
        horizon="swing",
        timeframe="1d",
        min_indicator_coverage_ratio=0.5,
        min_factor_coverage_ratio=0.0,
        market_regime=market_regime,
    )

    assert recommendations.calls[0]["market_regime"] == market_regime


def test_recommendation_pipeline_passes_member_theme_groups_to_factor_service() -> None:
    """候选池成员携带题材因子上下文时，流水线应透传给因子服务入帧。"""

    pipeline = UniverseRecommendationPipeline.__new__(UniverseRecommendationPipeline)
    factors = _Factors()
    member = _Member(
        asset_id="ashare:600519",
        symbol="600519",
        payload={
            "theme_context": {
                "factor_groups": [
                    {"group": "sector_strength", "status": "available", "score": 92},
                    {"group": "leadership", "status": "available", "score": 88},
                    {"group": "technical", "status": "available", "score": 100},
                ]
            }
        },
    )
    pipeline.factors = factors

    result = pipeline._compute_factor(
        member=member,
        timeframe="1d",
        horizon="swing",
        errors=[],
    )

    assert result == _FactorResult(asset_id="ashare:600519")
    assert factors.kwargs == [
        {
            "asset_id": "ashare:600519",
            "timeframe": "1d",
            "horizon": "swing",
            "fallback_symbol": "600519",
            "fallback_market": "ashare",
            "supplemental_factor_groups": [
                {"group": "sector_strength", "status": "available", "score": 92},
                {"group": "leadership", "status": "available", "score": 88},
            ],
        }
    ]


def test_recommendation_pipeline_merges_generated_theme_context_before_factor_stage() -> None:
    """生产侧题材上下文服务生成的因子组，应在因子计算前进入 supplemental groups。"""

    pipeline = UniverseRecommendationPipeline.__new__(UniverseRecommendationPipeline)
    member = _Member(
        asset_id="ashare:600519",
        symbol="600519",
        payload={
            "theme_context": {
                "factor_groups": [
                    {"group": "sector_strength", "status": "available", "score": 50}
                ]
            }
        },
    )
    pipeline.universes = _Universes([member])
    pipeline.indicators = _Indicators(available_asset_ids={"ashare:600519"})
    factors = _Factors()
    pipeline.factors = factors
    pipeline.screening_repository = None
    pipeline.screenings = _Screenings()
    pipeline.scoring = _Scoring()
    pipeline.signals = _Signals()
    pipeline.recommendations = _Recommendations()
    theme_contexts = _ThemeContexts(
        {
            "ashare:600519": {
                "factor_groups": [
                    {"group": "sector_strength", "status": "available", "score": 92},
                    {"group": "leadership", "status": "available", "score": 88},
                ]
            }
        }
    )
    pipeline.theme_contexts = theme_contexts

    pipeline.run_for_universe(
        universe_id="universe:test:ashare",
        horizon="swing",
        timeframe="1d",
        min_indicator_coverage_ratio=0.5,
        min_factor_coverage_ratio=0.0,
    )

    assert theme_contexts.calls == [["ashare:600519"]]
    assert factors.kwargs[0]["supplemental_factor_groups"] == [
        {"group": "sector_strength", "status": "available", "score": 92},
        {"group": "leadership", "status": "available", "score": 88},
    ]
