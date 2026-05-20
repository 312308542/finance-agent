"""金融 Agent Prompt 结构化模板注册表。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from hashlib import sha1
from typing import Any

JsonDict = dict[str, Any]
TEMPLATE_VERSION = "1.0.0"


@dataclass(frozen=True)
class PromptTemplateSection:
    """Prompt 模板分段。"""

    name: str
    content: str
    required: bool = True

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class PromptTemplate:
    """结构化 Prompt 模板。"""

    template_id: str
    version: str
    model_role: str
    market_scope: tuple[str, ...]
    workflow_scope: tuple[str, ...]
    sections: tuple[PromptTemplateSection, ...]
    output_schema: JsonDict

    def prompt_hash(self) -> str:
        """根据模板内容生成稳定 hash。"""

        material = {
            "template_id": self.template_id,
            "version": self.version,
            "model_role": self.model_role,
            "market_scope": self.market_scope,
            "workflow_scope": self.workflow_scope,
            "sections": [section.to_dict() for section in self.sections],
            "output_schema": self.output_schema,
        }
        payload = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return sha1(payload).hexdigest()


@dataclass(frozen=True)
class PromptRenderContext:
    """Prompt 渲染上下文。"""

    context_envelope: JsonDict
    model_role: str
    role_name: str | None
    market_type: str
    workflow_type: str
    role_view: JsonDict
    available_tools: tuple[str, ...]
    memory_summary: JsonDict
    risk_summary: JsonDict


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
你必须优先执行风险反驳、补证据和降级，不得默认放行高风险建议。
"""

ROLE_PROMPTS: dict[str, str] = {
    "technical_analyst": "你是技术分析角色，只看 K 线、技术指标、趋势、量价和信号方向。",
    "factor_analyst": "你是因子分析角色，只看因子、评分、估值、财务、衍生品和证据引用。",
    "risk_rebuttal": "你是风险反驳角色，只寻找风险事件、数据缺口、冲突信号和历史失败证据。",
    "portfolio_manager": "你是组合经理角色，只考虑持仓、观察池、候选排序、仓位约束和换股/换币比较。",
    "memory_manager": "你是记忆管理员角色，只负责召回、压缩、冲突检查和长期事实写回建议。",
}

OUTPUT_SCHEMA_PRIMARY_ANALYST = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary_zh": {"type": "string"},
        "action": {
            "type": "string",
            "enum": ["watch", "buy_candidate", "avoid", "sell_review", "swap_review", "need_more_data"],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
        "tool_requests": {"type": "array", "items": {"type": "string"}},
        "reasoning_brief_zh": {"type": "string"},
        "report_sections": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "technical": {"type": "string"},
                "factor": {"type": "string"},
                "risk_rebuttal": {"type": "string"},
                "portfolio": {"type": "string"},
                "memory": {"type": "string"},
            },
            "required": ["technical", "factor", "risk_rebuttal", "portfolio", "memory"],
        },
    },
    "required": [
        "summary_zh",
        "action",
        "confidence",
        "evidence_ids",
        "risk_flags",
        "tool_requests",
        "reasoning_brief_zh",
        "report_sections",
    ],
}

OUTPUT_SCHEMA_HIGH_RISK = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "review_status": {
            "type": "string",
            "enum": ["approve", "downgrade", "reject", "need_more_data"],
        },
        "summary_zh": {"type": "string"},
        "blocking_risks": {"type": "array", "items": {"type": "string"}},
        "missing_evidence": {"type": "array", "items": {"type": "string"}},
        "required_follow_up_tools": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": [
        "review_status",
        "summary_zh",
        "blocking_risks",
        "missing_evidence",
        "required_follow_up_tools",
        "confidence",
    ],
}

OUTPUT_SCHEMA_TOP_LEVEL = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {
            "type": "string",
            "enum": ["ready", "need_more_data", "blocked"],
        },
        "summary_zh": {"type": "string"},
        "task_plan": {"type": "array", "items": {"type": "string"}},
        "tool_requests": {"type": "array", "items": {"type": "string"}},
        "reasoning_brief_zh": {"type": "string"},
    },
    "required": ["status", "summary_zh", "task_plan", "tool_requests", "reasoning_brief_zh"],
}

TOOL_PROTOCOL = """工具调用协议：
1. 只能使用已入库事实与 FinanceToolRuntime 暴露的只读工具。
2. 事实不足时必须输出 need_more_data，并列出需要的工具，而不是猜测补全。
3. 不得编造行情、因子、风险、记忆或持仓。
4. 财务、因子、技术、风险和记忆结果只能作为证据，不可替代审计记录。
5. 真正交易动作仍然禁用，只能给出建议与人工确认任务。
"""

