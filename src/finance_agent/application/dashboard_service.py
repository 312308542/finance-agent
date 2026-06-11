"""Dashboard 聚合查询服务。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from finance_agent.agents.runtime import load_model_registry
from finance_agent.agents.tools.runtime import (
    json_value,
    serialize_asset_recommendation,
    serialize_portfolio,
    serialize_position,
    serialize_recommendation_run,
    serialize_watchlist_item,
)
from finance_agent.scheduler import read_scheduler_health
from finance_agent.storage.orm import (
    AgentWorkflowRunORM,
    AssistantMemoryORM,
    AssistantTriggerEventORM,
    DataQualitySnapshotORM,
    DecisionLogORM,
    MonitoringAlertORM,
)
from finance_agent.storage.repositories import (
    DataQualityRepository,
    DecisionLogRepository,
    MemoryRepository,
    ModelRuntimeConfigRepository,
    PortfolioRepository,
    RecommendationRepository,
    WatchlistRepository,
)

JsonDict = dict[str, Any]


class DashboardService:
    """为 Web 控制台提供只读聚合视图。

    本服务只读取已经入库的事实、审计和配置，不抓取外部行情，也不触发金融决策。
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.portfolios = PortfolioRepository(session)
        self.watchlists = WatchlistRepository(session)
        self.recommendations = RecommendationRepository(session)
        self.decisions = DecisionLogRepository(session)
        self.memories = MemoryRepository(session)
        self.data_quality = DataQualityRepository(session)
        self.models = ModelRuntimeConfigRepository(session)

    def build_summary(self, *, owner_id: str) -> JsonDict:
        """构建控制台总览快照。"""

        portfolio = self.get_portfolio_overview(owner_id=owner_id)
        watchlists = self.get_watchlists(owner_id=owner_id, limit=8)
        recommendations = self.get_latest_recommendations(owner_id=owner_id, limit=8)
        risks = self.get_risk_overview(owner_id=owner_id, limit=8)
        workflows = self.get_workflow_overview(owner_id=owner_id, limit=8)
        memories = self.get_memory_overview(owner_id=owner_id, limit=6)
        data_health = self.get_data_health(limit=8)
        model_config = self.get_model_config()
        sections = {
            "portfolio": portfolio,
            "watchlists": watchlists,
            "recommendations": recommendations,
            "risks": risks,
            "workflows": workflows,
            "memories": memories,
            "data_health": data_health,
            "models": model_config,
        }
        return {
            "owner_id": owner_id,
            "status": summarize_section_status(sections),
            "generated_at": datetime.now(UTC).isoformat(),
            "sections": sections,
        }

    def get_portfolio_overview(self, *, owner_id: str) -> JsonDict:
        """读取用户组合与持仓概览。"""

        portfolios = self.portfolios.list_portfolios(owner_id=owner_id, status="active")
        if not portfolios:
            return {"status": "empty", "portfolios": [], "positions": [], "metrics": {}}
        active = portfolios[0]
        positions = self.portfolios.list_positions(active.portfolio_id)
        positive_count = sum(
            1 for position in positions if (position.unrealized_pnl or Decimal("0")) > 0
        )
        negative_count = sum(
            1 for position in positions if (position.unrealized_pnl or Decimal("0")) < 0
        )
        total_weight = sum(
            (position.portfolio_weight or Decimal("0")) for position in positions
        )
        return {
            "status": "ok",
            "active_portfolio_id": active.portfolio_id,
            "portfolios": [serialize_portfolio(item) for item in portfolios],
            "positions": [serialize_position(item) for item in positions],
            "metrics": {
                "position_count": len(positions),
                "positive_position_count": positive_count,
                "negative_position_count": negative_count,
                "total_weight": json_value(total_weight),
                "risk_profile": active.risk_profile,
            },
        }

    def get_watchlists(self, *, owner_id: str, limit: int = 20) -> JsonDict:
        """读取活跃观察项。"""

        items = self.watchlists.list_active_items(owner_id=owner_id)[:limit]
        serialized_items: list[JsonDict] = []
        pool_counts = {str(pool["key"]): 0 for pool in watchlist_pool_definitions()}
        for item in items:
            pool = classify_watchlist_pool(item)
            pool_counts[pool] = pool_counts.get(pool, 0) + 1
            serialized = serialize_watchlist_item(item)
            serialized["pool"] = pool
            serialized["pool_label"] = watchlist_pool_label(pool)
            serialized_items.append(serialized)

        return {
            "status": "ok" if items else "empty",
            "items": serialized_items,
            "pools": [
                {
                    **pool,
                    "count": pool_counts.get(str(pool["key"]), 0),
                }
                for pool in watchlist_pool_definitions()
            ],
            "metrics": {
                "active_count": len(items),
                "high_risk_count": sum(1 for item in items if item.risk_level == "high"),
                "research_count": pool_counts.get("system_research_pool", 0),
                "manual_count": pool_counts.get("manual_watchlist", 0),
                "markets": sorted({item.market for item in items}),
            },
        }

    def get_latest_recommendations(
        self,
        *,
        owner_id: str,
        market: str | None = None,
        limit: int = 20,
    ) -> JsonDict:
        """读取最近可用推荐运行和推荐列表。

        `owner_id` 当前用于接口统一和后续个人化推荐过滤。推荐运行表现阶段未强制
        绑定 owner，先按最近可用运行返回。
        """

        _ = owner_id
        since = datetime.now(UTC) - timedelta(days=30)
        runs = self.recommendations.list_available_runs_since(
            since=since,
            market=market,
            limit=5,
        )
        if not runs:
            return {"status": "empty", "runs": [], "recommendations": []}
        active_run = runs[0]
        items = self.recommendations.list_top_recommendations(
            run_id=active_run.run_id,
            limit=limit,
        )
        return {
            "status": "ok" if items else "empty",
            "runs": [serialize_recommendation_run(run) for run in runs],
            "active_run": serialize_recommendation_run(active_run),
            "recommendations": [serialize_asset_recommendation(item) for item in items],
            "metrics": {
                "recommendation_count": len(items),
                "buy_count": sum(1 for item in items if "buy" in item.action),
                "watch_count": sum(1 for item in items if item.action == "watch"),
                "markets": sorted({item.market for item in items}),
            },
        }

    def get_risk_overview(self, *, owner_id: str, limit: int = 20) -> JsonDict:
        """读取风险事件、提醒和触发摘要。"""

        triggers = self._list_recent_trigger_events(owner_id=owner_id, limit=limit)
        alerts = self._list_recent_alerts(owner_id=owner_id, limit=limit)
        qualities = self.data_quality.list_latest_quality(limit=limit)
        status = "ok" if triggers or alerts or qualities else "empty"
        return {
            "status": status,
            "triggers": [serialize_trigger_event(item) for item in triggers],
            "alerts": [serialize_alert(item) for item in alerts],
            "data_quality": [serialize_data_quality(item) for item in qualities],
            "metrics": {
                "trigger_count": len(triggers),
                "alert_count": len(alerts),
                "data_issue_count": sum(item.issue_count for item in qualities),
                "high_severity_count": sum(
                    1 for item in [*triggers, *alerts] if item.severity == "high"
                ),
            },
        }

    def get_workflow_overview(self, *, owner_id: str, limit: int = 20) -> JsonDict:
        """读取最近 Workflow 审计运行。"""

        from finance_agent.agents.interfaces import FinanceAgentInterface

        runs = self._list_recent_workflow_runs(owner_id=owner_id, limit=limit)
        available = FinanceAgentInterface(self.session).list_workflows().to_dict()["data"]
        return {
            "status": "ok" if runs else "empty",
            "available": available.get("workflows", []),
            "runs": [serialize_workflow_run(item) for item in runs],
            "metrics": {
                "recent_count": len(runs),
                "running_count": sum(1 for item in runs if item.status == "running"),
                "failed_count": sum(1 for item in runs if item.status == "failed"),
            },
        }

    def get_memory_overview(self, *, owner_id: str, limit: int = 20) -> JsonDict:
        """读取 Finance Memory 摘要。"""

        memories = self.memories.list_memories(
            owner_id=owner_id,
            statuses=("active", "stale"),
            limit=limit,
        )
        decisions = self.decisions.list_recent_decisions(owner_id=owner_id, limit=limit)
        return {
            "status": "ok" if memories or decisions else "empty",
            "memories": [serialize_memory(item) for item in memories],
            "decisions": [serialize_decision(item) for item in decisions],
            "metrics": {
                "memory_count": len(memories),
                "decision_count": len(decisions),
                "stale_memory_count": sum(1 for item in memories if item.status == "stale"),
            },
        }

    def get_data_health(self, *, limit: int = 20) -> JsonDict:
        """读取数据质量快照摘要。"""

        qualities = self.data_quality.list_latest_quality(limit=limit)
        return {
            "status": "ok" if qualities else "empty",
            "items": [serialize_data_quality(item) for item in qualities],
            "metrics": {
                "quality_count": len(qualities),
                "issue_count": sum(item.issue_count for item in qualities),
                "unavailable_count": sum(
                    1 for item in qualities if item.status in {"unavailable", "error"}
                ),
            },
        }

    def get_model_config(self) -> JsonDict:
        """读取模型配置脱敏摘要。"""

        registry = load_model_registry(prefer_database=True)
        return {
            "status": "ok" if registry.models else "empty",
            "registry": registry.to_safe_dict(),
            "providers": [
                serialize_model_provider(item) for item in self.models.list_providers()
            ],
            "models": [
                serialize_model_instance(item)
                for item in self.models.list_model_instances()
            ],
            "routes": [
                serialize_model_route(item)
                for item in self.models.list_routing_rules()
            ],
            "retrieval_profiles": [
                serialize_retrieval_profile(item)
                for item in self.models.list_retrieval_profiles()
            ],
        }

    def read_scheduler_status(
        self,
        *,
        status_file: str | Path = "runtime/base_data_scheduler/status.json",
        max_age_seconds: int | None = None,
    ) -> JsonDict:
        """读取基础数据调度器状态文件。"""

        health = read_scheduler_health(status_file, max_age_seconds=max_age_seconds)
        return {
            "status": "ok" if health.get("healthy") else health.get("status", "unavailable"),
            "health": health,
        }

    def _list_recent_trigger_events(
        self,
        *,
        owner_id: str,
        limit: int,
    ) -> list[AssistantTriggerEventORM]:
        statement = (
            select(AssistantTriggerEventORM)
            .where(AssistantTriggerEventORM.owner_id == owner_id)
            .order_by(AssistantTriggerEventORM.triggered_at.desc())
            .limit(limit)
        )
        return list(self.session.scalars(statement))

    def _list_recent_alerts(self, *, owner_id: str, limit: int) -> list[MonitoringAlertORM]:
        statement = (
            select(MonitoringAlertORM)
            .where(MonitoringAlertORM.owner_id == owner_id)
            .order_by(MonitoringAlertORM.as_of.desc())
            .limit(limit)
        )
        return list(self.session.scalars(statement))

    def _list_recent_workflow_runs(
        self,
        *,
        owner_id: str,
        limit: int,
    ) -> list[AgentWorkflowRunORM]:
        statement: Select[tuple[AgentWorkflowRunORM]] = (
            select(AgentWorkflowRunORM)
            .where(AgentWorkflowRunORM.owner_id == owner_id)
            .order_by(AgentWorkflowRunORM.started_at.desc())
            .limit(limit)
        )
        return list(self.session.scalars(statement))


