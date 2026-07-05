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
        "elliott-wave": "structural_lite_elliott",
        "harmonic": "structural_lite_harmonic",
        "smc": "structural_lite_smc",
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


def test_structural_lite_skills_transfer_references_as_read_only_interpretation() -> None:
    registry = load_methodology_skills(P3_METHODOLOGY_SKILL_NAMES)
    skills = {
        skill.name: skill.body
        for skill in registry.skills
        if skill.name in {"elliott-wave", "harmonic", "smc"}
    }

    assert "## 失效条件" in skills["elliott-wave"]
    assert "浪2不破浪1起点" in skills["elliott-wave"]
    assert "浪3不是最短" in skills["elliott-wave"]
    assert "浪4不进入浪1区域" in skills["elliott-wave"]
    assert section(skills["elliott-wave"], "## 禁令") == (
        "不得自己数浪、不得在低置信度时强行输出波浪观点、不得引用入库数据之外的事实、"
        "不得给目标价、不得修改系统分数、信号方向、风险标记或动作枚举。"
    )

    assert "## 失效条件" in skills["harmonic"]
    assert "Gartley" in skills["harmonic"]
    assert "Bat" in skills["harmonic"]
    assert "PRZ" in skills["harmonic"]
    assert section(skills["harmonic"], "## 禁令") == (
        "不得自己画 XABCD、不得自己计算斐波那契比例、不得声称 pyharmonics 已启用、"
        "不得引用入库数据之外的事实、不得给目标价、不得修改系统分数、信号方向、风险标记或动作枚举。"
    )

    assert "## 失效条件" in skills["smc"]
    assert "BOS" in skills["smc"]
    assert "CHoCH" in skills["smc"]
    assert "订单块" in skills["smc"]
    assert "不得强结论" in skills["smc"]
    assert section(skills["smc"], "## 禁令") == (
        "不得自己标注订单块、不得自己判断 BOS/CHoCH、不得把 FVG 解释成必然回补、"
        "不得引用入库数据之外的事实、不得给目标价、不得修改系统分数、信号方向、风险标记或动作枚举。"
    )

    for body in skills.values():
        assert "来源：Vibe-Trading references，MIT，已按只读解读视角改写。" in body


def section(body: str, title: str) -> str:
    start = body.index(title) + len(title)
    tail = body[start:]
    next_title = tail.find("\n## ")
    if next_title >= 0:
        tail = tail[:next_title]
    return tail.strip()
