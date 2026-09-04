"""规则版持仓监控工作流。

第一版先用确定性规则跑通闭环：读取持仓、信号、风险和记忆，输出可审计的
操作建议。后续可把本文件中的规则节点替换为 LangGraph / LLM Agent 节点。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any

from finance_agent.monitoring.models import PositionAction
from finance_agent.storage.orm import (
    AssistantMemoryORM,
    PortfolioORM,
    PositionORM,
    RiskFindingORM,
    SignalSnapshotORM,
)


@dataclass(frozen=True)
class PortfolioMonitoringInput:
    """持仓监控工作流输入。"""

    owner_id: str
    portfolio: PortfolioORM
    positions: tuple[PositionORM, ...]
    signals_by_asset: dict[str, SignalSnapshotORM | None]
    risks_by_asset: dict[str, tuple[RiskFindingORM, ...]]
    memories_by_asset: dict[str, tuple[AssistantMemoryORM, ...]]
    as_of: datetime
    data_snapshot_id: str | None = None
    decision_gate_id: str | None = None
    decision_gate_status: str | None = None
    intraday_quotes_by_asset: dict[str, tuple[dict[str, Any], ...]] | None = None
    position_actions_by_position: dict[str, PositionAction] | None = None


@dataclass(frozen=True)
class PortfolioMonitoringDecision:
    """持仓监控工作流输出的单标的建议。"""

    asset_id: str
    symbol: str
    market: str
    suggested_action: str
    decision_type: str
    severity: str
    summary: str
    risk_rebuttal: str
    trigger_condition: str
    thesis: str
    review_questions: tuple[dict[str, str], ...]
    signal_ids: tuple[str, ...]
    risk_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    data_snapshot_id: str | None = None
    decision_gate_id: str | None = None
    decision_gate_status: str | None = None
    intraday_quotes: tuple[dict[str, Any], ...] = ()
    intended_action: str | None = None


@dataclass(frozen=True)
class PortfolioMonitoringResult:
    """一次持仓监控工作流结果。"""

    owner_id: str
    portfolio_id: str
    as_of: datetime
    decisions: tuple[PortfolioMonitoringDecision, ...]
    data_snapshot_id: str | None = None
    decision_gate_id: str | None = None
    decision_gate_status: str | None = None


class PortfolioMonitoringWorkflow:
    """规则版持仓监控工作流。"""

    def run(self, workflow_input: PortfolioMonitoringInput) -> PortfolioMonitoringResult:
        """执行持仓监控并输出结构化建议。"""

        decisions = tuple(
            self._decide_for_position(
                position=position,
                signal=workflow_input.signals_by_asset.get(position.asset_id),
                risks=workflow_input.risks_by_asset.get(position.asset_id, ()),
                memories=workflow_input.memories_by_asset.get(position.asset_id, ()),
                intraday_quotes=(workflow_input.intraday_quotes_by_asset or {}).get(
                    position.asset_id, ()
                ),
                position_action=(workflow_input.position_actions_by_position or {}).get(
                    position.position_id
                ),
            )
            for position in workflow_input.positions
        )
        if workflow_input.decision_gate_status and workflow_input.decision_gate_status != "approved":
            decisions = tuple(
                apply_decision_gate(decision, workflow_input=workflow_input)
                for decision in decisions
            )
        return PortfolioMonitoringResult(
            owner_id=workflow_input.owner_id,
            portfolio_id=workflow_input.portfolio.portfolio_id,
            as_of=workflow_input.as_of,
            decisions=decisions,
            data_snapshot_id=workflow_input.data_snapshot_id,
            decision_gate_id=workflow_input.decision_gate_id,
            decision_gate_status=workflow_input.decision_gate_status,
        )

    def _decide_for_position(
        self,
        *,
        position: PositionORM,
        signal: SignalSnapshotORM | None,
        risks: tuple[RiskFindingORM, ...],
        memories: tuple[AssistantMemoryORM, ...],
        intraday_quotes: tuple[dict[str, Any], ...],
        position_action: PositionAction | None = None,
    ) -> PortfolioMonitoringDecision:
        """根据单个持仓的信号、风险和记忆生成建议。"""

        direction = signal.direction if signal else "neutral"
        if position_action is not None:
            return decision_from_position_action(
                position=position,
                signal=signal,
                risks=risks,
                memories=memories,
                intraday_quotes=intraday_quotes,
                position_action=position_action,
            )
        high_risks = [risk for risk in risks if risk.severity in {"high", "critical"}]
        pnl_pct = position.unrealized_pnl_pct
        if high_risks or direction == "bearish":
            action = "reduce"
            decision_type = "reduce"
            severity = "high" if high_risks else "medium"
        elif direction == "bullish" and positive_decimal(pnl_pct):
            action = "hold"
            decision_type = "hold"
            severity = "low"
        elif direction == "bullish":
            action = "watch"
            decision_type = "watch"
            severity = "medium"
        else:
            action = "hold"
            decision_type = "hold"
            severity = "medium"

        risk_ids = tuple(risk.risk_id for risk in risks)
        evidence_ids = tuple(
            sorted({evidence_id for risk in risks for evidence_id in risk.evidence_ids})
        )

        signal_ids = (signal.signal_id,) if signal else ()
        return PortfolioMonitoringDecision(
            asset_id=position.asset_id,
            symbol=position.symbol,
            market=position.market,
            suggested_action=action,
            decision_type=decision_type,
            severity=severity,
            summary=build_summary(position=position, signal=signal, action=action),
            risk_rebuttal=build_risk_rebuttal(
                signal=signal,
                risks=risks,
                memories=memories,
            ),
            trigger_condition=build_trigger_condition(
                position=position,
                signal=signal,
                risks=risks,
            ),
            thesis=build_thesis(position=position, signal=signal, action=action),
            review_questions=build_review_questions(action=action, signal=signal, risks=risks),
            signal_ids=signal_ids,
            risk_ids=risk_ids,
            evidence_ids=evidence_ids,
            intraday_quotes=intraday_quotes,
        )


def decision_from_position_action(
    *,
    position: PositionORM,
    signal: SignalSnapshotORM | None,
    risks: tuple[RiskFindingORM, ...],
    memories: tuple[AssistantMemoryORM, ...],
    intraday_quotes: tuple[dict[str, Any], ...],
    position_action: PositionAction,
) -> PortfolioMonitoringDecision:
    """将盘中动作作为工作流唯一动作来源，避免被简化规则覆盖。"""

    action = position_action.action
    intended_action = position_action.intended_action
    suggested_action = (
        "wait"
        if action == "unexecutable"
        else "sell"
        if action == "exit"
        else action
    )
    decision_type = (
        "position_monitoring_unexecutable"
        if action == "unexecutable"
        else "position_monitoring"
    )
    reason = "、".join(position_action.reason_codes) or "盘中监控动作已更新"
    summary = (
        f"{position.symbol} 盘中监控建议 {intended_action or '未知动作'}，"
        f"当前状态不可执行（{reason}）。"
        if action == "unexecutable"
        else f"{position.symbol} 盘中监控建议 {action}（{reason}）。"
    )
    risk_ids = tuple(risk.risk_id for risk in risks)
    evidence_ids = tuple(
        sorted({evidence_id for risk in risks for evidence_id in risk.evidence_ids})
    )
    return PortfolioMonitoringDecision(
        asset_id=position.asset_id,
        symbol=position.symbol,
        market=position.market,
        suggested_action=suggested_action,
        decision_type=decision_type,
        severity=position_action.severity,
        summary=summary,
        risk_rebuttal=build_risk_rebuttal(signal=signal, risks=risks, memories=memories),
        trigger_condition=f"盘中监控动作：{reason}。",
        thesis=build_thesis(position=position, signal=signal, action=suggested_action),
        review_questions=build_review_questions(
            action=suggested_action, signal=signal, risks=risks
        ),
        signal_ids=(signal.signal_id,) if signal else (),
        risk_ids=risk_ids,
        evidence_ids=evidence_ids,
        intraday_quotes=intraday_quotes,
        intended_action=intended_action,
    )


def apply_decision_gate(
    decision: PortfolioMonitoringDecision,
    *,
    workflow_input: PortfolioMonitoringInput,
) -> PortfolioMonitoringDecision:
    """数据闸门未放行时阻止减仓动作，但继续保留风险提醒。"""

    status = workflow_input.decision_gate_status or "data_unavailable"
    if decision.suggested_action not in {"reduce", "sell", "swap", "buy"}:
        return replace(
            decision,
            data_snapshot_id=workflow_input.data_snapshot_id,
            decision_gate_id=workflow_input.decision_gate_id,
            decision_gate_status=status,
        )
    return replace(
        decision,
        suggested_action="wait",
        decision_type=f"{decision.decision_type}_gate_wait",
        summary=f"{decision.summary} 当前闸门状态为 {status}，暂不进入可执行动作。",
        data_snapshot_id=workflow_input.data_snapshot_id,
        decision_gate_id=workflow_input.decision_gate_id,
        decision_gate_status=status,
    )


def positive_decimal(value: Decimal | None) -> bool:
    """判断 Decimal 是否为正。"""

    return value is not None and value > Decimal("0")


def build_summary(
    *,
    position: PositionORM,
    signal: SignalSnapshotORM | None,
    action: str,
) -> str:
    """生成中文建议摘要。"""

    direction = signal.direction if signal else "neutral"
    pnl_pct = f"{float(position.unrealized_pnl_pct or 0):.2%}"
    if action == "reduce":
        return (
            f"{position.symbol} 当前信号 {direction}，持仓盈亏 {pnl_pct}，"
            "建议考虑减仓并等待风险缓解。"
        )
    if action == "watch":
        return f"{position.symbol} 信号偏强但确认不足，建议保留观察，不急于加仓。"
    return (
        f"{position.symbol} 当前信号 {direction}，持仓盈亏 {pnl_pct}，"
        "建议继续持有并观察触发条件。"
    )


def build_risk_rebuttal(
    *,
    signal: SignalSnapshotORM | None,
    risks: tuple[RiskFindingORM, ...],
    memories: tuple[AssistantMemoryORM, ...],
) -> str:
    """生成风险反驳文本。"""

    parts: list[str] = []
    if signal is None:
        parts.append("缺少最新信号，不能只根据持仓盈亏做操作。")
    elif signal.direction in {"neutral", "mixed"}:
        parts.append("信号并不单边明确，需要防止追涨或过早加仓。")
    if risks:
        risk_titles = "；".join(risk.title for risk in risks[:3])
        parts.append(f"近期风险提示包括：{risk_titles}。")
    if memories:
        memory_text = "；".join(memory.content for memory in memories[:2])
        parts.append(f"历史记忆提示：{memory_text}。")
    if not parts:
        parts.append("暂无强风险反驳，但仍需等待价格、资金流和事件面继续确认。")
    return "".join(parts)


def build_trigger_condition(
    *,
    position: PositionORM,
    signal: SignalSnapshotORM | None,
    risks: tuple[RiskFindingORM, ...],
) -> str:
    """生成本次触发条件说明。"""

    if risks:
        return f"{position.symbol} 出现 {len(risks)} 条风险或关注事项，需要复核持仓。"
    if signal is not None:
        return f"{position.symbol} 最新信号为 {signal.direction}，触发持仓复核。"
    return f"{position.symbol} 按计划执行定时持仓复核。"


def build_thesis(
    *,
    position: PositionORM,
    signal: SignalSnapshotORM | None,
    action: str,
) -> str:
    """生成可沉淀到 Finance Memory 的投资假设。"""

    direction = signal.direction if signal else "neutral"
    if action == "reduce":
        return (
            f"{position.symbol} 的持仓假设需要重新验证，"
            f"当前 {direction} 信号不支持继续扩大仓位。"
        )
    if action == "watch":
        return f"{position.symbol} 有潜在机会，但需要等待信号和价格行为进一步确认。"
    return f"{position.symbol} 当前仍满足继续持有条件，后续重点观察信号是否转弱。"


def build_review_questions(
    *,
    action: str,
    signal: SignalSnapshotORM | None,
    risks: tuple[RiskFindingORM, ...],
) -> tuple[dict[str, str], ...]:
    """生成下一次复盘问题。"""

    questions = [
        {"question": "最新信号方向是否发生变化？"},
        {"question": "持仓盈亏和仓位占比是否仍符合风险预算？"},
    ]
    if action == "reduce":
        questions.append({"question": "减仓后风险是否明显下降？"})
    elif action == "watch":
        questions.append({"question": "观察条件是否已经被确认或失效？"})
    elif signal is not None and signal.direction == "bullish":
        questions.append({"question": "是否出现加仓所需的量价或资金流确认？"})
    if risks:
        questions.append({"question": "本次风险提示是否已经解除？"})
    return tuple(questions[:3])
