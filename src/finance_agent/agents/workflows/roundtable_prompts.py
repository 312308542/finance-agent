"""圆桌模型观点的 Prompt 模板。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from finance_agent.skills.loader import MethodologySkill, MethodologySkillRegistry

ROLE_SKILL_BUDGET_CHARS = 6000

ROLE_PROMPTS: dict[str, str] = {
    "technical_analyst": (
        "你是技术分析师，只基于输入中的趋势、量价、K 线和风险上下文解释技术面。"
        "不要给出目标价，不要改写系统分数。"
    ),
    "factor_analyst": (
        "你是因子分析师，只基于输入中的因子、评分、估值、基本面和证据解释因子质量。"
        "不得引用上下文之外的数据。"
    ),
    "event_analyst": (
        "你是市场事件分析师，只基于输入中的新闻、公告、舆情、监管、题材和事件证据解读。"
        "必须做数据维度自检，明确本次覆盖和缺失的事件维度。"
    ),
    "flow_analyst": (
        "你是资金流分析师，只基于输入中的个股资金流、北向、板块资金、龙虎榜席位和衍生品快照解读。"
        "必须做数据维度自检，明确本次覆盖和缺失的资金维度。"
    ),
    "risk_rebuttal": (
        "你是风险反驳员，职责是挑出当前结论的脆弱点。"
        "必须尽量输出反方观点；找不到充分反驳时，要明确说明数据不足。"
        "数据维度缺失本身就是一条反方观点。"
    ),
    "portfolio_manager": (
        "你是组合经理，职责是综合资产、持仓、观察池和推荐上下文，"
        "给出动作约束和组合层面的权衡。"
    ),
    "memory_manager": (
        "你是记忆管理员，职责是检查历史记录、用户偏好和过往复盘是否支持当前判断。"
        "没有记忆时要明确说明缺口。"
    ),
}

DEFAULT_ROLE_PROMPT = (
    "你是金融圆桌分析员，只能基于输入上下文生成解释性观点。"
    "不得生成上下文之外的新事实、目标价或交易执行指令。"
)

OUTPUT_SCHEMA_PROMPT = """
请只输出一个 JSON 对象，字段如下：
{
  "role": "角色 key",
  "stance": "bullish|bearish|neutral|conflicted",
  "confidence": 0.0,
  "summary": "一句中文摘要",
  "key_points": ["基于上下文事实的要点"],
  "rebuttals": ["反方观点或失败条件"],
  "evidence_ids": ["只能引用输入上下文中出现过的 evidence_id"],
  "data_gaps": ["缺失数据或不确定性"]
}
要求：
1. 不得输出 JSON 以外的解释文字。
2. 不得引用上下文之外的数据。
3. 不得修改任何分数、信号方向或动作枚举。
4. 面向金融新手解释术语，避免空泛结论。
""".strip()


def role_prompt(role: str, *, skill_registry: "MethodologySkillRegistry | None" = None) -> str:
    """按角色返回圆桌模型提示词。"""

    prompt = ROLE_PROMPTS.get(role, DEFAULT_ROLE_PROMPT)
    registry = skill_registry or load_default_skill_registry()
    if registry is None:
        return prompt
    skills = registry.for_role(role)
    if not skills:
        return prompt
    skill_text = build_role_skill_text(skills)
    return (
        f"{prompt}\n\n"
        "## 可加载方法论技能\n"
        "以下技能只提供解读口径，不能产生新事实；计算型结构必须来自确定性引擎输出。\n"
        f"{skill_text}\n\n"
        "## 方法论红线\n"
        "- 不得自己计算笔/中枢/浪型/形态，只能引用已入库或引擎输出。\n"
        "- 不得引用入库数据之外的事实。\n"
        "- 不得给目标价，不得修改系统分数、信号方向、风险标记或动作枚举。"
    )


def build_role_skill_text(skills: list["MethodologySkill"]) -> str:
    """按优先级和总预算拼接角色可用方法论技能。"""

    ordered_skills = sorted(enumerate(skills), key=lambda item: (skill_priority(item[1].name), item[0]))
    chunks: list[str] = []
    omitted = 0
    used = 0
    separator = "\n\n"
    for _, skill in ordered_skills:
        excerpt = skill.prompt_excerpt()
        extra = len(excerpt) + (len(separator) if chunks else 0)
        if used + extra > ROLE_SKILL_BUDGET_CHARS:
            omitted += 1
            continue
        chunks.append(excerpt)
        used += extra
    if omitted:
        note = f"另有 {omitted} 个技能未注入，原因：超过角色方法论注入预算。"
        extra = len(note) + (len(separator) if chunks else 0)
        while chunks and used + extra > ROLE_SKILL_BUDGET_CHARS:
            removed = chunks.pop()
            used -= len(removed) + (len(separator) if chunks else 0)
            omitted += 1
            note = f"另有 {omitted} 个技能未注入，原因：超过角色方法论注入预算。"
            extra = len(note) + (len(separator) if chunks else 0)
        chunks.append(note)
    return separator.join(chunks)


def skill_priority(name: str) -> int:
    """返回 prompt 注入优先级：P1 优先，其次 L1 结构技能。"""

    from finance_agent.skills.loader import L1_METHODOLOGY_SKILL_NAMES, P1_METHODOLOGY_SKILL_NAMES

    if name in P1_METHODOLOGY_SKILL_NAMES:
        return 0
    if name in L1_METHODOLOGY_SKILL_NAMES:
        return 1
    return 2


def load_default_skill_registry() -> "MethodologySkillRegistry | None":
    """加载默认方法论技能；失败时保持旧 Prompt 可用。"""

    try:
        from finance_agent.skills.loader import load_active_methodology_skills

        return load_active_methodology_skills()
    except Exception:  # noqa: BLE001 - Prompt 注入失败不能阻断圆桌 fallback
        return None
