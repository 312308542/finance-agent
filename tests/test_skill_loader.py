from __future__ import annotations

from finance_agent.skills.loader import (
    P1_METHODOLOGY_SKILL_NAMES,
    load_all_methodology_skills,
    load_methodology_skill,
)


def test_load_methodology_skill_reads_metadata_and_body() -> None:
    skill = load_methodology_skill("technical-basic")

    assert skill.name == "technical-basic"
    assert skill.category == "technical"
    assert skill.markets == ["ashare", "crypto"]
    assert skill.requires_engine is None
    assert skill.roundtable_role == "technical_analyst"
    assert "## 禁令" in skill.body
    assert "不得自己计算" in skill.body
    assert "不得修改系统分数" in skill.body


def test_load_all_p1_skills_and_filter_by_roundtable_role() -> None:
    registry = load_all_methodology_skills()

    assert sorted(registry.skill_names()) == sorted(P1_METHODOLOGY_SKILL_NAMES)
    assert len(registry.skill_names()) == 18
    assert {skill.name for skill in registry.for_role("technical_analyst")} == {
        "technical-basic",
        "candlestick",
        "volatility",
    }
    assert {skill.name for skill in registry.for_role("flow_analyst")} == {
        "sector-rotation",
        "hk-connect-flow",
        "crypto-derivatives",
        "perp-funding-basis",
    }


def test_every_p1_skill_declares_red_line_prohibitions() -> None:
    registry = load_all_methodology_skills()

    for skill in registry.skills:
        assert "## 禁令" in skill.body
        assert "不得引用入库数据之外的事实" in skill.body
        assert "不得给目标价" in skill.body
        assert "不得修改系统分数" in skill.body
