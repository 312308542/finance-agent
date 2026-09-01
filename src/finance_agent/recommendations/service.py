"""推荐结果生成服务。

本服务属于数据层最后一截：读取 `asset_scores`、`signal_snapshots`、`risk_findings`
和资产主数据，写入 `recommendation_runs`、`recommendation_run_universes` 和
`asset_recommendations`。它不调用 LLM，不生成 Agent 分析，也不处理交易。
"""

from __future__ import annotations

import hashlib
from copy import copy
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.application.market_context_service import adjust_buy_percentile_threshold
from finance_agent.storage.orm import AssetScoreORM, RiskFindingORM, SignalSnapshotORM
from finance_agent.storage.repositories import (
    AssetRepository,
    AssetScoreRepository,
    BacktestRepository,
    IndicatorFrameRepository,
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
)


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
        raw_scores = (
            self.scores.list_scores_for_screening(
                screening_id,
                strategy_id=score_strategy_id,
            )
            if score_strategy_id is not None
            else self.scores.list_scores_for_screening(screening_id)
        )[:limit]
        scores = apply_memory_ranking_adjustments(raw_scores, memory_ranking_adjustments or {})
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

        for rank, score in enumerate(scores, start=1):
            signal = self.signals.get_latest_signal(asset_id=score.asset_id, horizon=horizon)
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
                asset_name=self.asset_name(score.asset_id, fallback_symbol=score.symbol),
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
        summary = build_run_summary(
            recommendation_count=len(recommendation_ids),
            market=screening.market,
            strategy=strategy,
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
        )

    def asset_name(self, asset_id: str, *, fallback_symbol: str) -> str:
        """查询资产名称，缺失时用 symbol 兜底。"""

        asset = self.assets.get_asset_or_none(asset_id)
        return asset.name if asset else fallback_symbol


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

    action = decide_action(
        score=score,
        signal=signal,
        risks=risks,
        decision_context=decision_context,
    )
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
    return payload


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


def compact_structure_frame(frame: Any) -> JsonDict | None:
    """把完整 indicator_frame 压缩为前端展示和审计所需的摘要。"""

    payload = frame.payload if isinstance(getattr(frame, "payload", None), dict) else {}
    horizon = str(getattr(frame, "horizon", None) or payload.get("schema_version") or "")
    if not horizon:
        return None
    status = str(payload.get("status") or getattr(frame, "status", None) or "unknown")
    result: JsonDict = {
        "horizon": horizon,
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
    return []


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
