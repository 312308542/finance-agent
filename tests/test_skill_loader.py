from __future__ import annotations

from finance_agent.skills.loader import (
    ENGINE_CAPABILITIES,
    P1_METHODOLOGY_SKILL_NAMES,
    EngineCapability,
    load_active_methodology_skills,
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


def test_load_active_skills_enable_p1_and_available_l1_capabilities() -> None:
    registry = load_active_methodology_skills()

    names = set(registry.skill_names())
    assert set(P1_METHODOLOGY_SKILL_NAMES) <= names
    assert {"smc", "harmonic", "elliott-wave", "ichimoku"} <= names
    assert "chanlun-interpret" not in names
    assert "correlation-analysis" not in names
    assert "pair-trading" not in names
    assert "seasonal" not in names
    assert ENGINE_CAPABILITIES["structural_lite_smc"].available is True
    assert ENGINE_CAPABILITIES["ichimoku"].available is True
    assert ENGINE_CAPABILITIES["chanlun"].available is False


def test_load_active_skills_allows_fake_capability_to_unlock_chanlun() -> None:
    fake_capabilities = {
        **ENGINE_CAPABILITIES,
        "chanlun": EngineCapability(
            key="chanlun",
            available=True,
            description="测试中模拟 czsc 已上线",
        ),
    }

    registry = load_active_methodology_skills(capabilities=fake_capabilities)

    assert "chanlun-interpret" in registry.skill_names()


def test_load_active_skills_enable_stage2_pure_knowledge_roles() -> None:
    """阶段二应为组合经理和风险反驳员启用纯知识技能。"""

    registry = load_active_methodology_skills()

    assert {skill.name for skill in registry.for_role("portfolio_manager")} >= {
        "asset-allocation",
        "hedging-strategy",
        "etf-analysis",
        "fund-analysis",
        "convertible-bond",
        "cross-market-strategy",
    }
    assert {skill.name for skill in registry.for_role("risk_rebuttal")} >= {
        "credit-analysis",
        "geopolitical-risk",
    }


def test_load_all_skills_aliases_active_registry_and_filter_by_roundtable_role() -> None:
    registry = load_all_methodology_skills()

    names = set(registry.skill_names())
    assert set(P1_METHODOLOGY_SKILL_NAMES) <= names
    assert {"smc", "harmonic", "elliott-wave"} <= names
    assert {skill.name for skill in registry.for_role("technical_analyst")} >= {
        "technical-basic",
        "candlestick",
        "volatility",
        "smc",
        "harmonic",
        "elliott-wave",
    }
    assert {skill.name for skill in registry.for_role("flow_analyst")} == {
        "sector-rotation",
        "hk-connect-flow",
        "crypto-derivatives",
        "perp-funding-basis",
    }


def test_every_active_skill_declares_red_line_prohibitions() -> None:
    registry = load_all_methodology_skills()

    for skill in registry.skills:
        assert "## 禁令" in skill.body
        assert "不得引用入库数据之外的事实" in skill.body
        assert "不得给目标价" in skill.body
        assert "不得修改系统分数" in skill.body
