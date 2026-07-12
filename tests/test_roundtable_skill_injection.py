from __future__ import annotations

from finance_agent.agents.workflows.roundtable_prompts import role_prompt
from finance_agent.skills.loader import (
    MethodologySkill,
    MethodologySkillRegistry,
    load_all_methodology_skills,
)


def test_role_prompt_injects_matching_methodology_skills() -> None:
    prompt = role_prompt(
        "technical_analyst",
        skill_registry=load_all_methodology_skills(),
    )

    assert "## 可加载方法论技能" in prompt
    assert "technical-basic" in prompt
    assert "candlestick" in prompt
    assert "volatility" in prompt
    assert "smc" in prompt
    assert "harmonic" in prompt
    assert "elliott-wave" in prompt
    assert "chanlun-interpret" not in prompt
    assert "valuation-model" not in prompt
    assert "不得自己计算笔/中枢/浪型/形态" in prompt


def test_role_prompt_injects_active_structural_skills_with_budget() -> None:
    registry = load_all_methodology_skills()
    prompt = role_prompt("technical_analyst", skill_registry=registry)
    skill_block = prompt.split("## 可加载方法论技能", 1)[1].split("## 方法论红线", 1)[0]

    assert "smc" in skill_block
    assert "harmonic" in skill_block
    assert "elliott-wave" in skill_block
    assert "structural-lite" in skill_block
    assert "失效条件" in skill_block
    assert len(skill_block) <= 6000
    for skill in registry.for_role("technical_analyst"):
        assert len(skill.prompt_excerpt()) <= 1200


def test_role_prompt_truncates_over_budget_skills_by_priority() -> None:
    registry = MethodologySkillRegistry(
        skills=(
            fake_skill("technical-basic", "technical", None),
            fake_skill("smc", "technical", "structural_lite_smc"),
            fake_skill("z-extra-1", "technical", None),
            fake_skill("z-extra-2", "technical", None),
            fake_skill("z-extra-3", "technical", None),
            fake_skill("z-extra-4", "technical", None),
            fake_skill("z-extra-5", "technical", None),
            fake_skill("z-extra-6", "technical", None),
        )
    )

    prompt = role_prompt("technical_analyst", skill_registry=registry)
    skill_block = prompt.split("## 可加载方法论技能", 1)[1].split("## 方法论红线", 1)[0]

    assert "technical-basic" in skill_block
    assert "smc" in skill_block
    assert "另有" in skill_block
    assert len(skill_block) <= 6000


def test_role_prompt_injects_stage2_pure_knowledge_roles_with_budget() -> None:
    """阶段二纯知识技能应按角色注入，并继续受总预算约束。"""

    registry = load_all_methodology_skills()
    expected_by_role = {
        "portfolio_manager": {
            "asset-allocation",
            "hedging-strategy",
            "etf-analysis",
            "fund-analysis",
            "convertible-bond",
            "cross-market-strategy",
        },
        "risk_rebuttal": {"credit-analysis", "geopolitical-risk"},
    }

    for role, expected_names in expected_by_role.items():
        prompt = role_prompt(role, skill_registry=registry)
        skill_block = prompt.split("## 可加载方法论技能", 1)[1].split(
            "## 方法论红线", 1
        )[0]

        assert expected_names <= {
            skill.name for skill in registry.for_role(role)
        }
        assert all(name in skill_block for name in expected_names)
        assert len(skill_block) <= 6000
        assert "不得引用入库数据之外的事实" in prompt


def test_role_prompt_keeps_red_lines_when_no_skill_matches() -> None:
    prompt = role_prompt(
        "unknown_role",
        skill_registry=load_all_methodology_skills(),
    )

    assert "金融圆桌分析员" in prompt
    assert "## 可加载方法论技能" not in prompt
    assert "不得生成上下文之外的新事实" in prompt


def fake_skill(name: str, category: str, requires_engine: str | None) -> MethodologySkill:
    body = (
        "## 适用场景\n测试。\n\n"
        "## 解读口径\n"
        + ("只解读引擎输出。" * 260)
        + "\n\n## 失效条件\n"
        + ("失效后降级。" * 260)
        + "\n\n## 禁令\n不得引用入库数据之外的事实、不得给目标价、不得修改系统分数。"
    )
    return MethodologySkill(
        name=name,
        category=category,
        markets=["ashare"],
        requires_engine=requires_engine,
        roundtable_role="technical_analyst",
        body=body,
        path=None,
    )
