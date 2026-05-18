"""V1.2 触发事件评估与 Workflow 派发服务。

触发层只读取已入库的持仓、观察池、推荐、信号和风险事实，不临时抓取行情，
也不临时计算 TA 或因子。TA、因子、评分和信号应先由数据层入库，触发层只判断
哪些变化值得唤起金融团队 Workflow。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha1
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.agents.interfaces import FinanceAgentInterface
from finance_agent.agents.tools.runtime import json_value
from finance_agent.application import PortfolioService, WatchlistService
from finance_agent.storage.orm import (
    AssistantTriggerEventORM,
    DataQualitySnapshotORM,
    PositionORM,
    RecommendationRunORM,
    SignalSnapshotORM,
    WatchlistItemORM,
)
from finance_agent.storage.repositories import (
    AssetScoreRepository,
    AssistantTriggerRepository,
    DataQualityRepository,
    FactorFrameRepository,
    IndicatorFrameRepository,
    PortfolioRepository,
    RecommendationRepository,
    RiskRepository,
    SignalSnapshotRepository,
)

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class TriggerEvaluationRequest:
    """触发评估请求。"""

    owner_id: str
    as_of: datetime
    portfolio_id: str | None = None
    watchlist_id: str | None = None
    recommendation_run_id: str | None = None
    horizon: str = "swing"
    timeframe: str = "1d"
    since_minutes: int = 60
    cooldown_minutes: int = 15
    recommendation_limit: int = 20
    drawdown_threshold: Decimal = Decimal("0.050000")


@dataclass(frozen=True)
class TriggerEventDraft:
    """待写入的触发事件草稿。"""

    trigger_type: str
    workflow_type: str
    severity: str
    dedup_key: str
    trigger_ref: str | None = None
    portfolio_id: str | None = None
    watchlist_id: str | None = None
    recommendation_run_id: str | None = None
    asset_id: str | None = None
    payload: JsonDict | None = None


@dataclass(frozen=True)
class TriggerEvaluationResult:
    """一次触发评估结果。"""

    owner_id: str
    created_events: tuple[AssistantTriggerEventORM, ...]
    suppressed_dedup_keys: tuple[str, ...]

    def to_dict(self) -> JsonDict:
        """转换为 JSON 友好的字典。"""

        return {
            "owner_id": self.owner_id,
            "created_count": len(self.created_events),
            "suppressed_count": len(self.suppressed_dedup_keys),
            "created_events": [serialize_trigger_event(event) for event in self.created_events],
            "suppressed_dedup_keys": list(self.suppressed_dedup_keys),
        }


@dataclass(frozen=True)
class WorkflowDispatchResult:
    """触发事件派发结果。"""

    dispatched_events: tuple[AssistantTriggerEventORM, ...]
    skipped_events: tuple[AssistantTriggerEventORM, ...]

    def to_dict(self) -> JsonDict:
        """转换为 JSON 友好的字典。"""

        return {
            "dispatched_count": len(self.dispatched_events),
            "skipped_count": len(self.skipped_events),
            "dispatched_events": [
                serialize_trigger_event(event) for event in self.dispatched_events
            ],
            "skipped_events": [serialize_trigger_event(event) for event in self.skipped_events],
        }


class TriggerService:
    """私人金融助手 V1.2 触发事件服务。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.triggers = AssistantTriggerRepository(session)
        self.portfolios = PortfolioService(session)
        self.portfolio_repo = PortfolioRepository(session)
        self.watchlists = WatchlistService(session)
        self.indicators = IndicatorFrameRepository(session)
        self.factors = FactorFrameRepository(session)
        self.scores = AssetScoreRepository(session)
        self.recommendations = RecommendationRepository(session)
        self.signals = SignalSnapshotRepository(session)
        self.risks = RiskRepository(session)
        self.data_quality = DataQualityRepository(session)

    def evaluate(self, request: TriggerEvaluationRequest) -> TriggerEvaluationResult:
        """评估已入库事实并生成触发事件。"""

        drafts: list[TriggerEventDraft] = []
        portfolio_snapshots = self._load_portfolio_snapshots(request)
        watchlist_items = tuple(
            self.watchlists.list_active_items(
                owner_id=request.owner_id,
                watchlist_id=request.watchlist_id,
            )
        )
        drafts.extend(
            self._evaluate_position_triggers(
                request=request,
                portfolio_snapshots=portfolio_snapshots,
            )
        )
        drafts.extend(self._evaluate_watchlist_triggers(request=request, items=watchlist_items))
        drafts.extend(
            self._evaluate_recommendation_triggers(
                request=request,
                portfolio_snapshots=portfolio_snapshots,
            )
        )
        drafts.extend(
            self._evaluate_risk_triggers(
                request=request,
                portfolio_snapshots=portfolio_snapshots,
                watchlist_items=watchlist_items,
            )
        )
        drafts.extend(
            self._evaluate_data_quality_triggers(
                request=request,
                portfolio_snapshots=portfolio_snapshots,
                watchlist_items=watchlist_items,
            )
        )
        return self._persist_drafts(request=request, drafts=drafts)

    def dispatch_pending(
        self,
        *,
        owner_id: str | None = None,
        limit: int = 20,
        as_of: datetime | None = None,
    ) -> WorkflowDispatchResult:
        """派发待处理触发事件到金融团队 Workflow。"""

        dispatch_time = as_of or datetime.now(UTC)
        interface = FinanceAgentInterface(self.session)
        dispatched: list[AssistantTriggerEventORM] = []
        skipped: list[AssistantTriggerEventORM] = []
        for event in self.triggers.list_pending_events(owner_id=owner_id, limit=limit):
            missing_reason = validate_dispatch_requirements(event)
            if missing_reason:
                skipped.append(
                    self.triggers.mark_skipped(
                        trigger_event_id=event.trigger_event_id,
                        skipped_at=dispatch_time,
                        reason=missing_reason,
                    )
                )
                continue

            workflow_run_id = build_trigger_workflow_run_id(event=event)
            try:
                interface.run_workflow(
                    workflow_type=event.workflow_type,
                    owner_id=event.owner_id,
                    workflow_run_id=workflow_run_id,
                    trigger_type=event.trigger_type,
                    trigger_ref=event.trigger_event_id,
                    started_at=dispatch_time,
                    initial_state={
                        "trigger_event_id": event.trigger_event_id,
                        "trigger_payload": event.payload or {},
                    },
                    portfolio_id=event.portfolio_id,
                    watchlist_id=event.watchlist_id,
                    recommendation_run_id=event.recommendation_run_id,
                    asset_id=event.asset_id,
                )
            except Exception as exc:
                skipped.append(
                    self.triggers.mark_skipped(
                        trigger_event_id=event.trigger_event_id,
                        skipped_at=dispatch_time,
                        reason=f"dispatch_failed: {exc}",
                    )
                )
                continue

            dispatched.append(
                self.triggers.mark_dispatched(
                    trigger_event_id=event.trigger_event_id,
                    workflow_run_id=workflow_run_id,
                    dispatched_at=dispatch_time,
                    payload={"dispatch_status": "succeeded"},
                )
            )
        return WorkflowDispatchResult(
            dispatched_events=tuple(dispatched),
            skipped_events=tuple(skipped),
        )

    def run_once(self, request: TriggerEvaluationRequest) -> JsonDict:
        """执行一次评估和派发。"""

        evaluation = self.evaluate(request)
        dispatch = self.dispatch_pending(
            owner_id=request.owner_id,
            limit=max(len(evaluation.created_events), 1),
            as_of=request.as_of,
        )
        return {
            "evaluation": evaluation.to_dict(),
            "dispatch": dispatch.to_dict(),
        }

    def _load_portfolio_snapshots(self, request: TriggerEvaluationRequest) -> tuple[Any, ...]:
        if request.portfolio_id:
            return (self.portfolios.load_portfolio_snapshot(request.portfolio_id),)
        portfolios = self.portfolio_repo.list_portfolios(owner_id=request.owner_id, status="active")
        return tuple(
            self.portfolios.load_portfolio_snapshot(portfolio.portfolio_id)
            for portfolio in portfolios
        )

    def _evaluate_position_triggers(
        self,
        *,
        request: TriggerEvaluationRequest,
        portfolio_snapshots: tuple[Any, ...],
    ) -> list[TriggerEventDraft]:
        drafts: list[TriggerEventDraft] = []
        for snapshot in portfolio_snapshots:
            portfolio = snapshot.portfolio
            drawdown_threshold = portfolio.max_drawdown_alert or request.drawdown_threshold
            for position in snapshot.positions:
                pnl_pct = abs_decimal(position.unrealized_pnl_pct)
                if (
                    position.unrealized_pnl_pct is not None
                    and position.unrealized_pnl_pct <= -drawdown_threshold
                ):
                    drafts.append(
                        build_position_trigger(
                            request=request,
                            position=position,
                            portfolio_id=portfolio.portfolio_id,
                            trigger_type="position_drawdown",
                            severity="high" if pnl_pct >= Decimal("0.100000") else "medium",
                            reason=(
                                f"{position.symbol} 当前回撤 {position.unrealized_pnl_pct}，"
                                f"超过阈值 {-drawdown_threshold}。"
                            ),
                        )
                    )

                recent_signals = self.signals.list_recent_signals(
                    asset_id=position.asset_id,
                    horizon=request.horizon,
                    limit=2,
                )
                if is_signal_flip_to_bearish(recent_signals):
                    latest = recent_signals[0]
                    previous = recent_signals[1]
                    drafts.append(
                        build_position_trigger(
                            request=request,
                            position=position,
                            portfolio_id=portfolio.portfolio_id,
                            trigger_type="signal_flip",
                            severity="high",
                            reason=(
                                f"{position.symbol} 信号从 {previous.direction} "
                                f"转为 {latest.direction}。"
                            ),
                            signal_id=latest.signal_id,
                        )
                    )
        return drafts

    def _evaluate_watchlist_triggers(
        self,
        *,
        request: TriggerEvaluationRequest,
        items: tuple[WatchlistItemORM, ...],
    ) -> list[TriggerEventDraft]:
        drafts: list[TriggerEventDraft] = []
        for item in items:
            signal = self.signals.get_latest_signal(
                asset_id=item.asset_id,
                horizon=request.horizon,
            )
            if signal is None:
                continue
            factor = self.factors.get_latest_factor_frame(
                asset_id=item.asset_id,
                horizon=request.horizon,
            )
            indicator = self.indicators.get_latest_indicator_frame(
                asset_id=item.asset_id,
                horizon=request.horizon,
                timeframe=request.timeframe,
            )
            score = self.scores.get_latest_score(
                asset_id=item.asset_id,
                horizon=request.horizon,
            )
            if not watchlist_trigger_conditions_matched(
                signal=signal,
                score=score,
                factor=factor,
                indicator=indicator,
                conditions=item.trigger_conditions or {},
            ):
                continue
            drafts.append(
                TriggerEventDraft(
                    trigger_type="watchlist_condition_hit",
                    workflow_type="asset_deep_analysis",
                    severity="medium",
                    trigger_ref=item.watchlist_item_id,
                    dedup_key=build_dedup_key(
                        owner_id=request.owner_id,
                        trigger_type="watchlist_condition_hit",
                        workflow_type="asset_deep_analysis",
                        asset_id=item.asset_id,
                        scope_id=item.watchlist_id,
                    ),
                    watchlist_id=item.watchlist_id,
                    asset_id=item.asset_id,
                    payload={
                        "reason": f"{item.symbol} 观察池启动条件满足。",
                        "watchlist_item_id": item.watchlist_item_id,
                        "signal_id": signal.signal_id,
                        "factor_frame_id": factor.factor_frame_id if factor else None,
                        "indicator_frame_id": (
                            indicator.indicator_frame_id if indicator else None
                        ),
                        "score_id": score.score_id if score else None,
                        "trigger_conditions": item.trigger_conditions or {},
                    },
                )
            )
        return drafts

    def _evaluate_recommendation_triggers(
        self,
        *,
        request: TriggerEvaluationRequest,
        portfolio_snapshots: tuple[Any, ...],
    ) -> list[TriggerEventDraft]:
        if not request.watchlist_id:
            return []
        portfolio_id = request.portfolio_id or first_portfolio_id(portfolio_snapshots)
        if not portfolio_id:
            return []

        runs: list[RecommendationRunORM] = []
        if request.recommendation_run_id:
            run = self.session.get(RecommendationRunORM, request.recommendation_run_id)
            if run is not None and run.status == "available":
                runs.append(run)
        else:
            runs.extend(
                self.recommendations.list_available_runs_since(
                    since=request.as_of - timedelta(minutes=request.since_minutes),
                    limit=request.recommendation_limit,
                )
            )

        return [
            TriggerEventDraft(
                trigger_type="recommendation_run_ready",
                workflow_type="recommendation_decision",
                severity="medium",
                trigger_ref=run.run_id,
                dedup_key=build_dedup_key(
                    owner_id=request.owner_id,
                    trigger_type="recommendation_run_ready",
                    workflow_type="recommendation_decision",
                    scope_id=run.run_id,
                ),
                portfolio_id=portfolio_id,
                watchlist_id=request.watchlist_id,
                recommendation_run_id=run.run_id,
                payload={
                    "reason": "新推荐运行已可用，触发 Agent 推荐决策。",
                    "market": run.market,
                    "strategy": run.strategy,
                    "horizon": run.horizon,
                },
            )
            for run in runs
        ]

    def _evaluate_risk_triggers(
        self,
        *,
        request: TriggerEvaluationRequest,
        portfolio_snapshots: tuple[Any, ...],
        watchlist_items: tuple[WatchlistItemORM, ...],
    ) -> list[TriggerEventDraft]:
        held_assets: dict[str, str] = {
            position.asset_id: snapshot.portfolio.portfolio_id
            for snapshot in portfolio_snapshots
            for position in snapshot.positions
        }
        watched_assets = {item.asset_id: item.watchlist_id for item in watchlist_items}
        asset_ids = sorted({*held_assets, *watched_assets})
        risks = self.risks.list_recent_risks_since(
            asset_ids=asset_ids,
            since=request.as_of - timedelta(minutes=request.since_minutes),
            severities=("high", "critical"),
            limit=50,
        )
        drafts: list[TriggerEventDraft] = []
        for risk in risks:
            if risk.asset_id is None:
                continue
            portfolio_id = held_assets.get(risk.asset_id)
            workflow_type = "portfolio_monitoring" if portfolio_id else "asset_deep_analysis"
            drafts.append(
                TriggerEventDraft(
                    trigger_type="risk_event_detected",
                    workflow_type=workflow_type,
                    severity="high" if risk.severity == "critical" else "medium",
                    trigger_ref=risk.risk_id,
                    dedup_key=build_dedup_key(
                        owner_id=request.owner_id,
                        trigger_type="risk_event_detected",
                        workflow_type=workflow_type,
                        asset_id=risk.asset_id,
                        scope_id=risk.risk_id,
                    ),
                    portfolio_id=portfolio_id,
                    watchlist_id=watched_assets.get(risk.asset_id),
                    asset_id=risk.asset_id,
                    payload={
                        "reason": f"新增高优先级风险：{risk.title}",
                        "risk_id": risk.risk_id,
                        "risk_type": risk.risk_type,
                        "risk_severity": risk.severity,
                        "evidence_ids": list(risk.evidence_ids or []),
                    },
                )
            )
        return drafts

    def _evaluate_data_quality_triggers(
        self,
        *,
        request: TriggerEvaluationRequest,
        portfolio_snapshots: tuple[Any, ...],
        watchlist_items: tuple[WatchlistItemORM, ...],
    ) -> list[TriggerEventDraft]:
        held_assets: dict[str, str] = {
            position.asset_id: snapshot.portfolio.portfolio_id
            for snapshot in portfolio_snapshots
            for position in snapshot.positions
        }
        watched_assets = {item.asset_id: item.watchlist_id for item in watchlist_items}
        asset_ids = sorted({*held_assets, *watched_assets})
        quality_items = self.data_quality.list_recent_quality_since(
            asset_ids=asset_ids,
            since=request.as_of - timedelta(minutes=request.since_minutes),
            limit=50,
        )
        drafts: list[TriggerEventDraft] = []
        for quality in quality_items:
            if not is_problematic_quality(quality):
                continue
            if quality.asset_id is None:
                continue
            portfolio_id = held_assets.get(quality.asset_id)
            workflow_type = "portfolio_monitoring" if portfolio_id else "asset_deep_analysis"
            severity = "high" if quality.status == "missing" else "medium"
            drafts.append(
                TriggerEventDraft(
                    trigger_type="data_quality_degraded",
                    workflow_type=workflow_type,
                    severity=severity,
                    trigger_ref=quality.quality_id,
                    dedup_key=build_dedup_key(
                        owner_id=request.owner_id,
                        trigger_type="data_quality_degraded",
                        workflow_type=workflow_type,
                        asset_id=quality.asset_id,
                        scope_id=quality.quality_id,
                    ),
                    portfolio_id=portfolio_id,
                    watchlist_id=watched_assets.get(quality.asset_id),
                    asset_id=quality.asset_id,
                    payload={
                        "reason": (
                            f"{quality.symbol or quality.asset_id} 的 "
                            f"{quality.data_domain} 数据质量变为 "
                            f"{quality.status}/{quality.freshness_status}。"
                        ),
                        "quality_id": quality.quality_id,
                        "data_domain": quality.data_domain,
                        "provider": quality.provider,
                        "quality_status": quality.status,
                        "freshness_status": quality.freshness_status,
                        "issue_count": quality.issue_count,
                        "missing_items": list(quality.missing_items or []),
                    },
                )
            )
        return drafts

    def _persist_drafts(
        self,
        *,
        request: TriggerEvaluationRequest,
        drafts: list[TriggerEventDraft],
    ) -> TriggerEvaluationResult:
        created: list[AssistantTriggerEventORM] = []
        suppressed: list[str] = []
        cooldown = timedelta(minutes=request.cooldown_minutes)
        for draft in drafts:
            if self.triggers.has_recent_event(
                dedup_key=draft.dedup_key,
                since=request.as_of - cooldown,
            ):
                suppressed.append(draft.dedup_key)
                continue
            event_id = build_trigger_event_id(draft=draft, as_of=request.as_of)
            created.append(
                self.triggers.upsert_trigger_event(
                    trigger_event_id=event_id,
                    owner_id=request.owner_id,
                    trigger_type=draft.trigger_type,
                    trigger_ref=draft.trigger_ref,
                    dedup_key=draft.dedup_key,
                    severity=draft.severity,
                    status="pending",
                    workflow_type=draft.workflow_type,
                    portfolio_id=draft.portfolio_id,
                    watchlist_id=draft.watchlist_id,
                    recommendation_run_id=draft.recommendation_run_id,
                    asset_id=draft.asset_id,
                    cooldown_until=request.as_of + cooldown,
                    triggered_at=request.as_of,
                    payload=draft.payload or {},
                )
            )
        return TriggerEvaluationResult(
            owner_id=request.owner_id,
            created_events=tuple(created),
            suppressed_dedup_keys=tuple(suppressed),
        )


