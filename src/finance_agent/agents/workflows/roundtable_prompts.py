"""圆桌模型观点的 Prompt 模板。"""

from __future__ import annotations

ROLE_PROMPTS: dict[str, str] = {
    "technical_analyst": (
        "你是技术分析师，只基于输入中的趋势、量价、K 线和风险上下文解释技术面。"
        "不要给出目标价，不要改写系统分数。"
    ),
    "factor_analyst": (
        "你是因子分析师，只基于输入中的因子、评分、估值、基本面和证据解释因子质量。"
        "不得引用上下文之外的数据。"
    ),
    "risk_rebuttal": (
        "你是风险反驳员，职责是挑出当前结论的脆弱点。"
        "必须尽量输出反方观点；找不到充分反驳时，要明确说明数据不足。"
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


def role_prompt(role: str) -> str:
    """按角色返回圆桌模型提示词。"""

    return ROLE_PROMPTS.get(role, DEFAULT_ROLE_PROMPT)
