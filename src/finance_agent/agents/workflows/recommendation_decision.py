"""推荐结果 Agent 决策工作流。

本工作流把推荐结果、当前持仓、观察池、信号、风险和长期记忆汇总为
可审计的“入池、买入、卖出、换股、忽略”决策。第一版采用确定性规则模拟
金融团队共识，后续可在保持 DTO 不变的前提下替换为 LLM / Hermes Agent。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal

from finance_agent.storage.orm import (
    AssetRecommendationORM,
    AssistantMemoryORM,
    PositionORM,
    RiskFindingORM,
    SignalSnapshotORM,
    WatchlistItemORM,
    WatchlistORM,
)


@dataclass(frozen=True)
class RecommendationDecisionInput:
    """推荐决策工作流输入。"""

    owner_id: str
    recommendation_run_id: str
    portfolio_id: str
    watchlist: WatchlistORM
    recommendations: tuple[AssetRecommendationORM, ...]
    positions: tuple[PositionORM, ...]
    watchlist_items: tuple[WatchlistItemORM, ...]
    signals_by_asset: dict[str, SignalSnapshotORM | None]
    risks_by_asset: dict[str, tuple[RiskFindingORM, ...]]
    memories_by_asset: dict[str, tuple[AssistantMemoryORM, ...]]
    as_of: datetime
    data_snapshot_id: str | None = None
    decision_gate_id: str | None = None
    decision_gate_status: str | None = None


@dataclass(frozen=True)
class RecommendationDecision:
    """推荐决策工作流输出的单标的决策。"""

    asset_id: str
    symbol: str
    name: str
    market: str
    recommendation_id: str
    agent_action: str
    trade_action: str
    decision_type: str
    severity: str
    summary: str
    rationale: str
    risk_rebuttal: str
    watchlist_status: str | None
    should_write_watchlist: bool
    should_alert: bool
    source_position_id: str | None
    target_position_id: str | None
    next_review_at: datetime | None
    review_questions: tuple[dict[str, str], ...]
    reason_ids: tuple[str, ...]
    signal_ids: tuple[str, ...]
    risk_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    data_snapshot_id: str | None = None
    decision_gate_id: str | None = None
    decision_gate_status: str | None = None


@dataclass(frozen=True)
class RecommendationDecisionResult:
    """一次推荐决策工作流结果。"""

    owner_id: str
    recommendation_run_id: str
    portfolio_id: str
    watchlist_id: str
    as_of: datetime
    decisions: tuple[RecommendationDecision, ...]
    data_snapshot_id: str | None = None
    decision_gate_id: str | None = None
    decision_gate_status: str | None = None


class RecommendationDecisionWorkflow:
    """确定性 Agent 决策工作流。"""

    buy_score_threshold = Decimal("85.000000")
    buy_confidence_threshold = Decimal("0.780000")
    watch_score_threshold = Decimal("70.000000")

    def run(self, workflow_input: RecommendationDecisionInput) -> RecommendationDecisionResult:
        """执行推荐决策并输出结构化动作。"""

        positions_by_asset = {position.asset_id: position for position in workflow_input.positions}
        watch_items_by_asset = {
            item.asset_id: item for item in workflow_input.watchlist_items
        }
        weak_positions = tuple(
            position
            for position in workflow_input.positions
            if is_weak_position(
                position=position,
                signal=workflow_input.signals_by_asset.get(position.asset_id),
                risks=workflow_input.risks_by_asset.get(position.asset_id, ()),
            )
        )
        decisions = tuple(
            self._decide_for_recommendation(
                recommendation=recommendation,
                current_position=positions_by_asset.get(recommendation.asset_id),
                existing_watch_item=watch_items_by_asset.get(recommendation.asset_id),
                weak_positions=weak_positions,
                signal=workflow_input.signals_by_asset.get(recommendation.asset_id),
                risks=workflow_input.risks_by_asset.get(recommendation.asset_id, ()),
                memories=workflow_input.memories_by_asset.get(recommendation.asset_id, ()),
                as_of=workflow_input.as_of,
            )
            for recommendation in workflow_input.recommendations
        )
        if workflow_input.decision_gate_status and workflow_input.decision_gate_status != "approved":
            decisions = tuple(
                apply_decision_gate(decision, workflow_input=workflow_input)
                for decision in decisions
            )
        return RecommendationDecisionResult(
            owner_id=workflow_input.owner_id,
            recommendation_run_id=workflow_input.recommendation_run_id,
            portfolio_id=workflow_input.portfolio_id,
            watchlist_id=workflow_input.watchlist.watchlist_id,
            as_of=workflow_input.as_of,
            decisions=decisions,
            data_snapshot_id=workflow_input.data_snapshot_id,
            decision_gate_id=workflow_input.decision_gate_id,
            decision_gate_status=workflow_input.decision_gate_status,
        )

    def _decide_for_recommendation(
        self,
        *,
        recommendation: AssetRecommendationORM,
        current_position: PositionORM | None,
        existing_watch_item: WatchlistItemORM | None,
        weak_positions: tuple[PositionORM, ...],
        signal: SignalSnapshotORM | None,
        risks: tuple[RiskFindingORM, ...],
        memories: tuple[AssistantMemoryORM, ...],
        as_of: datetime,
    ) -> RecommendationDecision:
        """对单条推荐做 Agent 动作决策。"""

        high_risks = tuple(risk for risk in risks if risk.severity in {"high", "critical"})
        reason_ids = tuple(
            value
            for value in (recommendation.score_id, recommendation.factor_frame_id)
            if value is not None
        )
        signal_ids = tuple(recommendation.signal_ids)
        if signal is not None and signal.signal_id not in signal_ids:
            signal_ids = (*signal_ids, signal.signal_id)
        risk_ids = tuple(sorted({*recommendation.risk_ids, *(risk.risk_id for risk in risks)}))
        evidence_ids = tuple(
            sorted(
                {
                    *recommendation.evidence_ids,
                    *(evidence_id for risk in risks for evidence_id in risk.evidence_ids),
                }
            )
        )

        if recommendation.action == "avoid" or high_risks:
            return build_decision(
                recommendation=recommendation,
                agent_action="reject",
                trade_action="avoid",
                decision_type="recommendation_reject",
                severity="high" if high_risks else "medium",
                rationale="推荐被回避或存在高风险，Agent 决定不入池、不交易。",
                risk_rebuttal=build_risk_rebuttal(signal=signal, risks=risks, memories=memories),
                watchlist_status=None,
                should_write_watchlist=False,
                should_alert=True,
                source_position_id=None,
                target_position_id=current_position.position_id if current_position else None,
                next_review_at=None,
                reason_ids=reason_ids,
                signal_ids=signal_ids,
                risk_ids=risk_ids,
                evidence_ids=evidence_ids,
            )

        if current_position is not None:
            trade_action = "hold"
            agent_action = "hold_position"
            decision_type = "recommendation_hold"
            severity = "low"
            rationale = "推荐标的已在当前持仓中，Agent 决定继续持有并观察。"
            if is_weak_position(position=current_position, signal=signal, risks=risks):
                trade_action = "sell"
                agent_action = "sell"
                decision_type = "recommendation_sell"
                severity = "high"
                rationale = "推荐标的虽在持仓中，但信号或风险不支持继续持有。"
            return build_decision(
                recommendation=recommendation,
                agent_action=agent_action,
                trade_action=trade_action,
                decision_type=decision_type,
                severity=severity,
                rationale=rationale,
                risk_rebuttal=build_risk_rebuttal(signal=signal, risks=risks, memories=memories),
                watchlist_status=existing_watch_item.status if existing_watch_item else None,
                should_write_watchlist=False,
                should_alert=True,
                source_position_id=current_position.position_id,
                target_position_id=current_position.position_id,
                next_review_at=as_of + timedelta(days=2),
                reason_ids=reason_ids,
                signal_ids=signal_ids,
                risk_ids=risk_ids,
                evidence_ids=evidence_ids,
            )

        if is_buy_candidate(recommendation=recommendation, signal=signal):
            weak_position = weak_positions[0] if weak_positions else None
            if weak_position is not None:
                agent_action = "swap"
                trade_action = "swap"
                decision_type = "recommendation_swap"
                severity = "high"
                rationale = (
                    f"推荐标的 {recommendation.symbol} 质量较强，"
                    f"同时持仓 {weak_position.symbol} 信号或风险转弱，Agent 决定换股。"
                )
            else:
                agent_action = "buy"
                trade_action = "buy"
                decision_type = "recommendation_buy"
                severity = "medium"
                rationale = "推荐评分、置信度和信号均达到买入候选条件，Agent 决定买入。"
            return build_decision(
                recommendation=recommendation,
                agent_action=agent_action,
                trade_action=trade_action,
                decision_type=decision_type,
                severity=severity,
                rationale=rationale,
                risk_rebuttal=build_risk_rebuttal(signal=signal, risks=risks, memories=memories),
                watchlist_status="ready",
                should_write_watchlist=True,
                should_alert=True,
                source_position_id=weak_position.position_id if weak_position else None,
                target_position_id=None,
                next_review_at=as_of + timedelta(days=1),
                reason_ids=reason_ids,
                signal_ids=signal_ids,
                risk_ids=risk_ids,
                evidence_ids=evidence_ids,
            )

        if recommendation.total_score >= self.watch_score_threshold:
            return build_decision(
                recommendation=recommendation,
                agent_action="add_to_watchlist",
                trade_action="watch",
                decision_type="recommendation_watch",
                severity="low",
                rationale="推荐分数具备潜力，但买入条件尚未完全满足，Agent 决定先入观察池。",
                risk_rebuttal=build_risk_rebuttal(signal=signal, risks=risks, memories=memories),
                watchlist_status="active",
                should_write_watchlist=True,
                should_alert=False,
                source_position_id=None,
                target_position_id=None,
                next_review_at=as_of + timedelta(days=2),
                reason_ids=reason_ids,
                signal_ids=signal_ids,
                risk_ids=risk_ids,
                evidence_ids=evidence_ids,
            )

        return build_decision(
            recommendation=recommendation,
            agent_action="ignore",
            trade_action="avoid",
            decision_type="recommendation_ignore",
            severity="low",
            rationale="推荐分数或置信度不足，Agent 决定暂不入池、不交易。",
            risk_rebuttal=build_risk_rebuttal(signal=signal, risks=risks, memories=memories),
            watchlist_status=None,
            should_write_watchlist=False,
            should_alert=False,
            source_position_id=None,
            target_position_id=None,
            next_review_at=None,
            reason_ids=reason_ids,
            signal_ids=signal_ids,
            risk_ids=risk_ids,
            evidence_ids=evidence_ids,
        )


def is_buy_candidate(
    *,
    recommendation: AssetRecommendationORM,
    signal: SignalSnapshotORM | None,
) -> bool:
    """判断推荐是否达到 Agent 买入候选标准。"""

    signal_ok = (
        signal is not None
        and signal.direction == "bullish"
        and signal.score >= RecommendationDecisionWorkflow.buy_score_threshold
        and signal.confidence >= RecommendationDecisionWorkflow.buy_confidence_threshold
    )
    return (
        recommendation.action in {"buy_candidate", "strong_buy"}
        and recommendation.total_score >= RecommendationDecisionWorkflow.buy_score_threshold
        and recommendation.confidence >= RecommendationDecisionWorkflow.buy_confidence_threshold
        and recommendation.conviction == "high"
        and signal_ok
    )


def apply_decision_gate(
    decision: RecommendationDecision,
    *,
    workflow_input: RecommendationDecisionInput,
) -> RecommendationDecision:
    """把未放行的交易动作降级为等待，同时保留研究和风险上下文。"""

    if decision.trade_action not in {"buy", "sell", "swap", "reduce"}:
        return replace(
            decision,
            data_snapshot_id=workflow_input.data_snapshot_id,
            decision_gate_id=workflow_input.decision_gate_id,
            decision_gate_status=workflow_input.decision_gate_status,
        )
    status = workflow_input.decision_gate_status or "data_unavailable"
    return replace(
        decision,
        agent_action="wait_for_decision_gate",
        trade_action="wait",
        decision_type=f"{decision.decision_type}_gate_wait",
        summary=f"{decision.summary} 当前闸门状态为 {status}，暂不进入可执行动作。",
        should_alert=True,
        data_snapshot_id=workflow_input.data_snapshot_id,
        decision_gate_id=workflow_input.decision_gate_id,
        decision_gate_status=status,
    )


def is_weak_position(
    *,
    position: PositionORM,
    signal: SignalSnapshotORM | None,
    risks: tuple[RiskFindingORM, ...],
) -> bool:
    """判断持仓是否弱到需要卖出或换股。"""

    has_high_risk = any(risk.severity in {"high", "critical"} for risk in risks)
    signal_bearish = signal is not None and signal.direction == "bearish"
    drawdown_large = (
        position.unrealized_pnl_pct is not None
        and position.unrealized_pnl_pct <= Decimal("-0.050000")
    )
    overweight = (
        position.portfolio_weight is not None
        and position.portfolio_weight >= Decimal("0.500000")
    )
    return has_high_risk or signal_bearish or (drawdown_large and overweight)


def build_decision(
    *,
    recommendation: AssetRecommendationORM,
    agent_action: str,
    trade_action: str,
    decision_type: str,
    severity: str,
    rationale: str,
    risk_rebuttal: str,
    watchlist_status: str | None,
    should_write_watchlist: bool,
    should_alert: bool,
    source_position_id: str | None,
    target_position_id: str | None,
    next_review_at: datetime | None,
    reason_ids: tuple[str, ...],
    signal_ids: tuple[str, ...],
    risk_ids: tuple[str, ...],
    evidence_ids: tuple[str, ...],
) -> RecommendationDecision:
    """构建推荐 Agent 决策。"""

    summary = (
        f"{recommendation.symbol} Agent 决策：{agent_action}，"
        f"交易动作：{trade_action}。{rationale}"
    )
    return RecommendationDecision(
        asset_id=recommendation.asset_id,
        symbol=recommendation.symbol,
        name=recommendation.name,
        market=recommendation.market,
        recommendation_id=recommendation.recommendation_id,
        agent_action=agent_action,
        trade_action=trade_action,
        decision_type=decision_type,
        severity=severity,
        summary=summary,
        rationale=rationale,
        risk_rebuttal=risk_rebuttal,
        watchlist_status=watchlist_status,
        should_write_watchlist=should_write_watchlist,
        should_alert=should_alert,
        source_position_id=source_position_id,
        target_position_id=target_position_id,
        next_review_at=next_review_at,
        review_questions=build_review_questions(
            agent_action=agent_action,
            trade_action=trade_action,
        ),
        reason_ids=reason_ids,
        signal_ids=signal_ids,
        risk_ids=risk_ids,
        evidence_ids=evidence_ids,
    )


def build_risk_rebuttal(
    *,
    signal: SignalSnapshotORM | None,
    risks: tuple[RiskFindingORM, ...],
    memories: tuple[AssistantMemoryORM, ...],
) -> str:
    """生成推荐决策的风险反驳。"""

    parts: list[str] = []
    if signal is None:
        parts.append("缺少最新信号，不能直接执行买卖动作。")
    elif signal.direction != "bullish":
        parts.append(f"最新信号为 {signal.direction}，需要警惕推荐与趋势不一致。")
    if risks:
        parts.append(f"风险提示包括：{'；'.join(risk.title for risk in risks[:3])}。")
    if memories:
        parts.append(f"历史记忆提示：{'；'.join(memory.content for memory in memories[:2])}。")
    if not parts:
        parts.append("暂无强反方证据，但仍需等待价格、资金流和事件面继续确认。")
    return "".join(parts)


def build_review_questions(
    *,
    agent_action: str,
    trade_action: str,
) -> tuple[dict[str, str], ...]:
    """生成推荐决策复盘问题。"""

    if trade_action == "swap":
        return (
            {"question": "换入标的是否继续保持强信号和高置信度？"},
            {"question": "换出标的的风险是否按预期缓解？"},
            {"question": "换股后组合仓位是否仍符合风险预算？"},
        )
    if trade_action == "buy":
        return (
            {"question": "买入后是否出现预期内的趋势延续？"},
            {"question": "止损条件和失效条件是否被触发？"},
            {"question": "仓位是否仍在单标的上限内？"},
        )
    if agent_action == "add_to_watchlist":
        return (
            {"question": "观察条件是否被进一步确认？"},
            {"question": "是否出现真实买入点，而不只是评分较高？"},
        )
    return (
        {"question": "本次忽略或回避的原因是否仍然成立？"},
        {"question": "是否出现新的数据改变原决策？"},
    )