def build_position_trigger(
    *,
    request: TriggerEvaluationRequest,
    position: PositionORM,
    portfolio_id: str,
    trigger_type: str,
    severity: str,
    reason: str,
    signal_id: str | None = None,
) -> TriggerEventDraft:
    """构建持仓相关触发事件。"""

    return TriggerEventDraft(
        trigger_type=trigger_type,
        workflow_type="portfolio_monitoring",
        severity=severity,
        trigger_ref=position.position_id,
        dedup_key=build_dedup_key(
            owner_id=request.owner_id,
            trigger_type=trigger_type,
            workflow_type="portfolio_monitoring",
            asset_id=position.asset_id,
            scope_id=portfolio_id,
        ),
        portfolio_id=portfolio_id,
        asset_id=position.asset_id,
        payload={
            "reason": reason,
            "position_id": position.position_id,
            "symbol": position.symbol,
            "market": position.market,
            "unrealized_pnl_pct": json_value(position.unrealized_pnl_pct),
            "signal_id": signal_id,
        },
    )


def watchlist_trigger_conditions_matched(
    *,
    signal: SignalSnapshotORM,
    score: Any | None = None,
    factor: Any | None = None,
    indicator: Any | None = None,
    conditions: JsonDict,
) -> bool:
    """判断观察池启动条件是否被最新信号满足。"""

    if not conditions:
        return False
    expected_direction = conditions.get("signal_direction") or conditions.get("direction")
    if expected_direction and signal.direction != expected_direction:
        return False
    min_score = decimal_from_condition(conditions.get("min_signal_score"))
    if min_score is not None and signal.score < min_score:
        return False
    min_confidence = decimal_from_condition(conditions.get("min_signal_confidence"))
    if min_confidence is not None and signal.confidence < min_confidence:
        return False
    min_total_score = decimal_from_condition(conditions.get("min_total_score"))
    if min_total_score is not None and (score is None or score.total_score < min_total_score):
        return False
    min_factor_groups = conditions.get("min_factor_groups")
    if min_factor_groups is not None:
        if factor is None or factor.total_available_groups < int(min_factor_groups):
            return False
    required_factor_status = conditions.get("factor_status")
    if required_factor_status and (factor is None or factor.status != required_factor_status):
        return False
    min_rsi = decimal_from_condition(conditions.get("min_rsi_14"))
    if min_rsi is not None and (indicator is None or indicator.rsi_14 is None):
        return False
    if min_rsi is not None and indicator is not None and indicator.rsi_14 < min_rsi:
        return False
    max_rsi = decimal_from_condition(conditions.get("max_rsi_14"))
    if max_rsi is not None and (indicator is None or indicator.rsi_14 is None):
        return False
    if max_rsi is not None and indicator is not None and indicator.rsi_14 > max_rsi:
        return False
    require_macd_positive = conditions.get("require_macd_positive")
    if require_macd_positive is True:
        if indicator is None or indicator.macd_hist is None or indicator.macd_hist <= 0:
            return False
    return True


