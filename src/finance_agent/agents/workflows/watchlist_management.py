"""规则版观察池管理工作流。

观察池用于承载“有潜力但还没到操作点”的资产。第一版先用确定性规则
判断继续观察、升级为买入前候选，或从观察池剔除。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from finance_agent.storage.orm import (
    AssetThesisORM,
    AssistantMemoryORM,
    RiskFindingORM,
    SignalSnapshotORM,
    WatchlistItemORM,
    WatchlistORM,
)


@dataclass(frozen=True)
class WatchlistManagementInput:
    """观察池管理工作流输入。"""

    owner_id: str
    watchlist: WatchlistORM
    items: tuple[WatchlistItemORM, ...]
    signals_by_asset: dict[str, SignalSnapshotORM | None]
    risks_by_asset: dict[str, tuple[RiskFindingORM, ...]]
    memories_by_asset: dict[str, tuple[AssistantMemoryORM, ...]]
    theses_by_asset: dict[str, tuple[AssetThesisORM, ...]]
    as_of: datetime


@dataclass(frozen=True)
class WatchlistManagementDecision:
    """观察池管理工作流输出的单标的状态流转建议。"""

    watchlist_item_id: str
    asset_id: str
    symbol: str
    market: str
    suggested_action: str
    decision_type: str
    next_status: str
    severity: str
    summary: str
    daily_watch_reason: str
    risk_rebuttal: str
    trigger_condition: str
    next_review_at: datetime | None
    removed_reason: str | None
    review_questions: tuple[dict[str, str], ...]
    signal_ids: tuple[str, ...]
    risk_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    thesis_ids: tuple[str, ...]


@dataclass(frozen=True)
class WatchlistManagementResult:
    """一次观察池管理工作流结果。"""

    owner_id: str
    watchlist_id: str
    as_of: datetime
    decisions: tuple[WatchlistManagementDecision, ...]


class WatchlistManagementWorkflow:
    """规则版观察池管理工作流。"""

    promote_score_threshold = Decimal("70.000000")
    promote_confidence_threshold = Decimal("0.650000")

    def run(self, workflow_input: WatchlistManagementInput) -> WatchlistManagementResult:
        """执行观察池状态检查并输出结构化流转建议。"""

        decisions = tuple(
            self._decide_for_item(
                item=item,
                signal=workflow_input.signals_by_asset.get(item.asset_id),
                risks=workflow_input.risks_by_asset.get(item.asset_id, ()),
                memories=workflow_input.memories_by_asset.get(item.asset_id, ()),
                theses=workflow_input.theses_by_asset.get(item.asset_id, ()),
                as_of=workflow_input.as_of,
            )
            for item in workflow_input.items
        )
        return WatchlistManagementResult(
            owner_id=workflow_input.owner_id,
            watchlist_id=workflow_input.watchlist.watchlist_id,
            as_of=workflow_input.as_of,
            decisions=decisions,
        )

    def _decide_for_item(
        self,
        *,
        item: WatchlistItemORM,
        signal: SignalSnapshotORM | None,
        risks: tuple[RiskFindingORM, ...],
        memories: tuple[AssistantMemoryORM, ...],
        theses: tuple[AssetThesisORM, ...],
        as_of: datetime,
    ) -> WatchlistManagementDecision:
        """根据单个观察项的信号、风险、记忆和投资假设生成流转建议。"""

        direction = signal.direction if signal else "neutral"
        high_risks = tuple(risk for risk in risks if risk.severity in {"high", "critical"})
        if high_risks or direction == "bearish":
            action = "remove"
            next_status = "removed"
            severity = "high" if high_risks else "medium"
            next_review_at = None
            removed_reason = build_removed_reason(item=item, signal=signal, risks=risks)
        elif is_promotable_signal(signal) and not high_risks:
            action = "promote_to_candidate"
            next_status = "ready"
            severity = "medium"
            next_review_at = as_of + timedelta(days=1)
            removed_reason = None
        else:
            action = "keep_watch"
            next_status = "active"
            severity = "low"
            next_review_at = as_of + timedelta(days=3)
            removed_reason = None

        signal_ids = (signal.signal_id,) if signal else ()
        risk_ids = tuple(risk.risk_id for risk in risks)
        evidence_ids = tuple(
            sorted({evidence_id for risk in risks for evidence_id in risk.evidence_ids})
        )
        thesis_ids = tuple(thesis.thesis_id for thesis in theses)
        return WatchlistManagementDecision(
            watchlist_item_id=item.watchlist_item_id,
            asset_id=item.asset_id,
            symbol=item.symbol,
            market=item.market,
            suggested_action=action,
            decision_type=f"watchlist_{action}",
            next_status=next_status,
            severity=severity,
            summary=build_summary(item=item, signal=signal, action=action),
            daily_watch_reason=build_daily_watch_reason(
                item=item,
                signal=signal,
                risks=risks,
                theses=theses,
                action=action,
            ),
            risk_rebuttal=build_risk_rebuttal(
                signal=signal,
                risks=risks,
                memories=memories,
                theses=theses,
            ),
            trigger_condition=build_trigger_condition(item=item, signal=signal, risks=risks),
            next_review_at=next_review_at,
            removed_reason=removed_reason,
            review_questions=build_review_questions(action=action, risks=risks),
            signal_ids=signal_ids,
            risk_ids=risk_ids,
            evidence_ids=evidence_ids,
            thesis_ids=thesis_ids,
        )


def is_promotable_signal(signal: SignalSnapshotORM | None) -> bool:
    """判断信号是否足以把观察项升级为候选。"""

    if signal is None:
        return False
    return (
        signal.direction == "bullish"
        and signal.score >= WatchlistManagementWorkflow.promote_score_threshold
        and signal.confidence >= WatchlistManagementWorkflow.promote_confidence_threshold
    )


def build_removed_reason(
    *,
    item: WatchlistItemORM,
    signal: SignalSnapshotORM | None,
    risks: tuple[RiskFindingORM, ...],
) -> str:
    """生成观察项剔除原因。"""

    high_risks = [risk.title for risk in risks if risk.severity in {"high", "critical"}]
    if high_risks:
        return f"{item.symbol} 出现高风险：{'；'.join(high_risks[:3])}。"
    direction = signal.direction if signal else "neutral"
    return f"{item.symbol} 最新信号为 {direction}，不再满足观察池保留条件。"


def build_summary(
    *,
    item: WatchlistItemORM,
    signal: SignalSnapshotORM | None,
    action: str,
) -> str:
    """生成中文状态流转摘要。"""

    direction = signal.direction if signal else "neutral"
    if action == "remove":
        return f"{item.symbol} 观察条件失效或风险升高，建议从观察池剔除。"
    if action == "promote_to_candidate":
        return (
            f"{item.symbol} 最新信号 {direction}，评分和置信度达到观察池升级条件，"
            "建议进入买入前候选。"
        )
    return f"{item.symbol} 暂未满足升级或剔除条件，继续保留观察。"


def build_daily_watch_reason(
    *,
    item: WatchlistItemORM,
    signal: SignalSnapshotORM | None,
    risks: tuple[RiskFindingORM, ...],
    theses: tuple[AssetThesisORM, ...],
    action: str,
) -> str:
    """生成当天继续关注或升级观察项的原因。"""

    direction = signal.direction if signal else "neutral"
    signal_score = f"，信号评分 {signal.score}" if signal else ""
    thesis_hint = theses[0].thesis if theses else item.reason
    if action == "promote_to_candidate":
        return (
            f"{item.symbol} 原始关注逻辑仍成立：{thesis_hint}；"
            f"今日最新信号为 {direction}{signal_score}，已达到买入前候选复核条件。"
        )
    if action == "keep_watch":
        risk_titles = "；".join(risk.title for risk in risks[:2])
        risk_part = f"；但仍需观察风险：{risk_titles}" if risk_titles else ""
        return (
            f"{item.symbol} 原始入池原因仍未失效：{item.reason}；"
            f"今日最新信号为 {direction}{signal_score}，尚未触发买入或剔除{risk_part}。"
        )
    return f"{item.symbol} 今日不再继续关注，原因将按剔除事件记录：{item.reason}"


def build_risk_rebuttal(
    *,
    signal: SignalSnapshotORM | None,
    risks: tuple[RiskFindingORM, ...],
    memories: tuple[AssistantMemoryORM, ...],
    theses: tuple[AssetThesisORM, ...],
) -> str:
    """生成观察池风险反驳。"""

    parts: list[str] = []
    if signal is None:
        parts.append("缺少最新信号，不能直接升级为候选。")
    elif signal.direction in {"neutral", "mixed"}:
        parts.append("信号方向尚不明确，需要防止过早加入买入候选。")
    if risks:
        parts.append(f"近期风险包括：{'；'.join(risk.title for risk in risks[:3])}。")
    thesis_risks = [
        str(point.get("text", point))
        for thesis in theses[:2]
        for point in thesis.risk_points[:2]
    ]
    if thesis_risks:
        parts.append(f"投资假设中的反方点：{'；'.join(thesis_risks[:3])}。")
    if memories:
        parts.append(f"历史记忆提示：{'；'.join(memory.content for memory in memories[:2])}。")
    if not parts:
        parts.append("暂无强反方证据，但仍需等待价格、资金流和事件面继续确认。")
    return "".join(parts)


def build_trigger_condition(
    *,
    item: WatchlistItemORM,
    signal: SignalSnapshotORM | None,
    risks: tuple[RiskFindingORM, ...],
) -> str:
    """生成本次观察池检查触发条件。"""

    if risks:
        return f"{item.symbol} 出现 {len(risks)} 条风险或关注事项，触发观察池复核。"
    if signal:
        return f"{item.symbol} 最新 swing 信号为 {signal.direction}，触发观察池复核。"
    return f"{item.symbol} 按计划执行观察池复核。"


def build_review_questions(
    *,
    action: str,
    risks: tuple[RiskFindingORM, ...],
) -> tuple[dict[str, str], ...]:
    """生成下一次观察池复盘问题。"""

    if action == "promote_to_candidate":
        questions = [
            {"question": "是否出现真实买入点，而不只是观察信号转强？"},
            {"question": "如果买入，单标的仓位上限和止损线如何设置？"},
            {"question": "风险反驳是否已经被价格、资金流或事件面缓解？"},
        ]
    elif action == "remove":
        questions = [
            {"question": "剔除原因是否来自信号转弱、风险升高或原假设失效？"},
            {"question": "是否需要设置冷却期，避免短期反复加入观察池？"},
        ]
    else:
        questions = [
            {"question": "观察条件是否有新证据支持？"},
            {"question": "触发条件和失效条件是否需要调整？"},
        ]
    if risks:
        questions.append({"question": "本次风险提示是否已经解除？"})
    return tuple(questions[:3])
