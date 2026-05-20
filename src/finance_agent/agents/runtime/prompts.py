"""金融 Agent Prompt 注册表。"""

from __future__ import annotations

from typing import Any

JsonDict = dict[str, Any]

TOP_LEVEL_STABLE_PROMPT = """你是 finance-agent 的金融总调度 Agent。
你的职责是读取已入库事实、调用金融工具和 Workflow、组织角色圆桌，并输出可审计的中文选股/选币建议。
你必须遵守：不直接访问外部数据源，不编造行情、因子、风险或记忆，不直接执行真实交易。
当上下文足够明确时，先行动再解释；需要事实时先调用工具或 Workflow，再总结结论。
"""

PRIMARY_ANALYST_STABLE_PROMPT = """你是 DeepSeek 主分析 Agent。
你负责常规金融分析、圆桌摘要、推荐排序解释和中文报告草稿。
你只能基于工具结果、因子、信号、风险、记忆和 workflow 上下文输出结论。
"""

HIGH_RISK_REVIEW_STABLE_PROMPT = """你是 GPT-5.5 高风险复核 Agent。
你只复核卖出、换股/换币、大仓位调整、强风险、数据缺口和信号冲突。
你的任务是反驳、降级、要求补证据或确认建议，而不是重新生成完整推荐链路。
"""

ROLE_PROMPTS: dict[str, str] = {
    "technical_analyst": "你是技术分析角色，只看 K 线、技术指标、趋势、量价和信号方向。",
    "factor_analyst": "你是因子分析角色，只看因子、评分、估值、财务、衍生品和证据引用。",
    "risk_rebuttal": "你是风险反驳角色，只寻找风险事件、数据缺口、冲突信号和历史失败证据。",
    "portfolio_manager": "你是组合经理角色，只考虑持仓、观察池、候选排序、仓位约束和换股/换币比较。",
    "memory_manager": "你是记忆管理员角色，只负责召回、压缩、冲突检查和长期事实写回建议。",
}


def build_prompt_bundle(
    *,
    model_role: str,
    context_envelope: dict[str, Any],
    role_name: str | None = None,
) -> JsonDict:
    """按模型角色和 workflow 角色拼装 prompt 片段。"""

    stable_prompt = resolve_stable_prompt(model_role)
    role_prompt = ROLE_PROMPTS.get(role_name or "", "")
    role_view = (context_envelope.get("role_views") or {}).get(role_name or "", {})
    return {
        "model_role": model_role,
        "role_name": role_name,
        "stable": stable_prompt,
        "context": build_context_prompt(context_envelope),
        "volatile": build_volatile_prompt(context_envelope, role_view=role_view),
        "role": role_prompt,
    }


def resolve_stable_prompt(model_role: str) -> str:
    """按模型角色选择稳定 prompt。"""

    if model_role == "primary_financial_analyst":
        return PRIMARY_ANALYST_STABLE_PROMPT
    if model_role == "high_risk_reviewer":
        return HIGH_RISK_REVIEW_STABLE_PROMPT
    return TOP_LEVEL_STABLE_PROMPT


def build_context_prompt(context_envelope: dict[str, Any]) -> str:
    """把任务上下文压缩成 prompt 片段。"""

    context = context_envelope.get("context") or {}
    return (
        f"workflow_type={context.get('workflow_type')}; "
        f"market_type={context.get('market_type')}; "
        f"asset_ids={context.get('asset_ids')}; "
        f"available_tools={context.get('available_tools')}."
    )


def build_volatile_prompt(
    context_envelope: dict[str, Any],
    *,
    role_view: dict[str, Any],
) -> str:
    """把波动上下文压缩成 prompt 片段。"""

    volatile = context_envelope.get("volatile") or {}
    memory_summary = volatile.get("memory_summary") or {}
    risk_summary = volatile.get("risk_summary") or {}
    visible_sections = role_view.get("visible_sections") or []
    return (
        f"visible_sections={visible_sections}; "
        f"memory_count={memory_summary.get('memory_count', 0)}; "
        f"risk_count={risk_summary.get('risk_count', 0)}; "
        f"high_risk_count={risk_summary.get('high_risk_count', 0)}."
    )
