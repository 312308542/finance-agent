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


def test_chanlun_skill_transfers_references_as_read_only_interpretation() -> None:
    skill = load_methodology_skill("chanlun-interpret")

    assert "## 失效条件" in skill.body
    assert "三买" in skill.body
    assert "中枢" in skill.body
    assert "分型" in skill.body
    assert "来源：Vibe-Trading references，MIT，已按只读解读视角改写。" in skill.body
    assert section(skill.body, "## 禁令") == (
        "不得自己数笔、不得自己画中枢、不得自行判定买卖点；只引用 chanlun 引擎输出；"
        "不得引用入库数据之外的事实、不得给目标价、不得修改系统分数、信号方向、风险标记或动作枚举。"
    )


def section(body: str, title: str) -> str:
    start = body.index(title) + len(title)
    tail = body[start:]
    next_title = tail.find("\n## ")
    if next_title >= 0:
        tail = tail[:next_title]
    return tail.strip()
