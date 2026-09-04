"""V1.2 触发事件评估与 Agent 唤醒服务。

触发层只读取已入库的持仓、观察池、推荐、信号和风险事实，不临时抓取行情，
也不临时计算 TA 或因子。TA、因子、评分和信号应先由数据层入库，触发层只判断
哪些变化值得唤醒 Hermes 或内部金融 Agent。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha1
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_agent.agents.tools.runtime import json_value
from finance_agent.application import PortfolioService, WatchlistService
from finance_agent.monitoring.models import PositionAction
from finance_agent.storage.orm import (
    AssistantTriggerEventORM,
    DataQualitySnapshotORM,
    IntradayQuoteLatestORM,
    PositionMonitoringStateORM,
    PositionORM,
    RealtimeQuoteSnapshotORM,
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
    trigger_groups: tuple[str, ...] = ()
    intraday_quote_window_minutes: int = 30
    intraday_sharp_drop_threshold: Decimal = Decimal("-0.040000")
    intraday_volume_surge_multiplier: Decimal = Decimal("3.000000")
    intraday_price_change_threshold: Decimal = Decimal("0.020000")


@dataclass(frozen=True)
class TriggerEventDraft:
    """待写入的触发事件草稿。"""

    trigger_type: str
    requested_workflow_type: str
    severity: str
    dedup_key: str
    trigger_ref: str | None = None
    portfolio_id: str | None = None
    watchlist_id: str | None = None
    recommendation_run_id: str | None = None
    asset_id: str | None = None
    payload: JsonDict | None = None


@dataclass(frozen=True)
class IntradayVolatilityDraftResult:
    """盘中波动规则生成的草稿与跳过计数。"""

    drafts: tuple[TriggerEventDraft, ...]
    skipped_no_data_count: int = 0


@dataclass(frozen=True)
class TriggerEvaluationResult:
    """一次触发评估结果。"""

    owner_id: str
    created_events: tuple[AssistantTriggerEventORM, ...]
    suppressed_dedup_keys: tuple[str, ...]
    skipped_no_data_count: int = 0

    def to_dict(self) -> JsonDict:
        """转换为 JSON 友好的字典。"""

        return {
            "owner_id": self.owner_id,
            "created_count": len(self.created_events),
            "suppressed_count": len(self.suppressed_dedup_keys),
            "skipped_no_data_count": self.skipped_no_data_count,
            "created_events": [serialize_trigger_event(event) for event in self.created_events],
            "suppressed_dedup_keys": list(self.suppressed_dedup_keys),
        }


@dataclass(frozen=True)
class AgentWakeupDispatchResult:
    """触发事件派发到 Agent 唤醒队列的结果。"""

    dispatched_events: tuple[AssistantTriggerEventORM, ...]
    skipped_events: tuple[AssistantTriggerEventORM, ...]
    failed_events: tuple[AssistantTriggerEventORM, ...] = ()

    def to_dict(self) -> JsonDict:
        """转换为 JSON 友好的字典。"""

        return {
            "dispatched_count": len(self.dispatched_events),
            "skipped_count": len(self.skipped_events),
            "failed_count": len(self.failed_events),
            "dispatched_events": [
                serialize_trigger_event(event) for event in self.dispatched_events
            ],
            "skipped_events": [serialize_trigger_event(event) for event in self.skipped_events],
            "failed_events": [serialize_trigger_event(event) for event in self.failed_events],
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
        skipped_no_data_count = 0
        portfolio_snapshots = self._load_portfolio_snapshots(request)
        watchlist_items = tuple(
            self.watchlists.list_active_items(
                owner_id=request.owner_id,
                watchlist_id=request.watchlist_id,
            )
        )
        if trigger_group_enabled(request, "position", "signal"):
            drafts.extend(
                self._evaluate_position_triggers(
                    request=request,
                    portfolio_snapshots=portfolio_snapshots,
                )
            )
        if trigger_group_enabled(request, "watchlist"):
            drafts.extend(self._evaluate_watchlist_triggers(request=request, items=watchlist_items))
        if trigger_group_enabled(request, "recommendation"):
            drafts.extend(
                self._evaluate_recommendation_triggers(
                    request=request,
                    portfolio_snapshots=portfolio_snapshots,
                )
            )
        if trigger_group_enabled(request, "risk"):
            drafts.extend(
                self._evaluate_risk_triggers(
                    request=request,
                    portfolio_snapshots=portfolio_snapshots,
                    watchlist_items=watchlist_items,
                )
            )
        if trigger_group_enabled(request, "data_quality"):
            drafts.extend(
                self._evaluate_data_quality_triggers(
                    request=request,
                    portfolio_snapshots=portfolio_snapshots,
                    watchlist_items=watchlist_items,
                )
            )
        if trigger_group_enabled(request, "intraday_volatility"):
            intraday_result = self._evaluate_intraday_volatility_triggers(
                request=request,
                portfolio_snapshots=portfolio_snapshots,
                watchlist_items=watchlist_items,
            )
            drafts.extend(intraday_result.drafts)
            skipped_no_data_count += intraday_result.skipped_no_data_count
        return self._persist_drafts(
            request=request,
            drafts=drafts,
            skipped_no_data_count=skipped_no_data_count,
        )

    def persist_position_actions(
        self,
        actions: Sequence[PositionAction],
        as_of: datetime,
        cooldown_minutes: int = 15,
    ) -> TriggerEvaluationResult:
        """将盘中监控动作转换为持仓工作流触发事件并幂等写入。

        去重键包含动作和原因码，因此相同动作不会在冷却期内重复唤醒，
        动作发生变化时仍可及时触发；不可执行动作会保留原计划动作。
        """

        action_items = tuple(actions)
        drafts: list[TriggerEventDraft] = []
        for action in action_items:
            payload = dict(action.to_dict())
            context = action.payload or {}
            state_row = None
            if self.session is not None and hasattr(self.session, "get"):
                state_row = self.session.get(
                    PositionMonitoringStateORM,
                    f"monitoring:{action.position_id}",
                )
            payload.update(
                {
                    "owner_id": context.get("owner_id")
                    or getattr(state_row, "owner_id", None)
                    or "default-owner",
                    "portfolio_id": context.get("portfolio_id")
                    or getattr(state_row, "portfolio_id", None),
                    "asset_id": context.get("asset_id")
                    or getattr(state_row, "asset_id", None),
                    "symbol": context.get("symbol")
                    or getattr(state_row, "symbol", None),
                }
            )
            drafts.append(
                TriggerEventDraft(
                    trigger_type="position_monitoring_action",
                    requested_workflow_type="portfolio_monitoring",
                    severity=action.severity,
                    trigger_ref=action.position_id,
                    dedup_key=build_dedup_key(
                        owner_id=str(payload["owner_id"]),
                        trigger_type="position_monitoring_action",
                        requested_workflow_type="portfolio_monitoring",
                        scope_id=(
                            f"{action.position_id}:{action.action}:"
                            f"{','.join(action.reason_codes)}"
                        ),
                        asset_id=str(payload.get("asset_id") or action.position_id),
                    ),
                    portfolio_id=payload.get("portfolio_id"),
                    asset_id=payload.get("asset_id"),
                    payload=payload,
                )
            )
        request = TriggerEvaluationRequest(
            owner_id=str(
                (actions[0].payload or {}).get("owner_id", "default-owner")
                if action_items
                else "default-owner"
            ),
            as_of=as_of,
            cooldown_minutes=max(0, int(cooldown_minutes)),
        )
        return self._persist_drafts(request=request, drafts=drafts)

    def dispatch_pending(
        self,
        *,
        owner_id: str | None = None,
        limit: int = 20,
        as_of: datetime | None = None,
        agent_runtime: str | None = None,
        publisher: Callable[[AssistantTriggerEventORM], None] | None = None,
        retry_backoff_seconds: int = 30,
    ) -> AgentWakeupDispatchResult:
        """派发待处理触发事件到 Agent 唤醒队列。

        触发层只负责把事件交给上层 Hermes 或内部金融 Agent。Agent 被唤醒后，
        可以根据 `requested_workflow_type` 和 payload 决定是否调用内部 Workflow。
        """

        dispatch_time = as_of or datetime.now(UTC)
        dispatched: list[AssistantTriggerEventORM] = []
        skipped: list[AssistantTriggerEventORM] = []
        failed: list[AssistantTriggerEventORM] = []
        for event in self.triggers.list_pending_events(
            owner_id=owner_id,
            agent_runtime=agent_runtime,
            limit=limit,
        ):
            if not dispatch_retry_ready(event, dispatch_time):
                continue
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

            agent_task_id = build_trigger_agent_task_id(event=event)
            runtime = agent_runtime or event.agent_runtime
            if runtime == "hermes_agent":
                if publisher is None:
                    failed.append(
                        self.triggers.mark_dispatch_failed(
                            trigger_event_id=event.trigger_event_id,
                            failed_at=dispatch_time,
                            retry_at=dispatch_time
                            + timedelta(seconds=max(retry_backoff_seconds, 1)),
                            error_message="未配置 Hermes Webhook 发布器",
                        )
                    )
                    continue
                try:
                    publisher(event)
                except Exception as exc:
                    failed.append(
                        self.triggers.mark_dispatch_failed(
                            trigger_event_id=event.trigger_event_id,
                            failed_at=dispatch_time,
                            retry_at=dispatch_time
                            + timedelta(seconds=max(retry_backoff_seconds, 1)),
                            error_message=str(exc),
                        )
                    )
                    continue
            dispatched.append(
                self.triggers.mark_dispatched(
                    trigger_event_id=event.trigger_event_id,
                    agent_task_id=agent_task_id,
                    dispatched_at=dispatch_time,
                    agent_runtime=runtime,
                    payload={
                        "dispatch_status": "agent_wakeup_queued",
                        "agent_runtime": runtime,
                        "agent_task_id": agent_task_id,
                        "requested_workflow_type": event.requested_workflow_type,
                    },
                )
            )
        return AgentWakeupDispatchResult(
            dispatched_events=tuple(dispatched),
            skipped_events=tuple(skipped),
            failed_events=tuple(failed),
        )

    def run_once(
        self,
        request: TriggerEvaluationRequest,
        *,
        publisher: Callable[[AssistantTriggerEventORM], None] | None = None,
    ) -> JsonDict:
        """执行一次评估和派发。"""

        evaluation = self.evaluate(request)
        dispatch = self.dispatch_pending(
            owner_id=request.owner_id,
            limit=max(len(evaluation.created_events), 1),
            as_of=request.as_of,
            publisher=publisher,
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
                    requested_workflow_type="asset_deep_analysis",
                    severity="medium",
                    trigger_ref=item.watchlist_item_id,
                    dedup_key=build_dedup_key(
                        owner_id=request.owner_id,
                        trigger_type="watchlist_condition_hit",
                        requested_workflow_type="asset_deep_analysis",
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
                requested_workflow_type="recommendation_decision",
                severity="medium",
                trigger_ref=run.run_id,
                dedup_key=build_dedup_key(
                    owner_id=request.owner_id,
                    trigger_type="recommendation_run_ready",
                    requested_workflow_type="recommendation_decision",
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
            requested_workflow_type = (
                "portfolio_monitoring" if portfolio_id else "asset_deep_analysis"
            )
            drafts.append(
                TriggerEventDraft(
                    trigger_type="risk_event_detected",
                    requested_workflow_type=requested_workflow_type,
                    severity="high" if risk.severity == "critical" else "medium",
                    trigger_ref=risk.risk_id,
                    dedup_key=build_dedup_key(
                        owner_id=request.owner_id,
                        trigger_type="risk_event_detected",
                        requested_workflow_type=requested_workflow_type,
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
            requested_workflow_type = (
                "portfolio_monitoring" if portfolio_id else "asset_deep_analysis"
            )
            severity = "high" if quality.status == "missing" else "medium"
            drafts.append(
                TriggerEventDraft(
                    trigger_type="data_quality_degraded",
                    requested_workflow_type=requested_workflow_type,
                    severity=severity,
                    trigger_ref=quality.quality_id,
                    dedup_key=build_dedup_key(
                        owner_id=request.owner_id,
                        trigger_type="data_quality_degraded",
                        requested_workflow_type=requested_workflow_type,
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

    def _evaluate_intraday_volatility_triggers(
        self,
        *,
        request: TriggerEvaluationRequest,
        portfolio_snapshots: tuple[Any, ...],
        watchlist_items: tuple[WatchlistItemORM, ...],
    ) -> IntradayVolatilityDraftResult:
        """根据实时行情快照评估盘中急跌和放量异动。"""

        held_assets: dict[str, Any] = {
            position.asset_id: (snapshot.portfolio.portfolio_id, position)
            for snapshot in portfolio_snapshots
            for position in snapshot.positions
        }
        watched_assets = {item.asset_id: item for item in watchlist_items}
        asset_ids = sorted({*held_assets, *watched_assets})
        drafts: list[TriggerEventDraft] = []
        skipped_no_data_count = 0
        for asset_id in asset_ids:
            draft_count_before = len(drafts)
            snapshots = self._load_recent_realtime_quote_snapshots(
                asset_id=asset_id,
                since=request.as_of - timedelta(minutes=request.intraday_quote_window_minutes),
            )
            latest, previous = first_two_price_snapshots(snapshots)
            if latest is None:
                skipped_no_data_count += 1
                continue
            latest_price = decimal_or_none(latest.last_price)
            previous_price = decimal_or_none(previous.last_price) if previous is not None else None
            if previous_price is None:
                # 覆盖式临时表只保留最新值，使用同一行的昨收计算当前跌幅；
                # 这条路径只用于风险提醒，成交量突增仍要求有历史样本。
                previous_price = decimal_or_none(getattr(latest, "prev_close", None))
            if latest_price is None or previous_price is None or previous_price <= 0:
                skipped_no_data_count += 1
                continue

            price_change_ratio = (latest_price - previous_price) / previous_price
            portfolio_id, position = held_assets.get(asset_id, (None, None))
            watchlist_item = watched_assets.get(asset_id)
            requested_workflow_type = (
                "portfolio_monitoring" if portfolio_id else "asset_deep_analysis"
            )
            scope_id = str(portfolio_id or getattr(watchlist_item, "watchlist_id", ""))
            symbol = str(
                getattr(position, "symbol", "")
                or getattr(watchlist_item, "symbol", "")
                or latest.symbol
                or asset_id
            )
            trigger_ref = f"{asset_id}:{latest.as_of.isoformat()}"
            if price_change_ratio <= request.intraday_sharp_drop_threshold:
                drafts.append(
                    TriggerEventDraft(
                        trigger_type="intraday_sharp_drop",
                        requested_workflow_type=requested_workflow_type,
                        severity="high",
                        trigger_ref=trigger_ref,
                        dedup_key=build_dedup_key(
                            owner_id=request.owner_id,
                            trigger_type="intraday_sharp_drop",
                            requested_workflow_type=requested_workflow_type,
                            asset_id=asset_id,
                            scope_id=scope_id,
                        ),
                        portfolio_id=portfolio_id,
                        watchlist_id=getattr(watchlist_item, "watchlist_id", None),
                        asset_id=asset_id,
                        payload={
                            "reason": f"{symbol} 盘中快速下跌，触发风险监控。",
                            "symbol": symbol,
                            "latest_price": json_value(latest_price),
                            "previous_price": json_value(previous_price),
                            "price_change_ratio": format_decimal_ratio(price_change_ratio),
                            "latest_quote_at": json_value(latest.as_of),
                            "previous_quote_at": json_value(previous.as_of)
                            if previous is not None
                            else None,
                            "rule": "intraday_sharp_drop",
                        },
                    )
                )

            volume_result = intraday_volume_surge_ratio(
                latest=latest,
                history=snapshots[1:21] if previous is not None else [],
            )
            if volume_result is None:
                if len(drafts) == draft_count_before:
                    skipped_no_data_count += 1
                continue
            volume_surge_multiplier, baseline_volume = volume_result
            if (
                volume_surge_multiplier >= request.intraday_volume_surge_multiplier
                and abs(price_change_ratio) >= request.intraday_price_change_threshold
            ):
                drafts.append(
                    TriggerEventDraft(
                        trigger_type="intraday_volume_surge",
                        requested_workflow_type=requested_workflow_type,
                        severity="medium",
                        trigger_ref=trigger_ref,
                        dedup_key=build_dedup_key(
                            owner_id=request.owner_id,
                            trigger_type="intraday_volume_surge",
                            requested_workflow_type=requested_workflow_type,
                            asset_id=asset_id,
                            scope_id=scope_id,
                        ),
                        portfolio_id=portfolio_id,
                        watchlist_id=getattr(watchlist_item, "watchlist_id", None),
                        asset_id=asset_id,
                        payload={
                            "reason": f"{symbol} 盘中成交量显著放大，触发异动分析。",
                            "symbol": symbol,
                            "latest_price": json_value(latest_price),
                            "previous_price": json_value(previous_price),
                            "price_change_ratio": format_decimal_ratio(price_change_ratio),
                            "latest_volume": json_value(decimal_or_none(latest.volume)),
                            "baseline_volume": json_value(baseline_volume),
                            "volume_surge_multiplier": format_decimal_ratio(
                                volume_surge_multiplier
                            ),
                            "latest_quote_at": json_value(latest.as_of),
                            "rule": "intraday_volume_surge",
                        },
                    )
                )
        return IntradayVolatilityDraftResult(
            drafts=tuple(drafts),
            skipped_no_data_count=skipped_no_data_count,
        )

    def _load_recent_realtime_quote_snapshots(
        self,
        *,
        asset_id: str,
        since: datetime,
    ) -> list[Any]:
        """读取单个标的最近实时行情快照。"""

        statement = (
            select(RealtimeQuoteSnapshotORM)
            .where(
                RealtimeQuoteSnapshotORM.asset_id == asset_id,
                RealtimeQuoteSnapshotORM.as_of >= since,
                RealtimeQuoteSnapshotORM.status == "available",
            )
            .order_by(RealtimeQuoteSnapshotORM.as_of.desc())
            .limit(21)
        )
        rows = list(self.session.scalars(statement))
        if rows:
            return rows

        latest_statement = (
            select(IntradayQuoteLatestORM)
            .where(
                IntradayQuoteLatestORM.asset_id == asset_id,
                IntradayQuoteLatestORM.updated_at >= since,
                IntradayQuoteLatestORM.status == "available",
                IntradayQuoteLatestORM.quality_status == "available",
            )
            .order_by(IntradayQuoteLatestORM.updated_at.desc())
        )
        return list(self.session.scalars(latest_statement))

    def _persist_drafts(
        self,
        *,
        request: TriggerEvaluationRequest,
        drafts: list[TriggerEventDraft],
        skipped_no_data_count: int = 0,
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
                    owner_id=str((draft.payload or {}).get("owner_id") or request.owner_id),
                    trigger_type=draft.trigger_type,
                    trigger_ref=draft.trigger_ref,
                    dedup_key=draft.dedup_key,
                    severity=draft.severity,
                    status="pending",
                    requested_workflow_type=draft.requested_workflow_type,
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
            skipped_no_data_count=skipped_no_data_count,
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
        requested_workflow_type="portfolio_monitoring",
        severity=severity,
        trigger_ref=position.position_id,
        dedup_key=build_dedup_key(
            owner_id=request.owner_id,
            trigger_type=trigger_type,
            requested_workflow_type="portfolio_monitoring",
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


def trigger_group_enabled(request: TriggerEvaluationRequest, *groups: str) -> bool:
    """判断当前请求是否启用了指定触发规则组。"""

    if not request.trigger_groups:
        return True
    enabled_groups = {group for group in request.trigger_groups if group}
    return any(group in enabled_groups for group in groups)


def first_two_price_snapshots(
    snapshots: list[RealtimeQuoteSnapshotORM],
) -> tuple[RealtimeQuoteSnapshotORM | None, RealtimeQuoteSnapshotORM | None]:
    """返回最近两个具备价格的实时快照。"""

    price_snapshots = [
        snapshot for snapshot in snapshots if decimal_or_none(snapshot.last_price) is not None
    ]
    if len(price_snapshots) < 2:
        return None, None
    return price_snapshots[0], price_snapshots[1]


def intraday_volume_surge_ratio(
    *,
    latest: RealtimeQuoteSnapshotORM,
    history: list[RealtimeQuoteSnapshotORM],
) -> tuple[Decimal, Decimal] | None:
    """计算当前成交量相对近 20 个快照的放量倍数。"""

    latest_volume = decimal_or_none(latest.volume)
    if latest_volume is None:
        return None
    history_volumes: list[Decimal] = []
    for snapshot in history:
        volume = decimal_or_none(snapshot.volume)
        if volume is not None and volume > 0:
            history_volumes.append(volume)
    if len(history_volumes) < 20:
        return None
    baseline_volume = sum(history_volumes[:20], Decimal("0")) / Decimal("20")
    if baseline_volume <= 0:
        return None
    return latest_volume / baseline_volume, baseline_volume


def decimal_or_none(value: Any) -> Decimal | None:
    """把数值安全转换为 Decimal。"""

    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def format_decimal_ratio(value: Decimal) -> str:
    """把比例格式化为固定 6 位小数字符串，便于前端和日志展示。"""

    return str(value.quantize(Decimal("0.000001")))


def validate_dispatch_requirements(event: AssistantTriggerEventORM) -> str | None:
    """检查唤醒 Agent 时建议分析流程所需的上下文引用。"""

    if event.requested_workflow_type in {"portfolio_monitoring", "recommendation_decision"}:
        if not event.portfolio_id:
            return f"{event.requested_workflow_type} 缺少 portfolio_id"
    if event.requested_workflow_type in {"watchlist_management", "recommendation_decision"}:
        if not event.watchlist_id:
            return f"{event.requested_workflow_type} 缺少 watchlist_id"
    if (
        event.requested_workflow_type == "recommendation_decision"
        and not event.recommendation_run_id
    ):
        return "recommendation_decision 缺少 recommendation_run_id"
    if event.requested_workflow_type == "asset_deep_analysis" and not event.asset_id:
        return "asset_deep_analysis 缺少 asset_id"
    return None


def dispatch_retry_ready(event: AssistantTriggerEventORM, as_of: datetime) -> bool:
    """判断失败事件是否已经到达下一次发布时间。"""

    retry_at = (event.payload or {}).get("dispatch_retry_at")
    if not retry_at:
        return True
    try:
        parsed = datetime.fromisoformat(str(retry_at))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    current = as_of if as_of.tzinfo else as_of.replace(tzinfo=UTC)
    return parsed <= current


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
        "agent_runtime": event.agent_runtime,
        "agent_task_id": event.agent_task_id,
        "requested_workflow_type": event.requested_workflow_type,
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
    requested_workflow_type: str,
    asset_id: str | None = None,
    scope_id: str | None = None,
) -> str:
    """生成冷却去重键。"""

    return ":".join(
        value
        for value in (owner_id, trigger_type, requested_workflow_type, scope_id, asset_id)
        if value
    )


def build_trigger_event_id(*, draft: TriggerEventDraft, as_of: datetime) -> str:
    """生成触发事件 ID。"""

    digest = sha1(
        f"{draft.dedup_key}:{draft.trigger_ref}:{as_of.isoformat()}".encode()
    ).hexdigest()[:16]
    return f"trigger:{draft.trigger_type}:{digest}"


def build_trigger_agent_task_id(*, event: AssistantTriggerEventORM) -> str:
    """生成由触发事件派发的 Agent 唤醒任务 ID。"""

    owner = event.owner_id.replace(":", "_")
    digest = sha1(event.trigger_event_id.encode()).hexdigest()[:12]
    return f"agent_task:{owner}:{event.trigger_type}:trigger:{digest}"


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
