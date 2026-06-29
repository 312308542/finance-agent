from __future__ import annotations

from finance_agent.agents.workflows.roundtable_prompts import role_prompt
from finance_agent.skills.loader import load_all_methodology_skills


def test_role_prompt_injects_matching_methodology_skills() -> None:
    prompt = role_prompt(
        "technical_analyst",
        skill_registry=load_all_methodology_skills(),
    )

    assert "## 可加载方法论技能" in prompt
    assert "technical-basic" in prompt
    assert "candlestick" in prompt
    assert "volatility" in prompt
    assert "valuation-model" not in prompt
    assert "不得自己计算笔/中枢/浪型/形态" in prompt


def test_role_prompt_keeps_red_lines_when_no_skill_matches() -> None:
    prompt = role_prompt(
        "unknown_role",
        skill_registry=load_all_methodology_skills(),
    )

    assert "金融圆桌分析员" in prompt
    assert "## 可加载方法论技能" not in prompt
    assert "不得生成上下文之外的新事实" in prompt
