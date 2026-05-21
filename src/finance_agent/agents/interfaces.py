"""CLI 和 MCP 共用的金融助手调用接口。

本模块只做参数归一化、服务调用和 JSON 序列化，不承载金融决策逻辑。
CLI、MCP、Scheduler 或后续 API 都应通过这里调用 `FinanceAssistantService`，
避免多个入口各自拼装 Workflow。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.agents.personal_assistant import FinanceAssistantService
from finance_agent.agents.tools.runtime import FinanceToolRuntime, json_value
from finance_agent.agents.workflows.portfolio_monitoring import PortfolioMonitoringInput
from finance_agent.agents.workflows.recommendation_decision import RecommendationDecisionInput
from finance_agent.agents.workflows.watchlist_management import WatchlistManagementInput
from finance_agent.application import PortfolioService, WatchlistService
from finance_agent.graph import GraphSyncService
from finance_agent.graph.stores import DryRunGraphStore
from finance_agent.storage.orm import AgentWorkflowEventORM, AgentWorkflowRunORM
from finance_agent.storage.repositories import (
    MemoryRepository,
    RecommendationRepository,
    RiskRepository,
    SignalSnapshotRepository,
)

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class AgentInterfaceResult:
    """Agent 工具入口统一返回结构。"""

    status: str
    data: JsonDict
    message: str | None = None

    def to_dict(self) -> JsonDict:
        """转换为 JSON 友好的字典。"""

        result: JsonDict = {"status": self.status, "data": json_value(self.data)}
        if self.message:
            result["message"] = self.message
        return result


class FinanceAgentInterface:
    """供 CLI 和 MCP 复用的金融助手工具门面。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.assistant = FinanceAssistantService(session)
        self.tool_runtime = FinanceToolRuntime(session, graph_store=DryRunGraphStore())

    def list_workflows(self) -> AgentInterfaceResult:
        """列出可由上层 Agent 调用的 Workflow。"""

        return AgentInterfaceResult(status="ok", data=self.assistant.list_workflows())

    def list_tools(self) -> AgentInterfaceResult:
        """列出可读取已入库金融事实的工具。"""

        tools = [
            {
                "name": name,
                "description": self.tool_runtime.get_tool(name).description,
            }
            for name in self.tool_runtime.list_tools()
        ]
        return AgentInterfaceResult(status="ok", data={"tools": tools})

    def call_tool(self, *, name: str, arguments: JsonDict | None = None) -> AgentInterfaceResult:
        """调用只读金融事实工具。"""

        data = self.tool_runtime.call(name, **(arguments or {}))
        return AgentInterfaceResult(status="ok", data={"tool": name, "result": data})

    def graph_health(self) -> AgentInterfaceResult:
        """检查当前配置选择的图谱后端。"""

        return AgentInterfaceResult(status="ok", data=self.tool_runtime.graph_store.health_check())

    def graph_initialize(self) -> AgentInterfaceResult:
        """初始化当前配置选择的图谱后端。"""

        return AgentInterfaceResult(
            status="ok",
            data=self.tool_runtime.graph_store.initialize_schema(),
        )

    def graph_sync_asset(
        self,
        *,
        owner_id: str,
        asset_id: str,
        limit: int = 20,
    ) -> AgentInterfaceResult:
        """同步单标的图谱投影。"""

        data = GraphSyncService(
            session=self.session,
            graph_store=self.tool_runtime.graph_store,
        ).sync_asset_graph(owner_id=owner_id, asset_id=asset_id, limit=limit)
        return AgentInterfaceResult(status="ok", data=data.to_dict())

    def graph_sync_owner(
        self,
        *,
        owner_id: str,
        asset_ids: list[str] | None = None,
        limit_assets: int = 100,
        limit_per_asset: int = 20,
    ) -> AgentInterfaceResult:
        """同步某个用户的图谱投影。"""

        data = GraphSyncService(
            session=self.session,
            graph_store=self.tool_runtime.graph_store,
        ).sync_owner_graph(
            owner_id=owner_id,
            asset_ids=asset_ids,
            limit_assets=limit_assets,
            limit_per_asset=limit_per_asset,
        )
        return AgentInterfaceResult(status="ok", data=data)

    def graph_sync_all(
        self,
        *,
        owner_id: str | None = None,
        limit_assets: int = 200,
        limit_per_asset: int = 20,
    ) -> AgentInterfaceResult:
        """同步全部或指定用户的图谱投影。"""

        data = GraphSyncService(
            session=self.session,
            graph_store=self.tool_runtime.graph_store,
        ).sync_all_graph(
            owner_id=owner_id,
            limit_assets=limit_assets,
            limit_per_asset=limit_per_asset,
        )
        return AgentInterfaceResult(status="ok", data=data)

    def graph_trace_asset(
        self,
        *,
        owner_id: str,
        asset_id: str,
        max_depth: int = 2,
        limit: int = 20,
    ) -> AgentInterfaceResult:
        """同步并追踪单标的图谱路径。"""

        return self.call_tool(
            name="memory.trace_asset_graph",
            arguments={
                "owner_id": owner_id,
                "asset_id": asset_id,
                "max_depth": max_depth,
                "limit": limit,
            },
        )

    def graph_explain_candidate_reason_chain(
        self,
        *,
        owner_id: str,
        asset_id: str,
        limit: int = 5,
    ) -> AgentInterfaceResult:
        """同步并解释标的入池或持续关注原因链。"""

        return self.call_tool(
            name="memory.explain_candidate_reason_chain",
            arguments={"owner_id": owner_id, "asset_id": asset_id, "limit": limit},
        )

    def graph_find_similar_decision_paths(
        self,
        *,
        owner_id: str,
        asset_id: str,
        limit: int = 10,
    ) -> AgentInterfaceResult:
        """同步并查找相似历史决策路径。"""

        return self.call_tool(
            name="memory.find_similar_decision_paths",
            arguments={"owner_id": owner_id, "asset_id": asset_id, "limit": limit},
        )

    def graph_detect_risk_contagion(
        self,
        *,
        owner_id: str,
        asset_id: str | None = None,
        max_depth: int = 3,
        limit: int = 20,
    ) -> AgentInterfaceResult:
        """同步并检测风险传导路径。"""

        return self.call_tool(
            name="memory.detect_risk_contagion",
            arguments={
                "owner_id": owner_id,
                "asset_id": asset_id,
                "max_depth": max_depth,
                "limit": limit,
            },
        )

    def graph_find_memory_conflicts(
        self,
        *,
        owner_id: str,
        asset_id: str | None = None,
        limit: int = 10,
    ) -> AgentInterfaceResult:
        """同步并查找记忆冲突。"""

        return self.call_tool(
            name="memory.find_memory_conflicts",
            arguments={"owner_id": owner_id, "asset_id": asset_id, "limit": limit},
        )

    def memory_recall_asset_context(
        self,
        *,
        owner_id: str,
        asset_id: str,
        query: str,
        memory_type: str | None = None,
        limit: int = 10,
    ) -> AgentInterfaceResult:
        """读取标的 Finance Memory 相似召回和时间线。"""

        return self.call_tool(
            name="memory.get_asset_memory_context",
            arguments={
                "owner_id": owner_id,
                "asset_id": asset_id,
                "query": query,
                "memory_type": memory_type,
                "limit": limit,
            },
        )

    def memory_get_asset_timeline(
        self,
        *,
        owner_id: str,
        asset_id: str,
        memory_type: str | None = None,
        limit: int = 20,
    ) -> AgentInterfaceResult:
        """读取标的 Finance Memory 时间线。"""

        return self.call_tool(
            name="memory.get_asset_memory_timeline",
            arguments={
                "owner_id": owner_id,
                "asset_id": asset_id,
                "memory_type": memory_type,
                "limit": limit,
            },
        )

    def run_workflow(
        self,
        *,
        workflow_type: str,
        owner_id: str,
        workflow_run_id: str | None = None,
        trigger_type: str = "manual",
        trigger_ref: str | None = None,
        started_at: datetime | None = None,
        initial_state: JsonDict | None = None,
        portfolio_id: str | None = None,
        watchlist_id: str | None = None,
        recommendation_run_id: str | None = None,
        asset_id: str | None = None,
        asset_ids: list[str] | None = None,
        source_asset_id: str | None = None,
        candidate_asset_id: str | None = None,
        horizon: str = "swing",
        timeframe: str = "1d",
        recommendation_limit: int = 20,
    ) -> AgentInterfaceResult:
        """统一运行金融团队 Workflow。

        `recommendation_decision` 会从数据库组装专用 DTO；报告类 Workflow 使用
        通用 state 参数，通过工具层读取组合、观察池、推荐、因子、信号风险和记忆。
        """

        as_of = started_at or datetime.now(UTC)
        run_id = workflow_run_id or build_agent_workflow_run_id(
            workflow_type=workflow_type,
            owner_id=owner_id,
            as_of=as_of,
        )
        state = dict(initial_state or {})
        state["session"] = self.session
        state.setdefault("tool_runtime", self.tool_runtime)

        if workflow_type == "portfolio_monitoring":
            if not portfolio_id:
                raise ValueError("portfolio_monitoring 需要 portfolio_id。")
            state["workflow_input"] = build_portfolio_monitoring_input(
                session=self.session,
                owner_id=owner_id,
                portfolio_id=portfolio_id,
                as_of=as_of,
                horizon=horizon,
            )
        elif workflow_type == "watchlist_management":
            if not watchlist_id:
                raise ValueError("watchlist_management 需要 watchlist_id。")
            state["workflow_input"] = build_watchlist_management_input(
                session=self.session,
                owner_id=owner_id,
                watchlist_id=watchlist_id,
                as_of=as_of,
                horizon=horizon,
            )
        elif workflow_type == "recommendation_decision":
            if not portfolio_id:
                raise ValueError("recommendation_decision 需要 portfolio_id。")
            if not watchlist_id:
                raise ValueError("recommendation_decision 需要 watchlist_id。")
            if not recommendation_run_id:
                raise ValueError("recommendation_decision 需要 recommendation_run_id。")
            state["workflow_input"] = build_recommendation_decision_input(
                session=self.session,
                owner_id=owner_id,
                portfolio_id=portfolio_id,
                watchlist_id=watchlist_id,
                recommendation_run_id=recommendation_run_id,
                as_of=as_of,
                limit=recommendation_limit,
                horizon=horizon,
            )
        else:
            state.update(
                {
                    "owner_id": owner_id,
                    "portfolio_id": portfolio_id,
                    "watchlist_id": watchlist_id,
                    "recommendation_run_id": recommendation_run_id,
                    "asset_id": asset_id,
                    "asset_ids": asset_ids or [],
                    "source_asset_id": source_asset_id,
                    "candidate_asset_id": candidate_asset_id,
                    "horizon": horizon,
                    "timeframe": timeframe,
                    "recommendation_limit": recommendation_limit,
                }
            )

        summary = self.assistant.run_workflow(
            workflow_type=workflow_type,
            owner_id=owner_id,
            workflow_run_id=run_id,
            trigger_type=trigger_type,
            trigger_ref=trigger_ref or recommendation_run_id or portfolio_id or watchlist_id,
            started_at=as_of,
            initial_state=state,
            input_ref=build_input_ref(
                workflow_type=workflow_type,
                portfolio_id=portfolio_id,
                watchlist_id=watchlist_id,
                recommendation_run_id=recommendation_run_id,
                asset_id=asset_id,
                asset_ids=asset_ids,
            ),
            output_ref=f"agent_workflow:{run_id}",
        )
        return AgentInterfaceResult(
            status="ok",
            data=serialize_workflow_summary(summary),
        )

    def get_workflow_run(self, *, workflow_run_id: str) -> AgentInterfaceResult:
        """读取一次 Workflow 运行和审计事件。"""

        run = self.session.get(AgentWorkflowRunORM, workflow_run_id)
        events = self.assistant.langgraph_adapter.list_events(workflow_run_id)
        return AgentInterfaceResult(
            status="ok",
            data={
                "run": serialize_workflow_run(run) if run else None,
                "events": [serialize_workflow_event(event) for event in events],
            },
        )

    def get_report(self, *, workflow_run_id: str, markdown: bool = False) -> AgentInterfaceResult:
        """读取一次 Workflow 的中文报告草稿。"""

        events = self.assistant.langgraph_adapter.list_events(workflow_run_id)
        report = find_report_from_events(events)
        if markdown and report:
            return AgentInterfaceResult(
                status="ok",
                data={
                    "workflow_run_id": workflow_run_id,
                    "markdown": report.get("markdown", ""),
                    "report": report,
                },
            )
        return AgentInterfaceResult(
            status="ok",
            data={"workflow_run_id": workflow_run_id, "report": report},
        )


