"""板块强度确定性服务。

本服务只消费已经入库或上游适配层整理好的结构化事实，不直接抓外部数据，
也不让模型参与板块排序。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from math import log10
from typing import Any

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class SectorStrengthInput:
    """单个板块成员的强度输入。"""

    sector_id: str
    asset_id: str
    sector_name: str | None = None
    asset_name: str | None = None
    pct_change: float | int | None = None
    net_inflow: float | int | None = None
    limit_up: bool = False
    popularity_rank: int | None = None
    board_hits: int = 0
    returns_by_horizon: dict[int, float] = field(default_factory=dict)
    above_ma20: bool | None = None
    flow_positive_streak: int = 0
    breadth_change: float = 0.0
    valid_cross_sections: int = 1
    evidence_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SectorStrength:
    """板块强度输出。"""

    sector_id: str
    sector_name: str
    strength_score: float
    member_count: int
    total_net_inflow: float
    average_pct_change: float
    limit_up_count: int
    continuity: int
    excess_returns: dict[int, float]
    breadth: float
    ma20_ratio: float
    flow_streak: int
    evidence_ids: list[str]
    payload: JsonDict

    def to_factor_group(self) -> JsonDict:
        """转换为 factor_frames 可保存的题材因子组。"""

        return {
            "group": "sector_strength",
            "score": self.strength_score,
            "status": "available",
            "factors": {
                "sector_id": self.sector_id,
                "sector_name": self.sector_name,
                "member_count": self.member_count,
                "total_net_inflow": self.total_net_inflow,
                "average_pct_change": self.average_pct_change,
                "limit_up_count": self.limit_up_count,
                "continuity": self.continuity,
                "excess_returns": self.excess_returns,
                "breadth": self.breadth,
                "ma20_ratio": self.ma20_ratio,
                "flow_streak": self.flow_streak,
            },
            "evidence_ids": self.evidence_ids,
        }


class SectorStrengthService:
    """按板块聚合成员表现，输出可审计强度排名。"""

    def rank_sectors(self, inputs: list[SectorStrengthInput]) -> list[SectorStrength]:
        """计算热门板块排名。"""

        buckets: dict[str, list[SectorStrengthInput]] = {}
        for item in inputs:
            sector_id = item.sector_id.strip()
            if not sector_id:
                continue
            buckets.setdefault(sector_id, []).append(item)

        strengths = [self._build_strength(sector_id, members) for sector_id, members in buckets.items()]
        strengths.sort(key=lambda item: item.strength_score, reverse=True)
        percentiles = strength_percentiles(strengths)
        return [
            replace(
                item,
                payload={**item.payload, "strength_percentile": percentiles[item.sector_id]},
            )
            for item in strengths
        ]

    def _build_strength(
        self,
        sector_id: str,
        members: list[SectorStrengthInput],
    ) -> SectorStrength:
        """聚合单个板块。"""

        sector_name = next((item.sector_name for item in members if item.sector_name), sector_id)
        total_net_inflow = sum(float(item.net_inflow or 0) for item in members)
        changes = [float(item.pct_change or 0) for item in members]
        average_pct_change = sum(changes) / len(changes) if changes else 0.0
        limit_up_count = sum(1 for item in members if item.limit_up)
        continuity = sum(max(int(item.board_hits or 0), 1 if item.limit_up else 0) for item in members)
        best_popularity = min(
            (int(item.popularity_rank) for item in members if item.popularity_rank),
            default=None,
        )
        evidence_ids = unique_evidence_ids(members)
        excess_returns = {
            horizon: round(sum(values) / len(values), 6)
            for horizon in (1, 3, 5, 10, 20)
            if (
                values := [
                    float(item.returns_by_horizon[horizon])
                    for item in members
                    if horizon in item.returns_by_horizon
                ]
            )
        }
        advancing = sum(
            1
            for item in members
            if float(item.returns_by_horizon.get(1, float(item.pct_change or 0) / 100)) > 0
        )
        breadth = advancing / len(members) if members else 0.0
        ma20_values = [item.above_ma20 for item in members if item.above_ma20 is not None]
        ma20_ratio = (
            sum(bool(value) for value in ma20_values) / len(ma20_values)
            if ma20_values
            else breadth
        )
        flow_streak = max((max(0, item.flow_positive_streak) for item in members), default=0)
        strength_score = compute_sector_strength_score(
            total_net_inflow=total_net_inflow,
            average_pct_change=average_pct_change,
            limit_up_count=limit_up_count,
            continuity=continuity,
            best_popularity=best_popularity,
        )
        top_assets = sorted(
            members,
            key=lambda item: (
                float(item.pct_change or 0),
                float(item.net_inflow or 0),
                -int(item.popularity_rank or 9999),
            ),
            reverse=True,
        )[:5]
        return SectorStrength(
            sector_id=sector_id,
            sector_name=sector_name or sector_id,
            strength_score=strength_score,
            member_count=len(members),
            total_net_inflow=total_net_inflow,
            average_pct_change=round(average_pct_change, 6),
            limit_up_count=limit_up_count,
            continuity=continuity,
            excess_returns=excess_returns,
            breadth=round(breadth, 6),
            ma20_ratio=round(ma20_ratio, 6),
            flow_streak=flow_streak,
            evidence_ids=evidence_ids,
            payload={
                "top_assets": [
                    {
                        "asset_id": item.asset_id,
                        "asset_name": item.asset_name,
                        "pct_change": float(item.pct_change or 0),
                        "net_inflow": float(item.net_inflow or 0),
                    }
                    for item in top_assets
                ],
                "best_popularity_rank": best_popularity,
                "breadth_change": round(
                    sum(item.breadth_change for item in members) / len(members),
                    6,
                ),
                "valid_cross_sections": max(
                    (item.valid_cross_sections for item in members),
                    default=1,
                ),
            },
        )


def compute_sector_strength_score(
    *,
    total_net_inflow: float,
    average_pct_change: float,
    limit_up_count: int,
    continuity: int,
    best_popularity: int | None,
) -> float:
    """把板块事实压缩成 0~100 的确定性强度分。"""

    flow_score = clamp(log10(max(total_net_inflow, 0) / 10_000_000 + 1) * 18, 0, 30)
    change_score = clamp(average_pct_change * 3, 0, 30)
    limit_score = clamp(limit_up_count * 10, 0, 20)
    continuity_score = clamp(continuity * 4, 0, 12)
    popularity_score = 0.0
    if best_popularity is not None:
        popularity_score = clamp((100 - best_popularity) / 100 * 8, 0, 8)
    return round(flow_score + change_score + limit_score + continuity_score + popularity_score, 6)


def unique_evidence_ids(members: list[SectorStrengthInput]) -> list[str]:
    """按出现顺序去重证据 ID。"""

    result: list[str] = []
    for item in members:
        for evidence_id in item.evidence_ids:
            if evidence_id not in result:
                result.append(evidence_id)
    return result


def strength_percentiles(strengths: list[SectorStrength]) -> dict[str, float]:
    """按当前截面计算板块强度分位，常数截面统一返回 50。"""

    if not strengths:
        return {}
    values = {item.strength_score for item in strengths}
    if len(values) == 1:
        return {item.sector_id: 50.0 for item in strengths}
    ascending = sorted(strengths, key=lambda item: (item.strength_score, item.sector_id))
    size = len(ascending)
    return {
        item.sector_id: round((index + 1) / size * 100, 6)
        for index, item in enumerate(ascending)
    }


def clamp(value: float, low: float, high: float) -> float:
    """裁剪数值区间。"""

    return max(low, min(high, value))