RISK_PROTOCOL = """风险反驳协议：
1. 高风险动作默认先反驳，再考虑放行。
2. 发现数据缺失、冲突、过期、来源不一致时必须降级。
3. A 股遇到停牌、退市、ST、涨跌停、质押、限售解禁、重大公告时，不得直接输出强买入。
4. 数字货币遇到资金费率异常、爆仓风险、流动性不足、稳定币风险、交易所事件时，不得直接输出强买入。
5. 风险反驳结果必须明确写出缺口、冲突和需要补的证据。
"""

REPORTING_CONSTRAINTS = """中英文报告约束：
1. 面向用户的结论、解释和报告正文使用中文。
2. JSON 字段名、工具名、模型名、流程名保留英文，summary_zh、reasoning_brief_zh、report_sections 等面向用户内容字段使用中文语义。
3. 不输出内部推理长链路，只输出简短 reasoning_brief_zh。
4. 报告必须能拆成技术、因子、风险反驳、组合和记忆五段。
5. 不把建议写成真实交易指令，只写推荐、观察、回避或复核。
"""

ASHARE_MARKET_RULES = """A 股市场规则：
- 交易日、停复牌和交易时段必须显式考虑。
- 涨跌停、ST、退市、质押、限售解禁和重大公告都是强风险信号。
- 需要关注财报、业绩预告、分红、北向资金、主力资金和龙虎榜。
- 指数成分、行业、概念和资金流只能作为候选筛选和解释依据。
"""

CRYPTO_MARKET_RULES = """数字货币市场规则：
- 24/7 连续交易，不能默认存在 A 股那样的交易日边界。
- 需要关注交易对、交易所、资金费率、合约持仓、爆仓风险和流动性。
- 稳定币、链上或交易所事件、插针和极端波动都属于强风险信号。
- 只能基于交易对、交易所和行情事实做推荐，不得假设统一市场结构。
"""

PROMPT_TEMPLATE_REGISTRY: dict[str, PromptTemplate] = {
    "top_level_dispatcher": PromptTemplate(
        template_id="top_level_dispatcher",
        version=TEMPLATE_VERSION,
        model_role="top_level_dispatcher",
        market_scope=("*",),
        workflow_scope=("*",),
        sections=(
            PromptTemplateSection("identity", TOP_LEVEL_STABLE_PROMPT),
            PromptTemplateSection("act_guidance", "先行动后解释，先调工具再下结论。"),
            PromptTemplateSection("tool_protocol", TOOL_PROTOCOL),
            PromptTemplateSection("reporting_constraints", REPORTING_CONSTRAINTS),
        ),
        output_schema=OUTPUT_SCHEMA_TOP_LEVEL,
    ),
    "primary_financial_analyst": PromptTemplate(
        template_id="primary_financial_analyst",
        version=TEMPLATE_VERSION,
        model_role="primary_financial_analyst",
        market_scope=("ashare", "crypto"),
        workflow_scope=(
            "portfolio_monitoring",
            "watchlist_management",
            "recommendation_decision",
            "asset_deep_analysis",
            "swap_decision",
            "daily_review",
        ),
        sections=(
            PromptTemplateSection("identity", PRIMARY_ANALYST_STABLE_PROMPT),
            PromptTemplateSection("act_guidance", "需要事实时先调工具或 Workflow，再给结论。"),
            PromptTemplateSection("tool_protocol", TOOL_PROTOCOL),
            PromptTemplateSection("market_rules_ashare", ASHARE_MARKET_RULES),
            PromptTemplateSection("market_rules_crypto", CRYPTO_MARKET_RULES),
            PromptTemplateSection("risk_protocol", RISK_PROTOCOL),
            PromptTemplateSection("reporting_constraints", REPORTING_CONSTRAINTS),
        ),
        output_schema=OUTPUT_SCHEMA_PRIMARY_ANALYST,
    ),
    "high_risk_reviewer": PromptTemplate(
        template_id="high_risk_reviewer",
        version=TEMPLATE_VERSION,
        model_role="high_risk_reviewer",
        market_scope=("ashare", "crypto"),
        workflow_scope=(
            "recommendation_decision",
            "asset_deep_analysis",
            "swap_decision",
            "daily_review",
        ),
        sections=(
            PromptTemplateSection("identity", HIGH_RISK_REVIEW_STABLE_PROMPT),
            PromptTemplateSection("risk_protocol", RISK_PROTOCOL),
            PromptTemplateSection("reporting_constraints", REPORTING_CONSTRAINTS),
        ),
        output_schema=OUTPUT_SCHEMA_HIGH_RISK,
    ),
}