def build_portfolio_monitoring_input(
    *,
    session: Session,
    owner_id: str,
    portfolio_id: str,
    as_of: datetime,
    horizon: str,
) -> PortfolioMonitoringInput:
    """从事实库组装持仓监控 Workflow 输入。"""

    portfolios = PortfolioService(session)
    signals = SignalSnapshotRepository(session)
    risks = RiskRepository(session)
    memories = MemoryRepository(session)
    snapshot = portfolios.load_portfolio_snapshot(portfolio_id)
    asset_ids = [position.asset_id for position in snapshot.positions]
    return PortfolioMonitoringInput(
        owner_id=owner_id,
        portfolio=snapshot.portfolio,
        positions=tuple(snapshot.positions),
        signals_by_asset={
            asset_id: signals.get_latest_signal(asset_id=asset_id, horizon=horizon)
            for asset_id in asset_ids
        },
        risks_by_asset={
            asset_id: tuple(risks.list_recent_risks(asset_id=asset_id, limit=5))
            for asset_id in asset_ids
        },
        memories_by_asset={
            asset_id: tuple(
                memories.list_active_memories(
                    owner_id=owner_id,
                    asset_id=asset_id,
                    limit=5,
                )
            )
            for asset_id in asset_ids
        },
        as_of=as_of,
    )


