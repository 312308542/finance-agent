from __future__ import annotations

from dataclasses import dataclass
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

    def compute_for_asset(self, **kwargs: Any) -> _FactorResult:
        asset_id = str(kwargs["asset_id"])
        self.calls.append(asset_id)
        return _FactorResult(asset_id=asset_id)


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