def summarize_section_status(sections: dict[str, JsonDict]) -> str:
    """按分区状态汇总总览状态。"""

    statuses = {str(section.get("status") or "empty") for section in sections.values()}
    if "unavailable" in statuses:
        return "unavailable"
    if statuses and statuses <= {"empty"}:
        return "empty"
    if statuses & {"empty", "partial", "stale"}:
        return "partial"
    return "ok"


def unavailable_summary(*, owner_id: str, message: str) -> JsonDict:
    """数据库或依赖不可用时的总览降级响应。"""

    return {
        "owner_id": owner_id,
        "status": "unavailable",
        "generated_at": datetime.now(UTC).isoformat(),
        "message": message,
        "sections": {
            "portfolio": {"status": "unavailable", "portfolios": [], "positions": []},
            "watchlists": {"status": "unavailable", "items": []},
            "recommendations": {"status": "unavailable", "runs": [], "recommendations": []},
            "risks": {"status": "unavailable", "triggers": [], "alerts": []},
            "workflows": {"status": "unavailable", "runs": []},
            "memories": {"status": "unavailable", "memories": [], "decisions": []},
            "data_health": {"status": "unavailable", "items": []},
            "models": {"status": "unavailable", "models": []},
        },
    }


def serialize_trigger_event(event: AssistantTriggerEventORM) -> JsonDict:
    """序列化 Agent 触发事件。"""

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
        "triggered_at": json_value(event.triggered_at),
        "dispatched_at": json_value(event.dispatched_at),
        "payload": json_value(event.payload or {}),
    }


