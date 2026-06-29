"""金融分析方法论 SKILL.md 加载器。

技能只提供圆桌角色的解读口径，不负责计算新事实。计算型方法论必须先由确定性
引擎产出结构化结果，再由技能解释。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

P1_METHODOLOGY_SKILL_NAMES: tuple[str, ...] = (
    "technical-basic",
    "candlestick",
    "volatility",
    "factor-research",
    "multi-factor",
    "valuation-model",
    "fundamental-filter",
    "financial-statement",
    "sector-rotation",
    "hk-connect-flow",
    "event-driven",
    "corporate-events",
    "sentiment-analysis",
    "behavioral-finance",
    "risk-analysis",
    "ashare-pre-st-filter",
    "crypto-derivatives",
    "perp-funding-basis",
)


@dataclass(frozen=True)
class MethodologySkill:
    """单个方法论技能的元数据与正文。"""

    name: str
    category: str
    markets: list[str]
    requires_engine: str | None
    roundtable_role: str
    body: str
    path: Path

    def prompt_excerpt(self) -> str:
        """转换为可注入圆桌角色 Prompt 的短文本。"""

        engine = self.requires_engine or "无，纯知识解读"
        markets = "、".join(self.markets)
        return (
            f"- {self.name}（{self.category}，市场：{markets}，"
            f"确定性引擎：{engine}）：\n{self.body.strip()}"
        )


@dataclass(frozen=True)
class MethodologySkillRegistry:
    """方法论技能注册表。"""

    skills: tuple[MethodologySkill, ...]

    def skill_names(self) -> list[str]:
        """返回已加载技能名称。"""

        return [skill.name for skill in self.skills]

    def for_role(self, role: str) -> list[MethodologySkill]:
        """按圆桌角色筛选技能。"""

        return [skill for skill in self.skills if skill.roundtable_role == role]


@lru_cache(maxsize=1)
def load_all_methodology_skills() -> MethodologySkillRegistry:
    """加载所有 P1 高频方法论技能。"""

    return MethodologySkillRegistry(
        skills=tuple(load_methodology_skill(name) for name in P1_METHODOLOGY_SKILL_NAMES)
    )


@lru_cache(maxsize=None)
def load_methodology_skill(name: str) -> MethodologySkill:
    """按目录名加载单个方法论技能。"""

    skill_path = Path(__file__).resolve().parent / name / "SKILL.md"
    text = skill_path.read_text(encoding="utf-8")
    metadata, body = parse_skill_markdown(text)
    return MethodologySkill(
        name=str(metadata["name"]),
        category=str(metadata["category"]),
        markets=list(metadata["markets"]),
        requires_engine=normalize_optional_string(metadata.get("requires_engine")),
        roundtable_role=str(metadata["roundtable_role"]),
        body=body,
        path=skill_path,
    )


def parse_skill_markdown(text: str) -> tuple[dict[str, Any], str]:
    """解析最小 YAML frontmatter，避免为技能元数据引入新依赖。"""

    if not text.startswith("---\n"):
        raise ValueError("SKILL.md 必须以 YAML frontmatter 开头。")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("SKILL.md 缺少 frontmatter 结束标记。")
    raw_meta = text[4:end]
    body = text[end + 5 :].strip()
    metadata: dict[str, Any] = {}
    for line in raw_meta.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition(":")
        if not key or not _:
            raise ValueError(f"无法解析技能元数据行：{line}")
        metadata[key.strip()] = parse_meta_value(value.strip())
    for required in ("name", "category", "markets", "roundtable_role"):
        if required not in metadata:
            raise ValueError(f"SKILL.md 缺少必填元数据：{required}")
    return metadata, body


def parse_meta_value(value: str) -> Any:
    """解析 frontmatter 中本项目使用的标量和短列表。"""

    if value in {"", "null", "None"}:
        return None
    if value.startswith("[") and value.endswith("]"):
        items = value[1:-1].strip()
        if not items:
            return []
        return [item.strip().strip("\"'") for item in items.split(",")]
    return value.strip("\"'")


def normalize_optional_string(value: Any) -> str | None:
    """把 null/空字符串归一化为 None。"""

    if value is None:
        return None
    text = str(value).strip()
    return text or None