def build_watchlist_management_input(
    *,
    session: Session,
    owner_id: str,
    watchlist_id: str,
    as_of: datetime,
    horizon: str,
) -> WatchlistManagementInput:
    """从事实库组装观察池管理 Workflow 输入。"""

    watchlists = WatchlistService(session)
    signals = SignalSnapshotRepository(session)
    risks = RiskRepository(session)
    memories = MemoryRepository(session)
    watchlist = watchlists.get_watchlist(watchlist_id)
    items = tuple(watchlists.list_active_items(owner_id=owner_id, watchlist_id=watchlist_id))
    asset_ids = [item.asset_id for item in items]
    return WatchlistManagementInput(
        owner_id=owner_id,
        watchlist=watchlist,
        items=items,
        signals_by_asset={
            asset_id: signals.get_latest_signal(asset_id=asset_id, horizon=horizon)
            for asset_id in asset_ids
        },
        risks_by_asset={
            asset_id: tuple(risks.list_recent_risks(asset_id=asset_id, limit=5))
            for asset_id in asset_ids
        },
        memories_by_asset={
            asset_id: tuple(
                memories.list_active_memories(
                    owner_id=owner_id,
                    asset_id=asset_id,
                    limit=5,
                )
            )
            for asset_id in asset_ids
        },
        theses_by_asset={
            asset_id: watchlists.list_asset_theses(owner_id=owner_id, asset_id=asset_id)
            for asset_id in asset_ids
        },
        as_of=as_of,
    )