def serialize_alert(alert: MonitoringAlertORM) -> JsonDict:
    """序列化监控提醒。"""

    return {
        "alert_id": alert.alert_id,
        "owner_id": alert.owner_id,
        "portfolio_id": alert.portfolio_id,
        "asset_id": alert.asset_id,
        "alert_type": alert.alert_type,
        "severity": alert.severity,
        "triggered_by": alert.triggered_by,
        "trigger_condition": alert.trigger_condition,
        "current_value": json_value(alert.current_value),
        "threshold_value": json_value(alert.threshold_value),
        "status": alert.status,
        "as_of": json_value(alert.as_of),
        "payload": json_value(alert.payload or {}),
    }


def serialize_data_quality(item: DataQualitySnapshotORM) -> JsonDict:
    """序列化数据质量快照。"""

    return {
        "quality_id": item.quality_id,
        "asset_id": item.asset_id,
        "symbol": item.symbol,
        "market": item.market,
        "data_domain": item.data_domain,
        "provider": item.provider,
        "status": item.status,
        "freshness_status": item.freshness_status,
        "latest_data_at": json_value(item.latest_data_at),
        "checked_at": json_value(item.checked_at),
        "missing_items": json_value(item.missing_items or []),
        "issue_count": item.issue_count,
        "payload": json_value(item.payload or {}),
    }


