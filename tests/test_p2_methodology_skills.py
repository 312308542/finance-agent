from __future__ import annotations

from finance_agent.skills.loader import (
    P2_METHODOLOGY_SKILL_NAMES,
    load_methodology_skills,
)


def test_load_all_p2_methodology_skills_with_red_lines() -> None:
    registry = load_methodology_skills(P2_METHODOLOGY_SKILL_NAMES)

    assert set(registry.skill_names()) == set(P2_METHODOLOGY_SKILL_NAMES)
    assert len(registry.skill_names()) == 22
    for skill in registry.skills:
        assert "## 禁令" in skill.body
        assert "不得引用入库数据之外的事实" in skill.body
        assert "不得修改系统分数" in skill.body


def test_p2_skills_are_assigned_to_roundtable_roles() -> None:
    registry = load_methodology_skills(P2_METHODOLOGY_SKILL_NAMES)

    assert {skill.name for skill in registry.for_role("technical_analyst")} >= {
        "minute-analysis",
        "market-microstructure",
        "quant-statistics",
    }
    assert {skill.name for skill in registry.for_role("portfolio_manager")} >= {
        "asset-allocation",
        "etf-analysis",
        "fund-analysis",
    }
    assert {skill.name for skill in registry.for_role("event_analyst")} >= {
        "regulatory-knowledge",
        "social-media-intelligence",
    }
