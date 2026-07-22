"""LangGraph 金融团队 Workflow 构建器。

本模块只负责把现有确定性 Workflow 包装成 LangGraph 图节点。LangGraph
缺失时会给出明确错误；现有规则版 Workflow 仍可作为 fallback 独立运行。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import inspect as sqlalchemy_inspect

from finance_agent.agents.reports import build_chinese_decision_report
from finance_agent.agents.runtime import (
    HighRiskReviewPolicy,
    ModelClient,
    ModelEndpointConfig,
    ModelRegistry,
    ModelRoutingPolicy,
    OpenAICompatibleModelClient,
    ReviewDecisionContext,
    build_workflow_context_envelope,
    load_model_registry,
)
from finance_agent.agents.runtime.model_config import (
    build_model_configs_from_repository,
    build_retrieval_profiles_from_repository,
)
from finance_agent.agents.tools import FinanceToolRuntime
from finance_agent.agents.workflows.portfolio_monitoring import (
    PortfolioMonitoringWorkflow,
)
from finance_agent.agents.workflows.recommendation_decision import (
    RecommendationDecisionInput,
    RecommendationDecisionWorkflow,
)
from finance_agent.agents.workflows.roundtable_model_nodes import (
    RoundtableOpinionRequest,
    build_fallback_opinion,
    collect_evidence_ids,
    generate_model_opinion,
)
from finance_agent.agents.workflows.watchlist_management import (
    WatchlistManagementWorkflow,
)
from finance_agent.graph.stores import DryRunGraphStore
from finance_agent.storage.repositories import ModelRuntimeConfigRepository

WorkflowGraphState = dict[str, Any]
ROUNDTABLE_MODEL_ROLES = (
    "technical_analyst",
    "factor_analyst",
    "event_analyst",
    "flow_analyst",
    "risk_rebuttal",
    "portfolio_manager",
    "memory_manager",
)
DAILY_REVIEW_DEFAULT_MODEL_ROLES = ("risk_rebuttal", "portfolio_manager")


class LangGraphWorkflowUnavailable(RuntimeError):
    """LangGraph 依赖不可用。"""


@dataclass(frozen=True)
class LangGraphWorkflowBuilder:
    """LangGraph Workflow 构建器元数据。"""

    workflow_type: str
    description: str
    build: Callable[[], Any]


def infer_market_type(state: WorkflowGraphState, *, default: str = "ashare") -> str:
    """从 workflow state 里推断市场类型。"""

    explicit = state.get("market_type")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()

    asset_contexts = state.get("asset_contexts") or {}
    for asset_context in asset_contexts.values():
        profile = asset_context.get("profile") or {}
        market = profile.get("market")
        if isinstance(market, str) and market.strip():
            return market.strip()

    workflow_input = state.get("workflow_input")
    recommendations = getattr(workflow_input, "recommendations", None)
    if recommendations:
        first = recommendations[0]
        market = getattr(first, "market", None)
        if isinstance(market, str) and market.strip():
            return market.strip()

    return default


def attach_context_envelope(
    state: WorkflowGraphState,
    *,
    workflow_type: str,
) -> dict[str, Any]:
    """把共享上下文 envelope 写入 state。"""

    return build_workflow_context_envelope(
        workflow_type=workflow_type,
        market_type=infer_market_type(state),
        asset_ids=state.get("asset_ids", []),
        asset_contexts=state.get("asset_contexts", {}),
        portfolio_context=state.get("portfolio_context"),
        watchlist_context=state.get("watchlist_context"),
        recommendation_context=state.get("recommendation_context"),
        trigger_event=state.get("trigger_event"),
        available_tools=state.get("available_tools", []),
    ).to_dict()


def build_recommendation_context_envelope(state: WorkflowGraphState):
    """为推荐决策 workflow 构建共享上下文 envelope。"""

    workflow_input: RecommendationDecisionInput = state["workflow_input"]
    asset_contexts = build_recommendation_asset_contexts(state)
    return build_workflow_context_envelope(
        workflow_type="recommendation_decision",
        market_type=infer_market_type(state),
        asset_ids=[recommendation.asset_id for recommendation in workflow_input.recommendations],
        asset_contexts=asset_contexts,
        portfolio_context={
            "portfolio_id": workflow_input.portfolio_id,
            "positions": [position.position_id for position in workflow_input.positions],
        },
        watchlist_context={
            "watchlist_id": workflow_input.watchlist.watchlist_id,
            "items": [item.watchlist_item_id for item in workflow_input.watchlist_items],
        },
        recommendation_context={
            "run_id": workflow_input.recommendation_run_id,
            "recommendations": [recommendation.recommendation_id for recommendation in workflow_input.recommendations],
        },
        trigger_event=state.get("trigger_event"),
        available_tools=state.get("available_tools", []),
    )


def build_recommendation_asset_contexts(
    state: WorkflowGraphState,
) -> dict[str, dict[str, Any]]:
    """为推荐决策圆桌组装各标的上下文。"""

    workflow_input: RecommendationDecisionInput = state["workflow_input"]
    factor_contexts = state.get("factor_contexts", {})
    structure_contexts = state.get("structure_contexts", {})
    return {
        recommendation.asset_id: {
            "profile": {
                "asset_id": recommendation.asset_id,
                "symbol": recommendation.symbol,
                "market": recommendation.market,
            },
            "factor": factor_contexts.get(recommendation.asset_id, {}),
            "structure": structure_contexts.get(recommendation.asset_id, {}),
            "signal_risk": {
                "signal": workflow_input.signals_by_asset.get(recommendation.asset_id),
                "risks": workflow_input.risks_by_asset.get(recommendation.asset_id, ()),
            },
            "memory": {
                "memories": workflow_input.memories_by_asset.get(recommendation.asset_id, ()),
            },
            "backtest": extract_recommendation_backtest_evidence(recommendation),
        }
        for recommendation in workflow_input.recommendations
    }


def extract_recommendation_backtest_evidence(recommendation: Any) -> dict[str, Any] | None:
    """从推荐 payload 中提取回测证据，供报告和圆桌上下文消费。"""

    payload = getattr(recommendation, "payload", None)
    if not isinstance(payload, dict):
        return None
    backtest = payload.get("backtest_evidence")
    return backtest if isinstance(backtest, dict) else None


def _load_langgraph() -> tuple[Any, Any, Any]:
    """延迟加载 LangGraph，便于本地缺依赖时保留规则 fallback。"""

    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as exc:
        raise LangGraphWorkflowUnavailable(
            "缺少 langgraph 依赖。请先安装项目依赖："
            ".venv\\Scripts\\python.exe -m pip install langgraph"
        ) from exc
    return StateGraph, START, END


def build_portfolio_monitoring_graph() -> Any:
    """构建持仓监控 LangGraph 工作流。"""

    return build_operational_roundtable_graph(
        workflow_type="portfolio_monitoring",
        title="持仓监控圆桌报告",
        summary="金融团队已基于持仓、TA 指标、因子评分、风险和记忆完成持仓监控圆桌裁决。",
        workflow=PortfolioMonitoringWorkflow(),
    )


def build_watchlist_management_graph() -> Any:
    """构建观察池管理 LangGraph 工作流。"""

    return build_operational_roundtable_graph(
        workflow_type="watchlist_management",
        title="观察池管理圆桌报告",
        summary="金融团队已基于观察池、TA 指标、因子评分、风险、投资假设和记忆完成观察池圆桌裁决。",
        workflow=WatchlistManagementWorkflow(),
    )


def build_operational_roundtable_graph(
    *,
    workflow_type: str,
    title: str,
    summary: str,
    workflow: Any,
) -> Any:
    """构建持仓和观察池这类运营型 Workflow 的圆桌图。"""

    StateGraph, START, END = _load_langgraph()
    review_policy = HighRiskReviewPolicy()
    graph = StateGraph(dict)

    def load_context(state: WorkflowGraphState) -> WorkflowGraphState:
        tool_runtime = build_tool_runtime(state)
        return {
            **state,
            "workflow_type": workflow_type,
            "tool_runtime": tool_runtime,
            "node_trace": [*state.get("node_trace", []), "load_context"],
        }

    def data_gathering(state: WorkflowGraphState) -> WorkflowGraphState:
        context = collect_operational_workflow_context(state, workflow_type=workflow_type)
        return {
            **state,
            **context,
            "node_trace": [*state.get("node_trace", []), "data_gathering"],
        }

    def roundtable_discussion(state: WorkflowGraphState) -> WorkflowGraphState:
        model_policy = build_model_policy(state)
        model_routes = [
            model_policy.route_primary(
                workflow_type=workflow_type,
                task="roundtable_discussion",
                asset_id=asset_id,
                reason="持仓和观察池圆桌基于已入库事实进行常规分析，默认使用 DeepSeek V4 Pro。",
            ).to_dict()
            for asset_id in state.get("asset_ids", [])
        ]
        opinions = build_report_roundtable_opinions(
            workflow_type=workflow_type,
            asset_contexts=state.get("asset_contexts", {}),
            portfolio_context=state.get("portfolio_context"),
            watchlist_context=state.get("watchlist_context"),
            recommendation_context=None,
            source_asset_id=None,
            candidate_asset_id=None,
        )
        opinions = enrich_roundtable_opinions_with_model(
            workflow_type=workflow_type,
            fallback_opinions=opinions,
            asset_contexts=state.get("asset_contexts", {}),
            state=state,
            model_routes=model_routes,
            portfolio_context=state.get("portfolio_context"),
            watchlist_context=state.get("watchlist_context"),
            recommendation_context=None,
        )
        return {
            **state,
            "context_envelope": attach_context_envelope(state, workflow_type=workflow_type),
            "model_routes": [*state.get("model_routes", []), *model_routes],
            "roundtable_opinions": opinions,
            "node_trace": [*state.get("node_trace", []), "roundtable_discussion"],
        }

    def decision_synthesis(state: WorkflowGraphState) -> WorkflowGraphState:
        workflow_input = state["workflow_input"]
        result = workflow.run(workflow_input)
        decisions = serialize_operational_decisions(
            workflow_type=workflow_type,
            decisions=result.decisions,
            asset_contexts=state.get("asset_contexts", {}),
        )
        return {
            **state,
            "result": result,
            "workflow_decisions": decisions,
            "decision_count": len(result.decisions),
            "node_trace": [*state.get("node_trace", []), "decision_synthesis"],
        }

    def high_risk_review(state: WorkflowGraphState) -> WorkflowGraphState:
        model_policy = build_model_policy(state)
        review_items = build_high_risk_review_items(
            workflow_type=workflow_type,
            decisions=state.get("workflow_decisions", []),
            asset_contexts=state.get("asset_contexts", {}),
            review_policy=review_policy,
            model_policy=model_policy,
        )
        review_routes = [
            item["model_review"]["route"]
            for item in review_items
            if item.get("model_review", {}).get("route")
        ]
        return {
            **state,
            "high_risk_reviews": review_items,
            "review_model_routes": review_routes,
            "node_trace": [*state.get("node_trace", []), "high_risk_review"],
        }

    def report_draft(state: WorkflowGraphState) -> WorkflowGraphState:
        asset_symbols = [
            state.get("asset_contexts", {}).get(asset_id, {}).get("profile", {}).get("symbol")
            or asset_id
            for asset_id in state.get("asset_ids", [])
        ]
        report = build_chinese_decision_report(
            title=build_report_title(
                title=title,
                workflow_type=workflow_type,
                asset_symbols=asset_symbols,
            ),
            summary=summary,
            workflow_type=workflow_type,
            asset_symbols=asset_symbols,
            decisions=state.get("workflow_decisions", []),
            roundtable_opinions=state.get("roundtable_opinions", []),
            high_risk_reviews=state.get("high_risk_reviews", []),
            asset_contexts=state.get("asset_contexts", {}),
            model_routes=state.get("model_routes", []),
            review_model_routes=state.get("review_model_routes", []),
        )
        return {
            **state,
            "context_envelope": state.get("context_envelope")
            or attach_context_envelope(state, workflow_type=workflow_type),
            "report": report,
            "node_trace": [*state.get("node_trace", []), "report_draft"],
        }

    graph.add_node("load_context", load_context)
    graph.add_node("data_gathering", data_gathering)
    graph.add_node("roundtable_discussion", roundtable_discussion)
    graph.add_node("decision_synthesis", decision_synthesis)
    graph.add_node("high_risk_review", high_risk_review)
    graph.add_node("report_draft", report_draft)
    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "data_gathering")
    graph.add_edge("data_gathering", "roundtable_discussion")
    graph.add_edge("roundtable_discussion", "decision_synthesis")
    graph.add_edge("decision_synthesis", "high_risk_review")
    graph.add_edge("high_risk_review", "report_draft")
    graph.add_edge("report_draft", END)
    return graph.compile()


def build_asset_deep_analysis_graph() -> Any:
    """构建单标的深度分析 LangGraph 骨架。

    第一阶段使用确定性圆桌摘要，后续可在同一结构上替换为 LLM 节点。
    """

    return build_roundtable_report_graph(
        workflow_type="asset_deep_analysis",
        title="单标的深度分析报告",
        summary="已创建单标的深度分析 Workflow 骨架，后续接入指标、因子、风险和记忆圆桌。",
    )


def build_swap_decision_graph() -> Any:
    """构建换股或换币比较 LangGraph 骨架。"""

    return build_roundtable_report_graph(
        workflow_type="swap_decision",
        title="换股/换币比较报告",
        summary="已创建弱持仓与强候选比较 Workflow 骨架，高风险动作后续进入复核。",
    )


def build_daily_review_graph() -> Any:
    """构建每日复盘 LangGraph 骨架。"""

    return build_roundtable_report_graph(
        workflow_type="daily_review",
        title="每日金融助手复盘报告",
        summary="已创建每日持仓、观察池、推荐和风险复盘 Workflow 骨架。",
    )


def build_static_report_graph(
    *,
    workflow_type: str,
    title: str,
    summary: str,
) -> Any:
    """构建只产出报告骨架的 LangGraph。"""

    StateGraph, START, END = _load_langgraph()
    graph = StateGraph(dict)

    def load_context(state: WorkflowGraphState) -> WorkflowGraphState:
        return {
            **state,
            "workflow_type": workflow_type,
            "node_trace": [*state.get("node_trace", []), "load_context"],
        }

    def report_draft(state: WorkflowGraphState) -> WorkflowGraphState:
        return {
            **state,
            "report": {
                "title": title,
                "summary": summary,
                "workflow_type": workflow_type,
            },
            "node_trace": [*state.get("node_trace", []), "report_draft"],
        }

    graph.add_node("load_context", load_context)
    graph.add_node("report_draft", report_draft)
    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "report_draft")
    graph.add_edge("report_draft", END)
    return graph.compile()


def build_roundtable_report_graph(
    *,
    workflow_type: str,
    title: str,
    summary: str,
) -> Any:
    """构建带工具调用、圆桌、复核和报告的通用 LangGraph。"""

    StateGraph, START, END = _load_langgraph()
    review_policy = HighRiskReviewPolicy()
    graph = StateGraph(dict)

    def load_context(state: WorkflowGraphState) -> WorkflowGraphState:
        tool_runtime = build_tool_runtime(state)
        return {
            **state,
            "workflow_type": workflow_type,
            "tool_runtime": tool_runtime,
            "node_trace": [*state.get("node_trace", []), "load_context"],
        }

    def data_gathering(state: WorkflowGraphState) -> WorkflowGraphState:
        context = collect_roundtable_report_context(state)
        return {
            **state,
            **context,
            "node_trace": [*state.get("node_trace", []), "data_gathering"],
        }

    def roundtable_discussion(state: WorkflowGraphState) -> WorkflowGraphState:
        model_policy = build_model_policy(state)
        model_routes = [
            model_policy.route_primary(
                workflow_type=workflow_type,
                task="roundtable_discussion",
                asset_id=asset_id,
                reason="圆桌角色基于已入库事实进行常规分析，默认使用 DeepSeek V4 Pro。",
            ).to_dict()
            for asset_id in state.get("asset_ids", [])
        ]
        opinions = build_report_roundtable_opinions(
            workflow_type=workflow_type,
            asset_contexts=state.get("asset_contexts", {}),
            portfolio_context=state.get("portfolio_context"),
            watchlist_context=state.get("watchlist_context"),
            recommendation_context=state.get("recommendation_context"),
            source_asset_id=state.get("source_asset_id"),
            candidate_asset_id=state.get("candidate_asset_id"),
        )
        opinions = enrich_roundtable_opinions_with_model(
            workflow_type=workflow_type,
            fallback_opinions=opinions,
            asset_contexts=state.get("asset_contexts", {}),
            state=state,
            model_routes=model_routes,
            portfolio_context=state.get("portfolio_context"),
            watchlist_context=state.get("watchlist_context"),
            recommendation_context=state.get("recommendation_context"),
        )
        return {
            **state,
            "context_envelope": attach_context_envelope(state, workflow_type=workflow_type),
            "model_routes": [*state.get("model_routes", []), *model_routes],
            "roundtable_opinions": opinions,
            "node_trace": [*state.get("node_trace", []), "roundtable_discussion"],
        }

    def decision_synthesis(state: WorkflowGraphState) -> WorkflowGraphState:
        decisions = build_report_workflow_decisions(
            workflow_type=workflow_type,
            asset_ids=state.get("asset_ids", []),
            asset_contexts=state.get("asset_contexts", {}),
            source_asset_id=state.get("source_asset_id"),
            candidate_asset_id=state.get("candidate_asset_id"),
        )
        return {
            **state,
            "workflow_decisions": decisions,
            "decision_count": len(decisions),
            "node_trace": [*state.get("node_trace", []), "decision_synthesis"],
        }

    def high_risk_review(state: WorkflowGraphState) -> WorkflowGraphState:
        model_policy = build_model_policy(state)
        review_items = []
        for decision in state.get("workflow_decisions", []):
            asset_context = state.get("asset_contexts", {}).get(decision["asset_id"], {})
            signal = get_nested(asset_context, "signal_risk", "signal") or {}
            risks = get_nested(asset_context, "signal_risk", "risks") or []
            context = ReviewDecisionContext(
                decision_type=decision["decision_type"],
                suggested_action=decision["action"],
                severity=decision["severity"],
                confidence=decision["confidence"],
                data_quality_status=decision["data_quality_status"],
                risk_severities=tuple(risk.get("severity", "unknown") for risk in risks),
                has_conflicting_signal=has_report_conflicting_signal(
                    suggested_action=decision["action"],
                    signal_direction=signal.get("direction"),
                ),
            )
            review_item = {
                "asset_id": decision["asset_id"],
                "decision_type": decision["decision_type"],
                "trade_action": decision["action"],
                "requires_review": review_policy.requires_review(context),
                "reason": context.__dict__,
            }
            review_item["model_review"] = model_policy.build_review_result(
                workflow_type=workflow_type,
                review_item=review_item,
                decision_summary=decision.get("summary"),
            )
            review_items.append(review_item)
        review_routes = [
            item["model_review"]["route"]
            for item in review_items
            if item.get("model_review", {}).get("route")
        ]
        return {
            **state,
            "high_risk_reviews": review_items,
            "review_model_routes": review_routes,
            "node_trace": [*state.get("node_trace", []), "high_risk_review"],
        }

    def report_draft(state: WorkflowGraphState) -> WorkflowGraphState:
        asset_symbols = [
            state.get("asset_contexts", {}).get(asset_id, {}).get("profile", {}).get("symbol")
            or asset_id
            for asset_id in state.get("asset_ids", [])
        ]
        report = build_chinese_decision_report(
            title=build_report_title(
                title=title,
                workflow_type=workflow_type,
                asset_symbols=asset_symbols,
            ),
            summary=summary,
            workflow_type=workflow_type,
            asset_symbols=asset_symbols,
            decisions=state.get("workflow_decisions", []),
            roundtable_opinions=state.get("roundtable_opinions", []),
            high_risk_reviews=state.get("high_risk_reviews", []),
            asset_contexts=state.get("asset_contexts", {}),
            model_routes=state.get("model_routes", []),
            review_model_routes=state.get("review_model_routes", []),
        )
        return {
            **state,
            "context_envelope": state.get("context_envelope")
            or attach_context_envelope(state, workflow_type=workflow_type),
            "report": report,
            "node_trace": [*state.get("node_trace", []), "report_draft"],
        }

    graph.add_node("load_context", load_context)
    graph.add_node("data_gathering", data_gathering)
    graph.add_node("roundtable_discussion", roundtable_discussion)
    graph.add_node("decision_synthesis", decision_synthesis)
    graph.add_node("high_risk_review", high_risk_review)
    graph.add_node("report_draft", report_draft)
    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "data_gathering")
    graph.add_edge("data_gathering", "roundtable_discussion")
    graph.add_edge("roundtable_discussion", "decision_synthesis")
    graph.add_edge("decision_synthesis", "high_risk_review")
    graph.add_edge("high_risk_review", "report_draft")
    graph.add_edge("report_draft", END)
    return graph.compile()


def build_recommendation_decision_graph() -> Any:
    """构建推荐决策 LangGraph 工作流。

    推荐决策使用受控圆桌形式：前置节点准备事实，上下文只能来自工具层；
    圆桌角色分别查看技术指标、因子评分、风险、组合和记忆；主席节点再调用
    确定性 fallback 生成结构化动作，后续进入高风险复核和报告摘要。
    """

    StateGraph, START, END = _load_langgraph()
    workflow = RecommendationDecisionWorkflow()
    review_policy = HighRiskReviewPolicy()
    graph = StateGraph(dict)

    def load_context(state: WorkflowGraphState) -> WorkflowGraphState:
        workflow_input: RecommendationDecisionInput = state["workflow_input"]
        tool_runtime = build_tool_runtime(state)
        contexts = {
            recommendation.asset_id: tool_runtime.call(
                "factor.get_asset_factor_context",
                asset_id=recommendation.asset_id,
                horizon=recommendation.horizon,
                timeframe=state.get("timeframe", "1d"),
                evidence_limit=state.get("evidence_limit", 5),
            )
            for recommendation in workflow_input.recommendations
        }
        structure_contexts = {
            recommendation.asset_id: tool_runtime.call(
                "structure.get_asset_context",
                asset_id=recommendation.asset_id,
                timeframe=state.get("timeframe", "1d"),
            )
            for recommendation in workflow_input.recommendations
        }
        return {
            **state,
            "tool_runtime": tool_runtime,
            "factor_contexts": contexts,
            "structure_contexts": structure_contexts,
            "node_trace": [*state.get("node_trace", []), "load_context"],
        }

    def data_quality_check(state: WorkflowGraphState) -> WorkflowGraphState:
        workflow_input: RecommendationDecisionInput = state["workflow_input"]
        factor_contexts = state.get("factor_contexts", {})
        data_quality = {
            recommendation.asset_id: classify_factor_context_quality(
                factor_contexts.get(recommendation.asset_id, {})
            )
            for recommendation in workflow_input.recommendations
        }
        return {
            **state,
            "data_quality": data_quality,
            "node_trace": [*state.get("node_trace", []), "data_quality_check"],
        }

    def roundtable_discussion(state: WorkflowGraphState) -> WorkflowGraphState:
        model_policy = build_model_policy(state)
        workflow_input: RecommendationDecisionInput = state["workflow_input"]
        opinions: list[dict[str, Any]] = []
        asset_contexts = build_recommendation_asset_contexts(state)
        model_routes = [
            model_policy.route_primary(
                workflow_type="recommendation_decision",
                task="roundtable_discussion",
                asset_id=recommendation.asset_id,
                reason="推荐决策圆桌的常规分析默认使用 DeepSeek V4 Pro。",
            ).to_dict()
            for recommendation in workflow_input.recommendations
        ]
        for recommendation in workflow_input.recommendations:
            factor_context = state.get("factor_contexts", {}).get(recommendation.asset_id, {})
            signal = workflow_input.signals_by_asset.get(recommendation.asset_id)
            risks = workflow_input.risks_by_asset.get(recommendation.asset_id, ())
            memories = workflow_input.memories_by_asset.get(recommendation.asset_id, ())
            opinions.extend(
                build_roundtable_opinions(
                    recommendation=recommendation,
                    factor_context=factor_context,
                    signal=signal,
                    risks=risks,
                    memories=memories,
                    positions=workflow_input.positions,
                )
            )
        opinions = enrich_roundtable_opinions_with_model(
            workflow_type="recommendation_decision",
            fallback_opinions=opinions,
            asset_contexts=asset_contexts,
            state=state,
            model_routes=model_routes,
            portfolio_context={
                "portfolio_id": workflow_input.portfolio_id,
                "positions": [
                    position.position_id for position in workflow_input.positions
                ],
            },
            watchlist_context={
                "watchlist_id": workflow_input.watchlist.watchlist_id,
                "items": [
                    item.watchlist_item_id
                    for item in workflow_input.watchlist_items
                ],
            },
            recommendation_context={
                "run_id": workflow_input.recommendation_run_id,
                "recommendations": [
                    recommendation.recommendation_id
                    for recommendation in workflow_input.recommendations
                ],
            },
        )
        return {
            **state,
            "context_envelope": build_recommendation_context_envelope(state).to_dict(),
            "model_routes": [*state.get("model_routes", []), *model_routes],
            "roundtable_opinions": opinions,
            "node_trace": [*state.get("node_trace", []), "roundtable_discussion"],
        }

    def decision_synthesis(state: WorkflowGraphState) -> WorkflowGraphState:
        workflow_input = state["workflow_input"]
        result = workflow.run(workflow_input)
        return {
            **state,
            "result": result,
            "decision_count": len(result.decisions),
            "node_trace": [*state.get("node_trace", []), "decision_synthesis"],
        }

    def high_risk_review(state: WorkflowGraphState) -> WorkflowGraphState:
        model_policy = build_model_policy(state)
        result = state["result"]
        workflow_input: RecommendationDecisionInput = state["workflow_input"]
        data_quality = state.get("data_quality", {})
        review_items = []
        for decision in result.decisions:
            risks = workflow_input.risks_by_asset.get(decision.asset_id, ())
            context = ReviewDecisionContext(
                decision_type=decision.decision_type,
                suggested_action=decision.trade_action,
                severity=decision.severity,
                confidence=derive_decision_confidence(
                    decision=decision,
                    workflow_input=workflow_input,
                ),
                data_quality_status=data_quality.get(decision.asset_id, "missing"),
                risk_severities=tuple(risk.severity for risk in risks),
                has_conflicting_signal=has_conflicting_signal(
                    decision=decision,
                    workflow_input=workflow_input,
                ),
            )
            review_item = {
                "asset_id": decision.asset_id,
                "decision_type": decision.decision_type,
                "trade_action": decision.trade_action,
                "requires_review": review_policy.requires_review(context),
                "reason": context.__dict__,
            }
            review_item["model_review"] = model_policy.build_review_result(
                workflow_type="recommendation_decision",
                review_item=review_item,
                decision_summary=decision.summary,
            )
            review_items.append(review_item)
        review_routes = [
            item["model_review"]["route"]
            for item in review_items
            if item.get("model_review", {}).get("route")
        ]
        return {
            **state,
            "high_risk_reviews": review_items,
            "review_model_routes": review_routes,
            "node_trace": [*state.get("node_trace", []), "high_risk_review"],
        }

    def report_draft(state: WorkflowGraphState) -> WorkflowGraphState:
        result = state["result"]
        recommendations = state["workflow_input"].recommendations
        primary_symbol = recommendations[0].symbol if recommendations else "空推荐"
        report = build_chinese_decision_report(
            title=f"{primary_symbol} 圆桌决策报告",
            summary="金融团队已基于推荐、TA 指标、AKShare 因子、评分、风险和记忆完成圆桌裁决。",
            workflow_type="recommendation_decision",
            asset_symbols=[recommendation.symbol for recommendation in recommendations],
            decisions=[
                serialize_recommendation_decision(decision)
                for decision in result.decisions
            ],
            roundtable_opinions=state.get("roundtable_opinions", []),
            high_risk_reviews=state.get("high_risk_reviews", []),
            asset_contexts=build_recommendation_asset_contexts(state),
            model_routes=state.get("model_routes", []),
            review_model_routes=state.get("review_model_routes", []),
        )
        return {
            **state,
            "context_envelope": state.get("context_envelope")
            or build_recommendation_context_envelope(state).to_dict(),
            "report": report,
            "node_trace": [*state.get("node_trace", []), "report_draft"],
        }

    graph.add_node("load_context", load_context)
    graph.add_node("data_quality_check", data_quality_check)
    graph.add_node("roundtable_discussion", roundtable_discussion)
    graph.add_node("decision_synthesis", decision_synthesis)
    graph.add_node("high_risk_review", high_risk_review)
    graph.add_node("report_draft", report_draft)
    graph.add_edge(START, "load_context")
    graph.add_edge("load_context", "data_quality_check")
    graph.add_edge("data_quality_check", "roundtable_discussion")
    graph.add_edge("roundtable_discussion", "decision_synthesis")
    graph.add_edge("decision_synthesis", "high_risk_review")
    graph.add_edge("high_risk_review", "report_draft")
    graph.add_edge("report_draft", END)
    return graph.compile()


def build_tool_runtime(state: WorkflowGraphState) -> FinanceToolRuntime:
    """从状态中读取或创建工具运行时。"""

    existing = state.get("tool_runtime")
    if isinstance(existing, FinanceToolRuntime):
        return existing
    session = state.get("session")
    if session is None:
        raise ValueError("推荐决策圆桌 Workflow 需要 session 或 tool_runtime。")
    return FinanceToolRuntime(session, graph_store=DryRunGraphStore())


def build_model_policy(state: WorkflowGraphState) -> ModelRoutingPolicy:
    """从 Workflow state 构建模型路由策略。"""

    repository = state.get("model_config_repository")
    if isinstance(repository, ModelRuntimeConfigRepository):
        return ModelRoutingPolicy(model_config_repository=repository)
    session = state.get("session")
    if session is None:
        return ModelRoutingPolicy()
    return ModelRoutingPolicy(model_config_repository=ModelRuntimeConfigRepository(session))


def resolve_roundtable_model_roles(
    state: WorkflowGraphState,
    *,
    workflow_type: str,
) -> list[str]:
    """解析当前 Workflow 允许真实模型生成的圆桌角色。"""

    raw_roles = state.get("roundtable_model_roles")
    if raw_roles is None:
        if workflow_type == "daily_review":
            return list(DAILY_REVIEW_DEFAULT_MODEL_ROLES)
        return list(ROUNDTABLE_MODEL_ROLES)
    if isinstance(raw_roles, str):
        requested = [raw_roles]
    elif isinstance(raw_roles, (list, tuple, set)):
        requested = [str(role) for role in raw_roles]
    else:
        return []
    if any(role.lower() == "all" for role in requested):
        return list(ROUNDTABLE_MODEL_ROLES)
    requested_set = set(requested)
    return [role for role in ROUNDTABLE_MODEL_ROLES if role in requested_set]


def enrich_roundtable_opinions_with_model(
    *,
    workflow_type: str,
    fallback_opinions: list[dict[str, Any]],
    asset_contexts: dict[str, dict[str, Any]],
    state: WorkflowGraphState,
    model_routes: list[dict[str, Any]],
    portfolio_context: dict[str, Any] | None = None,
    watchlist_context: dict[str, Any] | None = None,
    recommendation_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """在规则版圆桌观点外包一层真实模型增强。"""

    if state.get("roundtable_model_enabled") is False:
        return [
            mark_roundtable_fallback(opinion=opinion)
            for opinion in fallback_opinions
        ]
    enabled_roles = set(
        resolve_roundtable_model_roles(state, workflow_type=workflow_type)
    )
    if not enabled_roles:
        return [
            mark_roundtable_fallback(opinion=opinion)
            for opinion in fallback_opinions
        ]
    registry = resolve_roundtable_model_registry(state)
    client = resolve_roundtable_model_client(state)
    enriched: list[dict[str, Any]] = []
    for opinion in fallback_opinions:
        role = str(opinion.get("role") or "")
        if role not in enabled_roles:
            enriched.append(mark_roundtable_fallback(opinion=opinion))
            continue
        route = find_roundtable_model_route(opinion=opinion, model_routes=model_routes)
        model_config = resolve_roundtable_model_config(registry=registry, route=route)
        if model_config is None:
            enriched.append(
                mark_roundtable_fallback(
                    opinion=opinion,
                    data_gaps=["圆桌模型配置不可用，已使用规则版观点。"],
                )
            )
            continue
        request = RoundtableOpinionRequest(
            role=role,
            asset_id=str(opinion.get("asset_id") or route.get("asset_id") or ""),
            workflow_type=workflow_type,
            context=build_roundtable_model_context(
                opinion=opinion,
                asset_contexts=asset_contexts,
                portfolio_context=portfolio_context,
                watchlist_context=watchlist_context,
                recommendation_context=recommendation_context,
            ),
            question=f"请以 {role} 身份输出该 Workflow 的圆桌观点。",
            allowed_evidence_ids=build_roundtable_allowed_evidence_ids(
                opinion=opinion,
                asset_contexts=asset_contexts,
            ),
            fallback_opinion=opinion,
        )
        model_opinion = generate_model_opinion(
            request=request,
            model_client=client,
            model_config=model_config,
        )
        model_opinion.setdefault("tool_calls", opinion.get("tool_calls", []))
        model_opinion.setdefault("source_ids", opinion.get("source_ids", []))
        enriched.append(model_opinion)
    return enriched


def resolve_roundtable_model_registry(state: WorkflowGraphState) -> Any:
    """读取模型注册表；未注入时按项目默认配置加载。"""

    registry = state.get("model_registry")
    if hasattr(registry, "get"):
        return registry
    session_registry = build_roundtable_model_registry_from_state_repository(state)
    if session_registry is not None:
        return session_registry
    return load_model_registry()


def build_roundtable_model_registry_from_state_repository(
    state: WorkflowGraphState,
) -> ModelRegistry | None:
    """优先从当前 Workflow session 读取数据库模型配置。"""

    repository = state.get("model_config_repository")
    if repository is None:
        session = state.get("session")
        if session is None:
            return None
        repository = ModelRuntimeConfigRepository(session)
    try:
        models = build_model_configs_from_repository(repository)
    except Exception:  # noqa: BLE001 - 模型配置读取失败时保留 fallback
        return None
    if not models:
        return None
    try:
        retrieval_profiles = build_retrieval_profiles_from_repository(repository)
    except Exception:  # noqa: BLE001 - 检索配置不应阻断模型调用
        retrieval_profiles = {}
    return ModelRegistry(
        models=models,
        source="database-session",
        retrieval_profiles=retrieval_profiles,
    )


def resolve_roundtable_model_client(state: WorkflowGraphState) -> ModelClient:
    """读取模型客户端；未注入时使用 OpenAI-compatible 客户端。"""

    client = state.get("model_client")
    if hasattr(client, "invoke_json"):
        return client
    return OpenAICompatibleModelClient()


def resolve_roundtable_model_config(
    *,
    registry: Any,
    route: dict[str, Any],
) -> ModelEndpointConfig | None:
    """按圆桌模型路由读取可调用配置。"""

    model_key = str(route.get("model_key") or "")
    if not model_key or not hasattr(registry, "get"):
        return None
    config = registry.get(model_key)
    if not isinstance(config, ModelEndpointConfig) or not config.ready:
        return None
    return config


def find_roundtable_model_route(
    *,
    opinion: dict[str, Any],
    model_routes: list[dict[str, Any]],
) -> dict[str, Any]:
    """优先为同标的观点匹配模型路由。"""

    asset_id = opinion.get("asset_id")
    for route in model_routes:
        if route.get("asset_id") == asset_id:
            return route
    return model_routes[0] if model_routes else {}


def build_roundtable_model_context(
    *,
    opinion: dict[str, Any],
    asset_contexts: dict[str, dict[str, Any]],
    portfolio_context: dict[str, Any] | None,
    watchlist_context: dict[str, Any] | None,
    recommendation_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """为单条圆桌观点构造模型只读上下文。"""

    asset_id = str(opinion.get("asset_id") or "")
    role = str(opinion.get("role") or "")
    asset_context = asset_contexts.get(asset_id, {})
    return {
        "asset_context": build_role_scoped_asset_context(
            role=role,
            asset_context=asset_context,
        ),
        "portfolio_context": compact_roundtable_model_value(portfolio_context),
        "watchlist_context": compact_roundtable_model_value(watchlist_context),
        "recommendation_context": compact_roundtable_model_value(recommendation_context),
        "fallback_opinion": compact_roundtable_model_value(opinion),
    }


def build_role_scoped_asset_context(
    *,
    role: str,
    asset_context: dict[str, Any],
) -> dict[str, Any]:
    """按圆桌角色裁剪模型上下文，避免无关事实撑大 prompt。"""

    profile = asset_context.get("profile", {})
    if role == "risk_rebuttal":
        return compact_roundtable_model_value(
            {
                "profile": profile,
                "signal_risk": asset_context.get("signal_risk", {}),
                "memory": asset_context.get("memory", {}),
                "data_quality": asset_context.get("data_quality", {}),
            }
        )
    if role == "technical_analyst":
        factor = asset_context.get("factor", {})
        return compact_roundtable_model_value(
            {
                "profile": profile,
                "indicator_frame": factor.get("indicator_frame"),
                "structure": asset_context.get("structure", {}),
                "signal_risk": {
                    "signal": (asset_context.get("signal_risk") or {}).get("signal"),
                },
            }
        )
    if role == "factor_analyst":
        return compact_roundtable_model_value(
            {
                "profile": profile,
                "factor": asset_context.get("factor", {}),
            }
        )
    if role == "portfolio_manager":
        return compact_roundtable_model_value({"profile": profile})
    if role == "memory_manager":
        return compact_roundtable_model_value(
            {
                "profile": profile,
                "memory": asset_context.get("memory", {}),
                "signal_risk": {
                    "risks": (asset_context.get("signal_risk") or {}).get("risks", ()),
                },
            }
        )
    return compact_roundtable_model_value({"profile": profile})


def compact_roundtable_model_value(value: Any, *, depth: int = 0) -> Any:
    """把模型上下文压缩为 JSON 友好结构，并限制长列表和长文本。"""

    if depth > 6:
        return shorten_roundtable_text(str(value))
    if value is None or isinstance(value, str | int | float | bool):
        return shorten_roundtable_text(value) if isinstance(value, str) else value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {
            str(key): compact_roundtable_model_value(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple | set):
        items = list(value)
        compacted = [
            compact_roundtable_model_value(item, depth=depth + 1)
            for item in items[:8]
        ]
        if len(items) > 8:
            compacted.append({"omitted_count": len(items) - 8})
        return compacted
    if is_dataclass(value) and not isinstance(value, type):
        return compact_roundtable_model_value(asdict(value), depth=depth + 1)
    orm_payload = compact_roundtable_orm_columns(value, depth=depth)
    if orm_payload is not None:
        return orm_payload
    return shorten_roundtable_text(str(value))


def compact_roundtable_orm_columns(value: Any, *, depth: int) -> dict[str, Any] | None:
    """把 SQLAlchemy ORM 实例转换为模型可读的轻量字典。"""

    try:
        inspected = sqlalchemy_inspect(value, raiseerr=False)
    except Exception:  # noqa: BLE001 - 模型上下文压缩不能因未知对象中断
        return None
    mapper = getattr(inspected, "mapper", None)
    if mapper is None:
        return None
    return {
        column.key: compact_roundtable_model_value(
            getattr(value, column.key),
            depth=depth + 1,
        )
        for column in mapper.column_attrs
    }


def shorten_roundtable_text(value: str, *, limit: int = 1200) -> str:
    """限制传给模型的单段文本长度。"""

    if len(value) <= limit:
        return value
    return f"{value[:limit]}...<truncated {len(value) - limit} chars>"


def build_roundtable_allowed_evidence_ids(
    *,
    opinion: dict[str, Any],
    asset_contexts: dict[str, dict[str, Any]],
) -> list[str]:
    """合并规则观点和工具上下文中的 evidence 白名单。"""

    asset_id = str(opinion.get("asset_id") or "")
    evidence_ids = unique_ids(opinion.get("evidence_ids", []))
    context_ids = collect_evidence_ids(asset_contexts.get(asset_id, {}))
    return unique_ids([*evidence_ids, *context_ids])


def mark_roundtable_fallback(
    *,
    opinion: dict[str, Any],
    data_gaps: list[str] | None = None,
) -> dict[str, Any]:
    """把规则版观点补齐为统一 fallback 结构。"""

    request = RoundtableOpinionRequest(
        role=str(opinion.get("role") or ""),
        asset_id=str(opinion.get("asset_id") or ""),
        workflow_type="fallback",
        context={},
        question="",
        fallback_opinion=opinion,
    )
    return build_fallback_opinion(request=request, data_gaps=data_gaps)


def serialize_recommendation_decision(decision: Any) -> dict[str, Any]:
    """把推荐决策对象转换为中文报告模板可消费的结构。"""

    return {
        "asset_id": decision.asset_id,
        "symbol": decision.symbol,
        "name": decision.name,
        "market": decision.market,
        "recommendation_id": decision.recommendation_id,
        "agent_action": decision.agent_action,
        "action": decision.trade_action,
        "decision_type": decision.decision_type,
        "severity": decision.severity,
        "confidence": None,
        "data_quality_status": None,
        "summary": decision.summary,
        "rationale": decision.rationale,
        "risk_rebuttal": decision.risk_rebuttal,
        "reason_ids": list(decision.reason_ids),
        "signal_ids": list(decision.signal_ids),
        "risk_ids": list(decision.risk_ids),
        "evidence_ids": list(decision.evidence_ids),
        "data_snapshot_id": getattr(decision, "data_snapshot_id", None),
        "decision_gate_id": getattr(decision, "decision_gate_id", None),
        "decision_gate_status": getattr(decision, "decision_gate_status", None),
    }


def serialize_operational_decisions(
    *,
    workflow_type: str,
    decisions: Any,
    asset_contexts: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """序列化持仓监控或观察池管理决策。"""

    if workflow_type == "portfolio_monitoring":
        return [
            serialize_portfolio_monitoring_decision(
                decision=decision,
                asset_context=asset_contexts.get(decision.asset_id, {}),
            )
            for decision in decisions
        ]
    if workflow_type == "watchlist_management":
        return [
            serialize_watchlist_management_decision(
                decision=decision,
                asset_context=asset_contexts.get(decision.asset_id, {}),
            )
            for decision in decisions
        ]
    raise ValueError(f"不支持的运营类 Workflow：{workflow_type}")


def serialize_portfolio_monitoring_decision(
    *,
    decision: Any,
    asset_context: dict[str, Any],
) -> dict[str, Any]:
    """把持仓监控决策转换为中文报告模板结构。"""

    return {
        "asset_id": decision.asset_id,
        "symbol": decision.symbol,
        "market": decision.market,
        "action": decision.suggested_action,
        "decision_type": f"portfolio_{decision.decision_type}",
        "severity": decision.severity,
        "confidence": derive_report_confidence(asset_context),
        "data_quality_status": classify_report_context_quality(asset_context),
        "summary": decision.summary,
        "risk_rebuttal": decision.risk_rebuttal,
        "trigger_condition": decision.trigger_condition,
        "thesis": decision.thesis,
        "signal_ids": list(decision.signal_ids),
        "risk_ids": list(decision.risk_ids),
        "evidence_ids": list(decision.evidence_ids),
        "review_questions": list(decision.review_questions),
        "data_snapshot_id": getattr(decision, "data_snapshot_id", None),
        "decision_gate_id": getattr(decision, "decision_gate_id", None),
        "decision_gate_status": getattr(decision, "decision_gate_status", None),
        "intraday_quotes": list(getattr(decision, "intraday_quotes", ()) or ()),
    }


def serialize_watchlist_management_decision(
    *,
    decision: Any,
    asset_context: dict[str, Any],
) -> dict[str, Any]:
    """把观察池管理决策转换为中文报告模板结构。"""

    return {
        "asset_id": decision.asset_id,
        "symbol": decision.symbol,
        "market": decision.market,
        "watchlist_item_id": decision.watchlist_item_id,
        "action": decision.suggested_action,
        "decision_type": decision.decision_type,
        "next_status": decision.next_status,
        "severity": decision.severity,
        "confidence": derive_report_confidence(asset_context),
        "data_quality_status": classify_report_context_quality(asset_context),
        "summary": decision.summary,
        "daily_watch_reason": decision.daily_watch_reason,
        "risk_rebuttal": decision.risk_rebuttal,
        "trigger_condition": decision.trigger_condition,
        "next_review_at": decision.next_review_at.isoformat()
        if decision.next_review_at
        else None,
        "removed_reason": decision.removed_reason,
        "signal_ids": list(decision.signal_ids),
        "risk_ids": list(decision.risk_ids),
        "evidence_ids": list(decision.evidence_ids),
        "thesis_ids": list(decision.thesis_ids),
        "review_questions": list(decision.review_questions),
    }


def build_high_risk_review_items(
    *,
    workflow_type: str,
    decisions: list[dict[str, Any]],
    asset_contexts: dict[str, dict[str, Any]],
    review_policy: HighRiskReviewPolicy,
    model_policy: ModelRoutingPolicy,
) -> list[dict[str, Any]]:
    """构建统一高风险复核摘要。"""

    review_items: list[dict[str, Any]] = []
    for decision in decisions:
        asset_id = str(decision.get("asset_id") or "")
        asset_context = asset_contexts.get(asset_id, {})
        signal = get_nested(asset_context, "signal_risk", "signal") or {}
        risks = get_nested(asset_context, "signal_risk", "risks") or []
        context = ReviewDecisionContext(
            decision_type=str(decision.get("decision_type") or ""),
            suggested_action=str(decision.get("action") or ""),
            severity=str(decision.get("severity") or "unknown"),
            confidence=float(decision.get("confidence") or 0),
            data_quality_status=str(decision.get("data_quality_status") or "missing"),
            risk_severities=tuple(risk.get("severity", "unknown") for risk in risks),
            has_conflicting_signal=has_report_conflicting_signal(
                suggested_action=str(decision.get("action") or ""),
                signal_direction=signal.get("direction"),
            ),
        )
        review_item = {
            "asset_id": asset_id,
            "decision_type": decision.get("decision_type"),
            "trade_action": decision.get("action"),
            "requires_review": review_policy.requires_review(context),
            "reason": context.__dict__,
        }
        review_item["model_review"] = model_policy.build_review_result(
            workflow_type=workflow_type,
            review_item=review_item,
            decision_summary=decision.get("summary"),
        )
        review_items.append(review_item)
    return review_items


def collect_operational_workflow_context(
    state: WorkflowGraphState,
    *,
    workflow_type: str,
) -> dict[str, Any]:
    """为持仓监控和观察池管理收集已入库金融事实上下文。"""

    workflow_input = state["workflow_input"]
    tool_runtime = build_tool_runtime(state)
    owner_id = workflow_input.owner_id
    tool_calls: list[dict[str, Any]] = []

    portfolio_context = None
    watchlist_context = None
    asset_ids: list[str] = []
    if workflow_type == "portfolio_monitoring":
        portfolio_id = workflow_input.portfolio.portfolio_id
        portfolio_context = tool_runtime.call("portfolio.get_snapshot", portfolio_id=portfolio_id)
        tool_calls.append({"tool": "portfolio.get_snapshot", "portfolio_id": portfolio_id})
        asset_ids = unique_ids(position.asset_id for position in workflow_input.positions)
    elif workflow_type == "watchlist_management":
        watchlist_id = workflow_input.watchlist.watchlist_id
        watchlist_context = tool_runtime.call(
            "watchlist.get_active_items",
            owner_id=owner_id,
            watchlist_id=watchlist_id,
        )
        tool_calls.append(
            {
                "tool": "watchlist.get_active_items",
                "owner_id": owner_id,
                "watchlist_id": watchlist_id,
            }
        )
        asset_ids = unique_ids(item.asset_id for item in workflow_input.items)
    else:
        raise ValueError(f"不支持的运营类 Workflow：{workflow_type}")

    asset_contexts = collect_asset_contexts(
        tool_runtime=tool_runtime,
        owner_id=owner_id,
        asset_ids=asset_ids,
        horizon=state.get("horizon", "swing"),
        timeframe=state.get("timeframe", "1d"),
        evidence_limit=state.get("evidence_limit", 5),
        risk_limit=state.get("risk_limit", 5),
        quality_limit=state.get("quality_limit", 5),
        memory_limit=state.get("memory_limit", 5),
        tool_calls=tool_calls,
    )

    return {
        "asset_ids": asset_ids,
        "asset_contexts": asset_contexts,
        "portfolio_context": portfolio_context,
        "watchlist_context": watchlist_context,
        "tool_calls": tool_calls,
    }


def collect_asset_contexts(
    *,
    tool_runtime: FinanceToolRuntime,
    owner_id: str,
    asset_ids: list[str],
    horizon: str,
    timeframe: str,
    evidence_limit: int,
    risk_limit: int,
    quality_limit: int,
    memory_limit: int,
    tool_calls: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """通过工具运行时收集单标的事实包，并记录工具调用。"""

    asset_contexts: dict[str, dict[str, Any]] = {}
    for asset_id in asset_ids:
        factor_context = tool_runtime.call(
            "factor.get_asset_factor_context",
            asset_id=asset_id,
            horizon=horizon,
            timeframe=timeframe,
            evidence_limit=evidence_limit,
        )
        tool_calls.append(
            {
                "tool": "factor.get_asset_factor_context",
                "asset_id": asset_id,
                "horizon": horizon,
            }
        )
        structure_context = tool_runtime.call(
            "structure.get_asset_context",
            asset_id=asset_id,
            timeframe=timeframe,
        )
        tool_calls.append(
            {
                "tool": "structure.get_asset_context",
                "asset_id": asset_id,
                "timeframe": timeframe,
            }
        )
        signal_risk_context = tool_runtime.call(
            "signal_risk.get_asset_context",
            asset_id=asset_id,
            horizon=horizon,
            risk_limit=risk_limit,
            quality_limit=quality_limit,
        )
        tool_calls.append(
            {
                "tool": "signal_risk.get_asset_context",
                "asset_id": asset_id,
                "horizon": horizon,
            }
        )
        memory_context = tool_runtime.call(
            "memory.recall_asset_memories",
            owner_id=owner_id,
            asset_id=asset_id,
            limit=memory_limit,
        )
        tool_calls.append(
            {
                "tool": "memory.recall_asset_memories",
                "asset_id": asset_id,
                "owner_id": owner_id,
            }
        )
        asset_contexts[asset_id] = {
            "profile": build_asset_profile(asset_id=asset_id, factor_context=factor_context),
            "factor": factor_context,
            "structure": structure_context,
            "signal_risk": signal_risk_context,
            "memory": memory_context,
        }
    return asset_contexts


def collect_roundtable_report_context(state: WorkflowGraphState) -> dict[str, Any]:
    """为报告类 Workflow 收集已入库金融事实上下文。"""

    tool_runtime = build_tool_runtime(state)
    owner_id = state["owner_id"]
    tool_calls: list[dict[str, Any]] = []

    portfolio_context = None
    portfolio_id = state.get("portfolio_id")
    if portfolio_id:
        portfolio_context = tool_runtime.call("portfolio.get_snapshot", portfolio_id=portfolio_id)
        tool_calls.append({"tool": "portfolio.get_snapshot", "portfolio_id": portfolio_id})

    watchlist_context = None
    watchlist_id = state.get("watchlist_id")
    if watchlist_id:
        watchlist_context = tool_runtime.call(
            "watchlist.get_active_items",
            owner_id=owner_id,
            watchlist_id=watchlist_id,
        )
        tool_calls.append(
            {
                "tool": "watchlist.get_active_items",
                "owner_id": owner_id,
                "watchlist_id": watchlist_id,
            }
        )

    recommendation_context = None
    recommendation_run_id = state.get("recommendation_run_id")
    if recommendation_run_id:
        recommendation_context = tool_runtime.call(
            "recommendation.get_run",
            run_id=recommendation_run_id,
            limit=state.get("recommendation_limit", 20),
        )
        tool_calls.append(
            {"tool": "recommendation.get_run", "run_id": recommendation_run_id}
        )

    asset_ids = resolve_report_asset_ids(
        state,
        portfolio_context=portfolio_context,
        watchlist_context=watchlist_context,
        recommendation_context=recommendation_context,
    )
    asset_contexts: dict[str, dict[str, Any]] = {}
    for asset_id in asset_ids:
        factor_context = tool_runtime.call(
            "factor.get_asset_factor_context",
            asset_id=asset_id,
            horizon=state.get("horizon", "swing"),
            timeframe=state.get("timeframe", "1d"),
            evidence_limit=state.get("evidence_limit", 5),
        )
        tool_calls.append(
            {
                "tool": "factor.get_asset_factor_context",
                "asset_id": asset_id,
                "horizon": state.get("horizon", "swing"),
            }
        )
        structure_context = tool_runtime.call(
            "structure.get_asset_context",
            asset_id=asset_id,
            timeframe=state.get("timeframe", "1d"),
        )
        tool_calls.append(
            {
                "tool": "structure.get_asset_context",
                "asset_id": asset_id,
                "timeframe": state.get("timeframe", "1d"),
            }
        )
        signal_risk_context = tool_runtime.call(
            "signal_risk.get_asset_context",
            asset_id=asset_id,
            horizon=state.get("horizon", "swing"),
            risk_limit=state.get("risk_limit", 5),
            quality_limit=state.get("quality_limit", 5),
        )
        tool_calls.append(
            {
                "tool": "signal_risk.get_asset_context",
                "asset_id": asset_id,
                "horizon": state.get("horizon", "swing"),
            }
        )
        memory_context = tool_runtime.call(
            "memory.recall_asset_memories",
            owner_id=owner_id,
            asset_id=asset_id,
            limit=state.get("memory_limit", 5),
        )
        tool_calls.append(
            {
                "tool": "memory.recall_asset_memories",
                "asset_id": asset_id,
                "owner_id": owner_id,
            }
        )
        asset_contexts[asset_id] = {
            "profile": build_asset_profile(asset_id=asset_id, factor_context=factor_context),
            "factor": factor_context,
            "structure": structure_context,
            "signal_risk": signal_risk_context,
            "memory": memory_context,
        }

    return {
        "asset_ids": asset_ids,
        "asset_contexts": asset_contexts,
        "portfolio_context": portfolio_context,
        "watchlist_context": watchlist_context,
        "recommendation_context": recommendation_context,
        "tool_calls": tool_calls,
    }


def resolve_report_asset_ids(
    state: WorkflowGraphState,
    *,
    portfolio_context: dict[str, Any] | None = None,
    watchlist_context: dict[str, Any] | None = None,
    recommendation_context: dict[str, Any] | None = None,
) -> list[str]:
    """从 Workflow 输入中解析需要讨论的标的。"""

    asset_ids: list[str] = []
    for key in ("asset_id", "source_asset_id", "candidate_asset_id"):
        value = state.get(key)
        if value and value not in asset_ids:
            asset_ids.append(value)
    for value in state.get("asset_ids", []):
        if value and value not in asset_ids:
            asset_ids.append(value)
    if not asset_ids and recommendation_context:
        for item in recommendation_context.get("recommendations", []):
            value = item.get("asset_id")
            if value and value not in asset_ids:
                asset_ids.append(value)
    if not asset_ids and watchlist_context:
        for item in watchlist_context.get("items", []):
            value = item.get("asset_id")
            if value and value not in asset_ids:
                asset_ids.append(value)
    if not asset_ids and portfolio_context:
        for item in portfolio_context.get("positions", []):
            value = item.get("asset_id")
            if value and value not in asset_ids:
                asset_ids.append(value)
    if not asset_ids:
        raise ValueError("报告类圆桌 Workflow 需要 asset_id 或 asset_ids。")
    return asset_ids


def unique_ids(values: Any) -> list[str]:
    """按出现顺序去重 ID。"""

    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def build_asset_profile(*, asset_id: str, factor_context: dict[str, Any]) -> dict[str, Any]:
    """从因子工具上下文提取标的基础展示信息。"""

    for key in ("score", "factor_frame", "indicator_frame"):
        item = factor_context.get(key) or {}
        if item:
            return {
                "asset_id": asset_id,
                "symbol": item.get("symbol") or asset_id,
                "market": item.get("market"),
            }
    return {"asset_id": asset_id, "symbol": asset_id, "market": None}


def build_report_roundtable_opinions(
    *,
    workflow_type: str,
    asset_contexts: dict[str, dict[str, Any]],
    portfolio_context: dict[str, Any] | None,
    watchlist_context: dict[str, Any] | None,
    recommendation_context: dict[str, Any] | None,
    source_asset_id: str | None,
    candidate_asset_id: str | None,
) -> list[dict[str, Any]]:
    """构建报告类 Workflow 的受控圆桌观点。"""

    opinions: list[dict[str, Any]] = []
    for asset_id, context in asset_contexts.items():
        profile = context.get("profile", {})
        factor_context = context.get("factor", {})
        signal_risk_context = context.get("signal_risk", {})
        memory_context = context.get("memory", {})
        indicator = factor_context.get("indicator_frame") or {}
        factor = factor_context.get("factor_frame") or {}
        score = factor_context.get("score") or {}
        evidence = factor_context.get("evidence") or []
        risks = signal_risk_context.get("risks") or []
        memories = memory_context.get("memories") or []
        signal = signal_risk_context.get("signal")
        symbol = profile.get("symbol") or asset_id
        factor_tool_call = {
            "tool": "factor.get_asset_factor_context",
            "asset_id": asset_id,
        }
        structure_tool_call = {
            "tool": "structure.get_asset_context",
            "asset_id": asset_id,
        }
        risk_tool_call = {
            "tool": "signal_risk.get_asset_context",
            "asset_id": asset_id,
        }
        memory_tool_call = {
            "tool": "memory.recall_asset_memories",
            "asset_id": asset_id,
        }

        opinions.extend(
            [
                {
                    "role": "technical_analyst",
                    "asset_id": asset_id,
                    "stance": "support" if indicator else "insufficient",
                    "summary": build_technical_summary_from_dict(
                        symbol=symbol,
                        indicator=indicator,
                        signal=signal,
                    ),
                    "tool_calls": [factor_tool_call, structure_tool_call, risk_tool_call],
                    "evidence_ids": [item["evidence_id"] for item in evidence],
                    "source_ids": compact_ids(indicator.get("indicator_frame_id")),
                },
                {
                    "role": "factor_analyst",
                    "asset_id": asset_id,
                    "stance": "support" if score and factor else "insufficient",
                    "summary": build_factor_summary(score=score, factor=factor),
                    "tool_calls": [factor_tool_call],
                    "evidence_ids": [item["evidence_id"] for item in evidence],
                    "source_ids": compact_ids(
                        factor.get("factor_frame_id"),
                        score.get("score_id"),
                        factor.get("indicator_frame_id"),
                    ),
                },
                {
                    "role": "risk_rebuttal",
                    "asset_id": asset_id,
                    "stance": "oppose" if has_high_dict_risk(risks) else "caution",
                    "summary": build_risk_summary_from_dicts(risks=risks),
                    "tool_calls": [risk_tool_call],
                    "evidence_ids": sorted(
                        {
                            evidence_id
                            for risk in risks
                            for evidence_id in risk.get("evidence_ids", [])
                        }
                    ),
                    "source_ids": [risk["risk_id"] for risk in risks],
                },
                {
                    "role": "memory_manager",
                    "asset_id": asset_id,
                    "stance": "recall" if memories else "no_memory",
                    "summary": build_memory_summary_from_dicts(memories=memories),
                    "tool_calls": [memory_tool_call],
                    "evidence_ids": [],
                    "source_ids": [memory["memory_id"] for memory in memories],
                },
            ]
        )

    opinions.append(
        {
            "role": "portfolio_manager",
            "asset_id": candidate_asset_id or source_asset_id or "",
            "stance": "compare" if workflow_type == "swap_decision" else "coordinate",
            "summary": build_portfolio_roundtable_summary(
                workflow_type=workflow_type,
                portfolio_context=portfolio_context,
                watchlist_context=watchlist_context,
                recommendation_context=recommendation_context,
                source_asset_id=source_asset_id,
                candidate_asset_id=candidate_asset_id,
            ),
            "tool_calls": build_portfolio_roundtable_tool_calls(
                portfolio_context=portfolio_context,
                watchlist_context=watchlist_context,
                recommendation_context=recommendation_context,
            ),
            "evidence_ids": [],
            "source_ids": build_portfolio_roundtable_source_ids(
                portfolio_context=portfolio_context,
                watchlist_context=watchlist_context,
                recommendation_context=recommendation_context,
            ),
        }
    )
    return opinions


def build_report_workflow_decisions(
    *,
    workflow_type: str,
    asset_ids: list[str],
    asset_contexts: dict[str, dict[str, Any]],
    source_asset_id: str | None,
    candidate_asset_id: str | None,
) -> list[dict[str, Any]]:
    """合成报告类 Workflow 的主席裁决摘要。"""

    if workflow_type == "swap_decision":
        candidate_id = candidate_asset_id or (asset_ids[-1] if asset_ids else "")
        source_id = source_asset_id or (asset_ids[0] if asset_ids else "")
        candidate_context = asset_contexts.get(candidate_id, {})
        source_context = asset_contexts.get(source_id, {})
        candidate_score = score_to_float(candidate_context)
        source_signal = signal_direction(source_context)
        action = "swap" if candidate_score >= 80 and source_signal == "bearish" else "watch"
        severity = "high" if action == "swap" else "medium"
        return [
            build_report_decision(
                workflow_type=workflow_type,
                asset_id=candidate_id,
                action=action,
                severity=severity,
                context=candidate_context,
                summary=(
                    f"候选标的评分 {candidate_score:.1f}，弱持仓信号 {source_signal}，"
                    f"主席裁决为 {action}。"
                ),
            )
        ]

    decisions: list[dict[str, Any]] = []
    for asset_id in asset_ids:
        context = asset_contexts.get(asset_id, {})
        score = score_to_float(context)
        direction = signal_direction(context)
        has_high_risk = has_high_dict_risk(get_nested(context, "signal_risk", "risks") or [])
        if workflow_type == "daily_review":
            action = "review_next_day"
            severity = "medium" if has_high_risk else "low"
        elif score >= 80 and direction == "bullish" and not has_high_risk:
            action = "deep_analysis_support"
            severity = "medium"
        elif has_high_risk:
            action = "risk_watch"
            severity = "high"
        else:
            action = "keep_watching"
            severity = "low"
        decisions.append(
            build_report_decision(
                workflow_type=workflow_type,
                asset_id=asset_id,
                action=action,
                severity=severity,
                context=context,
                summary=(
                    f"评分 {score:.1f}，信号 {direction}，高风险={has_high_risk}，"
                    f"主席裁决为 {action}。"
                ),
            )
        )
    return decisions


def build_report_decision(
    *,
    workflow_type: str,
    asset_id: str,
    action: str,
    severity: str,
    context: dict[str, Any],
    summary: str,
) -> dict[str, Any]:
    """构建报告类 Workflow 的结构化裁决。"""

    return {
        "asset_id": asset_id,
        "decision_type": f"{workflow_type}_{action}",
        "action": action,
        "severity": severity,
        "confidence": derive_report_confidence(context),
        "data_quality_status": classify_report_context_quality(context),
        "summary": summary,
    }


def classify_report_context_quality(context: dict[str, Any]) -> str:
    """判断报告类 Workflow 的数据质量。"""

    factor_context = context.get("factor") or {}
    signal_risk_context = context.get("signal_risk") or {}
    quality_items = signal_risk_context.get("data_quality") or []
    if not factor_context:
        return "missing"
    if factor_context.get("factor_frame") is None or factor_context.get("indicator_frame") is None:
        return "partial"
    if any(item.get("freshness_status") == "stale" for item in quality_items):
        return "stale"
    return "available"


def derive_report_confidence(context: dict[str, Any]) -> float:
    """从评分或信号上下文推导复核置信度。"""

    score = get_nested(context, "factor", "score") or {}
    if score.get("confidence") is not None:
        return float(score["confidence"])
    signal = get_nested(context, "signal_risk", "signal") or {}
    if signal.get("confidence") is not None:
        return float(signal["confidence"])
    return 0.5


def score_to_float(context: dict[str, Any]) -> float:
    """读取总评分。"""

    score = get_nested(context, "factor", "score") or {}
    return float(score.get("total_score") or 0)


def signal_direction(context: dict[str, Any]) -> str:
    """读取信号方向。"""

    signal = get_nested(context, "signal_risk", "signal") or {}
    return signal.get("direction") or "unknown"


def has_report_conflicting_signal(
    *,
    suggested_action: str,
    signal_direction: str | None,
) -> bool:
    """判断报告类 Workflow 裁决是否和信号冲突。"""

    if signal_direction is None:
        return True
    if suggested_action in {"swap", "deep_analysis_support"} and signal_direction == "bearish":
        return True
    return suggested_action == "risk_watch" and signal_direction == "bullish"


def build_technical_summary_from_dict(
    *,
    symbol: str,
    indicator: dict[str, Any],
    signal: dict[str, Any] | None,
) -> str:
    """基于序列化工具结果生成技术面摘要。"""

    if not indicator:
        return f"{symbol} 缺少 TA 指标快照，技术面只能保持中性。"
    direction = signal.get("direction") if signal else "unknown"
    return (
        f"{symbol} TA 指标显示 RSI14={indicator.get('rsi_14')}、"
        f"MACD={indicator.get('macd')}、布林位置={indicator.get('bb_percent_b')}；"
        f"最新信号方向为 {direction}。"
    )


def build_risk_summary_from_dicts(*, risks: list[dict[str, Any]]) -> str:
    """基于序列化风险结果生成风险反驳摘要。"""

    if not risks:
        return "未发现高优先级风险，但仍需监控事件、资金流和数据质量。"
    return "；".join(f"{risk['severity']}:{risk['title']}" for risk in risks[:3])


def build_memory_summary_from_dicts(*, memories: list[dict[str, Any]]) -> str:
    """基于序列化记忆结果生成记忆摘要。"""

    if not memories:
        return "暂无该标的历史金融记忆。"
    return "；".join(memory["content"] for memory in memories[:2])


def build_portfolio_roundtable_summary(
    *,
    workflow_type: str,
    portfolio_context: dict[str, Any] | None,
    watchlist_context: dict[str, Any] | None,
    recommendation_context: dict[str, Any] | None,
    source_asset_id: str | None,
    candidate_asset_id: str | None,
) -> str:
    """生成组合经理圆桌摘要。"""

    position_count = len((portfolio_context or {}).get("positions", []))
    watch_count = len((watchlist_context or {}).get("items", []))
    recommendation_count = len((recommendation_context or {}).get("recommendations", []))
    if workflow_type == "swap_decision":
        return (
            f"换股/换币比较需要在弱持仓 {source_asset_id} 与强候选 "
            f"{candidate_asset_id} 之间权衡；当前持仓 {position_count} 个，"
            f"观察项 {watch_count} 个，推荐结果 {recommendation_count} 条。"
        )
    return (
        f"当前持仓 {position_count} 个，观察项 {watch_count} 个，"
        f"推荐结果 {recommendation_count} 条；需要把单标的结论放回组合风险预算。"
    )


def build_portfolio_roundtable_tool_calls(
    *,
    portfolio_context: dict[str, Any] | None,
    watchlist_context: dict[str, Any] | None,
    recommendation_context: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """记录组合经理实际消费过的工具类型。"""

    calls: list[dict[str, Any]] = []
    if portfolio_context is not None:
        calls.append({"tool": "portfolio.get_snapshot"})
    if watchlist_context is not None:
        calls.append({"tool": "watchlist.get_active_items"})
    if recommendation_context is not None:
        calls.append({"tool": "recommendation.get_run"})
    return calls


def build_portfolio_roundtable_source_ids(
    *,
    portfolio_context: dict[str, Any] | None,
    watchlist_context: dict[str, Any] | None,
    recommendation_context: dict[str, Any] | None,
) -> list[str]:
    """提取组合经理观点引用的来源 ID。"""

    source_ids: list[str] = []
    if portfolio_context:
        portfolio = portfolio_context.get("portfolio") or {}
        source_ids.extend(compact_ids(portfolio.get("portfolio_id")))
        source_ids.extend(
            position["position_id"] for position in portfolio_context.get("positions", [])
        )
    if watchlist_context:
        source_ids.extend(
            item["watchlist_item_id"] for item in watchlist_context.get("items", [])
        )
    if recommendation_context:
        run = recommendation_context.get("run") or {}
        source_ids.extend(compact_ids(run.get("run_id")))
        source_ids.extend(
            item["recommendation_id"]
            for item in recommendation_context.get("recommendations", [])
        )
    return source_ids


def build_report_title(
    *,
    title: str,
    workflow_type: str,
    asset_symbols: list[str],
) -> str:
    """生成报告标题。"""

    if workflow_type == "daily_review":
        return title
    if asset_symbols:
        return f"{' / '.join(asset_symbols)} {title}"
    return title


def has_high_dict_risk(risks: list[dict[str, Any]]) -> bool:
    """判断序列化风险列表中是否存在高风险。"""

    return any(risk.get("severity") in {"high", "critical"} for risk in risks)


def get_nested(mapping: dict[str, Any], *keys: str) -> Any:
    """安全读取嵌套字典。"""

    current: Any = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def classify_factor_context_quality(context: dict[str, Any]) -> str:
    """根据工具上下文粗略判断数据质量。"""

    if not context:
        return "missing"
    if context.get("factor_frame") is None or context.get("indicator_frame") is None:
        return "partial"
    factor = context.get("factor_frame") or {}
    if factor.get("status") != "available":
        return "stale"
    return "available"


def build_roundtable_opinions(
    *,
    recommendation: Any,
    factor_context: dict[str, Any],
    signal: Any,
    risks: tuple[Any, ...],
    memories: tuple[Any, ...],
    positions: tuple[Any, ...],
) -> list[dict[str, Any]]:
    """构建受控圆桌观点。

    这里先用确定性摘要模拟各角色观点；后续接入 LLM 时仍复用同一结构，
    并保留 `tool_calls`、证据 ID 和观点摘要。
    """

    indicator = factor_context.get("indicator_frame") or {}
    factor = factor_context.get("factor_frame") or {}
    score = factor_context.get("score") or {}
    evidence = factor_context.get("evidence") or []
    position_symbols = [position.symbol for position in positions]
    high_risks = [risk for risk in risks if risk.severity in {"high", "critical"}]
    tool_call = {
        "tool": "factor.get_asset_factor_context",
        "asset_id": recommendation.asset_id,
        "horizon": recommendation.horizon,
    }
    structure_tool_call = {
        "tool": "structure.get_asset_context",
        "asset_id": recommendation.asset_id,
    }

    return [
        {
            "role": "technical_analyst",
            "asset_id": recommendation.asset_id,
            "stance": "support" if indicator else "insufficient",
            "summary": build_technical_summary(indicator=indicator, signal=signal),
            "tool_calls": [tool_call, structure_tool_call],
            "evidence_ids": [item["evidence_id"] for item in evidence],
            "source_ids": [indicator.get("indicator_frame_id")] if indicator else [],
        },
        {
            "role": "factor_analyst",
            "asset_id": recommendation.asset_id,
            "stance": "support" if score and factor else "insufficient",
            "summary": build_factor_summary(score=score, factor=factor),
            "tool_calls": [tool_call],
            "evidence_ids": [item["evidence_id"] for item in evidence],
            "source_ids": compact_ids(
                factor.get("factor_frame_id"),
                score.get("score_id"),
                factor.get("indicator_frame_id"),
            ),
        },
        {
            "role": "risk_rebuttal",
            "asset_id": recommendation.asset_id,
            "stance": "oppose" if high_risks else "caution",
            "summary": build_risk_summary(risks=risks),
            "tool_calls": [tool_call],
            "evidence_ids": sorted(
                {evidence_id for risk in risks for evidence_id in (risk.evidence_ids or [])}
            ),
            "source_ids": [risk.risk_id for risk in risks],
        },
        {
            "role": "portfolio_manager",
            "asset_id": recommendation.asset_id,
            "stance": "compare",
            "summary": (
                f"当前组合持仓包含 {', '.join(position_symbols) or '无活跃持仓'}，"
                f"推荐标的 {recommendation.symbol} 需要结合弱持仓和仓位上限比较。"
            ),
            "tool_calls": [{"tool": "portfolio.get_snapshot"}],
            "evidence_ids": [],
            "source_ids": [position.position_id for position in positions],
        },
        {
            "role": "memory_manager",
            "asset_id": recommendation.asset_id,
            "stance": "recall" if memories else "no_memory",
            "summary": build_memory_summary(memories=memories),
            "tool_calls": [
                {
                    "tool": "memory.recall_asset_memories",
                    "asset_id": recommendation.asset_id,
                }
            ],
            "evidence_ids": [],
            "source_ids": [memory.memory_id for memory in memories],
        },
    ]


def build_technical_summary(*, indicator: dict[str, Any], signal: Any) -> str:
    """生成技术分析角色摘要。"""

    if not indicator:
        return "缺少 TA 指标快照，技术面只能保持中性。"
    direction = signal.direction if signal else "unknown"
    return (
        f"TA 指标显示 RSI14={indicator.get('rsi_14')}、MACD={indicator.get('macd')}、"
        f"布林位置={indicator.get('bb_percent_b')}；最新信号方向为 {direction}。"
    )


def build_factor_summary(*, score: dict[str, Any], factor: dict[str, Any]) -> str:
    """生成因子分析角色摘要。"""

    if not factor:
        return "缺少因子快照，不能解释 AKShare、TA 或衍生品数据对推荐的贡献。"
    return (
        f"综合评分={score.get('total_score')}，技术={score.get('technical_score')}，"
        f"基本面={score.get('fundamental_score')}，资金流={score.get('flow_score')}，"
        f"缺失因子组={factor.get('missing_groups', [])}。"
    )


def build_risk_summary(*, risks: tuple[Any, ...]) -> str:
    """生成风险反驳角色摘要。"""

    if not risks:
        return "未发现高优先级风险，但仍需监控事件、资金流和数据质量。"
    return "；".join(f"{risk.severity}:{risk.title}" for risk in risks[:3])


def build_memory_summary(*, memories: tuple[Any, ...]) -> str:
    """生成记忆管理员摘要。"""

    if not memories:
        return "暂无该标的历史金融记忆。"
    return "；".join(memory.content for memory in memories[:2])


def compact_ids(*values: Any) -> list[str]:
    """压缩非空 ID。"""

    return [value for value in values if value]


def derive_decision_confidence(
    *,
    decision: Any,
    workflow_input: RecommendationDecisionInput,
) -> float:
    """从推荐或信号中提取用于复核策略的置信度。"""

    recommendation = next(
        item for item in workflow_input.recommendations if item.asset_id == decision.asset_id
    )
    return float(recommendation.confidence)


def has_conflicting_signal(*, decision: Any, workflow_input: RecommendationDecisionInput) -> bool:
    """判断决策动作和信号方向是否冲突。"""

    signal = workflow_input.signals_by_asset.get(decision.asset_id)
    if signal is None:
        return True
    if decision.trade_action in {"buy", "swap", "watch"} and signal.direction == "bearish":
        return True
    if decision.trade_action in {"sell", "reduce"} and signal.direction == "bullish":
        return True
    return False


def list_langgraph_workflow_builders() -> tuple[LangGraphWorkflowBuilder, ...]:
    """列出已具备 LangGraph 包装入口的 Workflow。"""

    return (
        LangGraphWorkflowBuilder(
            workflow_type="portfolio_monitoring",
            description="持仓监控 LangGraph 包装入口。",
            build=build_portfolio_monitoring_graph,
        ),
        LangGraphWorkflowBuilder(
            workflow_type="watchlist_management",
            description="观察池管理 LangGraph 包装入口。",
            build=build_watchlist_management_graph,
        ),
        LangGraphWorkflowBuilder(
            workflow_type="recommendation_decision",
            description="推荐入池、买卖和换股决策 LangGraph 包装入口。",
            build=build_recommendation_decision_graph,
        ),
        LangGraphWorkflowBuilder(
            workflow_type="asset_deep_analysis",
            description="单标的深度分析 LangGraph 骨架。",
            build=build_asset_deep_analysis_graph,
        ),
        LangGraphWorkflowBuilder(
            workflow_type="swap_decision",
            description="弱持仓与强候选之间的换股或换币比较 LangGraph 骨架。",
            build=build_swap_decision_graph,
        ),
        LangGraphWorkflowBuilder(
            workflow_type="daily_review",
            description="每日持仓、观察池、推荐和风险复盘 LangGraph 骨架。",
            build=build_daily_review_graph,
        ),
    )