def build_recommendation_decision_input(
    *,
    session: Session,
    owner_id: str,
    portfolio_id: str,
    watchlist_id: str,
    recommendation_run_id: str,
    as_of: datetime,
    limit: int,
    horizon: str,
) -> RecommendationDecisionInput:
    """从事实库组装推荐决策 Workflow 输入。"""

    portfolios = PortfolioService(session)
    watchlists = WatchlistService(session)
    recommendations = RecommendationRepository(session)
    signals = SignalSnapshotRepository(session)
    risks = RiskRepository(session)
    assistant = FinanceAssistantService(session)

    watchlist = watchlists.get_watchlist(watchlist_id)
    recommendation_items = tuple(
        recommendations.list_top_recommendations(run_id=recommendation_run_id, limit=limit)
    )
    portfolio_snapshot = portfolios.load_portfolio_snapshot(portfolio_id)
    watchlist_items = tuple(
        watchlists.list_active_items(owner_id=owner_id, watchlist_id=watchlist_id)
    )
    asset_ids = sorted(
        {
            *(item.asset_id for item in recommendation_items),
            *(position.asset_id for position in portfolio_snapshot.positions),
            *(item.asset_id for item in watchlist_items),
        }
    )
    return RecommendationDecisionInput(
        owner_id=owner_id,
        recommendation_run_id=recommendation_run_id,
        portfolio_id=portfolio_id,
        watchlist=watchlist,
        recommendations=recommendation_items,
        positions=tuple(portfolio_snapshot.positions),
        watchlist_items=watchlist_items,
        signals_by_asset={
            asset_id: signals.get_latest_signal(asset_id=asset_id, horizon=horizon)
            for asset_id in asset_ids
        },
        risks_by_asset={
            asset_id: tuple(risks.list_recent_risks(asset_id=asset_id, limit=5))
            for asset_id in asset_ids
        },
        memories_by_asset={
            asset_id: tuple(
                assistant.memories.list_active_memories(
                    owner_id=owner_id,
                    asset_id=asset_id,
                    limit=5,
                )
            )
            for asset_id in asset_ids
        },
        as_of=as_of,
    )