def is_problematic_quality(quality: DataQualitySnapshotORM) -> bool:
    """判断数据质量快照是否需要唤起复核。"""

    return (
        quality.status in {"missing", "failed", "partial"}
        or quality.freshness_status in {"missing", "stale"}
        or quality.issue_count > 0
    )


def is_signal_flip_to_bearish(signals: list[SignalSnapshotORM]) -> bool:
    """判断最近信号是否从非空头转为空头。"""

    if len(signals) < 2:
        return False
    latest, previous = signals[0], signals[1]
    return latest.direction == "bearish" and previous.direction != "bearish"


def validate_dispatch_requirements(event: AssistantTriggerEventORM) -> str | None:
    """检查派发 Workflow 所需参数。"""

    if event.workflow_type in {"portfolio_monitoring", "recommendation_decision"}:
        if not event.portfolio_id:
            return f"{event.workflow_type} 缺少 portfolio_id"
    if event.workflow_type in {"watchlist_management", "recommendation_decision"}:
        if not event.watchlist_id:
            return f"{event.workflow_type} 缺少 watchlist_id"
    if event.workflow_type == "recommendation_decision" and not event.recommendation_run_id:
        return "recommendation_decision 缺少 recommendation_run_id"
    if event.workflow_type == "asset_deep_analysis" and not event.asset_id:
        return "asset_deep_analysis 缺少 asset_id"
    return None


