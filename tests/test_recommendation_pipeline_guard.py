from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from finance_agent.pipelines.recommendation import (
    UniverseRecommendationPipeline,
    recommendation_strategy_gate,
)
from finance_agent.research.strategy_observation_service import BASELINE_STRATEGY_ID

SHORT = "strategy:ashare:short_swing"
THEME = "strategy:ashare:theme_momentum"
MIXED = "strategy:ashare:short_theme_mixed_v1"
ADAPTIVE = "strategy:ashare:adaptive_v1"


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
        self.calls: list[str] = []

    def compute_for_asset(self, **kwargs: Any) -> _IndicatorResult:
        asset_id = str(kwargs["asset_id"])
        self.calls.append(asset_id)
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
    def __init__(
        self,
        *,
        recommendation_count: int = 0,
        buy_ready_count: int = 0,
        decision_snapshot_id: str | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.recommendation_count = recommendation_count
        self.buy_ready_count = buy_ready_count
        self.decision_snapshot_id = decision_snapshot_id

    def rank_from_screening(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(
            recommendation_count=self.recommendation_count,
            buy_ready_count=self.buy_ready_count,
            active_count=0,
            exit_pending_count=0,
            decision_snapshot_id=self.decision_snapshot_id,
            run_id=f"run:{kwargs.get('score_strategy_id')}",
            top_recommendation_id=(
                f"recommendation:{kwargs.get('score_strategy_id')}"
                if self.recommendation_count
                else None
            ),
        )


def test_pipeline_publishes_zero_buy_snapshot_when_no_asset_passes_all_gates() -> None:
    """研究对象仍可发布，但不能为了凑数生成买入。"""

    pipeline = UniverseRecommendationPipeline.__new__(UniverseRecommendationPipeline)
    pipeline.universes = _Universes([_Member(asset_id="ashare:000001", symbol="000001")])
    pipeline.indicators = _Indicators(available_asset_ids={"ashare:000001"})
    pipeline.factors = _Factors()
    pipeline.screening_repository = None
    pipeline.screenings = _Screenings()
    pipeline.scoring = _Scoring()
    pipeline.signals = _Signals()
    pipeline.recommendations = _Recommendations(
        recommendation_count=2,
        buy_ready_count=0,
        decision_snapshot_id="decision:ashare:2026-09-08:test",
    )

    result = pipeline.run_for_universe(
        universe_id="universe:test:ashare",
        strategy_id=SHORT,
        min_indicator_coverage_ratio=0.5,
        min_factor_coverage_ratio=0.0,
    )

    assert result.status == "partial"
    assert result.recommendation_count == 0
    assert result.buy_ready_count == 0
    assert result.decision_snapshot_id is None


class _TrialStates:
    def __init__(self, states: dict[str, Any]) -> None:
        self.states = states

    def get_trial_state(self, strategy_id: str) -> Any | None:
        return self.states.get(strategy_id)


class _Observations:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def capture(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"status": "captured", "observation_id": "obs:test"}


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
    assert pipeline.recommendations.calls == []


def test_pipeline_scores_three_strategies_from_one_screening() -> None:
    """三策略必须复用一次指标、因子、初筛和信号计算。"""

    members = [
        _Member(asset_id="ashare:000001", symbol="000001"),
        _Member(asset_id="ashare:600519", symbol="600519"),
    ]
    pipeline = UniverseRecommendationPipeline.__new__(UniverseRecommendationPipeline)
    pipeline.universes = _Universes(members)
    indicators = _Indicators(available_asset_ids={item.asset_id for item in members})
    factors = _Factors()
    scoring = _Scoring()
    recommendations = _Recommendations(recommendation_count=1)
    observations = _Observations()
    pipeline.indicators = indicators
    pipeline.factors = factors
    pipeline.screening_repository = None
    pipeline.screenings = _Screenings()
    pipeline.scoring = scoring
    pipeline.signals = _Signals()
    pipeline.recommendations = recommendations
    pipeline.observations = observations
    pipeline.trial_states = _TrialStates(
        {
            THEME: SimpleNamespace(
                state="research",
                historical_evidence_id=None,
            ),
            MIXED: SimpleNamespace(
                state="trial",
                historical_evidence_id="bt:wf:mixed-passed",
            ),
        }
    )

    result = pipeline.run_for_universe(
        universe_id="universe:test:ashare",
        horizon="swing",
        timeframe="1d",
        strategy_ids=[SHORT, THEME, MIXED],
        observation_enabled=True,
        min_indicator_coverage_ratio=0.5,
        min_factor_coverage_ratio=0.0,
    )

    assert indicators.calls == [item.asset_id for item in members]
    assert factors.calls == [item.asset_id for item in members]
    assert [call["strategy_id"] for call in scoring.calls] == [SHORT, THEME, MIXED]
    assert observations.calls[0]["strategy_ids"] == (SHORT, THEME, MIXED)
    assert [call["score_strategy_id"] for call in recommendations.calls] == [MIXED]
    assert recommendations.calls[0]["trial_state"] == "trial"
    assert recommendations.calls[0]["validation_evidence_id"] == "bt:wf:mixed-passed"
    strategy_results = {item["strategy_id"]: item for item in result.strategy_results}
    assert strategy_results[THEME]["recommendation_status"] == "blocked"
    assert strategy_results[THEME]["blocked_reason"] == "historical_gate_not_passed"
    assert strategy_results[MIXED]["recommendation_status"] == "available"


def test_pipeline_blocks_disabled_and_allows_validated_strategy() -> None:
    """disabled 停止新推荐，validated 带验证证据生成独立推荐。"""

    pipeline = UniverseRecommendationPipeline.__new__(UniverseRecommendationPipeline)
    pipeline.universes = _Universes([_Member(asset_id="ashare:000001", symbol="000001")])
    pipeline.indicators = _Indicators(available_asset_ids={"ashare:000001"})
    pipeline.factors = _Factors()
    pipeline.screening_repository = None
    pipeline.screenings = _Screenings()
    pipeline.scoring = _Scoring()
    pipeline.signals = _Signals()
    recommendations = _Recommendations(recommendation_count=1)
    pipeline.recommendations = recommendations
    pipeline.observations = _Observations()
    pipeline.trial_states = _TrialStates(
        {
            THEME: SimpleNamespace(
                state="disabled",
                historical_evidence_id="bt:wf:theme-disabled",
            ),
            MIXED: SimpleNamespace(
                state="validated",
                historical_evidence_id="bt:wf:mixed-validated",
            ),
        }
    )

    result = pipeline.run_for_universe(
        universe_id="universe:test:ashare",
        horizon="swing",
        timeframe="1d",
        strategy_ids=[THEME, MIXED],
        min_indicator_coverage_ratio=0.5,
        min_factor_coverage_ratio=0.0,
    )

    assert len(recommendations.calls) == 1
    assert recommendations.calls[0]["score_strategy_id"] == MIXED
    assert recommendations.calls[0]["trial_state"] == "validated"
    assert recommendations.calls[0]["validation_evidence_id"] == "bt:wf:mixed-validated"
    statuses = {item["strategy_id"]: item for item in result.strategy_results}
    assert statuses[THEME]["blocked_reason"] == "strategy_disabled"
    assert statuses[MIXED]["recommendation_status"] == "available"


def test_trial_gate_requires_validation_evidence_and_marks_validated_non_trial() -> None:
    """状态本身不够，必须有历史证据；validated 不再标记为试运行。"""

    missing_evidence = recommendation_strategy_gate(
        market="ashare",
        strategy_id=MIXED,
        trial_states=_TrialStates(
            {MIXED: SimpleNamespace(state="trial", historical_evidence_id=None)}
        ),
    )
    validated = recommendation_strategy_gate(
        market="ashare",
        strategy_id=MIXED,
        trial_states=_TrialStates(
            {
                MIXED: SimpleNamespace(
                    state="validated",
                    historical_evidence_id="bt:wf:validated",
                )
            }
        ),
    )

    assert missing_evidence["allowed"] is False
    assert missing_evidence["blocked_reason"] == "validation_evidence_missing"
    assert validated["allowed"] is True
    assert validated["trial"] is False


def test_adaptive_strategy_also_requires_validation_gate() -> None:
    """自适应主线没有历史验证证据时只能停留在研究状态。"""

    gate = recommendation_strategy_gate(
        market="ashare",
        strategy_id=ADAPTIVE,
        trial_states=_TrialStates({}),
    )

    assert gate["allowed"] is False
    assert gate["trial"] is False
    assert gate["blocked_reason"] == "historical_gate_not_passed"


def test_baseline_strategy_also_requires_validation_gate() -> None:
    """基准策略不能绕过历史与前向验证门控。"""

    gate = recommendation_strategy_gate(
        market="ashare",
        strategy_id=BASELINE_STRATEGY_ID,
        trial_states=_TrialStates({}),
    )

    assert gate["allowed"] is False
    assert gate["blocked_reason"] == "historical_gate_not_passed"


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


def test_recommendation_pipeline_passes_owner_to_lifecycle_and_portfolio_context() -> None:
    pipeline = UniverseRecommendationPipeline.__new__(UniverseRecommendationPipeline)
    pipeline.universes = _Universes([_Member(asset_id="ashare:000001", symbol="000001")])
    pipeline.indicators = _Indicators(available_asset_ids={"ashare:000001"})
    pipeline.factors = _Factors()
    pipeline.screening_repository = None
    pipeline.screenings = _Screenings()
    pipeline.scoring = _Scoring()
    pipeline.signals = _Signals()
    recommendations = _Recommendations()
    pipeline.recommendations = recommendations

    pipeline.run_for_universe(
        universe_id="universe:test:ashare",
        owner_id="owner-a",
        min_indicator_coverage_ratio=0.5,
        min_factor_coverage_ratio=0.0,
    )

    assert recommendations.calls[0]["owner_id"] == "owner-a"


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
