"""推荐结果生成服务。

本服务属于数据层最后一截：读取 `asset_scores`、`signal_snapshots`、`risk_findings`
和资产主数据，写入 `recommendation_runs`、`recommendation_run_universes` 和
`asset_recommendations`。它不调用 LLM，不生成 Agent 分析，也不处理交易。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from copy import copy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.application.market_context_service import adjust_buy_percentile_threshold
from finance_agent.data.freshness import ashare_market_close_at
from finance_agent.recommendations.adaptive_decision import (
    AdaptiveAssetDecision,
    AdaptiveRecommendationDecisionEngine,
)
from finance_agent.recommendations.decision_snapshot import (
    DecisionFact,
    DecisionSnapshot,
    DecisionSnapshotBuilder,
    DecisionSnapshotInputs,
)
from finance_agent.recommendations.lifecycle import RecommendationState, StockSetup
from finance_agent.recommendations.portfolio_construction import (
    PortfolioPosition,
    PortfolioRiskBudget,
)
from finance_agent.recommendations.state_repository import RecommendationStateRepository
from finance_agent.recommendations.structural_decision import StructuralDecisionEngine
from finance_agent.storage.orm import AssetScoreORM, RiskFindingORM, SignalSnapshotORM
from finance_agent.storage.repositories import (
    AssetRepository,
    AssetScoreRepository,
    BacktestRepository,
    DataSnapshotRepository,
    FactorFrameRepository,
    IndicatorFrameRepository,
    MarketDataRepository,
    PortfolioRepository,
    RecommendationRepository,
    RiskRepository,
    ScreeningRepository,
    SignalSnapshotRepository,
)

JsonDict = dict[str, Any]

RULE_VERSION = "asset_recommendation_v1.0.0"
STRUCTURAL_LITE_LIBRARY = "structural-lite"
STRUCTURAL_LITE_HORIZONS: tuple[str, ...] = (
    "structural_swings_v2",
    "smc_lite_v2",
    "harmonic_lite_v2",
    "elliott_lite_v2",
    "ichimoku_v1",
)
ADAPTIVE_STRATEGY_ID = "strategy:ashare:adaptive_v1"


@dataclass(frozen=True)
class RecommendationRunResult:
    """一次推荐运行摘要。"""

    status: str
    run_id: str
    universe_id: str | None
    screening_id: str
    strategy: str
    market: str
    horizon: str
    recommendation_count: int
    top_recommendation_id: str | None
    decision_snapshot_id: str | None = None
    buy_ready_count: int = 0
    active_count: int = 0
    exit_pending_count: int = 0


@dataclass
class LifecycleScoreView:
    """让筛选池外的开放生命周期资产继续参与状态维护。"""

    score_id: str
    asset_id: str
    symbol: str
    market: str
    horizon: str
    total_score: Decimal
    confidence: Decimal
    factor_frame_id: str | None
    as_of: datetime
    payload: JsonDict
    rank: int
    missing_penalty: Decimal = Decimal("0")


@dataclass(frozen=True)
class RecommendationDecisionContext:
    """推荐动作裁决的候选池上下文。"""

    rank: int
    total: int
    style_tendency: JsonDict | None = None
    market_regime: JsonDict | None = None
    tradability: JsonDict | None = None
    absolute_floor: float = 45.0

    @property
    def percentile(self) -> float:
        """返回候选池内排名分位，越小越靠前。"""

        if self.total <= 0:
            return 1.0
        return max(self.rank, 1) / self.total

    @property
    def buy_percentile_threshold(self) -> float:
        """按画像决定买入候选分位阈值。"""

        style = self.style_tendency or {}
        theme_weight = float(style.get("theme") or 0)
        value_weight = float(style.get("value") or 0)
        if theme_weight >= 0.65:
            return 0.20
        if value_weight >= 0.65:
            return 0.08
        return 0.12

    @property
    def adjusted_buy_percentile_threshold(self) -> float:
        """叠加大盘环境和择时姿态后的买入分位阈值。"""

        regime = str((self.market_regime or {}).get("regime") or "range")
        timing_posture = str((self.style_tendency or {}).get("timing_posture") or "balanced")
        return adjust_buy_percentile_threshold(
            base_threshold=self.buy_percentile_threshold,
            regime=regime,
            timing_posture=timing_posture,
        )


@dataclass(frozen=True)
class MemoryRankingAdjustment:
    """Finance Memory 对推荐排序的可审计调整。"""

    asset_id: str
    adjustment: float
    reasons: tuple[str, ...]

    def to_dict(self) -> JsonDict:
        """转换为推荐 payload 可保存的结构。"""

        return {
            "asset_id": self.asset_id,
            "adjustment": self.adjustment,
            "reasons": list(self.reasons),
        }


class RecommendationService:
    """把评分、信号和风险组织成可查询的推荐结果。"""

    def __init__(self, session: Session) -> None:
        self.assets = AssetRepository(session)
        self.screenings = ScreeningRepository(session)
        self.scores = AssetScoreRepository(session)
        self.signals = SignalSnapshotRepository(session)
        self.risks = RiskRepository(session)
        self.indicators = IndicatorFrameRepository(session)
        self.recommendations = RecommendationRepository(session)
        self.backtests = BacktestRepository(session)
        self.factors = FactorFrameRepository(session)
        self.snapshots = DataSnapshotRepository(session)
        self.market_data = MarketDataRepository(session)
        self.portfolios = PortfolioRepository(session)
        self.lifecycle_states = RecommendationStateRepository(session)
        self.adaptive_decisions = AdaptiveRecommendationDecisionEngine()

    def rank_from_screening(
        self,
        *,
        screening_id: str,
        strategy: str = "balanced_swing_v1",
        horizon: str = "swing",
        limit: int = 20,
        score_strategy_id: str | None = None,
        rule_version: str = RULE_VERSION,
        audit_payload: JsonDict | None = None,
        profile_style_tendency: JsonDict | None = None,
        market_regime: JsonDict | None = None,
        memory_ranking_adjustments: dict[str, MemoryRankingAdjustment] | None = None,
        trial_state: str | None = None,
        validation_evidence_id: str | None = None,
        owner_id: str = "default-owner",
        decision_snapshot: DecisionSnapshot | None = None,
        portfolio_budget: PortfolioRiskBudget | None = None,
    ) -> RecommendationRunResult:
        """读取一次初筛的评分结果并生成推荐榜单。"""

        validate_trial_audit(
            trial_state=trial_state,
            validation_evidence_id=validation_evidence_id,
        )
        is_trial = trial_state == "trial"
        screening = self.screenings.get_screening_result(screening_id)
        ensure_recommendation_market(screening.market)
        started_at = datetime.now(tz=UTC)
        run_id = build_run_id(
            screening_id=screening_id,
            strategy=strategy,
            horizon=horizon,
            started_at=started_at,
            score_strategy_id=score_strategy_id,
            trial_state=trial_state,
            validation_evidence_id=validation_evidence_id,
        )
        listed_scores = (
            self.scores.list_scores_for_screening(
                screening_id,
                strategy_id=score_strategy_id,
            )
            if score_strategy_id is not None
            else self.scores.list_scores_for_screening(screening_id)
        )
        raw_scores = (
            listed_scores
            if score_strategy_id == ADAPTIVE_STRATEGY_ID
            else listed_scores[:limit]
        )
        scores = apply_memory_ranking_adjustments(raw_scores, memory_ranking_adjustments or {})
        if score_strategy_id == ADAPTIVE_STRATEGY_ID:
            scores = self.expand_adaptive_scores_with_open_assets(
                scores,
                owner_id=owner_id,
                strategy_id=score_strategy_id,
                market=screening.market,
                horizon=horizon,
            )
        ensure_scores_match_market(scores=scores, market=screening.market)
        backtest_strategy_id = resolve_backtest_strategy_id(
            scores=scores,
            fallback=score_strategy_id or strategy,
        )
        backtests = getattr(self, "backtests", None)
        backtest_evidence = (
            build_backtest_evidence(
                backtests=backtests,
                market=screening.market,
                strategy_id=backtest_strategy_id,
                universe_id=screening.universe_id,
            )
            if backtests is not None
            else build_missing_backtest_evidence(
                market=screening.market,
                strategy_id=backtest_strategy_id,
                universe_id=screening.universe_id,
            )
        )
        recommendation_ids: list[str] = []
        adaptive_result = None
        adaptive_by_asset: dict[str, AdaptiveAssetDecision] = {}
        if score_strategy_id == ADAPTIVE_STRATEGY_ID:
            state_repository = getattr(self, "lifecycle_states", None)
            state_rows = (
                state_repository.list_states(
                    owner_id=owner_id,
                    strategy_id=score_strategy_id,
                    asset_ids=tuple(score.asset_id for score in scores),
                )
                if state_repository is not None
                and hasattr(state_repository, "list_states")
                else []
            )
            state_rows_by_asset = {str(row.asset_id): row for row in state_rows}
            previous_states = {
                state.asset_id: state
                for row in state_rows
                if (state := _recommendation_state_from_row(row)) is not None
            }
            previous_assets = {
                str(row.asset_id): dict(decision_asset)
                for row in state_rows
                if isinstance((payload := getattr(row, "payload", None)), dict)
                and isinstance((decision_asset := payload.get("decision_asset")), dict)
            }
            if decision_snapshot is None:
                decision_snapshot = self.build_adaptive_decision_snapshot(
                    scores=scores,
                    market=screening.market,
                    horizon=horizon,
                    market_regime=market_regime,
                    previous_assets=previous_assets,
                )
            if decision_snapshot.market != screening.market:
                raise ValueError("决策快照市场与初筛市场不一致")
            adaptive_engine = getattr(
                self,
                "adaptive_decisions",
                AdaptiveRecommendationDecisionEngine(),
            )
            (
                portfolio_positions,
                resolved_portfolio_budget,
                closed_position_asset_ids,
            ) = self.load_portfolio_context(
                owner_id=owner_id,
                market=screening.market,
                market_regime=decision_snapshot.market_regime,
                explicit_budget=portfolio_budget,
                previous_states=previous_states,
            )
            adaptive_result = adaptive_engine.decide(
                decision_snapshot,
                previous_states=previous_states,
                positions=portfolio_positions,
                budget=resolved_portfolio_budget,
                closed_position_events=closed_position_asset_ids,
                owner_id=owner_id,
                strategy_id=score_strategy_id,
            )
            adaptive_by_asset = {
                item.asset_id: item for item in adaptive_result.decisions
            }
            scores.sort(
                key=lambda score: adaptive_by_asset.get(score.asset_id).alpha.alpha_score
                if score.asset_id in adaptive_by_asset
                else -1,
                reverse=True,
            )
            for rank, score in enumerate(scores, start=1):
                score.rank = rank
                decision = adaptive_by_asset.get(score.asset_id)
                if decision is not None:
                    score.total_score = Decimal(str(decision.alpha.alpha_score))
                    score.confidence = Decimal(str(decision.alpha.confidence))
            if state_repository is not None:
                for item in adaptive_result.decisions:
                    setup = stock_setup_from_adaptive_decision(
                        item,
                        owner_id=owner_id,
                        strategy_id=score_strategy_id,
                    )
                    if setup is not None and hasattr(state_repository, "save_setup"):
                        state_repository.save_setup(setup)
                    state_repository.save_transition(
                        item.transition,
                        current_state=state_rows_by_asset.get(item.asset_id),
                    )

        self.recommendations.upsert_run_universe(
            record_id=f"{run_id}:{screening.universe_id}",
            run_id=run_id,
            universe_id=screening.universe_id,
            market=screening.market,
            role="primary",
            weight=Decimal("1"),
            asset_count=screening.passed_count,
            payload={
                "screening_id": screening_id,
                "strategy": strategy,
                "score_strategy_id": score_strategy_id,
                "horizon": horizon,
                "trial": is_trial,
                "validation_state": trial_state,
                "validation_evidence_id": validation_evidence_id,
            },
        )

        asset_names = self.asset_names(tuple(score.asset_id for score in scores))
        for rank, score in enumerate(scores, start=1):
            adaptive_decision = adaptive_by_asset.get(score.asset_id)
            if score_strategy_id == ADAPTIVE_STRATEGY_ID:
                recommendation = build_adaptive_recommendation_payload(
                    score=score,
                    decision=adaptive_decision,
                    decision_snapshot=decision_snapshot,
                    asset_name=asset_names.get(score.asset_id, score.symbol),
                    rank=rank,
                    run_id=run_id,
                    rule_version=rule_version,
                    backtest_evidence=backtest_evidence,
                    trial_state=trial_state,
                    validation_evidence_id=validation_evidence_id,
                )
            else:
                signal = self.signals.get_latest_signal(
                    asset_id=score.asset_id,
                    horizon=horizon,
                )
                risks = self.risks.list_recent_risks(asset_id=score.asset_id, limit=10)
                decision_context = RecommendationDecisionContext(
                    rank=rank,
                    total=len(scores),
                    style_tendency=profile_style_tendency,
                    market_regime=market_regime,
                )
                recommendation = build_recommendation_payload(
                    score=score,
                    signal=signal,
                    risks=risks,
                    asset_name=asset_names.get(score.asset_id, score.symbol),
                    rank=rank,
                    run_id=run_id,
                    rule_version=rule_version,
                    backtest_evidence=backtest_evidence,
                    decision_context=decision_context,
                    structure_evidence=build_asset_structure_payload(
                        indicators=getattr(self, "indicators", None),
                        asset_id=score.asset_id,
                        timeframe=str(score.payload.get("timeframe") or "1d"),
                    ),
                    trial_state=trial_state,
                    validation_evidence_id=validation_evidence_id,
                )
            saved = self.recommendations.upsert_asset_recommendation(
                recommendation_id=recommendation["recommendation_id"],
                run_id=run_id,
                asset_id=score.asset_id,
                symbol=score.symbol,
                name=recommendation["name"],
                market=score.market,
                horizon=score.horizon,
                action=recommendation["action"],
                rank=rank,
                total_score=score.total_score,
                confidence=score.confidence,
                conviction=recommendation["conviction"],
                score_id=score.score_id,
                factor_frame_id=score.factor_frame_id,
                signal_ids=recommendation["signal_ids"],
                risk_ids=recommendation["risk_ids"],
                evidence_ids=recommendation["evidence_ids"],
                watch_conditions=recommendation["watch_conditions"],
                invalid_if=recommendation["invalid_if"],
                summary=recommendation["summary"],
                payload=recommendation,
            )
            recommendation_ids.append(saved.recommendation_id)

        finished_at = datetime.now(tz=UTC)
        status = "available" if recommendation_ids else "unavailable"
        summary = (
            "本次没有满足新增买入门槛的标的。"
            if adaptive_result is not None and adaptive_result.buy_ready_count == 0
            else build_run_summary(
                recommendation_count=len(recommendation_ids),
                market=screening.market,
                strategy=strategy,
            )
        )
        run_payload = {
            "schema_version": "1.0",
            "rule_version": rule_version,
            "recommendation_ids": recommendation_ids,
            "top_recommendations": recommendation_ids[: min(5, len(recommendation_ids))],
            "watchlist": recommendation_ids,
            "avoidlist": [],
            "source": {
                "screening_id": screening_id,
                "universe_id": screening.universe_id,
                "score_count": len(scores),
                "score_strategy_id": score_strategy_id,
                "trial": is_trial,
                "validation_state": trial_state,
                "validation_evidence_id": validation_evidence_id,
            },
            "backtest_evidence": backtest_evidence,
            "trial": is_trial,
            "validation_state": trial_state,
            "validation_evidence_id": validation_evidence_id,
            "decision_snapshot_id": (
                adaptive_result.decision_snapshot_id if adaptive_result is not None else None
            ),
            "buy_ready_count": (
                adaptive_result.buy_ready_count if adaptive_result is not None else 0
            ),
            "active_count": adaptive_result.active_count if adaptive_result is not None else 0,
            "exit_pending_count": (
                adaptive_result.exit_pending_count if adaptive_result is not None else 0
            ),
        }
        if profile_style_tendency:
            run_payload["profile_style_tendency"] = profile_style_tendency
        if market_regime:
            run_payload["market_regime"] = market_regime
        if audit_payload:
            run_payload.update(audit_payload)
        self.recommendations.upsert_run(
            run_id=run_id,
            universe_id=screening.universe_id,
            screening_id=screening_id,
            strategy=strategy,
            market=screening.market,
            horizon=horizon,
            limit=limit,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            summary=summary,
            payload=run_payload,
        )

        return RecommendationRunResult(
            status=status,
            run_id=run_id,
            universe_id=screening.universe_id,
            screening_id=screening_id,
            strategy=strategy,
            market=screening.market,
            horizon=horizon,
            recommendation_count=len(recommendation_ids),
            top_recommendation_id=recommendation_ids[0] if recommendation_ids else None,
            decision_snapshot_id=(
                adaptive_result.decision_snapshot_id if adaptive_result is not None else None
            ),
            buy_ready_count=(
                adaptive_result.buy_ready_count if adaptive_result is not None else 0
            ),
            active_count=adaptive_result.active_count if adaptive_result is not None else 0,
            exit_pending_count=(
                adaptive_result.exit_pending_count if adaptive_result is not None else 0
            ),
        )

    def build_adaptive_decision_snapshot(
        self,
        *,
        scores: list[AssetScoreORM],
        market: str,
        horizon: str,
        market_regime: JsonDict | None,
        previous_assets: dict[str, JsonDict] | None = None,
    ) -> DecisionSnapshot:
        """从已落库因子和结构事实冻结统一的自适应决策输入。"""

        if not scores:
            raise ValueError("自适应决策快照至少需要一个评分资产")
        as_of_values = [
            value for score in scores if isinstance((value := getattr(score, "as_of", None)), datetime)
        ]
        if not as_of_values:
            raise ValueError("自适应评分缺少显式 as_of，不能构造点时快照")
        decision_as_of = max(
            _normalize_fact_time(value, market=market) for value in as_of_values
        )
        assets: list[JsonDict] = []
        data_versions: JsonDict = {}
        sector_rows: dict[str, JsonDict] = {}
        risk_rows: list[JsonDict] = []
        structure_evidence_ids: list[str] = []
        factors = getattr(self, "factors", None)
        asset_ids = tuple(score.asset_id for score in scores)
        factor_rows = (
            factors.list_latest_factor_frames(
                asset_ids=asset_ids,
                horizon=horizon,
                as_of=decision_as_of,
            )
            if factors is not None and hasattr(factors, "list_latest_factor_frames")
            else load_factor_rows_individually(
                factors=factors,
                scores=scores,
                horizon=horizon,
                as_of=decision_as_of,
            )
        )
        factor_by_asset = {str(row.asset_id): row for row in factor_rows}
        indicators = getattr(self, "indicators", None)
        structure_rows = (
            indicators.list_latest_indicator_frames(
                asset_ids=asset_ids,
                timeframes=("1d", "60m"),
                horizons=STRUCTURAL_LITE_HORIZONS,
                library=STRUCTURAL_LITE_LIBRARY,
                as_of=decision_as_of,
            )
            if indicators is not None
            and hasattr(indicators, "list_latest_indicator_frames")
            else []
        )
        structure_by_asset: dict[str, list[Any]] = {}
        for row in structure_rows:
            structure_by_asset.setdefault(str(row.asset_id), []).append(row)
        status_rows = (
            self.assets.list_latest_statuses(
                asset_ids=asset_ids,
                as_of=decision_as_of,
            )
            if hasattr(self.assets, "list_latest_statuses")
            else []
        )
        status_by_asset = {str(row.asset_id): row for row in status_rows}
        price_rows = (
            self.market_data.list_latest_closed_bars(
                asset_ids=asset_ids,
                timeframe="1d",
                as_of=decision_as_of,
            )
            if hasattr(self, "market_data")
            and hasattr(self.market_data, "list_latest_closed_bars")
            else []
        )
        price_by_asset = {str(row.asset_id): row for row in price_rows}
        for score in scores:
            factor = factor_by_asset.get(score.asset_id)
            frames = (
                compact_decision_structure_frames(structure_by_asset[score.asset_id])
                if score.asset_id in structure_by_asset
                else build_asset_decision_structure_frames(
                    indicators=indicators,
                    asset_id=score.asset_id,
                    timeframes=("60m", "1d"),
                    as_of=decision_as_of,
                )
            )
            asset = build_adaptive_snapshot_asset(
                score=score,
                factor=factor,
                frames=frames,
                decision_as_of=decision_as_of,
                trading_status=status_by_asset.get(score.asset_id),
                price_bar=price_by_asset.get(score.asset_id),
            )
            assets.append(asset)
            factor_id = str(getattr(factor, "factor_frame_id", "") or "")
            if factor_id:
                data_versions[f"{score.asset_id}:factor_frame_id"] = factor_id
            price_bar = price_by_asset.get(score.asset_id)
            if price_bar is not None:
                data_versions[f"{score.asset_id}:price_as_of"] = (
                    price_bar.timestamp.isoformat()
                )
            sector_id = str(asset.get("sector_id") or "")
            if sector_id and sector_id != "sector:unknown":
                sector_rows[sector_id] = {
                    "sector_id": sector_id,
                    "sector_regime": asset.get("sector_regime"),
                }
            risk_rows.append(
                {
                    "asset_id": score.asset_id,
                    "downside_risk": asset.get("downside_risk"),
                }
            )
            structure_evidence_ids.extend(
                str(frame.get("evidence_id") or "")
                for frame in frames
                if str(frame.get("evidence_id") or "")
            )

        snapshot_repository = getattr(self, "snapshots", None)
        persisted_market = (
            snapshot_repository.get_latest(snapshot_type="market_regime", market=market)
            if market_regime is None
            and snapshot_repository is not None
            and hasattr(snapshot_repository, "get_latest")
            else None
        )
        persisted_sector = (
            snapshot_repository.get_latest(
                snapshot_type="sector_opportunities",
                market=market,
            )
            if snapshot_repository is not None
            and hasattr(snapshot_repository, "get_latest")
            else None
        )
        market_problem = persisted_context_problem(
            persisted_market,
            decision_as_of=decision_as_of,
            context_name="market",
        )
        sector_problem = persisted_context_problem(
            persisted_sector,
            decision_as_of=decision_as_of,
            context_name="sector",
        )
        normalized_regime = normalize_adaptive_market_regime(
            dict(persisted_market.payload or {})
            if persisted_market is not None and market_problem is None
            else market_regime
        )
        if market_problem is not None:
            normalized_regime = normalize_adaptive_market_regime(None)
            normalized_regime["reason_codes"] = list(
                dict.fromkeys(
                    [
                        *normalized_regime.get("reason_codes", []),
                        market_problem,
                    ]
                )
            )
        market_fact = DecisionFact(
            data_snapshot_id=(
                str(persisted_market.data_snapshot_id)
                if persisted_market is not None and market_problem is None
                else decision_fact_id(
                    "market",
                    payload=normalized_regime,
                    as_of=decision_as_of,
                )
            ),
            as_of=(
                persisted_market.as_of
                if persisted_market is not None and market_problem is None
                else decision_as_of
            ),
            quality_status="available",
            payload=normalized_regime,
        )
        if persisted_market is not None:
            data_versions["market_snapshot_id"] = str(
                persisted_market.data_snapshot_id
            )

        derived_sector_payload = tuple(sector_rows[key] for key in sorted(sector_rows))
        persisted_sector_payload = (
            (persisted_sector.payload or {}).get("sector_opportunities")
            if persisted_sector is not None
            else None
        )
        sector_payload = (
            tuple(
                dict(item)
                for item in persisted_sector_payload
                if isinstance(item, dict)
            )
            if sector_problem is None
            and isinstance(persisted_sector_payload, list | tuple)
            else derived_sector_payload
        )
        if market_problem is not None or sector_problem is not None:
            context_reasons = [
                reason
                for reason in (market_problem, sector_problem)
                if reason is not None
            ]
            for asset in assets:
                asset["quality_status"] = "partial"
                asset["context_reason_codes"] = context_reasons
        sector_fact = DecisionFact(
            data_snapshot_id=(
                str(persisted_sector.data_snapshot_id)
                if persisted_sector is not None and sector_problem is None
                else decision_fact_id(
                    "sector",
                    payload=sector_payload,
                    as_of=decision_as_of,
                )
            ),
            as_of=(
                persisted_sector.as_of
                if persisted_sector is not None and sector_problem is None
                else decision_as_of
            ),
            quality_status="available",
            payload=sector_payload,
        )
        if persisted_sector is not None:
            data_versions["sector_snapshot_id"] = str(
                persisted_sector.data_snapshot_id
            )
        structure_fact = DecisionFact(
            data_snapshot_id=decision_fact_id(
                "structure",
                payload=tuple(sorted(set(structure_evidence_ids))),
                as_of=decision_as_of,
            ),
            as_of=decision_as_of,
            quality_status="available",
            payload=tuple(sorted(set(structure_evidence_ids))),
        )
        risk_fact = DecisionFact(
            data_snapshot_id=decision_fact_id(
                "risk",
                payload=risk_rows,
                as_of=decision_as_of,
            ),
            as_of=decision_as_of,
            quality_status="available",
            payload=risk_rows,
        )
        builder = DecisionSnapshotBuilder(
            repository=getattr(self, "snapshots", None),
        )
        built = builder.build(
            DecisionSnapshotInputs(
                market=market,
                as_of=decision_as_of,
                market_regime=market_fact,
                sector_opportunities=sector_fact,
                structure=structure_fact,
                risk=risk_fact,
                assets=tuple(assets),
                data_versions=data_versions,
                previous_assets=previous_assets or {},
            )
        )
        if built.snapshot is None:
            reasons = ", ".join(built.reason_codes)
            raise ValueError(f"统一决策快照被点时门控阻止: {reasons}")
        return built.snapshot

    def load_portfolio_context(
        self,
        *,
        owner_id: str,
        market: str,
        market_regime: JsonDict,
        explicit_budget: PortfolioRiskBudget | None,
        previous_states: dict[str, RecommendationState],
    ) -> tuple[
        tuple[PortfolioPosition, ...],
        PortfolioRiskBudget,
        dict[str, datetime],
    ]:
        """批量读取用户真实持仓，并解析组合权益和交易约束。"""

        repository = getattr(self, "portfolios", None)
        if repository is None:
            return (
                (),
                explicit_budget or _portfolio_budget_from_market_regime(market_regime),
                {},
            )
        portfolios = repository.list_portfolios(owner_id=owner_id, status="active")
        rows = repository.list_active_positions_by_owner(
            owner_id=owner_id,
            market=market,
        )
        positions = tuple(portfolio_position_from_row(row) for row in rows)
        all_rows = (
            repository.list_positions_by_owner(
                owner_id=owner_id,
                market=market,
                status=None,
            )
            if hasattr(repository, "list_positions_by_owner")
            else rows
        )
        active_assets = {position.asset_id for position in positions}
        latest_closed: dict[str, datetime] = {}
        for row in all_rows:
            asset_id = str(getattr(row, "asset_id", "") or "")
            closed_at = getattr(row, "as_of", None)
            if (
                not asset_id
                or asset_id in active_assets
                or str(getattr(row, "status", "active")) != "closed"
                or not isinstance(closed_at, datetime)
            ):
                continue
            current = latest_closed.get(asset_id)
            if current is None or closed_at > current:
                latest_closed[asset_id] = closed_at
        closed_assets: dict[str, datetime] = {}
        for asset_id, closed_at in latest_closed.items():
            previous = previous_states.get(asset_id)
            consumed_at = parse_iso_datetime(
                (previous.payload if previous is not None else {}).get(
                    "closed_position_as_of"
                )
            )
            if consumed_at is None or closed_at > consumed_at:
                closed_assets[asset_id] = closed_at
        if explicit_budget is not None:
            return positions, explicit_budget, closed_assets
        equity = sum(
            (
                Decimal(str(portfolio.total_equity))
                for portfolio in portfolios
                if getattr(portfolio, "total_equity", None) is not None
            ),
            Decimal("0"),
        )
        base = _portfolio_budget_from_market_regime(market_regime)
        weekly_turnover = max(
            (
                float((getattr(portfolio, "payload", {}) or {}).get("weekly_turnover_ratio", 0))
                for portfolio in portfolios
            ),
            default=0.0,
        )
        if equity <= 0:
            return positions, PortfolioRiskBudget(
                equity=Decimal("1"),
                total_exposure=0.0,
                per_position_risk=0.0,
                allow_new_buys=False,
                max_sector_exposure=base.max_sector_exposure,
                minimum_positions=base.minimum_positions,
                maximum_positions=base.maximum_positions,
                weekly_turnover_ratio=weekly_turnover,
                maximum_weekly_turnover=base.maximum_weekly_turnover,
                weak_market_sector_override=False,
            ), closed_assets
        return positions, PortfolioRiskBudget(
            equity=equity,
            total_exposure=base.total_exposure,
            per_position_risk=base.per_position_risk,
            allow_new_buys=base.allow_new_buys,
            max_sector_exposure=base.max_sector_exposure,
            minimum_positions=base.minimum_positions,
            maximum_positions=base.maximum_positions,
            weekly_turnover_ratio=weekly_turnover,
            maximum_weekly_turnover=base.maximum_weekly_turnover,
            weak_market_sector_override=base.weak_market_sector_override,
        ), closed_assets

    def asset_name(self, asset_id: str, *, fallback_symbol: str) -> str:
        """查询资产名称，缺失时用 symbol 兜底。"""

        asset = self.assets.get_asset_or_none(asset_id)
        return asset.name if asset else fallback_symbol

    def expand_adaptive_scores_with_open_assets(
        self,
        scores: list[Any],
        *,
        owner_id: str,
        strategy_id: str,
        market: str,
        horizon: str,
    ) -> list[Any]:
        """把筛选池外的开放生命周期和持仓资产并入状态维护截面。"""

        existing_ids = {str(score.asset_id) for score in scores}
        fallback_as_of = max(
            (
                score.as_of
                for score in scores
                if isinstance(getattr(score, "as_of", None), datetime)
            ),
            default=datetime.now(tz=UTC),
        )
        state_repository = getattr(self, "lifecycle_states", None)
        open_states = (
            state_repository.list_open_states(
                owner_id=owner_id,
                strategy_id=strategy_id,
            )
            if state_repository is not None
            and hasattr(state_repository, "list_open_states")
            else ()
        )
        portfolio_repository = getattr(self, "portfolios", None)
        position_rows = (
            portfolio_repository.list_positions_by_owner(
                owner_id=owner_id,
                market=market,
                status=None,
            )
            if portfolio_repository is not None
            and hasattr(portfolio_repository, "list_positions_by_owner")
            else ()
        )
        candidates: dict[str, tuple[JsonDict, Any | None]] = {}
        for state in open_states:
            payload = dict(getattr(state, "payload", {}) or {})
            decision_asset = payload.get("decision_asset")
            candidates[str(state.asset_id)] = (
                dict(decision_asset) if isinstance(decision_asset, dict) else {},
                state,
            )
        for row in position_rows:
            asset_id = str(getattr(row, "asset_id", "") or "")
            if not asset_id:
                continue
            candidates.setdefault(
                asset_id,
                (
                    {
                        "asset_id": asset_id,
                        "symbol": str(getattr(row, "symbol", "") or asset_id.split(":")[-1]),
                    },
                    None,
                ),
            )
        result = list(scores)
        for asset_id in sorted(candidates):
            if asset_id in existing_ids:
                continue
            decision_asset, _state = candidates[asset_id]
            symbol = str(decision_asset.get("symbol") or asset_id.split(":")[-1])
            result.append(
                LifecycleScoreView(
                    score_id=f"lifecycle-score:{strategy_id}:{asset_id}",
                    asset_id=asset_id,
                    symbol=symbol,
                    market=market,
                    horizon=horizon,
                    total_score=Decimal(str(decision_asset.get("alpha_score") or 0)),
                    confidence=Decimal(str(decision_asset.get("confidence") or 0)),
                    factor_frame_id=(
                        str(decision_asset["factor_frame_id"])
                        if decision_asset.get("factor_frame_id")
                        else None
                    ),
                    as_of=fallback_as_of,
                    payload={
                        "strategy_id": strategy_id,
                        "lifecycle_maintenance": True,
                    },
                    rank=len(result) + 1,
                )
            )
        return result

    def asset_names(self, asset_ids: tuple[str, ...]) -> dict[str, str]:
        """批量读取资产名称；旧测试适配器回退到单条查询。"""

        if hasattr(self.assets, "find_by_ids"):
            return {
                str(asset.asset_id): str(asset.name or asset.symbol)
                for asset in self.assets.find_by_ids(asset_ids)
            }
        return {
            asset_id: self.asset_name(asset_id, fallback_symbol=asset_id.split(":")[-1])
            for asset_id in asset_ids
        }


def build_recommendation_payload(
    *,
    score: AssetScoreORM,
    signal: SignalSnapshotORM | None,
    risks: list[RiskFindingORM],
    asset_name: str,
    rank: int,
    run_id: str,
    rule_version: str,
    backtest_evidence: JsonDict | None = None,
    decision_context: RecommendationDecisionContext | None = None,
    structure_evidence: JsonDict | None = None,
    trial_state: str | None = None,
    validation_evidence_id: str | None = None,
) -> JsonDict:
    """构建单标的推荐 payload。"""

    proposed_action = decide_action(
        score=score,
        signal=signal,
        risks=risks,
        decision_context=decision_context,
    )
    structure_verdict = None
    action = proposed_action
    if structure_evidence is not None:
        structure_frames = structure_evidence.get("structure_frames")
        structure_verdict = StructuralDecisionEngine().evaluate(
            frames=structure_frames if isinstance(structure_frames, list) else (),
            current_price=_optional_decimal(
                score.payload.get("last_price") or score.payload.get("current_price")
            ),
        )
        if proposed_action == "buy_candidate" and not structure_verdict.buy_allowed:
            action = "watch"
    conviction = decide_conviction(score=score)
    signal_ids = [signal.signal_id] if signal else []
    risk_ids = [risk.risk_id for risk in risks]
    evidence_ids = sorted({evidence_id for risk in risks for evidence_id in risk.evidence_ids})
    missing_data = list(score.payload.get("missing_groups") or [])
    reasons = build_reasons(score=score, signal=signal)
    risk_rebuttals = build_risk_rebuttals(score=score, signal=signal, risks=risks)
    append_context_reasons(
        reasons=reasons,
        decision_context=decision_context,
    )
    append_context_rebuttals(
        risk_rebuttals=risk_rebuttals,
        tradability=decision_context.tradability if decision_context else None,
        memory_adjustment=score.payload.get("memory_ranking_adjustment"),
    )
    watch_conditions = build_watch_conditions(signal=signal, score=score)
    append_tradability_watch_condition(
        watch_conditions=watch_conditions,
        tradability=decision_context.tradability if decision_context else None,
    )
    invalid_if = build_invalid_if(signal=signal, risks=risks)
    summary = build_asset_summary(
        symbol=score.symbol,
        action=action,
        total_score=float(score.total_score),
        confidence=float(score.confidence),
    )

    payload = {
        "schema_version": "1.0",
        "rule_version": rule_version,
        "recommendation_id": build_recommendation_id(
            run_id=run_id,
            asset_id=score.asset_id,
            horizon=score.horizon,
        ),
        "asset_id": score.asset_id,
        "symbol": score.symbol,
        "name": asset_name,
        "market": score.market,
        "horizon": score.horizon,
        "action": action,
        "proposed_action": proposed_action,
        "rank": rank,
        "total_score": float(score.total_score),
        "conviction": conviction,
        "confidence": float(score.confidence),
        "summary": summary,
        "score_id": score.score_id,
        "factor_frame_id": score.factor_frame_id,
        "signal_ids": signal_ids,
        "risk_ids": risk_ids,
        "evidence_ids": evidence_ids,
        "reasons": reasons,
        "risk_rebuttals": risk_rebuttals,
        "watch_conditions": watch_conditions,
        "invalid_if": invalid_if,
        "missing_data": missing_data,
        "score_strategy_id": getattr(score, "strategy_id", None)
        or score.payload.get("strategy_id"),
        "score_weight_snapshot": score.payload.get("weight_snapshot"),
        "backtest_evidence": backtest_evidence,
        "tradability": decision_context.tradability if decision_context else None,
        "memory_ranking_adjustment": score.payload.get("memory_ranking_adjustment"),
        "decision_context": decision_context_payload(decision_context),
        "trial": trial_state == "trial",
        "validation_state": trial_state,
        "validation_evidence_id": validation_evidence_id,
    }
    if structure_evidence is not None:
        payload["structure"] = structure_evidence
        payload["structure_verdict"] = structure_verdict.to_dict() if structure_verdict else None
    return payload


def build_adaptive_recommendation_payload(
    *,
    score: AssetScoreORM,
    decision: AdaptiveAssetDecision | None,
    decision_snapshot: DecisionSnapshot | None,
    asset_name: str,
    rank: int,
    run_id: str,
    rule_version: str,
    backtest_evidence: JsonDict,
    trial_state: str | None,
    validation_evidence_id: str | None,
) -> JsonDict:
    """直接从统一决策构造推荐，避免旧分位动作污染自适应审计。"""

    snapshot_id = decision_snapshot.decision_snapshot_id if decision_snapshot else None
    if decision is None:
        action = "watch"
        recommendation_state = "watch"
        previous_state = None
        state_changed_at = decision_snapshot.as_of if decision_snapshot else score.as_of
        reason_codes = ("decision_snapshot_asset_missing",)
        structure_payload: JsonDict = {"status": "no_structure_evidence"}
        structure_verdict = None
        expected_return = None
        downside_risk = None
        alpha_score = float(score.total_score)
        confidence = float(score.confidence)
        setup_id = None
        raw_payload: JsonDict = {}
    else:
        action = decision.action
        recommendation_state = decision.transition.to_state
        previous_state = decision.transition.from_state
        state_changed_at = decision.transition.occurred_at
        reason_codes = decision.reason_codes
        structure_payload = {
            "library": STRUCTURAL_LITE_LIBRARY,
            "structure_frames": list(decision.payload.get("structure_frames") or []),
        }
        structure_verdict = decision.structure.to_dict()
        expected_return = decision.alpha.expected_net_return
        downside_risk = decision.alpha.downside_risk
        alpha_score = decision.alpha.alpha_score
        confidence = decision.alpha.confidence
        setup_id = decision.transition.setup_id
        raw_payload = decision.payload
    summary = build_asset_summary(
        symbol=score.symbol,
        action=action,
        total_score=alpha_score,
        confidence=confidence,
    )
    evidence_ids = sorted(
        set(
            (structure_verdict or {}).get("primary_evidence_ids", [])
            + (structure_verdict or {}).get("auxiliary_evidence_ids", [])
        )
    )
    return {
        "schema_version": "2.0",
        "rule_version": rule_version,
        "recommendation_id": build_recommendation_id(
            run_id=run_id,
            asset_id=score.asset_id,
            horizon=score.horizon,
        ),
        "asset_id": score.asset_id,
        "symbol": score.symbol,
        "name": asset_name,
        "market": score.market,
        "horizon": score.horizon,
        "action": action,
        "intended_action": decision.intended_action if decision is not None else None,
        "execution_status": (
            decision.execution_status if decision is not None else "not_applicable"
        ),
        "proposed_action": action,
        "action_source": "adaptive_lifecycle_portfolio",
        "rank": rank,
        "total_score": alpha_score,
        "conviction": decide_conviction(score=score),
        "confidence": confidence,
        "summary": summary,
        "score_id": score.score_id,
        "factor_frame_id": score.factor_frame_id,
        "signal_ids": [],
        "risk_ids": [],
        "evidence_ids": evidence_ids,
        "reasons": [f"生命周期原因：{code}" for code in reason_codes],
        "risk_rebuttals": [],
        "watch_conditions": {
            "conditions": [f"等待条件变化：{code}" for code in reason_codes],
            "score_id": score.score_id,
        },
        "invalid_if": {
            "conditions": [
                f"结构失效价：{(structure_verdict or {}).get('invalidation_price')}"
            ]
            if (structure_verdict or {}).get("invalidation_price") is not None
            else []
        },
        "missing_data": list(raw_payload.get("missing_groups") or []),
        "score_strategy_id": ADAPTIVE_STRATEGY_ID,
        "score_weight_snapshot": score.payload.get("weight_snapshot"),
        "backtest_evidence": backtest_evidence,
        "tradability": {
            "tradable": bool(raw_payload.get("tradable", False)),
            "reasons": list(raw_payload.get("tradability_reasons") or []),
        },
        "memory_ranking_adjustment": score.payload.get("memory_ranking_adjustment"),
        "decision_context": None,
        "trial": trial_state == "trial",
        "validation_state": trial_state,
        "validation_evidence_id": validation_evidence_id,
        "recommendation_state": recommendation_state,
        "previous_state": previous_state,
        "state_changed_at": state_changed_at.isoformat(),
        "decision_snapshot_id": snapshot_id,
        "setup_id": setup_id,
        "planned_horizon_days": int(raw_payload.get("planned_horizon_days") or 10),
        "sector_regime": str(raw_payload.get("sector_regime") or "unknown"),
        "structure": structure_payload,
        "structure_verdict": structure_verdict,
        "entry_zone": (structure_verdict or {}).get("entry_zone"),
        "invalidation_price": (structure_verdict or {}).get("invalidation_price"),
        "target_price": (structure_verdict or {}).get("target_price"),
        "expected_net_return": expected_return,
        "downside_risk": downside_risk,
        "alpha_score": alpha_score,
        "data_quality": decision.data_quality if decision is not None else "unavailable",
        "lifecycle_reason_codes": list(reason_codes),
    }


def _recommendation_state_from_row(row: Any | None) -> RecommendationState | None:
    """把 ORM 或测试适配器返回值转换为纯生命周期状态。"""

    if row is None:
        return None
    if isinstance(row, RecommendationState):
        return row
    return RecommendationState(
        state_id=str(row.state_id),
        owner_id=str(row.owner_id),
        strategy_id=str(row.strategy_id),
        asset_id=str(row.asset_id),
        setup_id=str(row.setup_id) if row.setup_id is not None else None,
        current_state=str(row.current_state),  # type: ignore[arg-type]
        previous_state=(
            str(row.previous_state) if row.previous_state is not None else None
        ),  # type: ignore[arg-type]
        decision_snapshot_id=str(row.decision_snapshot_id),
        state_changed_at=row.state_changed_at,
        consecutive_valid_closes=int(row.consecutive_valid_closes),
        active_days=int(row.active_days),
        cooldown_until=row.cooldown_until,
        payload=dict(row.payload or {}),
    )


def stock_setup_from_adaptive_decision(
    decision: AdaptiveAssetDecision,
    *,
    owner_id: str,
    strategy_id: str,
) -> StockSetup | None:
    """把具备完整风险位的自适应决策转换为可审计股票设置。"""

    entry_zone = decision.structure.entry_zone
    setup_id = decision.transition.setup_id
    if setup_id is None or entry_zone is None or decision.structure.invalidation_price is None:
        return None
    target = decision.structure.target_price
    return StockSetup(
        setup_id=setup_id,
        owner_id=owner_id,
        decision_snapshot_id=decision.decision_snapshot_id,
        asset_id=decision.asset_id,
        strategy_id=strategy_id,
        setup_type=str(decision.payload.get("setup_type") or "structure_confirmed"),
        planned_horizon_days=int(decision.payload.get("planned_horizon_days") or 10),
        entry_zone={"low": str(entry_zone[0]), "high": str(entry_zone[1])},
        invalidation_price=decision.structure.invalidation_price,
        target_zone=(
            {"low": str(target), "high": str(target)} if target is not None else {}
        ),
        expected_net_return=Decimal(str(decision.alpha.expected_net_return)),
        downside_risk=Decimal(str(decision.alpha.downside_risk)),
        confidence=Decimal(str(decision.alpha.confidence)),
        as_of=decision.transition.occurred_at,
        payload={
            "structure_verdict": decision.structure.to_dict(),
            "lifecycle_reason_codes": list(decision.reason_codes),
        },
    )


def _portfolio_budget_from_market_regime(
    market_regime: JsonDict,
) -> PortfolioRiskBudget:
    """把市场风险预算转换为仅用于推荐容量裁决的标准名义组合。"""

    raw = market_regime.get("risk_budget")
    risk_budget = raw if isinstance(raw, dict) else {}
    return PortfolioRiskBudget(
        equity=Decimal("1000000"),
        total_exposure=max(
            0.0,
            min(1.0, float(risk_budget.get("total_exposure", 1.0))),
        ),
        per_position_risk=max(
            0.0,
            min(1.0, float(risk_budget.get("per_position_risk", 0.01))),
        ),
        allow_new_buys=bool(risk_budget.get("allow_new_buys", True)),
        weak_market_sector_override=bool(
            risk_budget.get("allow_sector_override", False)
            and str(market_regime.get("regime") or "") == "trend_down"
        ),
    )


def portfolio_position_from_row(row: Any) -> PortfolioPosition:
    """把持仓 ORM 转换为组合裁决所需的保守事实。"""

    payload = dict(getattr(row, "payload", {}) or {})
    quantity = Decimal(str(getattr(row, "quantity", 0) or 0))
    sellable = Decimal(str(payload.get("sellable_quantity", 0) or 0))
    price_raw = getattr(row, "last_price", None) or getattr(row, "avg_cost", None)
    price = Decimal(str(price_raw or 0))
    tradability_reasons = tuple(
        str(item) for item in payload.get("tradability_reasons", ()) if str(item)
    )
    return PortfolioPosition(
        position_id=str(row.position_id),
        asset_id=str(row.asset_id),
        sector_id=str(payload.get("sector_id") or "sector:unknown"),
        quantity=quantity,
        sellable_quantity=sellable,
        price=price,
        expected_net_return=float(payload.get("expected_net_return", 0.0) or 0.0),
        required_action="hold",
        tradable=bool(payload.get("tradable", price > 0)),
        tradability_reasons=tradability_reasons,
    )


def validate_trial_audit(
    *,
    trial_state: str | None,
    validation_evidence_id: str | None,
) -> None:
    """试运行/已验证推荐必须携带对应历史验证证据。"""

    if trial_state is None:
        return
    if trial_state not in {"trial", "validated"}:
        raise ValueError(f"不允许为策略状态 {trial_state} 生成推荐")
    if not validation_evidence_id:
        raise ValueError("试运行或已验证推荐缺少 validation_evidence_id")


def build_asset_structure_payload(
    *,
    indicators: Any,
    asset_id: str,
    timeframe: str,
) -> JsonDict:
    """读取 structural-lite 最新帧并生成推荐 payload 的精简结构证据。"""

    if indicators is None or not hasattr(indicators, "get_latest_indicator_frame"):
        return {"status": "no_structure_evidence"}
    frames: list[JsonDict] = []
    for horizon in STRUCTURAL_LITE_HORIZONS:
        frame = indicators.get_latest_indicator_frame(
            asset_id=asset_id,
            timeframe=timeframe,
            horizon=horizon,
            library=STRUCTURAL_LITE_LIBRARY,
        )
        if frame is None:
            continue
        compact = compact_structure_frame(frame)
        if compact is not None:
            frames.append(compact)
    if not frames or all(is_insufficient_structure_status(frame["status"]) for frame in frames):
        return {"status": "no_structure_evidence"}
    return {
        "library": STRUCTURAL_LITE_LIBRARY,
        "structure_frames": frames,
    }


def build_asset_decision_structure_frames(
    *,
    indicators: Any,
    asset_id: str,
    timeframes: tuple[str, ...],
    as_of: datetime,
) -> tuple[JsonDict, ...]:
    """读取结构决策所需字段，不把完整结构算法中间量带入快照。"""

    if indicators is None or not hasattr(indicators, "get_latest_indicator_frame"):
        return ()
    rows = []
    for timeframe in timeframes:
        for horizon in STRUCTURAL_LITE_HORIZONS:
            frame = indicators.get_latest_indicator_frame(
                asset_id=asset_id,
                timeframe=timeframe,
                horizon=horizon,
                library=STRUCTURAL_LITE_LIBRARY,
                as_of=as_of,
            )
            if frame is not None:
                rows.append(frame)
    return compact_decision_structure_frames(rows)


def compact_decision_structure_frames(rows: Sequence[Any]) -> tuple[JsonDict, ...]:
    """压缩一批已经按点时读取的结构帧。"""

    frames: list[JsonDict] = []
    for frame in rows:
        payload = dict(getattr(frame, "payload", {}) or {})
        timeframe = str(
            getattr(frame, "timeframe", None) or payload.get("timeframe") or "1d"
        )
        horizon = str(
            getattr(frame, "horizon", None) or payload.get("schema_version") or ""
        )
        if not horizon:
            continue
        raw_as_of = parse_iso_datetime(getattr(frame, "as_of", None)) or parse_iso_datetime(
            payload.get("input_end_at")
        )
        effective_as_of = (
            _normalize_fact_time(raw_as_of, market="ashare")
            if timeframe == "1d"
            else raw_as_of
        )
        compact: JsonDict = {
            "horizon": horizon,
            "timeframe": timeframe,
            "status": str(payload.get("status") or getattr(frame, "status", "unknown")),
            "confidence": normalize_structure_confidence(
                payload.get("confidence", getattr(frame, "confidence", 0))
            ),
            "evidence_id": str(
                payload.get("evidence_id") or getattr(frame, "evidence_id", "") or ""
            ),
            "as_of": effective_as_of.isoformat() if effective_as_of is not None else "",
        }
        for key in (
            "direction",
            "setup",
            "entry_setup",
            "entry_zone",
            "invalidation_price",
            "target_price",
            "input_end_at",
            "swings",
            "signals",
            "lines",
            "structure_events",
            "fair_value_gaps",
            "latest_bar",
            "segments",
        ):
            if key in payload:
                compact[key] = _json_safe(payload[key])
        frames.append(compact)
    return tuple(frames)


def load_factor_rows_individually(
    *,
    factors: Any,
    scores: Sequence[AssetScoreORM],
    horizon: str,
    as_of: datetime,
) -> list[Any]:
    """兼容测试适配器；生产仓储始终走批量点时查询。"""

    if factors is None:
        return []
    rows: list[Any] = []
    for score in scores:
        try:
            row = factors.get_latest_factor_frame(
                asset_id=score.asset_id,
                horizon=horizon,
                as_of=as_of,
            )
        except TypeError:
            row = factors.get_latest_factor_frame(
                asset_id=score.asset_id,
                horizon=horizon,
            )
        if row is not None:
            rows.append(row)
    return rows


def build_adaptive_snapshot_asset(
    *,
    score: AssetScoreORM,
    factor: Any | None,
    frames: tuple[JsonDict, ...],
    decision_as_of: datetime,
    trading_status: Any | None,
    price_bar: Any | None,
) -> JsonDict:
    """把旧因子帧适配为六组自适应 Alpha 和结构门控输入。"""

    factor_payload = dict(getattr(factor, "payload", {}) or {})
    raw_groups = factor_payload.get("factor_groups")
    groups = {
        str(item.get("group")): dict(item)
        for item in raw_groups
        if isinstance(item, dict) and str(item.get("group") or "")
    } if isinstance(raw_groups, list) else {}
    price_bar_as_of = _normalize_fact_time(
        getattr(price_bar, "timestamp", None),
        market="ashare",
    )
    price_is_current = (
        isinstance(price_bar_as_of, datetime)
        and price_bar_as_of <= decision_as_of
        and decision_as_of - price_bar_as_of <= timedelta(minutes=5)
    )
    current_price = (
        _optional_decimal(getattr(price_bar, "close", None))
        if price_is_current
        else None
    )
    structure = StructuralDecisionEngine().evaluate(
        frames=frames,
        current_price=current_price,
    )
    group_sources = {
        "trend": ("technical",),
        "sector_leadership": ("sector_strength", "leadership"),
        "capital_flow": ("capital_flow",),
        "fundamental_valuation": ("fundamental", "valuation"),
        "tradability_return_risk": ("liquidity", "risk"),
    }
    group_scores: JsonDict = {
        name: average_group_score(groups, source_names)
        for name, source_names in group_sources.items()
    }
    group_scores["structure"] = structure_score(structure)
    missing_groups = [name for name, value in group_scores.items() if value is None]
    partial_groups = [
        name
        for name, source_names in group_sources.items()
        if name not in missing_groups
        and any(str(groups.get(source, {}).get("status")) == "partial" for source in source_names)
    ]
    if frames and structure.status == "waiting":
        partial_groups.append("structure")
    daily_structure_times = [
        parsed
        for frame in frames
        if str(frame.get("timeframe") or "").lower() == "1d"
        and str(frame.get("horizon") or "") in {
            "structural_swings_v2",
            "smc_lite_v2",
            "ichimoku_v1",
        }
        and (parsed := parse_iso_datetime(frame.get("as_of"))) is not None
    ]
    intraday_structure_times = [
        parsed
        for frame in frames
        if str(frame.get("timeframe") or "").lower() in {"60m", "1h", "hourly"}
        and str(frame.get("horizon") or "") == "smc_lite_v2"
        and (parsed := parse_iso_datetime(frame.get("as_of"))) is not None
    ]
    sector_group = groups.get("sector_strength", {})
    sector_factors = (
        dict(sector_group.get("factors") or {})
        if isinstance(sector_group.get("factors"), dict)
        else {}
    )
    risk_score = average_group_score(groups, ("risk",))
    downside_risk = (
        max(0.0, (100.0 - risk_score) / 1000.0)
        if risk_score is not None
        else 0.10
    )
    status_as_of = getattr(trading_status, "as_of", None)
    status_is_current = (
        isinstance(status_as_of, datetime)
        and status_as_of.date() == decision_as_of.date()
    )
    trading_status_name = str(
        getattr(trading_status, "trading_status", "missing") or "missing"
    ).lower()
    tradable = bool(getattr(trading_status, "tradable", False)) and status_is_current
    tradability_reasons: list[str] = []
    if trading_status is None:
        tradability_reasons.append("trading_status_missing")
    elif not status_is_current:
        tradability_reasons.append("trading_status_stale")
    if trading_status_name in {"suspended", "st", "delisted", "unavailable"}:
        tradability_reasons.append(trading_status_name)
        tradable = False
    explicit_reason = str(getattr(trading_status, "reason", "") or "").strip()
    if explicit_reason and not tradable:
        tradability_reasons.append(explicit_reason)
    factor_as_of = _normalize_fact_time(getattr(factor, "as_of", None), market="ashare")
    quality_status = str(getattr(factor, "status", None) or "unavailable")
    normalized_decision_as_of = (
        decision_as_of
        if decision_as_of.tzinfo is not None
        else decision_as_of.replace(tzinfo=UTC)
    )
    required_structure_times = (
        max(daily_structure_times) if daily_structure_times else None,
        max(intraday_structure_times) if intraday_structure_times else None,
    )
    if any(
        structure_time is None
        or structure_time > normalized_decision_as_of
        or normalized_decision_as_of - structure_time > timedelta(minutes=5)
        for structure_time in required_structure_times
    ):
        quality_status = "partial"
        partial_groups.append("structure")
    if not tradable:
        quality_status = "partial"
        partial_groups.append("tradability_return_risk")
    if current_price is None:
        quality_status = "partial"
        partial_groups.append("tradability_return_risk")
    return {
        "asset_id": score.asset_id,
        "symbol": score.symbol,
        "quality_status": quality_status,
        "as_of": factor_as_of or decision_as_of,
        "group_scores": group_scores,
        "factor_as_of": {
            name: factor_as_of or decision_as_of for name in group_scores
        },
        "missing_groups": missing_groups,
        "partial_groups": tuple(dict.fromkeys(partial_groups)),
        "expected_return_hint": score.payload.get("expected_return_hint"),
        "downside_risk": downside_risk,
        "structure_frames": frames,
        "structure_invalidated": structure.status == "invalidated",
        "current_price": str(current_price) if current_price is not None else None,
        "entry_threshold": 70.0,
        "retention_threshold": 58.0,
        "sector_id": str(sector_factors.get("sector_id") or "sector:unknown"),
        "sector_regime": str(sector_factors.get("sector_regime") or "unknown"),
        "tradable": tradable,
        "tradability_reasons": tuple(dict.fromkeys(tradability_reasons)),
        "planned_horizon_days": int(score.payload.get("planned_horizon_days") or 10),
        "trade_date": decision_as_of.date().isoformat(),
    }


def parse_iso_datetime(value: Any) -> datetime | None:
    """解析结构证据时点并统一为带时区时间。"""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _normalize_fact_time(value: datetime | None, *, market: str) -> datetime | None:
    """把存储层自然日时间转换为真实交易知识时点。"""

    if value is None:
        return None
    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if market == "ashare" and normalized.hour == 0 and normalized.minute == 0:
        return ashare_market_close_at(normalized)
    return normalized


def average_group_score(
    groups: dict[str, JsonDict],
    names: tuple[str, ...],
) -> float | None:
    """返回一组旧因子的简单均值，缺失组不参与但不会被重新伪造。"""

    values = [
        float(value)
        for name in names
        if (value := groups.get(name, {}).get("score")) is not None
    ]
    return sum(values) / len(values) if values else None


def structure_score(verdict: Any) -> float | None:
    """把结构裁决映射为 Alpha 组分，最终买入仍由硬门槛决定。"""

    return {
        "confirmed": 100.0,
        "waiting": 50.0,
        "blocked": 20.0,
        "invalidated": 0.0,
    }.get(str(verdict.status))


def planned_entry_price(frames: tuple[JsonDict, ...]) -> Decimal | None:
    """缺少实时价时使用明确入场区间中点评估计划收益风险。"""

    for frame in frames:
        zone = frame.get("entry_zone")
        if not isinstance(zone, dict):
            continue
        low = _optional_decimal(zone.get("low"))
        high = _optional_decimal(zone.get("high"))
        if low is not None and high is not None:
            return (low + high) / 2
    return None


def normalize_adaptive_market_regime(value: JsonDict | None) -> JsonDict:
    """规范市场状态；缺失时以可审计的 risk_off 保护事实关闭买入。"""

    if not value:
        return {
            "regime": "risk_off",
            "quality_status": "unavailable",
            "reason_codes": ["market_regime_missing"],
            "risk_budget": {
                "total_exposure": 0.0,
                "per_position_risk": 0.0,
                "allow_new_buys": False,
                "allow_sector_override": False,
            },
        }
    result = dict(value)
    result["regime"] = {
        "bull": "trend_up",
        "bear": "trend_down",
    }.get(str(result.get("regime") or "range"), str(result.get("regime") or "range"))
    if not isinstance(result.get("risk_budget"), dict):
        regime = str(result["regime"])
        defaults = {
            "trend_up": (1.0, 0.01, True, True),
            "range": (0.7, 0.008, True, True),
            "trend_down": (0.35, 0.005, True, True),
            "risk_off": (0.0, 0.0, False, False),
        }
        exposure, per_position, allow_buys, allow_override = defaults.get(
            regime,
            defaults["risk_off"],
        )
        result["risk_budget"] = {
            "total_exposure": exposure,
            "per_position_risk": per_position,
            "allow_new_buys": allow_buys,
            "allow_sector_override": allow_override,
        }
    return result


def persisted_context_problem(
    record: Any | None,
    *,
    decision_as_of: datetime,
    context_name: str,
) -> str | None:
    """返回持久化上下文的质量或点时问题。"""

    if record is None:
        return None
    quality = str(getattr(record, "quality_status", "unavailable") or "unavailable")
    if quality != "available":
        return f"{context_name}_quality_{quality}"
    as_of = getattr(record, "as_of", None)
    if not isinstance(as_of, datetime):
        return f"{context_name}_as_of_missing"
    if as_of > decision_as_of:
        return f"{context_name}_future"
    if decision_as_of - as_of > timedelta(minutes=5):
        return f"{context_name}_stale"
    return None


def decision_fact_id(label: str, *, payload: Any, as_of: datetime) -> str:
    """为聚合事实生成可重放 ID。"""

    canonical = json.dumps(
        {"label": label, "as_of": as_of.isoformat(), "payload": _json_safe(payload)},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"decision-fact:{label}:{digest}"


def compact_structure_frame(frame: Any) -> JsonDict | None:
    """把完整 indicator_frame 压缩为前端展示和审计所需的摘要。"""

    payload = frame.payload if isinstance(getattr(frame, "payload", None), dict) else {}
    horizon = str(getattr(frame, "horizon", None) or payload.get("schema_version") or "")
    if not horizon:
        return None
    status = str(payload.get("status") or getattr(frame, "status", None) or "unknown")
    result: JsonDict = {
        "horizon": horizon,
        "timeframe": str(getattr(frame, "timeframe", None) or payload.get("timeframe") or "1d"),
        "status": status,
        "confidence": normalize_structure_confidence(payload.get("confidence", getattr(frame, "confidence", 0))),
        "evidence_id": str(payload.get("evidence_id") or getattr(frame, "evidence_id", "") or ""),
        "as_of": _isoformat(getattr(frame, "as_of", None)) or _isoformat(payload.get("as_of")) or "",
        "items": summarize_structure_items(horizon=horizon, payload=payload),
    }
    return result


def summarize_structure_items(*, horizon: str, payload: JsonDict) -> list[JsonDict]:
    """按引擎类型提取最多三条摘要，完整证据仍通过 evidence_id 回查。"""

    if horizon == "smc_lite_v2":
        return [
            {
                "name": str(item.get("name") or ""),
                "direction": str(item.get("direction") or ""),
                "break_level": _json_safe(item.get("break_level")),
            }
            for item in list_records(payload.get("structure_events"))[:3]
        ]
    if horizon == "harmonic_lite_v2":
        return [
            {
                "pattern": str(item.get("pattern") or ""),
                "direction": str(item.get("direction") or ""),
                "bars_since_d": _json_safe(item.get("bars_since_d")),
            }
            for item in list_records(payload.get("patterns"))[:3]
        ]
    if horizon == "elliott_lite_v2":
        return [
            {
                "pattern": str(item.get("pattern") or ""),
                "signal_hint": str(item.get("signal_hint") or ""),
            }
            for item in list_records(payload.get("candidates"))[:3]
        ]
    if horizon == "structural_swings_v2":
        segments = list_records(payload.get("segments"))
        return [{"direction": str(item.get("direction") or "")} for item in segments[-3:]]
    if horizon == "ichimoku_v1":
        return [
            {
                "name": str(item.get("name") or ""),
                "direction": str(item.get("direction") or ""),
            }
            for item in list_records(payload.get("signals"))[:3]
        ]
    return []


def _optional_decimal(value: Any) -> Decimal | None:
    """把推荐上下文中的可选价格转换为 Decimal。"""

    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def list_records(value: Any) -> list[JsonDict]:
    """只保留字典列表项，避免把完整复杂对象塞入推荐 payload。"""

    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def is_insufficient_structure_status(status: str) -> bool:
    """判断 structural-lite 状态是否不构成可展示结构证据。"""

    return status.startswith("insufficient") or status in {"no_structure_evidence"}


def normalize_structure_confidence(value: Any) -> float:
    """把结构置信度转换成 JSON 友好的浮点数。"""

    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def decision_context_payload(context: RecommendationDecisionContext | None) -> JsonDict | None:
    """输出推荐动作裁决上下文，便于推荐结果解释和审计。"""

    if context is None:
        return None
    return {
        "rank": context.rank,
        "total": context.total,
        "percentile": round(context.percentile, 6),
        "buy_percentile_threshold": context.buy_percentile_threshold,
        "adjusted_buy_percentile_threshold": context.adjusted_buy_percentile_threshold,
        "absolute_floor": context.absolute_floor,
        "style_tendency": context.style_tendency or {},
        "market_regime": context.market_regime or {},
        "tradability": context.tradability or {},
    }


def resolve_backtest_strategy_id(
    *,
    scores: list[AssetScoreORM],
    fallback: str,
) -> str:
    """从评分快照解析回测使用的策略 ID，缺失时回退到推荐策略名。"""

    for score in scores:
        strategy_id = score.payload.get("strategy_id")
        if strategy_id:
            return str(strategy_id)
    return fallback


def apply_memory_ranking_adjustments(
    scores: list[AssetScoreORM],
    adjustments: dict[str, MemoryRankingAdjustment],
) -> list[AssetScoreORM]:
    """按记忆回流调整呈现排序，不修改确定性 `total_score`。"""

    adjusted_scores: list[tuple[float, int, AssetScoreORM]] = []
    for original_index, score in enumerate(scores):
        adjustment = adjustments.get(score.asset_id)
        item = copy(score)
        payload = dict(score.payload or {})
        if adjustment is not None:
            payload["memory_ranking_adjustment"] = adjustment.to_dict()
        item.payload = payload
        adjusted_rank_score = float(score.total_score) + (adjustment.adjustment if adjustment else 0.0)
        adjusted_scores.append((adjusted_rank_score, original_index, item))

    ranked = [item for _, _, item in sorted(adjusted_scores, key=lambda row: (-row[0], row[1]))]
    for rank, item in enumerate(ranked, start=1):
        item.rank = rank
    return ranked


def build_backtest_evidence(
    *,
    backtests: BacktestRepository,
    market: str,
    strategy_id: str,
    universe_id: str,
) -> JsonDict:
    """读取同市场、同策略、同候选池的最近可用回测证据。"""

    row = backtests.get_latest_result(
        market=market,
        strategy_id=strategy_id,
        universe_id=universe_id,
        status="available",
    )
    if row is None:
        # 回测执行成功落库为 completed，而非 available；二者都应视为
        # 有效回测证据，否则推荐就绪度会误报 missing_backtest_evidence。
        row = backtests.get_latest_result(
            market=market,
            strategy_id=strategy_id,
            universe_id=universe_id,
            status="completed",
        )
    if row is None:
        return build_missing_backtest_evidence(
            market=market,
            strategy_id=strategy_id,
            universe_id=universe_id,
        )
    metrics = _json_safe(row.metrics or {})
    evidence = {
        "status": row.status,
        "backtest_id": row.backtest_id,
        "market": row.market,
        "strategy_id": row.strategy_id,
        "universe_id": row.universe_id,
        "start_at": _isoformat(row.start_at),
        "end_at": _isoformat(row.end_at),
        "rebalance_frequency": row.rebalance_frequency,
        "metrics": metrics,
        "data_versions": _json_safe(row.data_versions or {}),
        "created_at": _isoformat(row.created_at),
        "summary": build_backtest_summary(metrics=metrics, start_at=row.start_at, end_at=row.end_at),
    }
    warnings = (row.payload or {}).get("warnings") if isinstance(row.payload, dict) else None
    if warnings:
        evidence["warnings"] = _json_safe(warnings)
    return evidence


def build_missing_backtest_evidence(
    *,
    market: str,
    strategy_id: str,
    universe_id: str,
) -> JsonDict:
    """生成缺失回测证据的标准标记。"""

    return {
        "status": "missing",
        "market": market,
        "strategy_id": strategy_id,
        "universe_id": universe_id,
        "reason": "暂无同策略回测证据",
        "certainty_adjustment": "lower",
    }


def build_backtest_summary(
    *,
    metrics: JsonDict,
    start_at: datetime,
    end_at: datetime,
) -> str:
    """把核心回测指标整理成可直接进入报告的中文摘要。"""

    year_span = max(round((end_at - start_at).days / 365), 1)
    return (
        f"近 {year_span} 年模拟回放：年化收益 {format_ratio(metrics.get('cagr'))}，"
        f"最大回撤 {format_ratio(metrics.get('max_drawdown'))}，"
        f"夏普 {format_number(metrics.get('sharpe'))}，"
        f"周期胜率 {format_ratio(metrics.get('period_win_rate'))}。"
    )


def format_ratio(value: Any) -> str:
    """格式化回测比例指标。"""

    if value is None:
        return "未知"
    return f"{float(value) * 100:.2f}%"


def format_number(value: Any) -> str:
    """格式化回测普通数值指标。"""

    if value is None:
        return "未知"
    return f"{float(value):.2f}"


def _json_safe(value: Any) -> Any:
    """把 ORM/Decimal/时间对象转换为 JSON 友好结构。"""

    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _isoformat(value: Any) -> str | None:
    """安全输出 ISO 时间字符串。"""

    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def ensure_recommendation_market(market: str) -> None:
    """推荐运行只允许单一市场。"""

    if market == "mixed":
        raise ValueError("A 股和数字货币必须分别生成推荐榜单，不能使用 mixed 推荐运行。")


def ensure_scores_match_market(*, scores: list[AssetScoreORM], market: str) -> None:
    """确保同一次推荐运行的评分都属于同一市场。"""

    mismatched = [score.asset_id for score in scores if score.market != market]
    if mismatched:
        raise ValueError(
            f"推荐运行市场为 {market}，但评分结果包含其他市场标的：{', '.join(mismatched)}"
        )


def decide_action(
    *,
    score: AssetScoreORM,
    signal: SignalSnapshotORM | None,
    risks: list[RiskFindingORM],
    decision_context: RecommendationDecisionContext | None = None,
) -> str:
    """根据分数、信号和风险决定推荐动作。"""

    if any(risk.severity in {"critical", "high"} for risk in risks):
        return "avoid"
    total_score = float(score.total_score)
    confidence = float(score.confidence)
    direction = signal.direction if signal else "neutral"
    if decision_context is not None:
        if is_tradability_blocked(decision_context.tradability):
            return "watch"
        if direction == "bearish" or total_score < 40:
            return "avoid"
        if total_score < decision_context.absolute_floor:
            return "watch"
        if (
            decision_context.percentile <= decision_context.adjusted_buy_percentile_threshold
            and direction in {"bullish", "mixed"}
        ):
            return "buy_candidate"
        return "watch"
    if total_score >= 75 and confidence >= 0.65 and direction in {"bullish", "mixed"}:
        return "buy_candidate"
    if total_score >= 60 and confidence >= 0.45:
        return "watch"
    if direction == "bearish" or total_score < 40:
        return "avoid"
    return "watch"


def is_tradability_blocked(tradability: JsonDict | None) -> bool:
    """判断可买入性上下文是否阻断买入候选。"""

    if not isinstance(tradability, dict):
        return False
    return tradability.get("tradable") is False or tradability.get("blocking_level") == "blocked"


def append_tradability_watch_condition(
    *,
    watch_conditions: JsonDict,
    tradability: JsonDict | None,
) -> None:
    """把可买入性限制补充到观察条件。"""

    if not is_tradability_blocked(tradability):
        return
    reasons = tradability.get("reasons") if isinstance(tradability, dict) else None
    reason_text = "、".join(str(reason) for reason in reasons) if isinstance(reasons, list) else "未知"
    conditions = watch_conditions.setdefault("conditions", [])
    if isinstance(conditions, list):
        conditions.append(f"当前可买入性受限：{reason_text}。")


def append_context_reasons(
    *,
    reasons: list[str],
    decision_context: RecommendationDecisionContext | None,
) -> None:
    """把大盘环境等上下文补充到推荐理由。"""

    if decision_context is None or not decision_context.market_regime:
        return
    regime = decision_context.market_regime.get("regime", "unknown")
    strength = decision_context.market_regime.get("strength", "unknown")
    reasons.append(
        "大盘环境 "
        f"{regime}/{strength}，买入分位阈值从 "
        f"{decision_context.buy_percentile_threshold:.2%} 调整为 "
        f"{decision_context.adjusted_buy_percentile_threshold:.2%}。"
    )


def append_context_rebuttals(
    *,
    risk_rebuttals: list[str],
    tradability: JsonDict | None,
    memory_adjustment: JsonDict | None,
) -> None:
    """把可买入性和记忆回流补充到风险反驳。"""

    if is_tradability_blocked(tradability):
        reasons = tradability.get("reasons") if isinstance(tradability, dict) else None
        reason_text = "、".join(str(reason) for reason in reasons) if isinstance(reasons, list) else "未知"
        risk_rebuttals.append(f"当前买入受限：{reason_text}，不应直接升级为买入执行。")
    if isinstance(memory_adjustment, dict):
        for reason in memory_adjustment.get("reasons") or []:
            risk_rebuttals.append(f"记忆回流提示：{reason}")


def decide_conviction(score: AssetScoreORM) -> str:
    """根据分数和置信度决定推荐强度。"""

    total_score = float(score.total_score)
    confidence = float(score.confidence)
    if total_score >= 80 and confidence >= 0.75:
        return "high"
    if total_score >= 60 and confidence >= 0.45:
        return "medium"
    return "low"


def build_reasons(*, score: AssetScoreORM, signal: SignalSnapshotORM | None) -> list[str]:
    """生成数据层可解释原因。"""

    reasons = [
        f"透明评分为 {float(score.total_score):.2f}，候选池内排名第 {score.rank}。",
        (
            f"评分置信度为 {float(score.confidence):.2f}，"
            f"缺失惩罚为 {float(score.missing_penalty):.2f}。"
        ),
    ]
    if signal is not None:
        reasons.append(
            f"最新信号方向为 {signal.direction}，信号分为 {float(signal.score):.2f}。"
        )
    return reasons


def build_risk_rebuttals(
    *,
    score: AssetScoreORM,
    signal: SignalSnapshotORM | None,
    risks: list[RiskFindingORM],
) -> list[str]:
    """生成数据层风险反驳要点。"""

    rebuttals = []
    if float(score.missing_penalty) > 0:
        rebuttals.append("当前存在缺失数据，推荐强度需要打折。")
    if signal is None or signal.status != "available":
        rebuttals.append("信号快照不是完全可用状态，需要等待更多数据确认。")
    rebuttals.extend(risk.title for risk in risks[:3])
    return rebuttals or ["暂未发现明确风险，但仍需结合后续行情和事件变化复核。"]


def build_watch_conditions(*, signal: SignalSnapshotORM | None, score: AssetScoreORM) -> JsonDict:
    """生成观察条件。"""

    conditions = [
        "评分置信度提升到 0.60 以上。",
        "缺失因子组补齐后总分仍保持在当前区间。",
    ]
    if signal is not None:
        conditions.append(f"信号方向从 {signal.direction} 进一步转强。")
    return {"conditions": conditions, "score_id": score.score_id}


def build_invalid_if(*, signal: SignalSnapshotORM | None, risks: list[RiskFindingORM]) -> JsonDict:
    """生成失效条件。"""

    conditions = [
        "新增高严重度或极高严重度风险。",
        "评分跌破 40 分且信号转为空头。",
    ]
    if signal is not None:
        conditions.append(f"当前信号 {signal.signal_id} 被新的低置信度或反向信号替代。")
    if risks:
        conditions.append("现有风险项进一步升级。")
    return {"conditions": conditions}


def build_asset_summary(
    *,
    symbol: str,
    action: str,
    total_score: float,
    confidence: float,
) -> str:
    """生成单标的推荐摘要。"""

    action_label = {
        "buy_candidate": "候选买入",
        "watch": "观察",
        "avoid": "回避",
    }.get(action, action)
    return f"{symbol} 当前动作为{action_label}，总分 {total_score:.2f}，置信度 {confidence:.2f}。"


def build_run_summary(
    *,
    recommendation_count: int,
    market: str,
    strategy: str,
) -> str:
    """生成推荐运行摘要。"""

    return f"本次 {market} / {strategy} 推荐运行生成 {recommendation_count} 条标的推荐。"


def build_run_id(
    *,
    screening_id: str,
    strategy: str,
    horizon: str,
    started_at: datetime,
    score_strategy_id: str | None = None,
    trial_state: str | None = None,
    validation_evidence_id: str | None = None,
) -> str:
    """生成稳定推荐运行 ID。"""

    normalized_time = started_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    identity = "|".join(
        (
            screening_id,
            strategy,
            horizon,
            score_strategy_id or "legacy_default",
            trial_state or "production",
            validation_evidence_id or "no_validation_evidence",
            normalized_time,
        )
    )
    digest = hashlib.sha1(identity.encode()).hexdigest()[:12]
    return f"run:{strategy}:{horizon}:{normalized_time}:{digest}"


def build_recommendation_id(*, run_id: str, asset_id: str, horizon: str) -> str:
    """生成稳定单标的推荐 ID。"""

    digest = hashlib.sha1(f"{run_id}:{asset_id}:{horizon}".encode()).hexdigest()[:12]
    return f"asset_rec:{asset_id}:{horizon}:{digest}"