def serialize_workflow_run(run: AgentWorkflowRunORM) -> JsonDict:
    """序列化 Workflow 运行记录。"""

    return {
        "workflow_run_id": run.workflow_run_id,
        "owner_id": run.owner_id,
        "workflow_type": run.workflow_type,
        "trigger_type": run.trigger_type,
        "trigger_ref": run.trigger_ref,
        "status": run.status,
        "started_at": json_value(run.started_at),
        "finished_at": json_value(run.finished_at),
        "input_ref": run.input_ref,
        "output_ref": run.output_ref,
        "payload": json_value(run.payload or {}),
    }


def serialize_memory(memory: AssistantMemoryORM) -> JsonDict:
    """序列化 Finance Memory。"""

    return {
        "memory_id": memory.memory_id,
        "owner_id": memory.owner_id,
        "memory_type": memory.memory_type,
        "scope": memory.scope,
        "asset_id": memory.asset_id,
        "source_decision_id": memory.source_decision_id,
        "source_review_task_id": memory.source_review_task_id,
        "content": memory.content,
        "confidence": json_value(memory.confidence),
        "status": memory.status,
        "created_at": json_value(memory.created_at),
        "updated_at": json_value(memory.updated_at),
        "payload": json_value(memory.payload or {}),
    }


def serialize_decision(decision: DecisionLogORM) -> JsonDict:
    """序列化决策日志。"""

    return {
        "decision_id": decision.decision_id,
        "owner_id": decision.owner_id,
        "portfolio_id": decision.portfolio_id,
        "asset_id": decision.asset_id,
        "decision_type": decision.decision_type,
        "source_recommendation_id": decision.source_recommendation_id,
        "source_alert_id": decision.source_alert_id,
        "workflow_run_id": decision.workflow_run_id,
        "suggested_action": decision.suggested_action,
        "user_action": decision.user_action,
        "summary": decision.summary,
        "reason_ids": json_value(decision.reason_ids or []),
        "risk_ids": json_value(decision.risk_ids or []),
        "evidence_ids": json_value(decision.evidence_ids or []),
        "created_at": json_value(decision.created_at),
        "payload": json_value(decision.payload or {}),
    }


