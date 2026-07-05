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

P2_METHODOLOGY_SKILL_NAMES: tuple[str, ...] = (
    "ichimoku",
    "minute-analysis",
    "market-microstructure",
    "pair-trading",
    "correlation-analysis",
    "quant-statistics",
    "performance-attribution",
    "earnings-forecast",
    "earnings-revision",
    "dividend-analysis",
    "macro-analysis",
    "global-macro",
    "regulatory-knowledge",
    "asset-allocation",
    "etf-analysis",
    "fund-analysis",
    "convertible-bond",
    "liquidation-heatmap",
    "social-media-intelligence",
    "stablecoin-flow",
    "token-unlock-treasury",
    "chanlun-interpret",
)

P3_METHODOLOGY_SKILL_NAMES: tuple[str, ...] = (
    "elliott-wave",
    "harmonic",
    "smc",
    "seasonal",
    "credit-analysis",
    "geopolitical-risk",
    "hedging-strategy",
    "cross-market-strategy",
    "onchain-analysis",
    "defi-yield",
)

L1_METHODOLOGY_SKILL_NAMES: tuple[str, ...] = (
    "smc",
    "harmonic",
    "elliott-wave",
    "chanlun-interpret",
)

SKILL_EXCERPT_MAX_CHARS = 1200


@dataclass(frozen=True)
class EngineCapability:
    """确定性引擎能力的静态声明。"""

    key: str
    available: bool
    description: str


ENGINE_CAPABILITIES: dict[str, EngineCapability] = {
    "structural_lite_smc": EngineCapability(
        key="structural_lite_smc",
        available=True,
        description="structural-lite v2 SMC/BOS/CHoCH/FVG 已接入生产调度",
    ),
    "structural_lite_harmonic": EngineCapability(
        key="structural_lite_harmonic",
        available=True,
        description="structural-lite v2 harmonic_lite_v2 已接入生产调度",
    ),
    "structural_lite_elliott": EngineCapability(
        key="structural_lite_elliott",
        available=True,
        description="structural-lite v2 elliott_lite_v2 已接入生产调度",
    ),
    "structural_lite_swings": EngineCapability(
        key="structural_lite_swings",
        available=True,
        description="structural-lite v2 swing 结构基座已接入生产调度",
    ),
    "talib_cdl": EngineCapability(
        key="talib_cdl",
        available=True,
        description="TA-Lib K 线形态指标已接入日级指标体系",
    ),
    "chanlun": EngineCapability(
        key="chanlun",
        available=False,
        description="czsc 真实依赖未批准，ChanlunAdapter 保持 fail-closed",
    ),
    "ichimoku": EngineCapability(
        key="ichimoku",
        available=False,
        description="IchimokuAdapter 已实现但暂无生产调度/入库消费",
    ),
    "correlation": EngineCapability(
        key="correlation",
        available=False,
        description="CorrelationAdapter 暂无生产调度/入库消费",
    ),
    "pair_trading": EngineCapability(
        key="pair_trading",
        available=False,
        description="PairTradingAdapter 暂无生产调度/入库消费",
    ),
    "seasonal": EngineCapability(
        key="seasonal",
        available=False,
        description="SeasonalAdapter 暂无生产调度/入库消费",
    ),
}


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

    def prompt_excerpt(self, *, max_chars: int = SKILL_EXCERPT_MAX_CHARS) -> str:
        """转换为可注入圆桌角色 Prompt 的短文本。"""

        engine = self.requires_engine or "无，纯知识解读"
        markets = "、".join(self.markets)
        prefix = (
            f"- {self.name}（{self.category}，市场：{markets}，"
            f"确定性引擎：{engine}）：\n"
        )
        text = f"{prefix}{self.body.strip()}"
        if len(text) <= max_chars:
            return text
        compact_body = "\n\n".join(
            section
            for section in (
                extract_markdown_section(self.body, "## 解读口径"),
                extract_markdown_section(self.body, "## 失效条件"),
            )
            if section
        )
        compact = f"{prefix}{compact_body or self.body.strip()}"
        if len(compact) <= max_chars:
            return compact
        suffix = "\n...<truncated>"
        return f"{compact[: max(0, max_chars - len(suffix))]}{suffix}"


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
    """加载默认活跃方法论技能。

    已弃用：保留旧函数名兼容历史调用；新代码请使用
    `load_active_methodology_skills()`。
    """

    return load_active_methodology_skills()


def load_active_methodology_skills(
    *,
    capabilities: dict[str, EngineCapability] | None = None,
) -> MethodologySkillRegistry:
    """加载默认活跃技能：P1 + capability 放行的 L1 扩展技能。"""

    capability_map = capabilities or ENGINE_CAPABILITIES
    names = unique_skill_names((*P1_METHODOLOGY_SKILL_NAMES, *L1_METHODOLOGY_SKILL_NAMES))
    skills = tuple(
        skill
        for skill in (load_methodology_skill(name) for name in names)
        if is_skill_capability_available(skill, capability_map)
    )
    return MethodologySkillRegistry(skills=skills)


def is_skill_capability_available(
    skill: MethodologySkill,
    capabilities: dict[str, EngineCapability],
) -> bool:
    """判断技能依赖的确定性引擎能力是否可用。"""

    if skill.requires_engine is None:
        return True
    return capabilities.get(skill.requires_engine, EngineCapability(skill.requires_engine, False, "未登记")).available


def unique_skill_names(names: tuple[str, ...]) -> tuple[str, ...]:
    """保持顺序去重，避免同一技能重复注入 Prompt。"""

    seen: set[str] = set()
    result: list[str] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        result.append(name)
    return tuple(result)


def load_methodology_skills(names: tuple[str, ...]) -> MethodologySkillRegistry:
    """按名称列表加载方法论技能。"""

    return MethodologySkillRegistry(skills=tuple(load_methodology_skill(name) for name in names))


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


def extract_markdown_section(body: str, title: str) -> str:
    """提取指定二级标题下的内容，用于控制 prompt 注入体量。"""

    start = body.find(title)
    if start < 0:
        return ""
    tail = body[start:]
    next_title = tail.find("\n## ", len(title))
    if next_title >= 0:
        tail = tail[:next_title]
    return tail.strip()