def serialize_trigger_event(event: AssistantTriggerEventORM) -> JsonDict:
    """序列化触发事件。"""

    return {
        "trigger_event_id": event.trigger_event_id,
        "owner_id": event.owner_id,
        "trigger_type": event.trigger_type,
        "trigger_ref": event.trigger_ref,
        "dedup_key": event.dedup_key,
        "severity": event.severity,
        "status": event.status,
        "workflow_type": event.workflow_type,
        "workflow_run_id": event.workflow_run_id,
        "portfolio_id": event.portfolio_id,
        "watchlist_id": event.watchlist_id,
        "recommendation_run_id": event.recommendation_run_id,
        "asset_id": event.asset_id,
        "cooldown_until": json_value(event.cooldown_until),
        "triggered_at": json_value(event.triggered_at),
        "dispatched_at": json_value(event.dispatched_at),
        "payload": json_value(event.payload or {}),
    }


def build_dedup_key(
    *,
    owner_id: str,
    trigger_type: str,
    workflow_type: str,
    asset_id: str | None = None,
    scope_id: str | None = None,
) -> str:
    """生成冷却去重键。"""

    return ":".join(
        value
        for value in (owner_id, trigger_type, workflow_type, scope_id, asset_id)
        if value
    )


def build_trigger_event_id(*, draft: TriggerEventDraft, as_of: datetime) -> str:
    """生成触发事件 ID。"""

    digest = sha1(
        f"{draft.dedup_key}:{draft.trigger_ref}:{as_of.isoformat()}".encode()
    ).hexdigest()[:16]
    return f"trigger:{draft.trigger_type}:{digest}"


def build_trigger_workflow_run_id(*, event: AssistantTriggerEventORM) -> str:
    """生成由触发事件派发的 Workflow Run ID。"""

    owner = event.owner_id.replace(":", "_")
    digest = sha1(event.trigger_event_id.encode()).hexdigest()[:12]
    return f"workflow:{owner}:{event.workflow_type}:trigger:{digest}"


def first_portfolio_id(portfolio_snapshots: tuple[Any, ...]) -> str | None:
    """读取第一个组合 ID。"""

    if not portfolio_snapshots:
        return None
    return portfolio_snapshots[0].portfolio.portfolio_id


def abs_decimal(value: Decimal | None) -> Decimal:
    """安全取 Decimal 绝对值。"""

    if value is None:
        return Decimal("0")
    return abs(value)


def decimal_from_condition(value: Any) -> Decimal | None:
    """从 JSON 条件中读取 Decimal。"""

    if value is None:
        return None
    return Decimal(str(value))
