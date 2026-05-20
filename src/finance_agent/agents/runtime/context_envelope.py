"""工作流共享上下文 Envelope 与角色视图。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

JsonDict = dict[str, Any]
CONTEXT_ENVELOPE_VERSION = "1.0"
WORKFLOW_ROLE_NAMES = (
    "technical_analyst",
    "factor_analyst",
    "risk_rebuttal",
    "portfolio_manager",
    "memory_manager",
)


@dataclass(frozen=True)
class RoleView:
    """单个 workflow 角色可见的上下文视图。"""

    role: str
    visible_sections: tuple[str, ...]
    prompt: str
    asset_ids: tuple[str, ...] = ()

    def to_dict(self) -> JsonDict:
        return {
            "role": self.role,
            "visible_sections": list(self.visible_sections),
            "prompt": self.prompt,
            "asset_ids": list(self.asset_ids),
        }


@dataclass(frozen=True)
class ContextEnvelope:
    """Workflow 共享上下文。"""

    version: str
    workflow_type: str
    market_type: str
    stable: JsonDict
    context: JsonDict
    volatile: JsonDict
    role_views: dict[str, RoleView] = field(default_factory=dict)
    audit: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return {
            "version": self.version,
            "workflow_type": self.workflow_type,
            "market_type": self.market_type,
            "stable": json_clone(self.stable),
            "context": json_clone(self.context),
            "volatile": json_clone(self.volatile),
            "role_views": {role: view.to_dict() for role, view in self.role_views.items()},
            "audit": json_clone(self.audit),
        }


def build_workflow_context_envelope(
    *,
    workflow_type: str,
    market_type: str,
    asset_ids: list[str],
    asset_contexts: dict[str, dict[str, Any]],
    portfolio_context: dict[str, Any] | None = None,
    watchlist_context: dict[str, Any] | None = None,
    recommendation_context: dict[str, Any] | None = None,
    trigger_event: dict[str, Any] | None = None,
    available_tools: list[str] | None = None,
    memory_summary: dict[str, Any] | None = None,
    risk_summary: dict[str, Any] | None = None,
) -> ContextEnvelope:
    """构建默认 workflow 共享上下文。"""

    resolved_memory_summary = memory_summary or summarize_memory_contexts(asset_contexts)
    resolved_risk_summary = risk_summary or summarize_risk_contexts(asset_contexts)
    stable = build_stable_context()
    context = build_context_section(
        workflow_type=workflow_type,
        market_type=market_type,
        asset_ids=asset_ids,
        portfolio_context=portfolio_context,
        watchlist_context=watchlist_context,
        recommendation_context=recommendation_context,
        trigger_event=trigger_event,
        available_tools=available_tools or [],
    )
    volatile = build_volatile_context(
        asset_ids=asset_ids,
        asset_contexts=asset_contexts,
        memory_summary=resolved_memory_summary,
        risk_summary=resolved_risk_summary,
    )
    role_views = build_role_views(
        workflow_type=workflow_type,
        market_type=market_type,
        asset_ids=asset_ids,
        asset_contexts=asset_contexts,
        portfolio_context=portfolio_context,
        watchlist_context=watchlist_context,
        recommendation_context=recommendation_context,
        memory_summary=resolved_memory_summary,
        risk_summary=resolved_risk_summary,
    )
    audit = {
        "role_view_count": len(role_views),
        "asset_count": len(asset_ids),
        "has_memory_summary": bool(resolved_memory_summary),
        "has_risk_summary": bool(resolved_risk_summary),
    }
    return ContextEnvelope(
        version=CONTEXT_ENVELOPE_VERSION,
        workflow_type=workflow_type,
        market_type=market_type,
        stable=stable,
        context=context,
        volatile=volatile,
        role_views=role_views,
        audit=audit,
    )


def build_stable_context() -> JsonDict:
    """构建稳定层。"""

    return {
        "identity": "finance-agent workflow assistant",
        "boundaries": [
            "只使用已入库事实",
            "不直接访问外部数据源",
            "不直接执行真实交易",
            "A 股与数字货币分链路",
        ],
        "tool_discipline": [
            "先调用工具，再输出判断",
            "不编造行情、因子、风险或记忆",
        ],
        "act": "先行动后解释，必要时先拉起事实工具和工作流，再给结论。",
    }


def build_context_section(
    *,
    workflow_type: str,
    market_type: str,
    asset_ids: list[str],
    portfolio_context: dict[str, Any] | None,
    watchlist_context: dict[str, Any] | None,
    recommendation_context: dict[str, Any] | None,
    trigger_event: dict[str, Any] | None,
    available_tools: list[str],
) -> JsonDict:
    """构建任务上下文层。"""

    return {
        "workflow_type": workflow_type,
        "market_type": market_type,
        "asset_ids": list(asset_ids),
        "portfolio_context": json_clone(portfolio_context),
        "watchlist_context": json_clone(watchlist_context),
        "recommendation_context": json_clone(recommendation_context),
        "trigger_event": json_clone(trigger_event),
        "available_tools": list(available_tools),
    }


def build_volatile_context(
    *,
    asset_ids: list[str],
    asset_contexts: dict[str, dict[str, Any]],
    memory_summary: dict[str, Any] | None,
    risk_summary: dict[str, Any] | None,
) -> JsonDict:
    """构建波动层。"""

    compact_assets: dict[str, Any] = {}
    for asset_id in asset_ids:
        compact_assets[asset_id] = build_asset_volatile_view(asset_contexts.get(asset_id, {}))
    return {
        "assets": compact_assets,
        "memory_summary": json_clone(memory_summary),
        "risk_summary": json_clone(risk_summary),
    }


def build_role_views(
    *,
    workflow_type: str,
    market_type: str,
    asset_ids: list[str],
    asset_contexts: dict[str, dict[str, Any]],
    portfolio_context: dict[str, Any] | None,
    watchlist_context: dict[str, Any] | None,
    recommendation_context: dict[str, Any] | None,
    memory_summary: dict[str, Any] | None,
    risk_summary: dict[str, Any] | None,
) -> dict[str, RoleView]:
    """构建角色视图。"""

    views = {
        "technical_analyst": RoleView(
            role="technical_analyst",
            visible_sections=("indicator_frame", "signal_risk", "asset_profile"),
            prompt="只分析行情、指标、趋势、量价和信号方向。",
            asset_ids=tuple(asset_ids),
        ),
        "factor_analyst": RoleView(
            role="factor_analyst",
            visible_sections=("factor_frame", "score", "evidence", "asset_profile"),
            prompt="只分析因子、评分、估值、财务和证据引用。",
            asset_ids=tuple(asset_ids),
        ),
        "risk_rebuttal": RoleView(
            role="risk_rebuttal",
            visible_sections=("risk_items", "signal", "memory_items", "risk_summary", "data_quality"),
            prompt="只寻找风险、冲突、缺口和历史失败依据。",
            asset_ids=tuple(asset_ids),
        ),
        "portfolio_manager": RoleView(
            role="portfolio_manager",
            visible_sections=("portfolio_context", "watchlist_context", "recommendation_context"),
            prompt="只考虑持仓、观察池、推荐排序和仓位约束。",
            asset_ids=tuple(asset_ids),
        ),
        "memory_manager": RoleView(
            role="memory_manager",
            visible_sections=("memory_summary", "memory_items", "review_history"),
            prompt="只负责记忆回收、压缩、冲突检查和写回建议。",
            asset_ids=tuple(asset_ids),
        ),
    }
    if workflow_type == "swap_decision" and market_type == "crypto":
        views["portfolio_manager"] = RoleView(
            role="portfolio_manager",
            visible_sections=("portfolio_context", "watchlist_context", "recommendation_context", "source_asset", "candidate_asset"),
            prompt="聚焦换币比较、仓位约束和弱持仓替换。",
            asset_ids=tuple(asset_ids),
        )
    return views


def build_asset_volatile_view(asset_context: dict[str, Any]) -> JsonDict:
    """压缩单标的的波动层视图。"""

    factor = asset_context.get("factor") or {}
    signal_risk = asset_context.get("signal_risk") or {}
    memory = asset_context.get("memory") or {}
    return {
        "asset_profile": json_clone(asset_context.get("profile")),
        "indicator_frame": json_clone(factor.get("indicator_frame")),
        "factor_frame": json_clone(factor.get("factor_frame")),
        "score": json_clone(factor.get("score")),
        "evidence": json_clone(factor.get("evidence")),
        "signal": json_clone(signal_risk.get("signal")),
        "risk_items": json_clone(signal_risk.get("risks")),
        "memory_items": json_clone(memory.get("memories")),
        "memory_count": len(memory.get("memories") or []),
        "risk_count": len(signal_risk.get("risks") or []),
    }


def summarize_memory_contexts(asset_contexts: dict[str, dict[str, Any]]) -> JsonDict:
    """汇总资产记忆，作为 volatile 层默认记忆摘要。"""

    items: list[JsonDict] = []
    for asset_id, asset_context in asset_contexts.items():
        memory = asset_context.get("memory") or {}
        for item in memory.get("memories") or []:
            if isinstance(item, dict):
                items.append(
                    {
                        "asset_id": asset_id,
                        "memory_id": item.get("memory_id"),
                        "memory_type": item.get("memory_type"),
                        "content": item.get("content"),
                    }
                )
    return {
        "memory_count": len(items),
        "items": items[:10],
    }


def summarize_risk_contexts(asset_contexts: dict[str, dict[str, Any]]) -> JsonDict:
    """汇总风险项，作为 volatile 层默认风险摘要。"""

    items: list[JsonDict] = []
    for asset_id, asset_context in asset_contexts.items():
        signal_risk = asset_context.get("signal_risk") or {}
        for item in signal_risk.get("risks") or []:
            if isinstance(item, dict):
                items.append(
                    {
                        "asset_id": asset_id,
                        "risk_id": item.get("risk_id"),
                        "severity": item.get("severity"),
                        "title": item.get("title"),
                    }
                )
    high_count = sum(1 for item in items if item.get("severity") in {"high", "critical"})
    return {
        "risk_count": len(items),
        "high_risk_count": high_count,
        "items": items[:10],
    }


def json_clone(value: Any) -> Any:
    """浅层兼容 JSON 的拷贝，避免把运行时对象直接塞进 envelope。"""

    if isinstance(value, dict):
        return {key: json_clone(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_clone(item) for item in value]
    if isinstance(value, tuple):
        return [json_clone(item) for item in value]
    return value
