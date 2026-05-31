"""私人金融主 Agent 的最小服务编排。

第一版先提供确定性的服务入口，供 CLI、Scheduler、Hermes 或 MCP 后续调用。
它不直接抓行情、不计算因子、不改评分，只编排应用服务和底层工作流。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha1
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.agents.runtime import LangGraphWorkflowAdapter, WorkflowNodeEvent
from finance_agent.agents.workflows import (
    LangGraphWorkflowBuilder,
    PortfolioMonitoringInput,
    PortfolioMonitoringResult,
    PortfolioMonitoringWorkflow,
    RecommendationDecisionInput,
    RecommendationDecisionResult,
    RecommendationDecisionWorkflow,
    WatchlistManagementInput,
    WatchlistManagementResult,
    WatchlistManagementWorkflow,
    list_langgraph_workflow_builders,
)
from finance_agent.application import (
    MemoryService,
    PortfolioService,
    WatchlistService,
    WorkflowService,
)
from finance_agent.storage.repositories import (
    MemoryRepository,
    RecommendationRepository,
    RiskRepository,
    SignalSnapshotRepository,
)

MAX_DATABASE_ID_LENGTH = 160
COMPACT_ID_DIGEST_LENGTH = 24


@dataclass(frozen=True)
class PortfolioMonitoringRunSummary:
    """一次持仓监控闭环运行摘要。"""

    workflow_run_id: str
    decision_ids: tuple[str, ...]
    memory_ids: tuple[str, ...]
    review_task_ids: tuple[str, ...]
    result: PortfolioMonitoringResult


@dataclass(frozen=True)
class WatchlistManagementRunSummary:
    """一次观察池管理闭环运行摘要。"""

    workflow_run_id: str
    decision_ids: tuple[str, ...]
    memory_ids: tuple[str, ...]
    review_task_ids: tuple[str, ...]
    result: WatchlistManagementResult


@dataclass(frozen=True)
class RecommendationIntakeRunSummary:
    """一次推荐结果进入私人观察池的运行摘要。"""

    workflow_run_id: str
    watchlist_item_ids: tuple[str, ...]
    decision_ids: tuple[str, ...]
    memory_ids: tuple[str, ...]
    review_task_ids: tuple[str, ...]


@dataclass(frozen=True)
class RecommendationAgentDecisionRunSummary:
    """一次推荐 Agent 决策运行摘要。"""

    workflow_run_id: str
    watchlist_item_ids: tuple[str, ...]
    decision_ids: tuple[str, ...]
    memory_ids: tuple[str, ...]
    review_task_ids: tuple[str, ...]
    result: RecommendationDecisionResult


@dataclass(frozen=True)
class FinanceWorkflowRunSummary:
    """统一 Workflow 调度运行摘要。"""

    workflow_run_id: str
    workflow_type: str
    final_state: dict[str, Any]
    report: dict[str, Any] | None


class PersonalFinanceAgentService:
    """私人金融主 Agent 服务。"""

    def __init__(self, session: Session) -> None:
        self.portfolios = PortfolioService(session)
        self.watchlists = WatchlistService(session)
        self.memory = MemoryService(session)
        self.workflow_audit = WorkflowService(session)
        self.langgraph_adapter = LangGraphWorkflowAdapter(session)
        self.signals = SignalSnapshotRepository(session)
        self.risks = RiskRepository(session)
        self.recommendations = RecommendationRepository(session)
        self.memories = MemoryRepository(session)
        self.portfolio_workflow = PortfolioMonitoringWorkflow()
        self.watchlist_workflow = WatchlistManagementWorkflow()
        self.recommendation_decision_workflow = RecommendationDecisionWorkflow()

    def monitor_portfolio(
        self,
        *,
        owner_id: str,
        portfolio_id: str,
        as_of: datetime,
        workflow_run_id: str | None = None,
    ) -> PortfolioMonitoringRunSummary:
        """执行一次持仓监控闭环。"""

        snapshot = self.portfolios.load_portfolio_snapshot(portfolio_id)
        run_id = workflow_run_id or build_workflow_run_id(portfolio_id=portfolio_id, as_of=as_of)
        self.workflow_audit.start_run(
            workflow_run_id=run_id,
            owner_id=owner_id,
            workflow_type="portfolio_monitoring",
            trigger_type="schedule",
            trigger_ref=portfolio_id,
            started_at=as_of,
            input_ref=f"portfolio:{portfolio_id}:snapshot:{as_of.isoformat()}",
            payload={
                "portfolio_id": portfolio_id,
                "position_count": len(snapshot.positions),
            },
        )
        self.workflow_audit.record_event(
            workflow_event_id=f"{run_id}:event:load_context",
            workflow_run_id=run_id,
            event_type="context_loaded",
            agent_name="PersonalFinanceAgent",
            message="已读取组合持仓、信号、风险和 Finance Memory 上下文。",
            created_at=as_of,
            payload={"portfolio_id": portfolio_id},
        )

        workflow_input = PortfolioMonitoringInput(
            owner_id=owner_id,
            portfolio=snapshot.portfolio,
            positions=snapshot.positions,
            signals_by_asset={
                position.asset_id: self.signals.get_latest_signal(
                    asset_id=position.asset_id,
                    horizon="swing",
                )
                for position in snapshot.positions
            },
            risks_by_asset={
                position.asset_id: tuple(
                    self.risks.list_recent_risks(asset_id=position.asset_id, limit=5)
                )
                for position in snapshot.positions
            },
            memories_by_asset={
                position.asset_id: tuple(
                    self.memories.list_active_memories(
                        owner_id=owner_id,
                        asset_id=position.asset_id,
                        limit=5,
                    )
                )
                for position in snapshot.positions
            },
            as_of=as_of,
        )
        result = self.portfolio_workflow.run(workflow_input)
        decision_ids: list[str] = []
        memory_ids: list[str] = []
        review_task_ids: list[str] = []

        for decision in result.decisions:
            alert = self.memory.record_alert(
                alert_id=build_alert_id(run_id=run_id, asset_id=decision.asset_id),
                owner_id=owner_id,
                portfolio_id=portfolio_id,
                asset_id=decision.asset_id,
                alert_type="portfolio_monitoring",
                severity=decision.severity,
                triggered_by="schedule",
                trigger_condition=decision.trigger_condition,
                as_of=as_of,
                payload={"workflow_run_id": run_id, "summary": decision.summary},
            )
            decision_log = self.memory.record_decision(
                decision_id=build_decision_id(
                    run_id=run_id,
                    asset_id=decision.asset_id,
                    decision_type=decision.decision_type,
                ),
                owner_id=owner_id,
                portfolio_id=portfolio_id,
                asset_id=decision.asset_id,
                decision_type=decision.decision_type,
                source_alert_id=alert.alert_id,
                workflow_run_id=run_id,
                suggested_action=decision.suggested_action,
                user_action="unknown",
                summary=decision.summary,
                reason_ids=[],
                risk_ids=list(decision.risk_ids),
                evidence_ids=list(decision.evidence_ids),
                created_at=as_of,
                payload={
                    "risk_rebuttal": decision.risk_rebuttal,
                    "thesis": decision.thesis,
                    "signal_ids": decision.signal_ids,
                },
            )
            finance_memory = self.memory.upsert_memory(
                memory_id=build_memory_id(run_id=run_id, asset_id=decision.asset_id),
                owner_id=owner_id,
                memory_type="decision_summary",
                scope="asset",
                asset_id=decision.asset_id,
                source_decision_id=decision_log.decision_id,
                content=f"{decision.summary} 风险反驳：{decision.risk_rebuttal}",
                confidence=Decimal("0.800000"),
                payload={"workflow_run_id": run_id},
            )
            self.memory.link_memory_edge(
                edge_id=build_edge_id(
                    source_id=decision_log.decision_id,
                    target_id=finance_memory.memory_id,
                ),
                owner_id=owner_id,
                source_type="decision",
                source_id=decision_log.decision_id,
                relation_type="supports",
                target_type="memory",
                target_id=finance_memory.memory_id,
                confidence=Decimal("0.900000"),
                reason="持仓监控决策沉淀为 Finance Memory。",
            )
            review_task = self.memory.schedule_review(
                review_task_id=build_review_task_id(run_id=run_id, asset_id=decision.asset_id),
                owner_id=owner_id,
                asset_id=decision.asset_id,
                source_decision_id=decision_log.decision_id,
                review_type="decision_outcome",
                due_at=as_of + timedelta(days=3),
                review_questions=list(decision.review_questions),
                payload={"workflow_run_id": run_id},
            )
            decision_ids.append(decision_log.decision_id)
            memory_ids.append(finance_memory.memory_id)
            review_task_ids.append(review_task.review_task_id)
            self.workflow_audit.record_event(
                workflow_event_id=f"{run_id}:event:decision:{decision.asset_id}",
                workflow_run_id=run_id,
                event_type="decision_recorded",
                agent_name="portfolio_decision",
                message=decision.summary,
                evidence_ids=list(decision.evidence_ids),
                created_at=as_of,
                payload={"decision_id": decision_log.decision_id},
            )

        self.workflow_audit.finish_run(
            workflow_run_id=run_id,
            owner_id=owner_id,
            workflow_type="portfolio_monitoring",
            trigger_type="schedule",
            trigger_ref=portfolio_id,
            status="succeeded",
            started_at=as_of,
            finished_at=as_of,
            input_ref=f"portfolio:{portfolio_id}:snapshot:{as_of.isoformat()}",
            output_ref=f"portfolio_monitoring:{run_id}:decisions",
            payload={
                "decision_ids": decision_ids,
                "memory_ids": memory_ids,
                "review_task_ids": review_task_ids,
            },
        )
        return PortfolioMonitoringRunSummary(
            workflow_run_id=run_id,
            decision_ids=tuple(decision_ids),
            memory_ids=tuple(memory_ids),
            review_task_ids=tuple(review_task_ids),
            result=result,
        )

    def manage_watchlist(
        self,
        *,
        owner_id: str,
        watchlist_id: str,
        as_of: datetime,
        workflow_run_id: str | None = None,
    ) -> WatchlistManagementRunSummary:
        """执行一次观察池管理闭环。"""

        watchlist = self.watchlists.get_watchlist(watchlist_id)
        items = self.watchlists.list_active_items(owner_id=owner_id, watchlist_id=watchlist_id)
        run_id = workflow_run_id or build_watchlist_workflow_run_id(
            watchlist_id=watchlist_id,
            as_of=as_of,
        )
        self.workflow_audit.start_run(
            workflow_run_id=run_id,
            owner_id=owner_id,
            workflow_type="watchlist_management",
            trigger_type="schedule",
            trigger_ref=watchlist_id,
            started_at=as_of,
            input_ref=f"watchlist:{watchlist_id}:snapshot:{as_of.isoformat()}",
            payload={
                "watchlist_id": watchlist_id,
                "item_count": len(items),
            },
        )
        self.workflow_audit.record_event(
            workflow_event_id=f"{run_id}:event:load_context",
            workflow_run_id=run_id,
            event_type="context_loaded",
            agent_name="PersonalFinanceAgent",
            message="已读取观察池、信号、风险、投资假设和 Finance Memory 上下文。",
            created_at=as_of,
            payload={"watchlist_id": watchlist_id},
        )

        workflow_input = WatchlistManagementInput(
            owner_id=owner_id,
            watchlist=watchlist,
            items=items,
            signals_by_asset={
                item.asset_id: self.signals.get_latest_signal(
                    asset_id=item.asset_id,
                    horizon="swing",
                )
                for item in items
            },
            risks_by_asset={
                item.asset_id: tuple(
                    self.risks.list_recent_risks(asset_id=item.asset_id, limit=5)
                )
                for item in items
            },
            memories_by_asset={
                item.asset_id: tuple(
                    self.memories.list_active_memories(
                        owner_id=owner_id,
                        asset_id=item.asset_id,
                        limit=5,
                    )
                )
                for item in items
            },
            theses_by_asset={
                item.asset_id: self.watchlists.list_asset_theses(
                    owner_id=owner_id,
                    asset_id=item.asset_id,
                )
                for item in items
            },
            as_of=as_of,
        )
        result = self.watchlist_workflow.run(workflow_input)
        decision_ids: list[str] = []
        memory_ids: list[str] = []
        review_task_ids: list[str] = []

        items_by_id = {item.watchlist_item_id: item for item in items}
        for decision in result.decisions:
            source_item = items_by_id[decision.watchlist_item_id]
            self.watchlists.transition_item(
                item=source_item,
                status=decision.next_status,
                next_review_at=decision.next_review_at,
                removed_at=as_of if decision.next_status == "removed" else None,
                removed_reason=decision.removed_reason,
                owner_id=owner_id,
                event_type="watchlist_transition",
                reason=decision.summary,
                event_at=as_of,
                payload={
                    "workflow_run_id": run_id,
                    "suggested_action": decision.suggested_action,
                    "last_reviewed_at": as_of.isoformat(),
                    "original_intake_reason": source_item.reason,
                    "daily_watch_reason": decision.daily_watch_reason,
                    "risk_rebuttal": decision.risk_rebuttal,
                },
            )
            alert = self.memory.record_alert(
                alert_id=build_alert_id(run_id=run_id, asset_id=decision.asset_id),
                owner_id=owner_id,
                asset_id=decision.asset_id,
                alert_type="watchlist_management",
                severity=decision.severity,
                triggered_by="schedule",
                trigger_condition=decision.trigger_condition,
                as_of=as_of,
                payload={
                    "watchlist_id": watchlist_id,
                    "watchlist_item_id": decision.watchlist_item_id,
                    "workflow_run_id": run_id,
                    "summary": decision.summary,
                },
            )
            decision_log = self.memory.record_decision(
                decision_id=build_decision_id(
                    run_id=run_id,
                    asset_id=decision.asset_id,
                    decision_type=decision.decision_type,
                ),
                owner_id=owner_id,
                asset_id=decision.asset_id,
                decision_type=decision.decision_type,
                source_alert_id=alert.alert_id,
                workflow_run_id=run_id,
                suggested_action=decision.suggested_action,
                user_action="unknown",
                summary=decision.summary,
                reason_ids=list(decision.thesis_ids),
                risk_ids=list(decision.risk_ids),
                evidence_ids=list(decision.evidence_ids),
                created_at=as_of,
                payload={
                    "watchlist_id": watchlist_id,
                    "watchlist_item_id": decision.watchlist_item_id,
                    "next_status": decision.next_status,
                    "original_intake_reason": source_item.reason,
                    "daily_watch_reason": decision.daily_watch_reason,
                    "risk_rebuttal": decision.risk_rebuttal,
                    "signal_ids": decision.signal_ids,
                },
            )
            if decision.next_status != "removed":
                daily_reason_event = self.watchlists.record_event(
                    event_id=build_daily_watch_reason_event_id(
                        watchlist_item_id=source_item.watchlist_item_id,
                        as_of=as_of,
                    ),
                    owner_id=owner_id,
                    watchlist_id=watchlist_id,
                    watchlist_item_id=source_item.watchlist_item_id,
                    asset_id=decision.asset_id,
                    event_type="daily_watch_reason",
                    from_status=source_item.status,
                    to_status=decision.next_status,
                    reason=decision.daily_watch_reason,
                    source_decision_id=decision_log.decision_id,
                    created_at=as_of,
                    payload={
                        "workflow_run_id": run_id,
                        "watchlist_id": watchlist_id,
                        "watchlist_item_id": source_item.watchlist_item_id,
                        "original_intake_reason": source_item.reason,
                        "daily_watch_reason": decision.daily_watch_reason,
                        "suggested_action": decision.suggested_action,
                        "next_status": decision.next_status,
                        "risk_rebuttal": decision.risk_rebuttal,
                        "signal_ids": decision.signal_ids,
                        "risk_ids": decision.risk_ids,
                        "thesis_ids": decision.thesis_ids,
                    },
                )
                daily_reason_memory = self.memory.upsert_memory(
                    memory_id=build_typed_memory_id(
                        run_id=run_id,
                        asset_id=decision.asset_id,
                        memory_type="watchlist_daily_reason",
                    ),
                    owner_id=owner_id,
                    memory_type="watchlist_daily_reason",
                    scope="asset",
                    asset_id=decision.asset_id,
                    source_decision_id=decision_log.decision_id,
                    content=(
                        f"{decision.symbol} 今日继续关注原因："
                        f"{decision.daily_watch_reason} 风险反驳：{decision.risk_rebuttal}"
                    ),
                    confidence=Decimal("0.780000"),
                    payload={
                        "workflow_run_id": run_id,
                        "watchlist_id": watchlist_id,
                        "watchlist_item_id": source_item.watchlist_item_id,
                        "source_type": "watchlist_item_event",
                        "source_id": daily_reason_event.event_id,
                        "original_intake_reason": source_item.reason,
                        "daily_watch_reason": decision.daily_watch_reason,
                        "next_status": decision.next_status,
                    },
                )
                self.memory.link_memory_edge(
                    edge_id=build_edge_id(
                        source_id=daily_reason_event.event_id,
                        target_id=daily_reason_memory.memory_id,
                    ),
                    owner_id=owner_id,
                    source_type="watchlist_event",
                    source_id=daily_reason_event.event_id,
                    relation_type="summarizes",
                    target_type="memory",
                    target_id=daily_reason_memory.memory_id,
                    confidence=Decimal("0.900000"),
                    reason="每日观察原因沉淀为 Finance Memory。",
                )
                memory_ids.append(daily_reason_memory.memory_id)
            finance_memory = self.memory.upsert_memory(
                memory_id=build_memory_id(run_id=run_id, asset_id=decision.asset_id),
                owner_id=owner_id,
                memory_type="watchlist_decision",
                scope="asset",
                asset_id=decision.asset_id,
                source_decision_id=decision_log.decision_id,
                content=f"{decision.summary} 风险反驳：{decision.risk_rebuttal}",
                confidence=Decimal("0.800000"),
                payload={
                    "watchlist_id": watchlist_id,
                    "workflow_run_id": run_id,
                    "next_status": decision.next_status,
                    "daily_watch_reason": decision.daily_watch_reason,
                },
            )
            self.memory.link_memory_edge(
                edge_id=build_edge_id(
                    source_id=decision_log.decision_id,
                    target_id=finance_memory.memory_id,
                ),
                owner_id=owner_id,
                source_type="decision",
                source_id=decision_log.decision_id,
                relation_type="supports",
                target_type="memory",
                target_id=finance_memory.memory_id,
                confidence=Decimal("0.900000"),
                reason="观察池管理决策沉淀为 Finance Memory。",
            )
            review_task = self.memory.schedule_review(
                review_task_id=build_review_task_id(run_id=run_id, asset_id=decision.asset_id),
                owner_id=owner_id,
                asset_id=decision.asset_id,
                source_decision_id=decision_log.decision_id,
                review_type="watchlist_followup",
                due_at=decision.next_review_at or as_of + timedelta(days=7),
                review_questions=list(decision.review_questions),
                payload={
                    "watchlist_id": watchlist_id,
                    "workflow_run_id": run_id,
                    "next_status": decision.next_status,
                },
            )
            decision_ids.append(decision_log.decision_id)
            memory_ids.append(finance_memory.memory_id)
            review_task_ids.append(review_task.review_task_id)
            self.workflow_audit.record_event(
                workflow_event_id=f"{run_id}:event:decision:{decision.asset_id}",
                workflow_run_id=run_id,
                event_type="decision_recorded",
                agent_name="watchlist_manager",
                message=decision.summary,
                evidence_ids=list(decision.evidence_ids),
                created_at=as_of,
                payload={
                    "decision_id": decision_log.decision_id,
                    "next_status": decision.next_status,
                },
            )

        self.workflow_audit.finish_run(
            workflow_run_id=run_id,
            owner_id=owner_id,
            workflow_type="watchlist_management",
            trigger_type="schedule",
            trigger_ref=watchlist_id,
            status="succeeded",
            started_at=as_of,
            finished_at=as_of,
            input_ref=f"watchlist:{watchlist_id}:snapshot:{as_of.isoformat()}",
            output_ref=f"watchlist_management:{run_id}:decisions",
            payload={
                "decision_ids": decision_ids,
                "memory_ids": memory_ids,
                "review_task_ids": review_task_ids,
            },
        )
        return WatchlistManagementRunSummary(
            workflow_run_id=run_id,
            decision_ids=tuple(decision_ids),
            memory_ids=tuple(memory_ids),
            review_task_ids=tuple(review_task_ids),
            result=result,
        )

    def sync_recommendations_to_watchlist(
        self,
        *,
        owner_id: str,
        recommendation_run_id: str,
        watchlist_id: str,
        as_of: datetime,
        limit: int = 10,
        workflow_run_id: str | None = None,
    ) -> RecommendationIntakeRunSummary:
        """把一次推荐运行的 Top N 同步到私人观察池。"""

        run_id = workflow_run_id or build_recommendation_intake_workflow_run_id(
            recommendation_run_id=recommendation_run_id,
            watchlist_id=watchlist_id,
            as_of=as_of,
        )
        self.workflow_audit.start_run(
            workflow_run_id=run_id,
            owner_id=owner_id,
            workflow_type="recommendation_intake",
            trigger_type="recommendation_run",
            trigger_ref=recommendation_run_id,
            started_at=as_of,
            input_ref=f"recommendation_run:{recommendation_run_id}",
            payload={"watchlist_id": watchlist_id, "limit": limit},
        )
        recommendations = self.recommendations.list_top_recommendations(
            run_id=recommendation_run_id,
            limit=limit,
        )
        self.workflow_audit.record_event(
            workflow_event_id=f"{run_id}:event:load_recommendations",
            workflow_run_id=run_id,
            event_type="recommendations_loaded",
            agent_name="PersonalFinanceAgent",
            message="已读取推荐榜，准备同步到私人观察池。",
            created_at=as_of,
            payload={
                "recommendation_run_id": recommendation_run_id,
                "recommendation_count": len(recommendations),
                "watchlist_id": watchlist_id,
            },
        )

        watchlist_item_ids: list[str] = []
        decision_ids: list[str] = []
        memory_ids: list[str] = []
        review_task_ids: list[str] = []

        for recommendation in recommendations:
            if recommendation.action == "avoid":
                self.workflow_audit.record_event(
                    workflow_event_id=(
                        f"{run_id}:event:skip:{recommendation.asset_id}"
                    ),
                    workflow_run_id=run_id,
                    event_type="recommendation_skipped",
                    agent_name="recommendation_intake",
                    message="推荐动作为 avoid，已跳过入池。",
                    evidence_ids=list(recommendation.evidence_ids),
                    created_at=as_of,
                    payload={
                        "recommendation_id": recommendation.recommendation_id,
                        "action": recommendation.action,
                    },
                )
                continue

            watchlist_item_id = build_recommendation_watchlist_item_id(
                watchlist_id=watchlist_id,
                asset_id=recommendation.asset_id,
            )
            reason = recommendation.summary or (
                f"{recommendation.name} 推荐排名第 {recommendation.rank}，"
                f"评分 {recommendation.total_score}，适合进入观察池。"
            )
            item = self.watchlists.add_or_update_item(
                watchlist_item_id=watchlist_item_id,
                watchlist_id=watchlist_id,
                asset_id=recommendation.asset_id,
                symbol=recommendation.symbol,
                market=recommendation.market,
                source_type="recommendation",
                source_id=recommendation.recommendation_id,
                reason=reason,
                watch_conditions=recommendation.watch_conditions,
                trigger_conditions=recommendation.watch_conditions,
                invalid_conditions=recommendation.invalid_if,
                risk_level=map_recommendation_risk_level(
                    action=recommendation.action,
                    conviction=recommendation.conviction,
                ),
                status="active",
                next_review_at=as_of + timedelta(days=2),
                payload={
                    "workflow_run_id": run_id,
                    "recommendation_run_id": recommendation_run_id,
                    "rank": recommendation.rank,
                    "total_score": str(recommendation.total_score),
                    "confidence": str(recommendation.confidence),
                    "conviction": recommendation.conviction,
                },
            )
            self.watchlists.record_event(
                event_id=build_recommendation_intake_event_id(
                    watchlist_item_id=item.watchlist_item_id,
                    recommendation_id=recommendation.recommendation_id,
                    as_of=as_of,
                ),
                owner_id=owner_id,
                watchlist_id=watchlist_id,
                watchlist_item_id=item.watchlist_item_id,
                asset_id=recommendation.asset_id,
                event_type="recommendation_intake",
                from_status=None,
                to_status=item.status,
                reason=reason,
                created_at=as_of,
                payload={
                    "workflow_run_id": run_id,
                    "recommendation_run_id": recommendation_run_id,
                    "recommendation_id": recommendation.recommendation_id,
                },
            )
            alert = self.memory.record_alert(
                alert_id=build_alert_id(run_id=run_id, asset_id=recommendation.asset_id),
                owner_id=owner_id,
                asset_id=recommendation.asset_id,
                alert_type="recommendation_intake",
                severity=map_recommendation_severity(
                    action=recommendation.action,
                    conviction=recommendation.conviction,
                ),
                triggered_by="recommendation_run",
                trigger_condition=(
                    f"推荐运行 {recommendation_run_id} 给出 "
                    f"{recommendation.action}，排名 {recommendation.rank}。"
                ),
                as_of=as_of,
                payload={
                    "workflow_run_id": run_id,
                    "watchlist_id": watchlist_id,
                    "watchlist_item_id": item.watchlist_item_id,
                    "recommendation_id": recommendation.recommendation_id,
                },
            )
            decision_log = self.memory.record_decision(
                decision_id=build_decision_id(
                    run_id=run_id,
                    asset_id=recommendation.asset_id,
                    decision_type="recommendation_intake",
                ),
                owner_id=owner_id,
                asset_id=recommendation.asset_id,
                decision_type="recommendation_intake",
                source_recommendation_id=recommendation.recommendation_id,
                source_alert_id=alert.alert_id,
                workflow_run_id=run_id,
                suggested_action="add_to_watchlist",
                user_action="unknown",
                summary=reason,
                reason_ids=[
                    value
                    for value in [
                        recommendation.score_id,
                        recommendation.factor_frame_id,
                    ]
                    if value is not None
                ],
                risk_ids=list(recommendation.risk_ids),
                evidence_ids=list(recommendation.evidence_ids),
                created_at=as_of,
                payload={
                    "watchlist_id": watchlist_id,
                    "watchlist_item_id": item.watchlist_item_id,
                    "recommendation_run_id": recommendation_run_id,
                    "recommendation_action": recommendation.action,
                    "signal_ids": recommendation.signal_ids,
                    "watch_conditions": recommendation.watch_conditions,
                    "invalid_if": recommendation.invalid_if,
                },
            )
            finance_memory = self.memory.upsert_memory(
                memory_id=build_memory_id(run_id=run_id, asset_id=recommendation.asset_id),
                owner_id=owner_id,
                memory_type="recommendation_intake",
                scope="asset",
                asset_id=recommendation.asset_id,
                source_decision_id=decision_log.decision_id,
                content=(
                    f"{recommendation.name} 已从推荐运行进入观察池：{reason}"
                ),
                confidence=recommendation.confidence,
                payload={
                    "workflow_run_id": run_id,
                    "watchlist_id": watchlist_id,
                    "watchlist_item_id": item.watchlist_item_id,
                    "recommendation_id": recommendation.recommendation_id,
                },
            )
            self.memory.link_memory_edge(
                edge_id=build_edge_id(
                    source_id=decision_log.decision_id,
                    target_id=finance_memory.memory_id,
                ),
                owner_id=owner_id,
                source_type="decision",
                source_id=decision_log.decision_id,
                relation_type="supports",
                target_type="memory",
                target_id=finance_memory.memory_id,
                confidence=Decimal("0.900000"),
                reason="推荐入池决策沉淀为 Finance Memory。",
            )
            review_task = self.memory.schedule_review(
                review_task_id=build_review_task_id(
                    run_id=run_id,
                    asset_id=recommendation.asset_id,
                ),
                owner_id=owner_id,
                asset_id=recommendation.asset_id,
                source_decision_id=decision_log.decision_id,
                review_type="recommendation_intake_followup",
                due_at=as_of + timedelta(days=2),
                review_questions=[
                    {"question": "推荐入池后是否触发买入条件？"},
                    {"question": "观察条件和失效条件是否仍然成立？"},
                ],
                payload={
                    "workflow_run_id": run_id,
                    "watchlist_id": watchlist_id,
                    "watchlist_item_id": item.watchlist_item_id,
                    "recommendation_id": recommendation.recommendation_id,
                },
            )
            watchlist_item_ids.append(item.watchlist_item_id)
            decision_ids.append(decision_log.decision_id)
            memory_ids.append(finance_memory.memory_id)
            review_task_ids.append(review_task.review_task_id)
            self.workflow_audit.record_event(
                workflow_event_id=f"{run_id}:event:intake:{recommendation.asset_id}",
                workflow_run_id=run_id,
                event_type="watchlist_item_recorded",
                agent_name="recommendation_intake",
                message=reason,
                evidence_ids=list(recommendation.evidence_ids),
                created_at=as_of,
                payload={
                    "watchlist_item_id": item.watchlist_item_id,
                    "decision_id": decision_log.decision_id,
                    "memory_id": finance_memory.memory_id,
                    "review_task_id": review_task.review_task_id,
                },
            )

        self.workflow_audit.finish_run(
            workflow_run_id=run_id,
            owner_id=owner_id,
            workflow_type="recommendation_intake",
            trigger_type="recommendation_run",
            trigger_ref=recommendation_run_id,
            status="succeeded",
            started_at=as_of,
            finished_at=as_of,
            input_ref=f"recommendation_run:{recommendation_run_id}",
            output_ref=f"watchlist:{watchlist_id}:recommendation_intake:{run_id}",
            payload={
                "watchlist_item_ids": watchlist_item_ids,
                "decision_ids": decision_ids,
                "memory_ids": memory_ids,
                "review_task_ids": review_task_ids,
            },
        )
        return RecommendationIntakeRunSummary(
            workflow_run_id=run_id,
            watchlist_item_ids=tuple(watchlist_item_ids),
            decision_ids=tuple(decision_ids),
            memory_ids=tuple(memory_ids),
            review_task_ids=tuple(review_task_ids),
        )

    def decide_recommendation_actions(
        self,
        *,
        owner_id: str,
        recommendation_run_id: str,
        portfolio_id: str,
        watchlist_id: str,
        as_of: datetime,
        limit: int = 10,
        workflow_run_id: str | None = None,
    ) -> RecommendationAgentDecisionRunSummary:
        """由 Agent 决定推荐结果是否入池、买入、卖出或换股。"""

        run_id = workflow_run_id or build_recommendation_decision_workflow_run_id(
            recommendation_run_id=recommendation_run_id,
            portfolio_id=portfolio_id,
            watchlist_id=watchlist_id,
            as_of=as_of,
        )
        portfolio_snapshot = self.portfolios.load_portfolio_snapshot(portfolio_id)
        watchlist = self.watchlists.get_watchlist(watchlist_id)
        watchlist_items = self.watchlists.list_active_items(
            owner_id=owner_id,
            watchlist_id=watchlist_id,
        )
        recommendations = tuple(
            self.recommendations.list_top_recommendations(
                run_id=recommendation_run_id,
                limit=limit,
            )
        )
        asset_ids = tuple(
            sorted(
                {
                    *(recommendation.asset_id for recommendation in recommendations),
                    *(position.asset_id for position in portfolio_snapshot.positions),
                    *(item.asset_id for item in watchlist_items),
                }
            )
        )
        self.workflow_audit.start_run(
            workflow_run_id=run_id,
            owner_id=owner_id,
            workflow_type="recommendation_agent_decision",
            trigger_type="recommendation_run",
            trigger_ref=recommendation_run_id,
            started_at=as_of,
            input_ref=f"recommendation_run:{recommendation_run_id}",
            payload={
                "portfolio_id": portfolio_id,
                "watchlist_id": watchlist_id,
                "recommendation_count": len(recommendations),
                "position_count": len(portfolio_snapshot.positions),
            },
        )
        self.workflow_audit.record_event(
            workflow_event_id=f"{run_id}:event:load_context",
            workflow_run_id=run_id,
            event_type="context_loaded",
            agent_name="PersonalFinanceAgent",
            message="已读取推荐、持仓、观察池、信号、风险和 Finance Memory 上下文。",
            created_at=as_of,
            payload={
                "recommendation_run_id": recommendation_run_id,
                "portfolio_id": portfolio_id,
                "watchlist_id": watchlist_id,
                "asset_ids": list(asset_ids),
            },
        )

        result = self.recommendation_decision_workflow.run(
            RecommendationDecisionInput(
                owner_id=owner_id,
                recommendation_run_id=recommendation_run_id,
                portfolio_id=portfolio_id,
                watchlist=watchlist,
                recommendations=recommendations,
                positions=portfolio_snapshot.positions,
                watchlist_items=watchlist_items,
                signals_by_asset={
                    asset_id: self.signals.get_latest_signal(
                        asset_id=asset_id,
                        horizon="swing",
                    )
                    for asset_id in asset_ids
                },
                risks_by_asset={
                    asset_id: tuple(
                        self.risks.list_recent_risks(asset_id=asset_id, limit=5)
                    )
                    for asset_id in asset_ids
                },
                memories_by_asset={
                    asset_id: tuple(
                        self.memories.list_active_memories(
                            owner_id=owner_id,
                            asset_id=asset_id,
                            limit=5,
                        )
                    )
                    for asset_id in asset_ids
                },
                as_of=as_of,
            )
        )

        watchlist_item_ids: list[str] = []
        decision_ids: list[str] = []
        memory_ids: list[str] = []
        review_task_ids: list[str] = []
        for decision in result.decisions:
            watchlist_item_id: str | None = None
            candidate_item = None
            if decision.should_write_watchlist and decision.watchlist_status is not None:
                watchlist_item_id = build_recommendation_watchlist_item_id(
                    watchlist_id=watchlist_id,
                    asset_id=decision.asset_id,
                )
                item = self.watchlists.add_or_update_item(
                    watchlist_item_id=watchlist_item_id,
                    watchlist_id=watchlist_id,
                    asset_id=decision.asset_id,
                    symbol=decision.symbol,
                    market=decision.market,
                    source_type="agent_decision",
                    source_id=decision.recommendation_id,
                    reason=decision.summary,
                    watch_conditions={
                        "agent_action": decision.agent_action,
                        "trade_action": decision.trade_action,
                        "rationale": decision.rationale,
                        "intake_reason": decision.summary,
                    },
                    trigger_conditions={
                        "trade_action": decision.trade_action,
                        "source_position_id": decision.source_position_id,
                    },
                    invalid_conditions={"risk_rebuttal": decision.risk_rebuttal},
                    risk_level=decision.severity,
                    status=decision.watchlist_status,
                    next_review_at=decision.next_review_at,
                    payload={
                        "workflow_run_id": run_id,
                        "recommendation_run_id": recommendation_run_id,
                        "recommendation_id": decision.recommendation_id,
                        "agent_action": decision.agent_action,
                        "trade_action": decision.trade_action,
                        "source_position_id": decision.source_position_id,
                        "intake_reason": decision.summary,
                        "risk_rebuttal": decision.risk_rebuttal,
                        "reason_ids": decision.reason_ids,
                        "signal_ids": decision.signal_ids,
                        "risk_ids": decision.risk_ids,
                    },
                )
                candidate_item = item
                watchlist_item_ids.append(item.watchlist_item_id)

            alert_id: str | None = None
            if decision.should_alert:
                alert = self.memory.record_alert(
                    alert_id=build_alert_id(run_id=run_id, asset_id=decision.asset_id),
                    owner_id=owner_id,
                    portfolio_id=portfolio_id,
                    asset_id=decision.asset_id,
                    alert_type="recommendation_agent_decision",
                    severity=decision.severity,
                    triggered_by="recommendation_run",
                    trigger_condition=decision.rationale,
                    as_of=as_of,
                    payload={
                        "workflow_run_id": run_id,
                        "watchlist_id": watchlist_id,
                        "watchlist_item_id": watchlist_item_id,
                        "recommendation_id": decision.recommendation_id,
                        "agent_action": decision.agent_action,
                        "trade_action": decision.trade_action,
                    },
                )
                alert_id = alert.alert_id

            decision_log = self.memory.record_decision(
                decision_id=build_decision_id(
                    run_id=run_id,
                    asset_id=decision.asset_id,
                    decision_type=decision.decision_type,
                ),
                owner_id=owner_id,
                portfolio_id=portfolio_id,
                asset_id=decision.asset_id,
                decision_type=decision.decision_type,
                source_recommendation_id=decision.recommendation_id,
                source_alert_id=alert_id,
                workflow_run_id=run_id,
                suggested_action=decision.trade_action,
                user_action="unknown",
                summary=decision.summary,
                reason_ids=list(decision.reason_ids),
                risk_ids=list(decision.risk_ids),
                evidence_ids=list(decision.evidence_ids),
                created_at=as_of,
                payload={
                    "watchlist_id": watchlist_id,
                    "watchlist_item_id": watchlist_item_id,
                    "agent_action": decision.agent_action,
                    "trade_action": decision.trade_action,
                    "intake_reason": decision.summary if watchlist_item_id else None,
                    "rationale": decision.rationale,
                    "risk_rebuttal": decision.risk_rebuttal,
                    "source_position_id": decision.source_position_id,
                    "target_position_id": decision.target_position_id,
                    "signal_ids": decision.signal_ids,
                },
            )
            if candidate_item is not None:
                intake_event_type = map_recommendation_intake_event_type(
                    watchlist_status=decision.watchlist_status,
                )
                intake_event = self.watchlists.record_event(
                    event_id=build_intake_reason_event_id(
                        watchlist_item_id=candidate_item.watchlist_item_id,
                        event_type=intake_event_type,
                        recommendation_id=decision.recommendation_id,
                        as_of=as_of,
                    ),
                    owner_id=owner_id,
                    watchlist_id=watchlist_id,
                    watchlist_item_id=candidate_item.watchlist_item_id,
                    asset_id=decision.asset_id,
                    event_type=intake_event_type,
                    from_status=None,
                    to_status=candidate_item.status,
                    reason=decision.summary,
                    source_decision_id=decision_log.decision_id,
                    created_at=as_of,
                    payload={
                        "workflow_run_id": run_id,
                        "recommendation_run_id": recommendation_run_id,
                        "recommendation_id": decision.recommendation_id,
                        "agent_action": decision.agent_action,
                        "trade_action": decision.trade_action,
                        "intake_reason": decision.summary,
                        "rationale": decision.rationale,
                        "risk_rebuttal": decision.risk_rebuttal,
                        "reason_ids": decision.reason_ids,
                        "signal_ids": decision.signal_ids,
                        "risk_ids": decision.risk_ids,
                        "evidence_ids": decision.evidence_ids,
                    },
                )
                intake_memory = self.memory.upsert_memory(
                    memory_id=build_typed_memory_id(
                        run_id=run_id,
                        asset_id=decision.asset_id,
                        memory_type=intake_event_type,
                    ),
                    owner_id=owner_id,
                    memory_type=intake_event_type,
                    scope="asset",
                    asset_id=decision.asset_id,
                    source_decision_id=decision_log.decision_id,
                    content=(
                        f"{decision.name} Agent 入池原因：{decision.summary} "
                        f"风险反驳：{decision.risk_rebuttal}"
                    ),
                    confidence=Decimal("0.850000"),
                    payload={
                        "workflow_run_id": run_id,
                        "watchlist_id": watchlist_id,
                        "watchlist_item_id": candidate_item.watchlist_item_id,
                        "source_type": "watchlist_item_event",
                        "source_id": intake_event.event_id,
                        "recommendation_id": decision.recommendation_id,
                        "intake_reason": decision.summary,
                        "risk_rebuttal": decision.risk_rebuttal,
                        "agent_action": decision.agent_action,
                        "trade_action": decision.trade_action,
                    },
                )
                self.memory.link_memory_edge(
                    edge_id=build_edge_id(
                        source_id=intake_event.event_id,
                        target_id=intake_memory.memory_id,
                    ),
                    owner_id=owner_id,
                    source_type="watchlist_event",
                    source_id=intake_event.event_id,
                    relation_type="summarizes",
                    target_type="memory",
                    target_id=intake_memory.memory_id,
                    confidence=Decimal("0.900000"),
                    reason="Agent 入池原因沉淀为 Finance Memory。",
                )
                memory_ids.append(intake_memory.memory_id)
            finance_memory = self.memory.upsert_memory(
                memory_id=build_memory_id(run_id=run_id, asset_id=decision.asset_id),
                owner_id=owner_id,
                memory_type="recommendation_agent_decision",
                scope="asset",
                asset_id=decision.asset_id,
                source_decision_id=decision_log.decision_id,
                content=f"{decision.summary} 风险反驳：{decision.risk_rebuttal}",
                confidence=Decimal("0.850000"),
                payload={
                    "workflow_run_id": run_id,
                    "recommendation_id": decision.recommendation_id,
                    "agent_action": decision.agent_action,
                    "trade_action": decision.trade_action,
                    "watchlist_item_id": watchlist_item_id,
                },
            )
            self.memory.link_memory_edge(
                edge_id=build_edge_id(
                    source_id=decision_log.decision_id,
                    target_id=finance_memory.memory_id,
                ),
                owner_id=owner_id,
                source_type="decision",
                source_id=decision_log.decision_id,
                relation_type="supports",
                target_type="memory",
                target_id=finance_memory.memory_id,
                confidence=Decimal("0.900000"),
                reason="推荐 Agent 决策沉淀为 Finance Memory。",
            )
            review_task = self.memory.schedule_review(
                review_task_id=build_review_task_id(run_id=run_id, asset_id=decision.asset_id),
                owner_id=owner_id,
                asset_id=decision.asset_id,
                source_decision_id=decision_log.decision_id,
                review_type="recommendation_agent_decision_followup",
                due_at=decision.next_review_at or as_of + timedelta(days=2),
                review_questions=list(decision.review_questions),
                payload={
                    "workflow_run_id": run_id,
                    "recommendation_id": decision.recommendation_id,
                    "agent_action": decision.agent_action,
                    "trade_action": decision.trade_action,
                    "watchlist_item_id": watchlist_item_id,
                },
            )
            decision_ids.append(decision_log.decision_id)
            memory_ids.append(finance_memory.memory_id)
            review_task_ids.append(review_task.review_task_id)
            self.workflow_audit.record_event(
                workflow_event_id=f"{run_id}:event:decision:{decision.asset_id}",
                workflow_run_id=run_id,
                event_type="agent_decision_recorded",
                agent_name="recommendation_decision_agent",
                message=decision.summary,
                evidence_ids=list(decision.evidence_ids),
                created_at=as_of,
                payload={
                    "decision_id": decision_log.decision_id,
                    "memory_id": finance_memory.memory_id,
                    "review_task_id": review_task.review_task_id,
                    "agent_action": decision.agent_action,
                    "trade_action": decision.trade_action,
                },
            )

        self.workflow_audit.finish_run(
            workflow_run_id=run_id,
            owner_id=owner_id,
            workflow_type="recommendation_agent_decision",
            trigger_type="recommendation_run",
            trigger_ref=recommendation_run_id,
            status="succeeded",
            started_at=as_of,
            finished_at=as_of,
            input_ref=f"recommendation_run:{recommendation_run_id}",
            output_ref=f"recommendation_agent_decision:{run_id}:decisions",
            payload={
                "watchlist_item_ids": watchlist_item_ids,
                "decision_ids": decision_ids,
                "memory_ids": memory_ids,
                "review_task_ids": review_task_ids,
            },
        )
        return RecommendationAgentDecisionRunSummary(
            workflow_run_id=run_id,
            watchlist_item_ids=tuple(watchlist_item_ids),
            decision_ids=tuple(decision_ids),
            memory_ids=tuple(memory_ids),
            review_task_ids=tuple(review_task_ids),
            result=result,
        )


class FinanceAssistantService(PersonalFinanceAgentService):
    """金融助手业务编排内核。

    当前继承旧的 `PersonalFinanceAgentService`，保持 M2/M3/M4 已有入口兼容。
    后续 Hermes、MCP、CLI 和 Dashboard 工具入口应优先依赖本类名。
    """

    def list_workflows(self) -> dict[str, list[dict[str, Any]]]:
        """列出本项目内部可调度的金融团队 Workflow。"""

        workflows = [
            {
                "workflow_type": builder.workflow_type,
                "status": "langgraph_ready",
                "description": builder.description,
            }
            for builder in list_langgraph_workflow_builders()
        ]
        return {"workflows": workflows}

    def run_workflow(
        self,
        *,
        workflow_type: str,
        owner_id: str,
        workflow_run_id: str,
        trigger_type: str,
        started_at: datetime,
        initial_state: dict[str, Any],
        trigger_ref: str | None = None,
        input_ref: str | None = None,
        output_ref: str | None = None,
    ) -> FinanceWorkflowRunSummary:
        """统一调度 LangGraph 金融团队 Workflow 并写入审计。"""

        builder = find_workflow_builder(workflow_type)
        graph = builder.build()
        final_state = graph.invoke(initial_state)
        node_events = build_workflow_node_events(final_state)
        self.langgraph_adapter.record_completed_graph(
            workflow_run_id=workflow_run_id,
            owner_id=owner_id,
            workflow_type=workflow_type,
            trigger_type=trigger_type,
            trigger_ref=trigger_ref,
            started_at=started_at,
            node_events=node_events,
            initial_state={key: value for key, value in initial_state.items() if key != "session"},
            final_state=sanitize_workflow_state(final_state),
            input_ref=input_ref,
            output_ref=output_ref,
            finished_at=started_at,
        )
        return FinanceWorkflowRunSummary(
            workflow_run_id=workflow_run_id,
            workflow_type=workflow_type,
            final_state=final_state,
            report=final_state.get("report"),
        )


def find_workflow_builder(workflow_type: str) -> LangGraphWorkflowBuilder:
    """按类型查找 LangGraph Workflow 构建器。"""

    for builder in list_langgraph_workflow_builders():
        if builder.workflow_type == workflow_type:
            return builder
    raise ValueError(f"未知 Workflow 类型：{workflow_type}")


def build_workflow_node_events(final_state: dict[str, Any]) -> tuple[WorkflowNodeEvent, ...]:
    """把 LangGraph 运行状态转换为可审计节点事件。"""

    events: list[WorkflowNodeEvent] = []
    for node_name in final_state.get("node_trace", []):
        events.append(
            WorkflowNodeEvent(
                name=node_name,
                output=build_node_event_output(node_name=node_name, final_state=final_state),
            )
        )
    for opinion in final_state.get("roundtable_opinions", []):
        events.append(
            WorkflowNodeEvent(
                name=f"roundtable:{opinion['role']}",
                output=opinion,
                evidence_ids=tuple(opinion.get("evidence_ids", ())),
                message=f"圆桌观点：{opinion['role']} - {opinion.get('summary', '')}",
            )
        )
    for index, route in enumerate(final_state.get("model_routes", []), start=1):
        events.append(
            WorkflowNodeEvent(
                name=f"model_route:primary:{index}",
                output=route,
                message=(
                    f"模型路由：{route.get('task')} -> "
                    f"{route.get('model_name') or route.get('model_key')}"
                ),
            )
        )
    for index, route in enumerate(final_state.get("review_model_routes", []), start=1):
        events.append(
            WorkflowNodeEvent(
                name=f"model_route:review:{index}",
                output=route,
                message=(
                    f"高风险模型路由：{route.get('decision_type')} -> "
                    f"{route.get('model_name') or route.get('model_key')}"
                ),
            )
        )
    for index, review in enumerate(final_state.get("high_risk_reviews", []), start=1):
        events.append(
            WorkflowNodeEvent(
                name=f"high_risk_review:{index}",
                output=review,
                message=(
                    f"高风险复核：{review.get('asset_id')} "
                    f"requires_review={review.get('requires_review')}"
                ),
            )
        )
        model_review = review.get("model_review")
        if model_review:
            events.append(
                WorkflowNodeEvent(
                    name=f"model_review:{index}",
                    output=model_review,
                    message=(
                        f"模型复核状态：{review.get('asset_id')} "
                        f"{model_review.get('review_status')}"
                    ),
                )
            )
    report = final_state.get("report")
    if report:
        events.append(
            WorkflowNodeEvent(
                name="report_draft",
                output=report,
                message=f"中文报告摘要：{report.get('title')}",
            )
        )
    return tuple(events)


def build_node_event_output(*, node_name: str, final_state: dict[str, Any]) -> dict[str, Any]:
    """生成普通节点事件输出摘要。"""

    if node_name == "roundtable_discussion":
        return {
            "opinion_count": len(final_state.get("roundtable_opinions", [])),
            "roles": sorted(
                {opinion["role"] for opinion in final_state.get("roundtable_opinions", [])}
            ),
        }
    if node_name == "data_gathering":
        return {
            "asset_ids": final_state.get("asset_ids", []),
            "tool_calls": final_state.get("tool_calls", []),
        }
    if node_name == "high_risk_review":
        return {
            "review_count": len(final_state.get("high_risk_reviews", [])),
            "requires_review_count": sum(
                1 for item in final_state.get("high_risk_reviews", [])
                if item.get("requires_review")
            ),
            "review_model_route_count": len(final_state.get("review_model_routes", [])),
        }
    if node_name == "report_draft":
        return {"report": final_state.get("report")}
    if node_name == "decision_synthesis":
        return {"decision_count": final_state.get("decision_count", 0)}
    return {"state_keys": sorted(key for key in final_state if key != "session")}


def sanitize_workflow_state(state: dict[str, Any]) -> dict[str, Any]:
    """清理不适合写入审计 payload 的运行状态。"""

    return {
        key: value
        for key, value in state.items()
        if key not in {"session", "tool_runtime", "workflow_input", "result"}
    }


def build_workflow_run_id(*, portfolio_id: str, as_of: datetime) -> str:
    """生成持仓监控 Workflow ID。"""

    return f"workflow:{portfolio_id}:portfolio_monitoring:{as_of:%Y%m%d%H%M%S}"


def build_watchlist_workflow_run_id(*, watchlist_id: str, as_of: datetime) -> str:
    """生成观察池管理 Workflow ID。"""

    return f"workflow:{watchlist_id}:watchlist_management:{as_of:%Y%m%d%H%M%S}"


def build_recommendation_intake_workflow_run_id(
    *,
    recommendation_run_id: str,
    watchlist_id: str,
    as_of: datetime,
) -> str:
    """生成推荐入池 Workflow ID。"""

    return (
        f"workflow:{watchlist_id}:recommendation_intake:"
        f"{recommendation_run_id}:{as_of:%Y%m%d%H%M%S}"
    )


def build_recommendation_decision_workflow_run_id(
    *,
    recommendation_run_id: str,
    portfolio_id: str,
    watchlist_id: str,
    as_of: datetime,
) -> str:
    """生成推荐 Agent 决策 Workflow ID。"""

    return (
        f"workflow:{portfolio_id}:{watchlist_id}:recommendation_agent_decision:"
        f"{recommendation_run_id}:{as_of:%Y%m%d%H%M%S}"
    )


def build_recommendation_watchlist_item_id(*, watchlist_id: str, asset_id: str) -> str:
    """生成推荐入池观察项 ID。"""

    return f"watchlist_item:{watchlist_id}:{asset_id}"


def build_recommendation_intake_event_id(
    *,
    watchlist_item_id: str,
    recommendation_id: str,
    as_of: datetime,
) -> str:
    """生成推荐入池事件 ID。"""

    return (
        f"watchlist_event:{watchlist_item_id}:recommendation_intake:"
        f"{recommendation_id}:{as_of:%Y%m%d%H%M%S}"
    )


def build_daily_watch_reason_event_id(
    *,
    watchlist_item_id: str,
    as_of: datetime,
) -> str:
    """生成每日继续关注原因事件 ID。"""

    return f"watchlist_event:{watchlist_item_id}:daily_watch_reason:{as_of:%Y%m%d}"


def build_intake_reason_event_id(
    *,
    watchlist_item_id: str,
    event_type: str,
    recommendation_id: str,
    as_of: datetime,
) -> str:
    """生成 Agent 入池原因事件 ID。"""

    return (
        f"watchlist_event:{watchlist_item_id}:{event_type}:"
        f"{recommendation_id}:{as_of:%Y%m%d%H%M%S}"
    )


def build_agent_decision_event_id(
    *,
    watchlist_item_id: str,
    recommendation_id: str,
    agent_action: str,
    as_of: datetime,
) -> str:
    """生成 Agent 决策观察池事件 ID。"""

    return (
        f"watchlist_event:{watchlist_item_id}:agent_decision:"
        f"{recommendation_id}:{agent_action}:{as_of:%Y%m%d%H%M%S}"
    )


def map_recommendation_intake_event_type(*, watchlist_status: str | None) -> str:
    """把 Agent 写入观察池的状态归一为入池原因事件类型。"""

    return "candidate_intake_reason"


def map_recommendation_risk_level(*, action: str, conviction: str) -> str:
    """把推荐动作和确信度映射为观察池风险等级。"""

    if action in {"sell_candidate", "reduce_candidate"}:
        return "high"
    if conviction == "high":
        return "medium"
    return "low"


def map_recommendation_severity(*, action: str, conviction: str) -> str:
    """把推荐动作和确信度映射为提醒等级。"""

    if action in {"sell_candidate", "reduce_candidate"} or conviction == "high":
        return "medium"
    return "low"


def build_alert_id(*, run_id: str, asset_id: str) -> str:
    """生成监控提醒 ID。"""

    return build_database_safe_id("alert", run_id, asset_id, keep_tail=(asset_id,))


def build_decision_id(*, run_id: str, asset_id: str, decision_type: str) -> str:
    """生成决策日志 ID。"""

    return build_database_safe_id(
        "decision",
        run_id,
        asset_id,
        decision_type,
        keep_tail=(asset_id, decision_type),
    )


def build_memory_id(*, run_id: str, asset_id: str) -> str:
    """生成 Finance Memory ID。"""

    return build_database_safe_id("memory", run_id, asset_id, keep_tail=(asset_id,))


def build_typed_memory_id(*, run_id: str, asset_id: str, memory_type: str) -> str:
    """生成带类型的 Finance Memory ID，避免一次运行中多种记忆互相覆盖。"""

    digest = sha1(f"{run_id}:{asset_id}:{memory_type}".encode()).hexdigest()[:10]
    return build_database_safe_id(
        "memory",
        run_id,
        asset_id,
        memory_type,
        digest,
        keep_tail=(asset_id, memory_type, digest),
    )


def build_edge_id(*, source_id: str, target_id: str) -> str:
    """生成轻量图谱边 ID。"""

    digest = sha1(f"{source_id}->{target_id}".encode()).hexdigest()[:24]
    return f"memory_edge:{digest}"


def build_review_task_id(*, run_id: str, asset_id: str) -> str:
    """生成复盘任务 ID。"""

    return build_database_safe_id("review", run_id, asset_id, keep_tail=(asset_id,))


def build_database_safe_id(
    prefix: str,
    *parts: str,
    keep_tail: tuple[str, ...] = (),
    max_length: int = MAX_DATABASE_ID_LENGTH,
) -> str:
    """生成不超过数据库主键长度限制的稳定 ID。"""

    raw_id = ":".join([prefix, *[str(part) for part in parts if str(part)]])
    if len(raw_id) <= max_length:
        return raw_id

    digest = sha1(raw_id.encode()).hexdigest()[:COMPACT_ID_DIGEST_LENGTH]
    readable_tail = ":".join(str(part) for part in keep_tail if str(part))
    compact_id = ":".join(part for part in [prefix, readable_tail, digest] if part)
    if len(compact_id) <= max_length:
        return compact_id

    reserved = len(prefix) + len(digest) + 2
    tail_budget = max(0, max_length - reserved)
    return f"{prefix}:{readable_tail[:tail_budget]}:{digest}"
