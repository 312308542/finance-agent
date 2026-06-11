"""候选池级推荐流水线。

本模块把已经存在的单标的服务串成候选池级运行入口：

`候选池成员 -> 指标 -> 因子 -> 初筛 -> 评分 -> 信号 -> 推荐排序`

它不跨市场合并候选池。一次运行只接收一个 `universe_id`，由候选池自身的
`market` 决定链路属于 A 股、数字货币现货或数字货币合约。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.factors import FactorComputationResult, FactorService
from finance_agent.indicators import IndicatorComputationResult, IndicatorService
from finance_agent.recommendations import RecommendationRunResult, RecommendationService
from finance_agent.scoring import ScoringRunResult, ScoringService
from finance_agent.screening import ScreeningRunResult, ScreeningService
from finance_agent.screening.service import ensure_single_market_universe
from finance_agent.signals import SignalComputationResult, SignalService
from finance_agent.storage.orm import AssetUniverseMemberORM, AssetUniverseORM
from finance_agent.storage.repositories import UniverseRepository
from finance_agent.storage.repositories import ScreeningRepository

JsonDict = dict[str, Any]
TECHNICAL_SCREENING_POOL_SOURCE = "technical_screening_pool"
TECHNICAL_SCREENING_STRATEGY = "technical_screening_v1"


@dataclass(frozen=True)
class UniverseRecommendationRunResult:
    """一次候选池推荐流水线运行摘要。"""

    status: str
    universe_id: str
    market: str
    strategy: str
    horizon: str
    timeframe: str
    member_count: int
    indicator_count: int
    factor_count: int
    signal_count: int
    screening_id: str | None
    scored_count: int
    recommendation_count: int
    recommendation_run_id: str | None
    top_recommendation_id: str | None
    errors: tuple[JsonDict, ...]


class UniverseRecommendationPipeline:
    """按单一候选池执行确定性推荐基础链路。"""

    def __init__(self, session: Session) -> None:
        self.universes = UniverseRepository(session)
        self.indicators = IndicatorService(session)
        self.factors = FactorService(session)
        self.screenings = ScreeningService(session)
        self.screening_repository = ScreeningRepository(session)
        self.scoring = ScoringService(session)
        self.signals = SignalService(session)
        self.recommendations = RecommendationService(session)

    def run_for_universe(
        self,
        *,
        universe_id: str,
        strategy: str = "balanced_swing_v1",
        horizon: str = "swing",
        timeframe: str | None = None,
        source: str | None = None,
        window: int = 120,
        min_bars: int = 2,
        limit: int = 20,
        min_indicator_coverage_ratio: float | None = None,
        min_factor_coverage_ratio: float | None = None,
        min_available_factor_groups: int = 1,
        candidate_source: str | None = None,
        technical_screening_strategy: str = TECHNICAL_SCREENING_STRATEGY,
    ) -> UniverseRecommendationRunResult:
        """执行一次候选池推荐流水线。"""

        universe = self.universes.get_universe(universe_id)
        members = self.universes.list_members(universe_id)
        ensure_single_market_universe(universe.market, members)
        members = prefer_technical_screening_members(
            members=members,
            market=universe.market,
            candidate_source=candidate_source,
            screening_repository=getattr(self, "screening_repository", None),
            strategy=technical_screening_strategy,
        )
        effective_timeframe = timeframe or default_timeframe(universe.market)
        required_indicator_coverage = normalize_indicator_coverage_ratio(
            market=universe.market,
            configured=min_indicator_coverage_ratio,
        )
        required_factor_coverage = normalize_factor_coverage_ratio(
            configured=min_factor_coverage_ratio,
        )
        indicator_results: list[IndicatorComputationResult] = []
        factor_results: list[FactorComputationResult] = []
        signal_results: list[SignalComputationResult] = []
        errors: list[JsonDict] = []

        for member in members:
            indicator = self._compute_indicator(
                member=member,
                timeframe=effective_timeframe,
                horizon=horizon,
                source=source,
                window=window,
                min_bars=min_bars,
                errors=errors,
            )
            if indicator is not None:
                indicator_results.append(indicator)

        indicator_count = count_successful_indicators(indicator_results)
        if should_stop_for_indicator_coverage(
            market=universe.market,
            member_count=len(members),
            indicator_count=indicator_count,
            min_indicator_coverage_ratio=required_indicator_coverage,
        ):
            errors.append(
                indicator_coverage_error(
                    market=universe.market,
                    member_count=len(members),
                    indicator_count=indicator_count,
                    min_indicator_coverage_ratio=required_indicator_coverage,
                )
            )
            return UniverseRecommendationRunResult(
                status="unavailable",
                universe_id=universe_id,
                market=universe.market,
                strategy=strategy,
                horizon=horizon,
                timeframe=effective_timeframe,
                member_count=len(members),
                indicator_count=indicator_count,
                factor_count=0,
                signal_count=0,
                screening_id=None,
                scored_count=0,
                recommendation_count=0,
                recommendation_run_id=None,
                top_recommendation_id=None,
                errors=tuple(errors),
            )

        indicator_backed_members = members_with_successful_indicators(
            members=members,
            indicator_results=indicator_results,
        )
        for member in indicator_backed_members:
            factor = self._compute_factor(
                member=member,
                timeframe=effective_timeframe,
                horizon=horizon,
                errors=errors,
            )
            if factor is not None:
                factor_results.append(factor)

        factor_count = count_usable_factors(
            factor_results,
            min_available_factor_groups=min_available_factor_groups,
        )
        if should_stop_for_factor_coverage(
            member_count=len(members),
            factor_count=factor_count,
            min_factor_coverage_ratio=required_factor_coverage,
        ):
            errors.append(
                factor_coverage_error(
                    market=universe.market,
                    member_count=len(members),
                    factor_count=factor_count,
                    min_factor_coverage_ratio=required_factor_coverage,
                    min_available_factor_groups=min_available_factor_groups,
                )
            )
            return UniverseRecommendationRunResult(
                status="unavailable",
                universe_id=universe_id,
                market=universe.market,
                strategy=strategy,
                horizon=horizon,
                timeframe=effective_timeframe,
                member_count=len(members),
                indicator_count=indicator_count,
                factor_count=factor_count,
                signal_count=0,
                screening_id=None,
                scored_count=0,
                recommendation_count=0,
                recommendation_run_id=None,
                top_recommendation_id=None,
                errors=tuple(errors),
            )

        screening = self.screenings.apply_rules(
            universe_id=universe_id,
            strategy=strategy,
            horizon=horizon,
        )
        scoring = self.scoring.score_screening(
            screening_id=screening.screening_id,
            horizon=horizon,
        )

        usable_factor_asset_ids = usable_factor_asset_ids_from_results(
            factor_results,
            min_available_factor_groups=min_available_factor_groups,
        )
        for member in members:
            if member.asset_id not in usable_factor_asset_ids:
                continue
            signal = self._compute_signal(member=member, horizon=horizon, errors=errors)
            if signal is not None:
                signal_results.append(signal)

        recommendation = self.recommendations.rank_from_screening(
            screening_id=screening.screening_id,
            strategy=strategy,
            horizon=horizon,
            limit=limit,
        )
        status = run_status(
            universe=universe,
            members=members,
            indicator_results=indicator_results,
            factor_results=factor_results,
            screening=screening,
            scoring=scoring,
            recommendation=recommendation,
            errors=errors,
        )
        return UniverseRecommendationRunResult(
            status=status,
            universe_id=universe_id,
            market=universe.market,
            strategy=strategy,
            horizon=horizon,
            timeframe=effective_timeframe,
            member_count=len(members),
            indicator_count=indicator_count,
            factor_count=count_usable_factors(
                factor_results,
                min_available_factor_groups=min_available_factor_groups,
            ),
            signal_count=count_successful_signals(signal_results),
            screening_id=screening.screening_id,
            scored_count=scoring.scored_count,
            recommendation_count=recommendation.recommendation_count,
            recommendation_run_id=recommendation.run_id,
            top_recommendation_id=recommendation.top_recommendation_id,
            errors=tuple(errors),
        )

    def _compute_indicator(
        self,
        *,
        member: AssetUniverseMemberORM,
        timeframe: str,
        horizon: str,
        source: str | None,
        window: int,
        min_bars: int,
        errors: list[JsonDict],
    ) -> IndicatorComputationResult | None:
        """计算单个成员指标，并把异常收集到流水线摘要。"""

        try:
            return self.indicators.compute_for_asset(
                asset_id=member.asset_id,
                timeframe=timeframe,
                horizon=horizon,
                source=source,
                window=window,
                min_bars=min_bars,
                fallback_symbol=member.symbol,
                fallback_market=member.market,
            )
        except Exception as exc:
            errors.append(error_payload(member=member, stage="indicator", error=exc))
            return None

    def _compute_factor(
        self,
        *,
        member: AssetUniverseMemberORM,
        timeframe: str,
        horizon: str,
        errors: list[JsonDict],
    ) -> FactorComputationResult | None:
        """计算单个成员因子，并把异常收集到流水线摘要。"""

        try:
            return self.factors.compute_for_asset(
                asset_id=member.asset_id,
                timeframe=timeframe,
                horizon=horizon,
                fallback_symbol=member.symbol,
                fallback_market=member.market,
            )
        except Exception as exc:
            errors.append(error_payload(member=member, stage="factor", error=exc))
            return None

    def _compute_signal(
        self,
        *,
        member: AssetUniverseMemberORM,
        horizon: str,
        errors: list[JsonDict],
    ) -> SignalComputationResult | None:
        """计算单个成员信号，并把异常收集到流水线摘要。"""

        try:
            return self.signals.compute_for_asset(asset_id=member.asset_id, horizon=horizon)
        except Exception as exc:
            errors.append(error_payload(member=member, stage="signal", error=exc))
            return None


def default_timeframe(market: str) -> str:
    """根据市场给出第一版默认 K 线周期。"""

    return "1h" if market.startswith("crypto") else "1d"


def count_successful_indicators(results: list[IndicatorComputationResult]) -> int:
    """统计成功写入指标快照的标的数量。"""

    return sum(1 for item in results if item.indicator_frame_id is not None)


def count_available_factors(results: list[FactorComputationResult]) -> int:
    """统计至少有一个可用因子组的标的数量。"""

    return sum(1 for item in results if item.status in {"available", "partial"})


def count_usable_factors(
    results: list[FactorComputationResult],
    *,
    min_available_factor_groups: int,
) -> int:
    """统计满足最小可用因子组数量的标的数量。"""

    minimum_groups = max(1, min_available_factor_groups)
    return sum(
        1
        for item in results
        if item.status in {"available", "partial"}
        and item.total_available_groups >= minimum_groups
    )


def usable_factor_asset_ids_from_results(
    results: list[FactorComputationResult],
    *,
    min_available_factor_groups: int,
) -> set[str]:
    """返回可继续进入信号阶段的标的 ID。"""

    minimum_groups = max(1, min_available_factor_groups)
    return {
        item.asset_id
        for item in results
        if item.status in {"available", "partial"}
        and item.total_available_groups >= minimum_groups
    }


def members_with_successful_indicators(
    *,
    members: list[AssetUniverseMemberORM],
    indicator_results: list[IndicatorComputationResult],
) -> list[AssetUniverseMemberORM]:
    """只保留已有指标快照的候选池成员，避免缺 K 线标的继续写入低质量因子。"""

    available_asset_ids = {
        item.asset_id for item in indicator_results if item.indicator_frame_id is not None
    }
    return [member for member in members if member.asset_id in available_asset_ids]


def prefer_technical_screening_members(
    *,
    members: list[AssetUniverseMemberORM],
    market: str,
    candidate_source: str | None,
    screening_repository: Any,
    strategy: str,
) -> list[AssetUniverseMemberORM]:
    """配置技术初筛来源时，优先使用最近技术初筛通过项。"""

    if candidate_source != TECHNICAL_SCREENING_POOL_SOURCE or screening_repository is None:
        return members
    latest = screening_repository.get_latest_screening_result(
        market=market,
        strategy=strategy or TECHNICAL_SCREENING_STRATEGY,
    )
    if latest is None:
        return members
    passed_items = screening_repository.list_items(
        screening_id=latest.screening_id,
        passed_only=True,
    )
    passed_asset_ids = {item.asset_id for item in passed_items}
    if not passed_asset_ids:
        return members
    selected = [member for member in members if member.asset_id in passed_asset_ids]
    return selected or members


def count_successful_signals(results: list[SignalComputationResult]) -> int:
    """统计成功写入信号快照的标的数量。"""

    return sum(1 for item in results if item.signal_id is not None)


def normalize_indicator_coverage_ratio(
    *,
    market: str,
    configured: float | None,
) -> float:
    """返回市场级指标覆盖率闸门。"""

    if configured is not None:
        return max(0.0, min(float(configured), 1.0))
    return 0.5


def normalize_factor_coverage_ratio(*, configured: float | None) -> float:
    """返回因子覆盖率闸门。"""

    if configured is not None:
        return max(0.0, min(float(configured), 1.0))
    return 0.0


def should_stop_for_indicator_coverage(
    *,
    market: str,
    member_count: int,
    indicator_count: int,
    min_indicator_coverage_ratio: float,
) -> bool:
    """判断是否因行情指标覆盖率不足而停止后续推荐。"""

    if member_count <= 0 or min_indicator_coverage_ratio <= 0:
        return False
    return indicator_count / member_count < min_indicator_coverage_ratio


def should_stop_for_factor_coverage(
    *,
    member_count: int,
    factor_count: int,
    min_factor_coverage_ratio: float,
) -> bool:
    """判断是否因可用因子覆盖率不足而停止后续推荐。"""

    if member_count <= 0 or min_factor_coverage_ratio <= 0:
        return False
    return factor_count / member_count < min_factor_coverage_ratio


def indicator_coverage_error(
    *,
    market: str,
    member_count: int,
    indicator_count: int,
    min_indicator_coverage_ratio: float,
) -> JsonDict:
    """生成指标覆盖率不足的流水线错误摘要。"""

    coverage_ratio = indicator_count / member_count if member_count else 0.0
    return {
        "stage": "indicator_coverage",
        "market": market,
        "successful_indicators": indicator_count,
        "member_count": member_count,
        "coverage_ratio": round(coverage_ratio, 6),
        "required_coverage_ratio": min_indicator_coverage_ratio,
        "message": "行情指标覆盖率不足，跳过本次因子、筛选、评分和推荐，等待 K 线补齐。",
    }


def factor_coverage_error(
    *,
    market: str,
    member_count: int,
    factor_count: int,
    min_factor_coverage_ratio: float,
    min_available_factor_groups: int,
) -> JsonDict:
    """生成因子覆盖率不足的流水线错误摘要。"""

    coverage_ratio = factor_count / member_count if member_count else 0.0
    return {
        "stage": "factor_coverage",
        "market": market,
        "usable_factors": factor_count,
        "member_count": member_count,
        "coverage_ratio": round(coverage_ratio, 6),
        "required_coverage_ratio": min_factor_coverage_ratio,
        "min_available_factor_groups": max(1, min_available_factor_groups),
        "message": "可用因子覆盖率不足，跳过本次筛选、评分、信号和推荐，等待基础数据补齐。",
    }


def run_status(
    *,
    universe: AssetUniverseORM,
    members: list[AssetUniverseMemberORM],
    indicator_results: list[IndicatorComputationResult],
    factor_results: list[FactorComputationResult],
    screening: ScreeningRunResult,
    scoring: ScoringRunResult,
    recommendation: RecommendationRunResult,
    errors: list[JsonDict],
) -> str:
    """汇总候选池流水线状态。"""

    if not members:
        return "unavailable"
    if recommendation.recommendation_count > 0:
        return "available" if not errors else "partial"
    if scoring.scored_count > 0 or screening.passed_count > 0:
        return "partial"
    if factor_results or indicator_results:
        return "partial"
    return "unavailable" if universe.status == "unavailable" else "partial"


def error_payload(
    *,
    member: AssetUniverseMemberORM,
    stage: str,
    error: Exception,
) -> JsonDict:
    """生成流水线错误摘要。"""

    return {
        "stage": stage,
        "asset_id": member.asset_id,
        "symbol": member.symbol,
        "market": member.market,
        "error": str(error),
        "error_type": type(error).__name__,
    }
