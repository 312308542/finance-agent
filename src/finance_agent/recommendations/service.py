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


class RecommendationService:
    """把评分、信号和风险组织成可查询的推荐结果。"""

    def __init__(self, session: Session) -> None:
        self.assets = AssetRepository(session)
        self.screenings = ScreeningRepository(session)
        self.scores = AssetScoreRepository(session)
        self.signals = SignalSnapshotRepository(session)
        self.risks = RiskRepository(session)
        self.recommendations = RecommendationRepository(session)

    def rank_from_screening(
        self,
        *,
        screening_id: str,
        strategy: str = "balanced_swing_v1",
        horizon: str = "swing",
        limit: int = 20,
        rule_version: str = RULE_VERSION,
        audit_payload: JsonDict | None = None,
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
            recommendation = build_recommendation_payload(
                score=score,
                signal=signal,
                risks=risks,
                asset_name=self.asset_name(score.asset_id, fallback_symbol=score.symbol),
                rank=rank,
                run_id=run_id,
                rule_version=rule_version,
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
        }
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
) -> JsonDict:
    """构建单标的推荐 payload。"""

    action = decide_action(score=score, signal=signal, risks=risks)
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
    }


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
) -> str:
    """根据分数、信号和风险决定推荐动作。"""

    if any(risk.severity in {"critical", "high"} for risk in risks):
        return "avoid"
    total_score = float(score.total_score)
    confidence = float(score.confidence)
    direction = signal.direction if signal else "neutral"
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
