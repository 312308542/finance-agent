"""CLI 和 MCP 共用的金融助手调用接口。

本模块只做参数归一化、服务调用和 JSON 序列化，不承载金融决策逻辑。
CLI、MCP、Scheduler 或后续 API 都应通过这里调用 `FinanceAssistantService`，
避免多个入口各自拼装 Workflow。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.orm import Session

from finance_agent.agents.personal_assistant import FinanceAssistantService
from finance_agent.agents.tools.runtime import (
    FinanceToolRuntime,
    json_value,
    serialize_intraday_quote,
)
from finance_agent.agents.workflows.portfolio_monitoring import PortfolioMonitoringInput
from finance_agent.agents.workflows.recommendation_decision import RecommendationDecisionInput
from finance_agent.agents.workflows.watchlist_management import WatchlistManagementInput
from finance_agent.application import PortfolioService, WatchlistService
from finance_agent.application.decision_gate import DecisionGateInput, DecisionGateService
from finance_agent.graph import GraphSyncService
from finance_agent.graph.stores import DryRunGraphStore
from finance_agent.monitoring.models import PositionAction
from finance_agent.storage.orm import AgentWorkflowEventORM, AgentWorkflowRunORM
from finance_agent.storage.repositories import (
    AssetRepository,
    DataSnapshotRepository,
    DecisionGateRepository,
    MemoryRepository,
    RecommendationRepository,
    RiskRepository,
    SignalSnapshotRepository,
)
from finance_agent.storage.snapshot_contracts import snapshot_from_orm

JsonDict = dict[str, Any]


def _decimal_value(value: Any) -> Decimal | None:
    """将触发事件中的数值安全转换为 Decimal。"""

    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return None


def build_workflow_gate_context(
    *,
    session: Session,
    workflow_type: str,
    as_of: datetime,
) -> JsonDict:
    """从最新 A 股事实快照构造 Workflow 的可拒绝闸门上下文。"""

    max_age = timedelta(minutes=10)
    if workflow_type == "recommendation_decision":
        max_age = timedelta(hours=24)
    latest = DataSnapshotRepository(session).get_latest(
        snapshot_type="ashare_realtime_quotes",
        market="ashare",
    )
    snapshot = snapshot_from_orm(latest) if latest is not None else None
    gate = DecisionGateService(max_age=max_age).evaluate(
        DecisionGateInput(
            decision_type=workflow_type,
            action="analysis",
            snapshot=snapshot,
            evaluated_at=as_of,
        )
    )
    DecisionGateRepository(session).insert_gate(gate)
    return {
        "data_snapshot_id": gate.data_snapshot_id,
        "decision_gate_id": gate.decision_gate_id,
        "decision_gate_status": gate.status,
    }


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
                "read_only": self.tool_runtime.get_tool(name).read_only,
                "requires_review": self.tool_runtime.get_tool(name).requires_review,
                "write_scope": self.tool_runtime.get_tool(name).write_scope,
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
        gate_context = state.get("decision_gate_context")
        if workflow_type in {"portfolio_monitoring", "recommendation_decision"}:
            gate_context = gate_context or build_workflow_gate_context(
                session=self.session,
                workflow_type=workflow_type,
                as_of=as_of,
            )

        if workflow_type == "portfolio_monitoring":
            if not portfolio_id:
                raise ValueError("portfolio_monitoring 需要 portfolio_id。")
            state["workflow_input"] = build_portfolio_monitoring_input(
                session=self.session,
                owner_id=owner_id,
                portfolio_id=portfolio_id,
                as_of=as_of,
                horizon=horizon,
                gate_context=gate_context,
            )
            trigger_payload = ((state.get("trigger_event") or {}).get("payload") or {})
            if (
                trigger_payload.get("action")
                and trigger_payload.get("position_id")
            ):
                action = PositionAction(
                    position_id=str(trigger_payload["position_id"]),
                    action=str(trigger_payload["action"]),
                    intended_action=trigger_payload.get("intended_action"),
                    severity=str(trigger_payload.get("severity") or "medium"),
                    reason_codes=tuple(trigger_payload.get("reason_codes") or ()),
                    protective_price=_decimal_value(trigger_payload.get("protective_price")),
                    suggested_quantity=_decimal_value(trigger_payload.get("suggested_quantity"))
                    or Decimal("0"),
                    evaluated_at=as_of,
                    quote_snapshot_id=str(trigger_payload.get("quote_snapshot_id") or ""),
                    decision_snapshot_id=trigger_payload.get("decision_snapshot_id"),
                    payload=dict(trigger_payload.get("payload") or {}),
                )
                state["workflow_input"] = replace(
                    state["workflow_input"],
                    position_actions_by_position={action.position_id: action},
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
                gate_context=gate_context,
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
        node_trace = extract_node_trace(events)
        model_routes = extract_model_routes(events, event_type="model_route")
        review_model_routes = extract_model_routes(events, event_type="model_review")
        return AgentInterfaceResult(
            status="ok",
            data={
                "run": serialize_workflow_run(run) if run else None,
                "events": [serialize_workflow_event(event) for event in events],
                "node_trace": node_trace,
                "model_routes": model_routes,
                "review_model_routes": review_model_routes,
            },
        )

    def get_report(self, *, workflow_run_id: str, markdown: bool = False) -> AgentInterfaceResult:
        """读取一次 Workflow 的中文报告草稿。"""

        events = self.assistant.langgraph_adapter.list_events(workflow_run_id)
        report = find_report_from_events(events)
        report_review_appended = (
            report.get("report_review_appended")
            if isinstance(report, dict)
            else build_report_review_appended([])
        )
        review_results = (
            report.get("review_results")
            if isinstance(report, dict)
            else []
        )
        if markdown and report:
            return AgentInterfaceResult(
                status="ok",
                data={
                    "workflow_run_id": workflow_run_id,
                    "markdown": report.get("markdown", ""),
                    "report": report,
                    "review_results": review_results,
                    "report_review_appended": report_review_appended,
                },
            )
        return AgentInterfaceResult(
            status="ok",
            data={
                "workflow_run_id": workflow_run_id,
                "report": report,
                "review_results": review_results,
                "report_review_appended": report_review_appended,
            },
        )


def build_portfolio_monitoring_input(
    *,
    session: Session,
    owner_id: str,
    portfolio_id: str,
    as_of: datetime,
    horizon: str,
    gate_context: JsonDict | None = None,
) -> PortfolioMonitoringInput:
    """从事实库组装持仓监控 Workflow 输入。"""

    portfolios = PortfolioService(session)
    signals = SignalSnapshotRepository(session)
    risks = RiskRepository(session)
    memories = MemoryRepository(session)
    snapshot = portfolios.load_portfolio_snapshot(portfolio_id)
    asset_ids = [position.asset_id for position in snapshot.positions]
    intraday_rows = AssetRepository(session).list_intraday_quote_latest(
        asset_ids=asset_ids,
        quality_statuses=("available", "partial", "conflict"),
    )
    intraday_quotes_by_asset: dict[str, list[dict[str, Any]]] = {}
    for row in intraday_rows:
        intraday_quotes_by_asset.setdefault(row.asset_id, []).append(serialize_intraday_quote(row))
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
        data_snapshot_id=str(gate_context.get("data_snapshot_id")) if gate_context else None,
        decision_gate_id=str(gate_context.get("decision_gate_id")) if gate_context else None,
        decision_gate_status=str(gate_context.get("decision_gate_status")) if gate_context else None,
        intraday_quotes_by_asset={
            asset_id: tuple(rows) for asset_id, rows in intraday_quotes_by_asset.items()
        },
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
    gate_context: JsonDict | None = None,
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
        data_snapshot_id=str(gate_context.get("data_snapshot_id")) if gate_context else None,
        decision_gate_id=str(gate_context.get("decision_gate_id")) if gate_context else None,
        decision_gate_status=str(gate_context.get("decision_gate_status")) if gate_context else None,
    )


def serialize_workflow_summary(summary: Any) -> JsonDict:
    """序列化 `FinanceWorkflowRunSummary`。"""

    return {
        "workflow_run_id": summary.workflow_run_id,
        "workflow_type": summary.workflow_type,
        "final_state": sanitize_interface_state(summary.final_state),
        "report": interface_json_value(summary.report),
    }


def sanitize_interface_state(state: JsonDict) -> JsonDict:
    """清理不适合通过 CLI/MCP 返回的运行时对象。"""

    return {
        key: interface_json_value(value)
        for key, value in state.items()
        if key
        not in {
            "session",
            "tool_runtime",
            "workflow_input",
            "result",
            "model_registry",
            "model_client",
            "model_config_repository",
        }
    }


def interface_json_value(value: Any) -> Any:
    """把 Workflow 输出深度转换为 JSON 友好值。"""

    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): interface_json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [interface_json_value(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return interface_json_value(asdict(value))
    orm_payload = serialize_orm_columns(value)
    if orm_payload is not None:
        return orm_payload
    return str(value)


def serialize_orm_columns(value: Any) -> JsonDict | None:
    """把 SQLAlchemy ORM 实例按列转为 JSON 友好字典。"""

    try:
        inspected = sqlalchemy_inspect(value, raiseerr=False)
    except Exception:  # noqa: BLE001 - 接口输出清洗不能因未知对象中断
        return None
    mapper = getattr(inspected, "mapper", None)
    if mapper is None:
        return None
    return {
        column.key: interface_json_value(getattr(value, column.key))
        for column in mapper.column_attrs
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
    return append_report_review_results(
        report=json_value(report),
        review_results=extract_report_review_results(events),
    )


def extract_report_review_results(events: tuple[AgentWorkflowEventORM, ...]) -> list[JsonDict]:
    """从审计链提取异步高风险复核结果。"""

    results: list[JsonDict] = []
    for event in events:
        payload = event.payload or {}
        result_payload: JsonDict | None = None
        if event.event_type == "model_review_result" and isinstance(payload, dict):
            result_payload = payload
        elif event.event_type == "model_review" and isinstance(payload, dict):
            output = payload.get("output")
            if isinstance(output, dict) and isinstance(output.get("review_result"), dict):
                result_payload = output["review_result"]
        if result_payload is None:
            continue
        result = json_value(result_payload)
        if not isinstance(result, dict):
            continue
        created_at = getattr(event, "created_at", None)
        if created_at is not None:
            result.setdefault("created_at", json_value(created_at))
        workflow_event_id = getattr(event, "workflow_event_id", None)
        if workflow_event_id:
            result.setdefault("workflow_event_id", workflow_event_id)
        results.append(result)
    return results


def append_report_review_results(
    *,
    report: JsonDict,
    review_results: list[JsonDict],
) -> JsonDict:
    """把异步复核结果追加到报告结构和 Markdown 中。"""

    if not review_results:
        return report
    enriched = dict(report)
    appended = build_report_review_appended(review_results)
    review_status = dict(enriched.get("review_status") or {})
    latest_result = review_results[-1]
    review_status["status"] = str(
        latest_result.get("review_status")
        or latest_result.get("verdict")
        or review_status.get("status")
        or "review_completed"
    )
    review_status["result_count"] = len(review_results)
    review_status["latest_result"] = latest_result
    enriched["review_status"] = review_status
    enriched["review_results"] = review_results
    enriched["report_review_appended"] = appended

    markdown = str(enriched.get("markdown") or "")
    appendix = appended["markdown"]
    if appendix and "## 异步高风险复核结果" not in markdown:
        enriched["markdown"] = (
            f"{markdown.rstrip()}\n\n{appendix}" if markdown else appendix
        )
    return enriched


def build_report_review_appended(review_results: list[JsonDict]) -> JsonDict:
    """构造报告详情页可直接展示的复核追加片段。"""

    if not review_results:
        return {"items": [], "markdown": ""}
    lines = ["## 异步高风险复核结果"]
    for index, result in enumerate(review_results, start=1):
        status = result.get("review_status") or result.get("verdict") or "unknown"
        verdict = result.get("verdict") or "unknown"
        confidence = result.get("confidence")
        confidence_text = "无" if confidence is None else str(confidence)
        lines.append(
            f"- 复核 {index}：状态 {status}，裁决 {verdict}，置信度 {confidence_text}。"
        )
        reasons = normalize_report_text_list(result.get("reasons"))
        blocking_risks = normalize_report_text_list(result.get("blocking_risks"))
        data_gaps = normalize_report_text_list(result.get("data_gaps"))
        if reasons:
            lines.extend(f"  - 理由：{item}" for item in reasons)
        if blocking_risks:
            lines.extend(f"  - 阻断风险：{item}" for item in blocking_risks)
        if data_gaps:
            lines.extend(f"  - 数据缺口：{item}" for item in data_gaps)
    return {"items": review_results, "markdown": "\n".join(lines)}


def normalize_report_text_list(value: object) -> list[str]:
    """安全读取复核结果中的文本列表。"""

    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def extract_node_trace(events: tuple[AgentWorkflowEventORM, ...]) -> list[str]:
    """从审计事件中提取节点执行顺序。"""

    node_trace: list[str] = []
    seen: set[str] = set()
    for event in events:
        if event.event_type not in {"workflow_node_completed", "high_risk_review", "report_draft"}:
            continue
        node_name = str(event.agent_name or "").strip()
        if not node_name or node_name in seen:
            continue
        seen.add(node_name)
        node_trace.append(node_name)
    return node_trace


def extract_model_routes(
    events: tuple[AgentWorkflowEventORM, ...],
    *,
    event_type: str,
) -> list[JsonDict]:
    """从审计事件中提取模型路由记录。"""

    routes: list[JsonDict] = []
    for event in events:
        if event.event_type != event_type:
            continue
        payload = event.payload or {}
        output = payload.get("output") if isinstance(payload, dict) else None
        if isinstance(output, dict):
            if event_type == "model_review" and isinstance(output.get("route"), dict):
                routes.append(json_value(output["route"]))
            else:
                routes.append(json_value(output))
    return routes


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
