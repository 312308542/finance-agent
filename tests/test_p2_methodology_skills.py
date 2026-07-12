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


def test_ichimoku_skill_transfers_references_as_read_only_interpretation() -> None:
    registry = load_methodology_skills(("ichimoku",))
    skill = registry.skills[0]

    assert "## 失效条件" in skill.body
    assert "转换线" in skill.body
    assert "基准线" in skill.body
    assert "云带" in skill.body
    assert "来源：Vibe-Trading references，MIT，已按只读解读视角改写。" in skill.body
    assert section(skill.body, "## 禁令") == (
        "不得自己计算一目均衡线、不得引用入库数据之外的事实、不得给目标价、"
        "不得修改系统分数、信号方向、风险标记或动作枚举。"
    )


def test_orphan_engine_skills_are_explicitly_reserved() -> None:
    """未建立生产消费方的引擎技能必须显式保持接口预留。"""

    registry = load_methodology_skills(
        ("correlation-analysis", "pair-trading", "seasonal")
    )

    for skill in registry.skills:
        assert "## 接入状态" in skill.body
        status = section(skill.body, "## 接入状态")
        assert "接口预留" in status
        assert "capability 保持 False" in status
        assert "不得默认加载" in status


def section(body: str, title: str) -> str:
    start = body.index(title) + len(title)
    tail = body[start:]
    next_title = tail.find("\n## ")
    if next_title >= 0:
        tail = tail[:next_title]
    return tail.strip()
