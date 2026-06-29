"""推荐结果生成服务。

本服务属于数据层最后一截：读取 `asset_scores`、`signal_snapshots`、`risk_findings`
和资产主数据，写入 `recommendation_runs`、`recommendation_run_universes` 和
`asset_recommendations`。它不调用 LLM，不生成 Agent 分析，也不处理交易。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.storage.orm import AssetScoreORM, RiskFindingORM, SignalSnapshotORM
from finance_agent.storage.repositories import (
    AssetRepository,
    AssetScoreRepository,
    BacktestRepository,
    RecommendationRepository,
    RiskRepository,
    ScreeningRepository,
    SignalSnapshotRepository,
)

JsonDict = dict[str, Any]

RULE_VERSION = "asset_recommendation_v1.0.0"


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


class RecommendationService:
    """把评分、信号和风险组织成可查询的推荐结果。"""

    def __init__(self, session: Session) -> None:
        self.assets = AssetRepository(session)
        self.screenings = ScreeningRepository(session)
        self.scores = AssetScoreRepository(session)
        self.signals = SignalSnapshotRepository(session)
        self.risks = RiskRepository(session)
        self.recommendations = RecommendationRepository(session)
        self.backtests = BacktestRepository(session)

    def rank_from_screening(
        self,
        *,
        screening_id: str,
        strategy: str = "balanced_swing_v1",
        horizon: str = "swing",
        limit: int = 20,
        rule_version: str = RULE_VERSION,
        audit_payload: JsonDict | None = None,
        profile_style_tendency: JsonDict | None = None,
    ) -> RecommendationRunResult:
        """读取一次初筛的评分结果并生成推荐榜单。"""

        screening = self.screenings.get_screening_result(screening_id)
        ensure_recommendation_market(screening.market)
        started_at = datetime.now(tz=UTC)
        run_id = build_run_id(
            screening_id=screening_id,
            strategy=strategy,
            horizon=horizon,
            started_at=started_at,
        )
        scores = self.scores.list_scores_for_screening(screening_id)[:limit]
        ensure_scores_match_market(scores=scores, market=screening.market)
        backtest_strategy_id = resolve_backtest_strategy_id(scores=scores, fallback=strategy)
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
                "horizon": horizon,
            },
        )

        for rank, score in enumerate(scores, start=1):
            signal = self.signals.get_latest_signal(asset_id=score.asset_id, horizon=horizon)
            risks = self.risks.list_recent_risks(asset_id=score.asset_id, limit=10)
            decision_context = RecommendationDecisionContext(
                rank=rank,
                total=len(scores),
                style_tendency=profile_style_tendency,
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
            },
            "backtest_evidence": backtest_evidence,
        }
        if profile_style_tendency:
            run_payload["profile_style_tendency"] = profile_style_tendency
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
    watch_conditions = build_watch_conditions(signal=signal, score=score)
    invalid_if = build_invalid_if(signal=signal, risks=risks)
    summary = build_asset_summary(
        symbol=score.symbol,
        action=action,
        total_score=float(score.total_score),
        confidence=float(score.confidence),
    )

    return {
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
        "score_strategy_id": score.payload.get("strategy_id"),
        "score_weight_snapshot": score.payload.get("weight_snapshot"),
        "backtest_evidence": backtest_evidence,
        "decision_context": decision_context_payload(decision_context),
    }


def decision_context_payload(context: RecommendationDecisionContext | None) -> JsonDict | None:
    """输出推荐动作裁决上下文，便于推荐结果解释和审计。"""

    if context is None:
        return None
    return {
        "rank": context.rank,
        "total": context.total,
        "percentile": round(context.percentile, 6),
        "buy_percentile_threshold": context.buy_percentile_threshold,
        "absolute_floor": context.absolute_floor,
        "style_tendency": context.style_tendency or {},
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


def _isoformat(value: datetime | None) -> str | None:
    """安全输出 ISO 时间字符串。"""

    return value.isoformat() if value is not None else None


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
        if direction == "bearish" or total_score < 40:
            return "avoid"
        if total_score < decision_context.absolute_floor:
            return "watch"
        if (
            decision_context.percentile <= decision_context.buy_percentile_threshold
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
) -> str:
    """生成稳定推荐运行 ID。"""

    normalized_time = started_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha1(f"{screening_id}:{strategy}:{horizon}".encode()).hexdigest()[:12]
    return f"run:{strategy}:{horizon}:{normalized_time}:{digest}"


def build_recommendation_id(*, run_id: str, asset_id: str, horizon: str) -> str:
    """生成稳定单标的推荐 ID。"""

    digest = hashlib.sha1(f"{run_id}:{asset_id}:{horizon}".encode()).hexdigest()[:12]
    return f"asset_rec:{asset_id}:{horizon}:{digest}"