def serialize_workflow_summary(summary: Any) -> JsonDict:
    """序列化 `FinanceWorkflowRunSummary`。"""

    return {
        "workflow_run_id": summary.workflow_run_id,
        "workflow_type": summary.workflow_type,
        "final_state": sanitize_interface_state(summary.final_state),
        "report": summary.report,
    }


def sanitize_interface_state(state: JsonDict) -> JsonDict:
    """清理不适合通过 CLI/MCP 返回的运行时对象。"""

    return {
        key: value
        for key, value in state.items()
        if key not in {"session", "tool_runtime", "workflow_input", "result"}
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


def serialize_workflow_event(event: AgentWorkflowEventORM) -> JsonDict:
    """序列化 Workflow 审计事件。"""

    return {
        "workflow_event_id": event.workflow_event_id,
        "workflow_run_id": event.workflow_run_id,
        "event_type": event.event_type,
        "agent_name": event.agent_name,
        "message": event.message,
        "evidence_ids": json_value(event.evidence_ids or []),
        "created_at": json_value(event.created_at),
        "payload": json_value(event.payload or {}),
    }


def find_report_from_events(events: tuple[AgentWorkflowEventORM, ...]) -> JsonDict | None:
    """从审计事件中提取最后一份中文报告。"""

    report_events = [event for event in events if event.event_type == "report_draft"]
    if not report_events:
        return None
    payload = report_events[-1].payload or {}
    output = payload.get("output", {})
    report = output.get("report") or output
    return json_value(report)


def build_agent_workflow_run_id(*, workflow_type: str, owner_id: str, as_of: datetime) -> str:
    """生成上层 Agent 触发的 Workflow Run ID。"""

    clean_owner = owner_id.replace(":", "_")
    return f"workflow:{clean_owner}:{workflow_type}:{as_of:%Y%m%d%H%M%S}"


def build_input_ref(
    *,
    workflow_type: str,
    portfolio_id: str | None,
    watchlist_id: str | None,
    recommendation_run_id: str | None,
    asset_id: str | None,
    asset_ids: list[str] | None,
) -> str:
    """生成可审计输入引用。"""

    parts = [workflow_type]
    for prefix, value in (
        ("portfolio", portfolio_id),
        ("watchlist", watchlist_id),
        ("recommendation_run", recommendation_run_id),
        ("asset", asset_id),
    ):
        if value:
            parts.append(f"{prefix}:{value}")
    if asset_ids:
        parts.append(f"assets:{','.join(asset_ids)}")
    return "|".join(parts)


def parse_datetime(value: str | None) -> datetime | None:
    """解析 CLI/MCP 传入的 ISO 时间。"""

    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