def build_prompt_bundle(
    *,
    model_role: str,
    context_envelope: dict[str, Any],
    role_name: str | None = None,
) -> JsonDict:
    """按模型角色和 workflow 角色拼装 prompt 片段。"""

    render_context = build_prompt_render_context(
        model_role=model_role,
        context_envelope=context_envelope,
        role_name=role_name,
    )
    template = resolve_prompt_template(render_context)
    sections = {section.name: section.content for section in template.sections}
    stable_prompt = "\n".join(
        content for content in (
            template.sections[0].content if template.sections else "",
            sections.get("act_guidance", ""),
            sections.get("tool_protocol", ""),
            sections.get("market_rules_ashare", "") if render_context.market_type == "ashare" else "",
            sections.get("market_rules_crypto", "") if render_context.market_type == "crypto" else "",
            sections.get("risk_protocol", ""),
            sections.get("reporting_constraints", ""),
        )
        if content
    )
    role_prompt = ROLE_PROMPTS.get(role_name or "", "")
    section_lengths = {section.name: len(section.content) for section in template.sections}
    tool_count = len(render_context.available_tools)
    prompt_hash = template.prompt_hash()
    return {
        "template_id": template.template_id,
        "prompt_version": template.version,
        "prompt_hash": prompt_hash,
        "model_role": model_role,
        "role_name": role_name,
        "market_type": render_context.market_type,
        "workflow_type": render_context.workflow_type,
        "stable": stable_prompt,
        "context": build_context_prompt(render_context.context_envelope),
        "volatile": build_volatile_prompt(render_context.context_envelope, role_view=render_context.role_view),
        "role": role_prompt,
        "tool_protocol": sections.get("tool_protocol", TOOL_PROTOCOL),
        "risk_protocol": sections.get("risk_protocol", RISK_PROTOCOL),
        "market_rules": build_market_rules_prompt(render_context.market_type),
        "output_schema": template.output_schema,
        "top_level_output_schema": OUTPUT_SCHEMA_TOP_LEVEL,
        "reporting_constraints": sections.get("reporting_constraints", REPORTING_CONSTRAINTS),
        "sections": [section.to_dict() for section in template.sections],
        "audit_summary": {
            "prompt_char_count": len(stable_prompt) + len(role_prompt),
            "section_lengths": section_lengths,
            "tool_count": tool_count,
            "prompt_hash_stable": bool(prompt_hash),
        },
        "prompt_hash_stable": bool(prompt_hash),
    }


def resolve_stable_prompt(model_role: str) -> str:
    """兼容旧接口：返回模型角色对应的稳定提示词。"""

    template = PROMPT_TEMPLATE_REGISTRY.get(model_role)
    if template is None:
        template = PROMPT_TEMPLATE_REGISTRY["top_level_dispatcher"]
    return "\n".join(
        section.content
        for section in template.sections
        if section.name in {"identity", "act_guidance", "tool_protocol"}
    )


def build_prompt_render_context(
    *,
    model_role: str,
    context_envelope: dict[str, Any],
    role_name: str | None,
) -> PromptRenderContext:
    """整理模板渲染所需的上下文。"""

    context = context_envelope.get("context") or {}
    role_view = (context_envelope.get("role_views") or {}).get(role_name or "", {})
    volatile = context_envelope.get("volatile") or {}
    return PromptRenderContext(
        context_envelope=context_envelope,
        model_role=model_role,
        role_name=role_name,
        market_type=str(context.get("market_type") or context_envelope.get("market_type") or "ashare"),
        workflow_type=str(context.get("workflow_type") or context_envelope.get("workflow_type") or ""),
        role_view=role_view,
        available_tools=tuple(context.get("available_tools") or ()),
        memory_summary=volatile.get("memory_summary") or {},
        risk_summary=volatile.get("risk_summary") or {},
    )


def resolve_prompt_template(render_context: PromptRenderContext) -> PromptTemplate:
    """按模型角色、市场和工作流选择模板。"""

    template = PROMPT_TEMPLATE_REGISTRY.get(render_context.model_role)
    if template is None:
        template = PROMPT_TEMPLATE_REGISTRY["top_level_dispatcher"]
    if render_context.market_type not in template.market_scope and "*" not in template.market_scope:
        return PROMPT_TEMPLATE_REGISTRY["top_level_dispatcher"]
    if render_context.workflow_type not in template.workflow_scope and "*" not in template.workflow_scope:
        return PROMPT_TEMPLATE_REGISTRY["top_level_dispatcher"]
    return template


def build_market_rules_prompt(market_type: str) -> str:
    """根据市场类型返回差异化规则。"""

    if market_type == "crypto":
        return CRYPTO_MARKET_RULES
    return ASHARE_MARKET_RULES


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
