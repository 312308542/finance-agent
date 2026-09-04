"""龙头识别确定性服务。

服务只根据入库后的板块成员表现、资金流、涨停结构和交易限制信号排序，
不让模型凭空挑选热门板块或龙头。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import log10
from typing import Any

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class LeaderCandidateInput:
    """单个板块成员的龙头候选输入。"""

    sector_id: str
    asset_id: str
    asset_name: str | None = None
    pct_change: float | int | None = None
    net_inflow: float | int | None = None
    limit_up_time: str | None = None
    consecutive_limit_up: int = 0
    one_word_limit: bool = False
    suspended: bool = False
    evidence_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LeaderRank:
    """龙头识别输出。"""

    asset_id: str
    sector_id: str
    leader_rank: int
    role: str
    leadership_score: float
    consecutive_limit_up: int
    relative_strength: float
    buyability_warning: str
    evidence_ids: list[str]
    payload: JsonDict

    def to_factor_group(self) -> JsonDict:
        """转换为 factor_frames 可保存的龙头因子组。"""

        return {
            "group": "leadership",
            "score": self.leadership_score,
            "status": "available",
            "factors": {
                "sector_id": self.sector_id,
                "leader_rank": self.leader_rank,
                "role": self.role,
                "consecutive_limit_up": self.consecutive_limit_up,
                "relative_strength": self.relative_strength,
                "buyability_warning": self.buyability_warning,
            },
            "evidence_ids": self.evidence_ids,
        }


class LeaderDetectionService:
    """在强板块内识别龙头、挑战者和跟风标的。"""

    def rank_leaders(
        self,
        candidates: list[LeaderCandidateInput],
        *,
        strong_sector_ids: list[str],
    ) -> list[LeaderRank]:
        """按板块和强度排序龙头候选。"""

        strong = {sector_id for sector_id in strong_sector_ids if sector_id}
        filtered = [item for item in candidates if item.sector_id in strong]
        result: list[LeaderRank] = []
        for sector_id in sorted(strong):
            scored = [
                (compute_leadership_score(item), item)
                for item in filtered
                if item.sector_id == sector_id
            ]
            scored.sort(key=lambda item: (-item[0], item[1].asset_id))
            for index, (score, item) in enumerate(scored, start=1):
                result.append(
                    LeaderRank(
                        asset_id=item.asset_id,
                        sector_id=item.sector_id,
                        leader_rank=index,
                        role=leader_role(index),
                        leadership_score=score,
                        consecutive_limit_up=max(int(item.consecutive_limit_up or 0), 0),
                        relative_strength=round(float(item.pct_change or 0), 6),
                        buyability_warning=buyability_warning(item),
                        evidence_ids=dedupe(item.evidence_ids),
                        payload={
                            "asset_name": item.asset_name,
                            "limit_up_time": item.limit_up_time,
                            "unbuyable_reasons": unbuyable_reasons(item),
                            "net_inflow": float(item.net_inflow or 0),
                        },
                    )
                )
        result.sort(key=lambda item: (-item.leadership_score, item.sector_id, item.asset_id))
        return result


def compute_leadership_score(item: LeaderCandidateInput) -> float:
    """计算 0~100 龙头强度分。"""

    pct_score = clamp(float(item.pct_change or 0) * 2.5, 0, 35)
    flow_score = clamp(log10(max(float(item.net_inflow or 0), 0) / 10_000_000 + 1) * 14, 0, 25)
    limit_score = clamp(max(int(item.consecutive_limit_up or 0), 0) * 10, 0, 30)
    time_score = 0.0
    if item.limit_up_time:
        time_score = clamp((11 * 60 + 30 - parse_hhmm_seconds(item.limit_up_time) / 60) / 150 * 10, 0, 10)
    return round(pct_score + flow_score + limit_score + time_score, 6)


def leader_role(rank: int) -> str:
    """按位次标注题材地位。"""

    if rank == 1:
        return "leader"
    if rank == 2:
        return "challenger"
    return "follower"


def buyability_warning(item: LeaderCandidateInput) -> str:
    """根据交易限制标注可买入性。"""

    return "unbuyable" if unbuyable_reasons(item) else "tradable"


def unbuyable_reasons(item: LeaderCandidateInput) -> list[str]:
    """列出不可买原因。"""

    reasons: list[str] = []
    if item.one_word_limit:
        reasons.append("one_word_limit")
    if item.suspended:
        reasons.append("suspended")
    return reasons


def parse_hhmm_seconds(value: str) -> int:
    """解析 HH:MM[:SS] 为秒数，解析失败时视为较晚涨停。"""

    try:
        parts = [int(part) for part in value.split(":")]
    except ValueError:
        return 15 * 60 * 60
    if len(parts) == 2:
        hour, minute = parts
        second = 0
    elif len(parts) == 3:
        hour, minute, second = parts
    else:
        return 15 * 60 * 60
    return hour * 3600 + minute * 60 + second


def dedupe(values: list[str]) -> list[str]:
    """按出现顺序去重。"""

    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def clamp(value: float, low: float, high: float) -> float:
    """裁剪数值区间。"""

    return max(low, min(high, value))
