from __future__ import annotations

from finance_agent.skills.loader import (
    P3_METHODOLOGY_SKILL_NAMES,
    load_methodology_skills,
)


def test_load_all_p3_methodology_skills_with_red_lines() -> None:
    registry = load_methodology_skills(P3_METHODOLOGY_SKILL_NAMES)

    assert set(registry.skill_names()) == set(P3_METHODOLOGY_SKILL_NAMES)
    assert len(registry.skill_names()) == 10
    for skill in registry.skills:
        assert "## 禁令" in skill.body
        assert "不得引用入库数据之外的事实" in skill.body
        assert "不得修改系统分数" in skill.body


def test_p3_b_type_skills_declare_required_engines() -> None:
    registry = load_methodology_skills(P3_METHODOLOGY_SKILL_NAMES)
    required_engines = {
        skill.name: skill.requires_engine
        for skill in registry.skills
        if skill.name in {"elliott-wave", "harmonic", "smc", "seasonal"}
    }

    assert required_engines == {
        "elliott-wave": "elliott_wave",
        "harmonic": "harmonic",
        "smc": "smc",
        "seasonal": "seasonal",
    }
