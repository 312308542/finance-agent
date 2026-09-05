"""候选池级推荐流水线。

本模块把已经存在的单标的服务串成候选池级运行入口：

`候选池成员 -> 指标 -> 因子 -> 初筛 -> 评分 -> 信号 -> 推荐排序`

它不跨市场合并候选池。一次运行只接收一个 `universe_id`，由候选池自身的
`market` 决定链路属于 A 股、数字货币现货或数字货币合约。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from finance_agent.application.theme_context_service import ThemeContextService
from finance_agent.factors import FactorComputationResult, FactorService
from finance_agent.indicators import IndicatorComputationResult, IndicatorService
from finance_agent.recommendations import RecommendationRunResult, RecommendationService
from finance_agent.research.strategy_observation_service import (
    create_strategy_observation_service,
)
from finance_agent.scoring import ScoringRunResult, ScoringService
from finance_agent.screening import ScreeningRunResult, ScreeningService
from finance_agent.screening.service import ensure_single_market_universe
from finance_agent.signals import SignalComputationResult, SignalService
from finance_agent.storage.orm import AssetUniverseMemberORM, AssetUniverseORM
from finance_agent.storage.repositories import (
    ScreeningRepository,
    StrategyObservationRepository,
    UniverseRepository,
)

JsonDict = dict[str, Any]
TECHNICAL_SCREENING_POOL_SOURCE = "technical_screening_pool"
TECHNICAL_SCREENING_STRATEGY = "technical_screening_v1"
THEME_FACTOR_GROUP_NAMES = {"sector_strength", "leadership"}
DEFAULT_OBSERVATION_ROUND_TRIP_COST = 0.003
ADAPTIVE_RESEARCH_STRATEGY_ID = "strategy:ashare:adaptive_v1"
ASHARE_TIMEZONE = ZoneInfo("Asia/Shanghai")


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
    strategy_results: tuple[JsonDict, ...] = ()
    decision_snapshot_id: str | None = None
    buy_ready_count: int = 0
    active_count: int = 0
    exit_pending_count: int = 0


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
        self.theme_contexts = ThemeContextService(session)
        self.observations = create_strategy_observation_service(session)
        self.trial_states = StrategyObservationRepository(session)

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
        avoid_universe_id: str | None = None,
        strategy_id: str | None = None,
        strategy_ids: Sequence[str] | None = None,
        observation_enabled: bool = False,
        observation_trade_date: date | None = None,
        round_trip_cost: float = DEFAULT_OBSERVATION_ROUND_TRIP_COST,
        market_regime: JsonDict | None = None,
        owner_id: str = "default-owner",
    ) -> UniverseRecommendationRunResult:
        """执行一次候选池推荐流水线。"""

        universe = self.universes.get_universe(universe_id)
        effective_strategy_ids = normalize_strategy_ids(
            strategy_id=strategy_id,
            strategy_ids=strategy_ids,
        )
        if observation_enabled and not math.isclose(
            round_trip_cost,
            DEFAULT_OBSERVATION_ROUND_TRIP_COST,
            abs_tol=1e-12,
        ):
            raise ValueError("前向观察交易成本固定为 0.003")
        members = self.universes.list_members(universe_id)
        ensure_single_market_universe(universe.market, members)
        members, audit_payload = exclude_avoid_pool_members(
            universes=self.universes,
            candidate_universe=universe,
            members=members,
            avoid_universe_id=avoid_universe_id,
        )
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
        generated_theme_contexts = build_theme_contexts_for_members(
            theme_contexts=getattr(self, "theme_contexts", None),
            members=indicator_backed_members,
            errors=errors,
        )
        for member in indicator_backed_members:
            factor = self._compute_factor(
                member=member,
                timeframe=effective_timeframe,
                horizon=horizon,
                errors=errors,
                generated_theme_context=generated_theme_contexts.get(member.asset_id),
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
        strategy_runs: list[JsonDict] = []
        scoring_results: list[tuple[str | None, Any, JsonDict]] = []
        for effective_strategy_id in effective_strategy_ids:
            score_strategy_id = effective_strategy_id or (
                f"strategy:{universe.market}:legacy_default"
            )
            strategy_result: JsonDict = {
                "strategy_id": score_strategy_id,
                "scoring_status": "pending",
                "scored_count": 0,
                "recommendation_status": "pending",
                "recommendation_count": 0,
                "recommendation_run_id": None,
                "top_recommendation_id": None,
                "decision_snapshot_id": None,
                "buy_ready_count": 0,
                "active_count": 0,
                "exit_pending_count": 0,
                "trial_state": None,
                "trial": False,
                "validation_evidence_id": None,
                "blocked_reason": None,
                "observation_status": "disabled" if not observation_enabled else "pending",
            }
            try:
                scoring = self.scoring.score_screening(
                    screening_id=screening.screening_id,
                    horizon=horizon,
                    strategy_id=effective_strategy_id,
                )
            except Exception as exc:
                strategy_result["scoring_status"] = "error"
                strategy_result["recommendation_status"] = "blocked"
                strategy_result["blocked_reason"] = "scoring_failed"
                errors.append(
                    strategy_stage_error(
                        stage="scoring",
                        strategy_id=score_strategy_id,
                        error=exc,
                    )
                )
                strategy_runs.append(strategy_result)
                continue
            strategy_result["scoring_status"] = "available"
            strategy_result["scored_count"] = int(scoring.scored_count)
            scoring_results.append((effective_strategy_id, scoring, strategy_result))
            strategy_runs.append(strategy_result)

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

        concrete_scored_strategy_ids = tuple(
            effective_strategy_id
            for effective_strategy_id, _scoring, _result in scoring_results
            if effective_strategy_id is not None
        )
        if observation_enabled and universe.market == "ashare" and concrete_scored_strategy_ids:
            try:
                observation = self.observations.capture(
                    screening_id=screening.screening_id,
                    trade_date=observation_trade_date
                    or datetime.now(tz=ASHARE_TIMEZONE).date(),
                    strategy_ids=concrete_scored_strategy_ids,
                )
                for _strategy_id, _scoring, strategy_result in scoring_results:
                    strategy_result["observation_status"] = observation["status"]
                    strategy_result["observation_id"] = observation.get("observation_id")
            except Exception as exc:
                for _strategy_id, _scoring, strategy_result in scoring_results:
                    strategy_result["observation_status"] = "error"
                errors.append(
                    strategy_stage_error(
                        stage="observation",
                        strategy_id=",".join(concrete_scored_strategy_ids),
                        error=exc,
                    )
                )

        recommendation_results: list[Any] = []
        for _effective_strategy_id, _scoring, strategy_result in scoring_results:
            score_strategy_id = str(strategy_result["strategy_id"])
            gate = recommendation_strategy_gate(
                market=universe.market,
                strategy_id=score_strategy_id,
                trial_states=getattr(self, "trial_states", None),
            )
            strategy_result.update(gate)
            # 未准入只限制新增买入，仍生成观察记录并维护已持仓状态。
            try:
                recommendation = self.recommendations.rank_from_screening(
                    screening_id=screening.screening_id,
                    strategy=strategy,
                    horizon=horizon,
                    limit=limit,
                    score_strategy_id=score_strategy_id,
                    audit_payload=recommendation_audit_payload(
                        audit_payload=audit_payload,
                        strategy_id=score_strategy_id,
                    ),
                    market_regime=market_regime,
                    trial_state=gate["trial_state"],
                    validation_evidence_id=gate["validation_evidence_id"],
                    owner_id=owner_id,
                )
            except Exception as exc:
                strategy_result["recommendation_status"] = "error"
                strategy_result["blocked_reason"] = "recommendation_failed"
                errors.append(
                    strategy_stage_error(
                        stage="recommendation",
                        strategy_id=score_strategy_id,
                        error=exc,
                    )
                )
                continue
            recommendation_results.append(recommendation)
            recommendation_count = int(recommendation.recommendation_count)
            strategy_result["recommendation_status"] = (
                "available" if recommendation_count > 0 else "unavailable"
            )
            strategy_result["recommendation_count"] = recommendation_count
            strategy_result["recommendation_run_id"] = recommendation.run_id
            strategy_result["top_recommendation_id"] = recommendation.top_recommendation_id
            strategy_result["decision_snapshot_id"] = getattr(
                recommendation,
                "decision_snapshot_id",
                None,
            )
            strategy_result["buy_ready_count"] = int(
                getattr(recommendation, "buy_ready_count", 0)
            )
            strategy_result["active_count"] = int(
                getattr(recommendation, "active_count", 0)
            )
            strategy_result["exit_pending_count"] = int(
                getattr(recommendation, "exit_pending_count", 0)
            )

        scored_count = sum(int(item["scored_count"]) for item in strategy_runs)
        recommendation_count = sum(int(item["recommendation_count"]) for item in strategy_runs)
        buy_ready_count = sum(int(item["buy_ready_count"]) for item in strategy_runs)
        active_count = sum(int(item["active_count"]) for item in strategy_runs)
        exit_pending_count = sum(int(item["exit_pending_count"]) for item in strategy_runs)
        primary_recommendation = recommendation_results[0] if recommendation_results else None
        status = multi_strategy_run_status(
            universe=universe,
            members=members,
            indicator_results=indicator_results,
            factor_results=factor_results,
            screening=screening,
            scored_count=scored_count,
            recommendation_count=recommendation_count,
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
            scored_count=scored_count,
            recommendation_count=recommendation_count,
            recommendation_run_id=(
                primary_recommendation.run_id if primary_recommendation is not None else None
            ),
            top_recommendation_id=(
                primary_recommendation.top_recommendation_id
                if primary_recommendation is not None
                else None
            ),
            errors=tuple(errors),
            strategy_results=tuple(strategy_runs),
            decision_snapshot_id=(
                getattr(primary_recommendation, "decision_snapshot_id", None)
                if primary_recommendation is not None
                else None
            ),
            buy_ready_count=buy_ready_count,
            active_count=active_count,
            exit_pending_count=exit_pending_count,
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
        generated_theme_context: Any | None = None,
    ) -> FactorComputationResult | None:
        """计算单个成员因子，并把异常收集到流水线摘要。"""

        try:
            return self.factors.compute_for_asset(
                asset_id=member.asset_id,
                timeframe=timeframe,
                horizon=horizon,
                fallback_symbol=member.symbol,
                fallback_market=member.market,
                supplemental_factor_groups=theme_factor_groups_from_member_payload(
                    member.payload,
                    generated_theme_context=generated_theme_context,
                ),
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


def normalize_strategy_ids(
    *,
    strategy_id: str | None,
    strategy_ids: Sequence[str] | None,
) -> tuple[str | None, ...]:
    """兼容单策略参数，并规范化显式多策略列表。"""

    if strategy_ids is None:
        return (strategy_id,)
    normalized = tuple(
        dict.fromkeys(str(item).strip() for item in strategy_ids if str(item).strip())
    )
    if not normalized:
        raise ValueError("strategy_ids 不能为空")
    if strategy_id is not None and strategy_id not in normalized:
        raise ValueError("strategy_id 与 strategy_ids 不一致")
    return normalized


def recommendation_strategy_gate(
    *,
    market: str,
    strategy_id: str,
    trial_states: Any,
) -> JsonDict:
    """统一判断新增买入准入，不阻止观察与持仓退出记录。"""

    from finance_agent.research.validation_gate import StrategyValidationGate

    if not market.startswith("ashare"):
        return {
            "allowed": True,
            "trial_state": None,
            "trial": False,
            "validation_evidence_id": None,
            "blocked_reason": None,
        }
    state = trial_states.get_trial_state(strategy_id) if trial_states is not None else None
    state_name = str(getattr(state, "state", "research"))
    evidence_id = getattr(state, "historical_evidence_id", None)
    decision = StrategyValidationGate().evaluate_runtime(state, action="buy_ready")
    if state_name == "disabled":
        blocked_reason = "strategy_disabled"
    elif state_name not in {"trial", "validated"}:
        blocked_reason = "historical_gate_not_passed"
    elif not evidence_id:
        blocked_reason = "validation_evidence_missing"
    elif not decision.allowed:
        blocked_reason = ",".join(decision.reason_codes)
    else:
        return {
            "allowed": True,
            "trial_state": state_name,
            "trial": state_name == "trial",
            "validation_evidence_id": str(evidence_id),
            "blocked_reason": None,
            "reason_codes": [],
        }
    return {
        "allowed": False,
        "trial_state": state_name,
        "trial": state_name == "trial",
        "validation_evidence_id": str(evidence_id) if evidence_id else None,
        "blocked_reason": blocked_reason,
        "reason_codes": list(decision.reason_codes),
    }


def multi_strategy_run_status(
    *,
    universe: AssetUniverseORM,
    members: list[AssetUniverseMemberORM],
    indicator_results: list[IndicatorComputationResult],
    factor_results: list[FactorComputationResult],
    screening: ScreeningRunResult,
    scored_count: int,
    recommendation_count: int,
    errors: list[JsonDict],
) -> str:
    """汇总多策略运行，同时保持单策略原有状态语义。"""

    if not members:
        return "unavailable"
    if recommendation_count > 0:
        return "available" if not errors else "partial"
    if scored_count > 0 or screening.passed_count > 0:
        return "partial"
    if factor_results or indicator_results:
        return "partial"
    return "unavailable" if universe.status == "unavailable" else "partial"


def strategy_stage_error(*, stage: str, strategy_id: str, error: Exception) -> JsonDict:
    """记录单策略失败，不阻断同批次其他策略。"""

    return {
        "stage": stage,
        "strategy_id": strategy_id,
        "error": str(error),
        "error_type": type(error).__name__,
    }


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


def theme_factor_groups_from_member_payload(
    payload: JsonDict | None,
    *,
    generated_theme_context: Any | None = None,
) -> list[JsonDict]:
    """从候选池成员 payload 中提取确定性题材因子组。"""

    candidates: list[Any] = []
    candidates.extend(theme_factor_groups_from_context(generated_theme_context))
    if isinstance(payload, dict):
        for key in ("supplemental_factor_groups", "theme_factor_groups", "factor_groups"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates.extend(value)
        theme_context = payload.get("theme_context")
        candidates.extend(theme_factor_groups_from_context(theme_context))

    result: list[JsonDict] = []
    seen: set[str] = set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        group_name = str(item.get("group") or "").strip()
        if group_name not in THEME_FACTOR_GROUP_NAMES or group_name in seen:
            continue
        seen.add(group_name)
        result.append(dict(item))
    return result


def theme_factor_groups_from_context(context: Any | None) -> list[JsonDict]:
    """从生产题材上下文对象或 dict 中提取 factor_groups。"""

    if context is None:
        return []
    if hasattr(context, "to_member_payload"):
        payload = context.to_member_payload()
        if isinstance(payload, dict):
            context = payload.get("theme_context")
    if isinstance(context, dict) and "theme_context" in context:
        nested = context.get("theme_context")
        if isinstance(nested, dict):
            context = nested
    if isinstance(context, dict) and isinstance(context.get("factor_groups"), list):
        return [dict(item) for item in context["factor_groups"] if isinstance(item, dict)]
    return []


def build_theme_contexts_for_members(
    *,
    theme_contexts: Any,
    members: list[AssetUniverseMemberORM],
    errors: list[JsonDict],
) -> dict[str, Any]:
    """调用生产题材上下文服务，失败时只记录错误，不阻塞推荐主链路。"""

    if theme_contexts is None or not hasattr(theme_contexts, "build_for_members"):
        return {}
    try:
        contexts = theme_contexts.build_for_members(members)
    except Exception as exc:
        errors.append(
            {
                "stage": "theme_context",
                "error": str(exc),
                "error_type": type(exc).__name__,
                "message": "题材上下文生成失败，本次推荐继续使用候选池原始 payload。",
            }
        )
        return {}
    if not isinstance(contexts, dict):
        return {}
    return contexts


def exclude_avoid_pool_members(
    *,
    universes: UniverseRepository,
    candidate_universe: AssetUniverseORM,
    members: list[AssetUniverseMemberORM],
    avoid_universe_id: str | None,
) -> tuple[list[AssetUniverseMemberORM], JsonDict]:
    """根据回避池剔除候选成员，并返回推荐运行审计 payload。"""

    if not avoid_universe_id:
        return members, {}
    avoid_universe = universes.get_universe(avoid_universe_id)
    if avoid_universe.market != candidate_universe.market:
        raise ValueError(
            f"回避池 {avoid_universe_id} 市场为 {avoid_universe.market}，"
            f"与候选池 {candidate_universe.universe_id} 市场 {candidate_universe.market} 不一致。"
        )
    avoid_members = universes.list_members(avoid_universe_id, included_only=False)
    ensure_single_market_universe(avoid_universe.market, avoid_members)
    avoid_by_asset_id = {
        member.asset_id: member
        for member in avoid_members
        if not member.included and avoid_member_matches_snapshot(member, avoid_universe)
    }
    if not avoid_by_asset_id:
        return members, {
            "avoid_pool_excluded": {
                "count": 0,
                "assets": [],
            }
        }

    kept_members: list[AssetUniverseMemberORM] = []
    excluded_assets: list[JsonDict] = []
    for member in members:
        avoid_member = avoid_by_asset_id.get(member.asset_id)
        if avoid_member is None:
            kept_members.append(member)
            continue
        excluded_assets.append(
            {
                "asset_id": member.asset_id,
                "symbol": member.symbol,
                "reason": avoid_pool_reason(avoid_member),
                "source_universe_id": avoid_universe_id,
            }
        )
    return kept_members, {
        "avoid_pool_excluded": {
            "count": len(excluded_assets),
            "assets": excluded_assets,
        }
    }


def avoid_member_matches_snapshot(member: Any, avoid_universe: AssetUniverseORM) -> bool:
    """只使用最新回避池快照；缺少水位时维持风险侧 fail-closed。"""

    snapshot_at = getattr(avoid_universe, "as_of", None)
    member_at = getattr(member, "as_of", None)
    if not isinstance(snapshot_at, datetime) or not isinstance(member_at, datetime):
        return True
    return member_at == snapshot_at


def recommendation_audit_payload(*, audit_payload: JsonDict, strategy_id: str | None) -> JsonDict:
    """补充推荐运行审计 payload，保留评分策略可追溯信息。"""

    payload = dict(audit_payload)
    if strategy_id:
        payload["scoring_strategy_id"] = strategy_id
    return payload


def avoid_pool_reason(member: AssetUniverseMemberORM) -> str | None:
    """从回避池成员 payload/removed_reason 中提取可审计剔除原因。"""

    payload = member.payload or {}
    avoid_reasons = payload.get("avoid_reasons")
    if isinstance(avoid_reasons, list) and avoid_reasons:
        return "；".join(str(item) for item in avoid_reasons if item)
    for key in ("reason", "removed_reason", "avoid_reason"):
        value = payload.get(key)
        if value:
            return str(value)
    return member.removed_reason


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
