from __future__ import annotations

from finance_agent.agents.workflows.roundtable_prompts import role_prompt
from finance_agent.skills.loader import load_methodology_skills, load_methodology_skill


def test_chanlun_skill_declares_engine_and_red_lines() -> None:
    skill = load_methodology_skill("chanlun-interpret")

    assert skill.name == "chanlun-interpret"
    assert skill.requires_engine == "chanlun"
    assert skill.roundtable_role == "technical_analyst"
    assert "不得自己数笔" in skill.body
    assert "只引用 chanlun 引擎输出" in skill.body


def test_chanlun_skill_can_be_injected_when_explicitly_enabled() -> None:
    registry = load_methodology_skills(("chanlun-interpret",))
    prompt = role_prompt("technical_analyst", skill_registry=registry)

    assert "chanlun-interpret" in prompt
    assert "确定性引擎：chanlun" in prompt
    assert "不得自己数笔" in prompt