def serialize_model_provider(item: Any) -> JsonDict:
    """序列化模型供应商，隐藏密钥。"""

    return {
        "provider_key": item.provider_key,
        "provider_vendor": item.provider_vendor,
        "provider_name": item.provider_name,
        "base_url": item.base_url,
        "api_key": mask_secret(item.api_key),
        "api_key_configured": bool(item.api_key),
        "timeout_seconds": item.timeout_seconds,
        "is_enabled": item.is_enabled,
        "is_default": item.is_default,
        "updated_at": json_value(item.updated_at),
    }


def serialize_model_instance(item: Any) -> JsonDict:
    """序列化模型实例。"""

    return {
        "model_key": item.model_key,
        "provider_key": item.provider_key,
        "model_type": item.model_type,
        "model_name": item.model_name,
        "role": item.role,
        "route_priority": item.route_priority,
        "timeout_seconds": item.timeout_seconds,
        "is_enabled": item.is_enabled,
        "is_default": item.is_default,
        "updated_at": json_value(item.updated_at),
    }


def serialize_model_route(item: Any) -> JsonDict:
    """序列化模型路由规则。"""

    return {
        "workflow_type": item.workflow_type,
        "task": item.task,
        "role": item.role,
        "decision_type": item.decision_type,
        "model_key": item.model_key,
        "priority": item.priority,
        "reason": item.reason,
        "is_enabled": item.is_enabled,
        "updated_at": json_value(item.updated_at),
    }


def serialize_retrieval_profile(item: Any) -> JsonDict:
    """序列化检索配置。"""

    return {
        "profile_key": item.profile_key,
        "profile_name": item.profile_name,
        "usage_scope": item.usage_scope,
        "search_method": item.search_method,
        "embedding_model_key": item.embedding_model_key,
        "rerank_model_key": item.rerank_model_key,
        "top_k": item.top_k,
        "score_threshold": json_value(item.score_threshold),
        "reranking_enable": item.reranking_enable,
        "reranking_mode": item.reranking_mode,
        "weights": json_value(item.weights or {}),
        "is_enabled": item.is_enabled,
        "is_default": item.is_default,
        "updated_at": json_value(item.updated_at),
    }


def mask_secret(value: str | None) -> str | None:
    """隐藏 API Key。"""

    if not value:
        return None
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}***{value[-4:]}"


def classify_watchlist_pool(item: Any) -> str:
    """将观察项归类到前端展示用的池子。"""

    payload = item.payload or {}
    watchlist_id = str(getattr(item, "watchlist_id", "") or "")
    source_type = str(getattr(item, "source_type", "") or "")
    payload_source_type = str(payload.get("source_type") or "")
    if (
        watchlist_id.endswith(":technical")
        or source_type == "technical_screening"
        or payload_source_type == "technical_screening"
    ):
        return "technical_screening_pool"
    if watchlist_id.endswith(":research") or payload.get("promotion_status") == "system_research":
        return "system_research_pool"
    if source_type in {"manual", "agent_confirmed", "portfolio", "fund"}:
        return "manual_watchlist"
    return "other_watchlist"


def watchlist_pool_definitions() -> list[JsonDict]:
    """返回观察池前端分组定义。"""

    return [
        {
            "key": "technical_screening_pool",
            "label": "技术初筛池",
            "description": "历史行情完成后的技术粗筛结果，只表示后续优先补齐，不代表买入建议。",
        },
        {
            "key": "system_research_pool",
            "label": "系统研究跟踪",
            "description": "系统推荐后自动跟踪，尚未代表用户确认关注。",
        },
        {
            "key": "manual_watchlist",
            "label": "用户观察池",
            "description": "用户手动加入或确认关注的资产。",
        },
        {
            "key": "other_watchlist",
            "label": "其他观察项",
            "description": "暂未归类到研究池或用户观察池的有效条目。",
        },
    ]


def watchlist_pool_label(pool: str) -> str:
    """返回观察池分组中文名。"""

    return {
        str(item["key"]): str(item["label"])
        for item in watchlist_pool_definitions()
    }.get(pool, "其他观察项")


def count_rows(session: Session, model: Any) -> int:
    """统计表行数，健康检查使用。"""

    return int(session.scalar(select(func.count()).select_from(model)) or 0)
