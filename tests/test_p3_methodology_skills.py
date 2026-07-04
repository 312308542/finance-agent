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


def test_structural_lite_skills_only_interpret_engine_outputs() -> None:
    registry = load_methodology_skills(P3_METHODOLOGY_SKILL_NAMES)
    skills = {
        skill.name: skill.body
        for skill in registry.skills
        if skill.name in {"elliott-wave", "harmonic", "smc"}
    }

    for body in skills.values():
        assert "structural-lite" in body
        assert "只读取" in body
        assert "不得自己" in body
    assert "低置信度" in skills["elliott-wave"]
    assert "thesis_confirmation_price" in skills["elliott-wave"]
    assert "thesis_invalidation_price" in skills["elliott-wave"]
    assert "XABCD" in skills["harmonic"]
    assert "BOS" in skills["smc"]
