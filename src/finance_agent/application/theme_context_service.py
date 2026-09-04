"""题材上下文生产服务。

本服务把已入库的板块成员、行情、资金流和交易状态整理为候选池成员可消费的
`theme_context`。它只做确定性计算，不直接抓外部数据，也不让模型判断热门题材。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from finance_agent.application.leader_detection_service import (
    LeaderCandidateInput,
    LeaderDetectionService,
    LeaderRank,
)
from finance_agent.application.sector_opportunity_service import (
    SectorOpportunity,
    SectorOpportunityHistory,
    SectorOpportunityService,
)
from finance_agent.application.sector_strength_service import (
    SectorStrength,
    SectorStrengthInput,
    SectorStrengthService,
)
from finance_agent.storage.orm import (
    AssetORM,
    AssetStatusSnapshotORM,
    AssetUniverseMemberORM,
    AssetUniverseORM,
    CapitalFlowSnapshotORM,
    MarketBarORM,
)

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class ThemeContextInput:
    """单个资产在某个板块内的题材事实输入。"""

    sector_id: str
    asset_id: str
    sector_name: str | None = None
    asset_name: str | None = None
    pct_change: float | int | Decimal | None = None
    net_inflow: float | int | Decimal | None = None
    limit_up: bool = False
    popularity_rank: int | None = None
    board_hits: int = 0
    consecutive_limit_up: int = 0
    limit_up_time: str | None = None
    one_word_limit: bool = False
    suspended: bool = False
    returns_by_horizon: dict[int, float] = field(default_factory=dict)
    above_ma20: bool | None = None
    flow_positive_streak: int = 0
    breadth_change: float = 0.0
    valid_cross_sections: int = 1
    previous_sector_regime: str | None = None
    evidence_ids: list[str] = field(default_factory=list)

    def to_sector_input(self) -> SectorStrengthInput:
        """转换为板块强度服务输入。"""

        return SectorStrengthInput(
            sector_id=self.sector_id,
            sector_name=self.sector_name,
            asset_id=self.asset_id,
            asset_name=self.asset_name,
            pct_change=self.pct_change,
            net_inflow=self.net_inflow,
            limit_up=self.limit_up,
            popularity_rank=self.popularity_rank,
            board_hits=self.board_hits,
            returns_by_horizon=self.returns_by_horizon,
            above_ma20=self.above_ma20,
            flow_positive_streak=self.flow_positive_streak,
            breadth_change=self.breadth_change,
            valid_cross_sections=self.valid_cross_sections,
            evidence_ids=self.evidence_ids,
        )

    def to_leader_input(self) -> LeaderCandidateInput:
        """转换为龙头识别服务输入。"""

        return LeaderCandidateInput(
            sector_id=self.sector_id,
            asset_id=self.asset_id,
            asset_name=self.asset_name,
            pct_change=self.pct_change,
            net_inflow=self.net_inflow,
            limit_up_time=self.limit_up_time,
            consecutive_limit_up=self.consecutive_limit_up,
            one_word_limit=self.one_word_limit,
            suspended=self.suspended,
            evidence_ids=self.evidence_ids,
        )


@dataclass(frozen=True)
class ThemeContext:
    """单个资产的生产题材上下文。"""

    asset_id: str
    factor_groups: tuple[JsonDict, ...]
    sectors: tuple[JsonDict, ...]
    leadership: JsonDict | None
    evidence_ids: tuple[str, ...]

    def to_member_payload(self) -> JsonDict:
        """转换为候选池成员 payload 可嵌入的结构。"""

        return {
            "theme_context": {
                "source": "deterministic_theme_context_v1",
                "factor_groups": [dict(item) for item in self.factor_groups],
                "sectors": [dict(item) for item in self.sectors],
                "leadership": dict(self.leadership) if self.leadership else None,
                "evidence_ids": list(self.evidence_ids),
            }
        }


class ThemeContextService:
    """生成题材因子上下文，供推荐流水线注入因子计算。"""

    def __init__(
        self,
        session: Session | None = None,
        *,
        sector_strength_service: SectorStrengthService | None = None,
        leader_detection_service: LeaderDetectionService | None = None,
        sector_opportunity_service: SectorOpportunityService | None = None,
    ) -> None:
        self.session = session
        self.sector_strength_service = sector_strength_service or SectorStrengthService()
        self.leader_detection_service = leader_detection_service or LeaderDetectionService()
        self.sector_opportunity_service = sector_opportunity_service or SectorOpportunityService()

    def build_contexts(
        self,
        inputs: list[ThemeContextInput],
        *,
        strong_sector_limit: int = 20,
        market_regime: str = "range",
    ) -> dict[str, ThemeContext]:
        """根据已整理题材事实生成每个资产的上下文。"""

        if not inputs:
            return {}
        sector_strengths = self.sector_strength_service.rank_sectors(
            [item.to_sector_input() for item in inputs]
        )
        strong_sector_ids = [
            item.sector_id for item in sector_strengths[: max(1, strong_sector_limit)]
        ]
        strength_by_sector = {item.sector_id: item for item in sector_strengths}
        leaders = self.leader_detection_service.rank_leaders(
            [item.to_leader_input() for item in inputs],
            strong_sector_ids=strong_sector_ids,
        )
        opportunities = self._sector_opportunities(
            inputs,
            strengths=sector_strengths,
            leaders=leaders,
            market_regime=market_regime,
        )

        best_sector_by_asset: dict[str, SectorStrength] = {}
        for item in inputs:
            strength = strength_by_sector.get(item.sector_id)
            if strength is None or item.sector_id not in strong_sector_ids:
                continue
            current = best_sector_by_asset.get(item.asset_id)
            if current is None or strength.strength_score > current.strength_score:
                best_sector_by_asset[item.asset_id] = strength

        best_leader_by_asset: dict[str, LeaderRank] = {}
        for leader in leaders:
            current = best_leader_by_asset.get(leader.asset_id)
            if current is None or leader.leadership_score > current.leadership_score:
                best_leader_by_asset[leader.asset_id] = leader

        contexts: dict[str, ThemeContext] = {}
        for asset_id, sector in best_sector_by_asset.items():
            sector_group = sector.to_factor_group()
            opportunity = opportunities[sector.sector_id]
            sector_group["factors"] = {
                **dict(sector_group["factors"]),
                "sector_regime": opportunity.regime,
                "override_eligible": opportunity.override_eligible,
                "chase_risk": opportunity.chase_risk,
                "excess_returns": opportunity.excess_returns,
            }
            factor_groups = [sector_group]
            leader = best_leader_by_asset.get(asset_id)
            leadership_payload: JsonDict | None = None
            if leader is not None:
                leadership_group = leader.to_factor_group()
                factor_groups.append(leadership_group)
                leadership_payload = {
                    "sector_id": leader.sector_id,
                    "leader_rank": leader.leader_rank,
                    "role": leader.role,
                    "score": leader.leadership_score,
                    "buyability_warning": leader.buyability_warning,
                }
            evidence_ids = dedupe_evidence_ids(
                evidence_id
                for group in factor_groups
                for evidence_id in group.get("evidence_ids", [])
            )
            contexts[asset_id] = ThemeContext(
                asset_id=asset_id,
                factor_groups=tuple(factor_groups),
                sectors=(
                    {
                        "sector_id": sector.sector_id,
                        "sector_name": sector.sector_name,
                        "strength_score": sector.strength_score,
                        "sector_regime": opportunity.regime,
                        "override_eligible": opportunity.override_eligible,
                    },
                ),
                leadership=leadership_payload,
                evidence_ids=tuple(evidence_ids),
            )
        return contexts

    def _sector_opportunities(
        self,
        inputs: list[ThemeContextInput],
        *,
        strengths: list[SectorStrength],
        leaders: list[LeaderRank],
        market_regime: str,
    ) -> dict[str, SectorOpportunity]:
        """把强度、角色和多日输入压缩为板块生命周期。"""

        inputs_by_sector: dict[str, list[ThemeContextInput]] = {}
        for item in inputs:
            inputs_by_sector.setdefault(item.sector_id, []).append(item)
        leaders_by_sector: dict[str, list[LeaderRank]] = {}
        for item in leaders:
            leaders_by_sector.setdefault(item.sector_id, []).append(item)
        result: dict[str, SectorOpportunity] = {}
        for strength in strengths:
            members = inputs_by_sector.get(strength.sector_id, [])
            sector_leaders = leaders_by_sector.get(strength.sector_id, [])
            history = SectorOpportunityHistory(
                sector_id=strength.sector_id,
                excess_returns=strength.excess_returns,
                breadth=strength.breadth,
                ma20_ratio=strength.ma20_ratio,
                flow_streak=strength.flow_streak,
                leader_asset_ids=tuple(
                    item.asset_id for item in sector_leaders if item.role == "leader"
                ),
                challenger_asset_ids=tuple(
                    item.asset_id for item in sector_leaders if item.role == "challenger"
                ),
                breadth_change=(
                    sum(item.breadth_change for item in members) / len(members)
                    if members
                    else 0.0
                ),
                valid_cross_sections=max(
                    (item.valid_cross_sections for item in members),
                    default=1,
                ),
                previous_regime=next(
                    (
                        item.previous_sector_regime
                        for item in members
                        if item.previous_sector_regime
                    ),
                    None,
                ),
                evidence_ids=tuple(strength.evidence_ids),
            )
            result[strength.sector_id] = self.sector_opportunity_service.evaluate(
                history,
                market_regime=market_regime,
            )
        return result

    def build_for_members(
        self,
        members: list[AssetUniverseMemberORM],
        *,
        strong_sector_limit: int = 20,
        market_regime: str = "range",
        as_of: datetime | None = None,
    ) -> dict[str, ThemeContext]:
        """从数据库中的板块成员、行情和资金流为候选池成员生成题材上下文。"""

        if self.session is None or not members:
            return {}
        asset_ids = sorted({member.asset_id for member in members})
        if not asset_ids:
            return {}

        effective_as_of = as_of or datetime.now(tz=UTC)
        latest_bars = self._latest_daily_bars(asset_ids, as_of=effective_as_of)
        latest_flows = self._latest_capital_flows(asset_ids, as_of=effective_as_of)
        latest_status = self._latest_asset_status(asset_ids, as_of=effective_as_of)
        asset_names = self._asset_names(asset_ids)
        inputs = [
            self._theme_input_from_membership(
                membership=membership,
                universe=universe,
                asset_name=asset_names.get(membership.asset_id),
                bar=latest_bars.get(membership.asset_id),
                flow=latest_flows.get(membership.asset_id),
                status=latest_status.get(membership.asset_id),
            )
            for membership, universe in self._theme_memberships(asset_ids)
        ]
        return self.build_contexts(
            inputs,
            strong_sector_limit=strong_sector_limit,
            market_regime=market_regime,
        )

    def _theme_memberships(
        self,
        asset_ids: list[str],
    ) -> list[tuple[AssetUniverseMemberORM, AssetUniverseORM]]:
        """查询候选资产参与的行业/概念成员关系。"""

        statement = (
            select(AssetUniverseMemberORM, AssetUniverseORM)
            .join(
                AssetUniverseORM,
                AssetUniverseORM.universe_id == AssetUniverseMemberORM.universe_id,
            )
            .where(
                AssetUniverseMemberORM.asset_id.in_(asset_ids),
                AssetUniverseMemberORM.included.is_(True),
                AssetUniverseORM.market == "ashare",
            )
        )
        rows = list(self.session.execute(statement).all()) if self.session else []
        return [
            (membership, universe)
            for membership, universe in rows
            if is_theme_universe(universe)
        ]

    def _latest_daily_bars(
        self,
        asset_ids: list[str],
        *,
        as_of: datetime,
    ) -> dict[str, MarketBarORM]:
        """批量查询每个资产最新日 K。"""

        if self.session is None:
            return {}
        latest = (
            select(
                MarketBarORM.asset_id.label("asset_id"),
                func.max(MarketBarORM.timestamp).label("timestamp"),
            )
            .where(
                MarketBarORM.asset_id.in_(asset_ids),
                MarketBarORM.timeframe == "1d",
                MarketBarORM.timestamp <= as_of,
                MarketBarORM.is_closed.is_(True),
                MarketBarORM.status.in_(("available", "partial")),
            )
            .group_by(MarketBarORM.asset_id)
            .subquery()
        )
        statement = select(MarketBarORM).join(
            latest,
            and_(
                MarketBarORM.asset_id == latest.c.asset_id,
                MarketBarORM.timestamp == latest.c.timestamp,
            ),
        )
        return {row.asset_id: row for row in self.session.scalars(statement)}

    def _latest_capital_flows(
        self,
        asset_ids: list[str],
        *,
        as_of: datetime,
    ) -> dict[str, CapitalFlowSnapshotORM]:
        """批量查询每个资产最新资金流。"""

        if self.session is None:
            return {}
        latest = (
            select(
                CapitalFlowSnapshotORM.asset_id.label("asset_id"),
                func.max(CapitalFlowSnapshotORM.as_of).label("as_of"),
            )
            .where(
                CapitalFlowSnapshotORM.asset_id.in_(asset_ids),
                CapitalFlowSnapshotORM.as_of <= as_of,
            )
            .group_by(CapitalFlowSnapshotORM.asset_id)
            .subquery()
        )
        statement = select(CapitalFlowSnapshotORM).join(
            latest,
            and_(
                CapitalFlowSnapshotORM.asset_id == latest.c.asset_id,
                CapitalFlowSnapshotORM.as_of == latest.c.as_of,
            ),
        )
        return {row.asset_id: row for row in self.session.scalars(statement)}

    def _latest_asset_status(
        self,
        asset_ids: list[str],
        *,
        as_of: datetime,
    ) -> dict[str, AssetStatusSnapshotORM]:
        """批量查询每个资产最新交易状态。"""

        if self.session is None:
            return {}
        latest = (
            select(
                AssetStatusSnapshotORM.asset_id.label("asset_id"),
                func.max(AssetStatusSnapshotORM.as_of).label("as_of"),
            )
            .where(
                AssetStatusSnapshotORM.asset_id.in_(asset_ids),
                AssetStatusSnapshotORM.as_of <= as_of,
            )
            .group_by(AssetStatusSnapshotORM.asset_id)
            .subquery()
        )
        statement = select(AssetStatusSnapshotORM).join(
            latest,
            and_(
                AssetStatusSnapshotORM.asset_id == latest.c.asset_id,
                AssetStatusSnapshotORM.as_of == latest.c.as_of,
            ),
        )
        return {row.asset_id: row for row in self.session.scalars(statement)}

    def _asset_names(self, asset_ids: list[str]) -> dict[str, str]:
        """查询资产名称。"""

        if self.session is None:
            return {}
        statement = select(AssetORM.asset_id, AssetORM.name).where(AssetORM.asset_id.in_(asset_ids))
        return {asset_id: name for asset_id, name in self.session.execute(statement) if name}

    def _theme_input_from_membership(
        self,
        *,
        membership: AssetUniverseMemberORM,
        universe: AssetUniverseORM,
        asset_name: str | None,
        bar: MarketBarORM | None,
        flow: CapitalFlowSnapshotORM | None,
        status: AssetStatusSnapshotORM | None,
    ) -> ThemeContextInput:
        """把数据库行整理为题材事实输入。"""

        pct_change = daily_pct_change(bar)
        limit_up = bool(pct_change is not None and pct_change >= 9.8)
        payload = membership.payload or {}
        evidence_ids = dedupe_evidence_ids(
            [
                membership.id,
                getattr(bar, "raw_record_id", None),
                getattr(flow, "snapshot_id", None),
            ]
        )
        return ThemeContextInput(
            sector_id=universe.universe_id,
            sector_name=universe.name,
            asset_id=membership.asset_id,
            asset_name=asset_name,
            pct_change=pct_change,
            net_inflow=decimal_to_float(flow.main_net_inflow) if flow else None,
            limit_up=limit_up,
            popularity_rank=first_int(payload, "popularity_rank", "rank", "排名")
            or membership.rank_hint,
            board_hits=first_int(payload, "board_hits", "上榜次数") or (1 if limit_up else 0),
            consecutive_limit_up=first_int(payload, "consecutive_limit_up", "连板数", "连板高度")
            or (1 if limit_up else 0),
            limit_up_time=first_str(payload, "limit_up_time", "涨停时间"),
            one_word_limit=bool(payload.get("one_word_limit") or payload.get("一字涨停")),
            suspended=bool(status and status.trading_status in {"suspended", "停牌"}),
            returns_by_horizon=return_horizon_map(payload.get("returns_by_horizon")),
            above_ma20=optional_bool(payload.get("above_ma20")),
            flow_positive_streak=(
                first_int(dict(getattr(flow, "payload", {}) or {}), "positive_streak", "flow_streak")
                if flow is not None
                else None
            )
            or first_int(payload, "flow_positive_streak", "flow_streak")
            or 0,
            breadth_change=first_float(payload, "breadth_change") or 0.0,
            valid_cross_sections=first_int(payload, "valid_cross_sections") or 1,
            previous_sector_regime=first_str(payload, "previous_sector_regime"),
            evidence_ids=evidence_ids,
        )


def is_theme_universe(universe: AssetUniverseORM) -> bool:
    """判断候选池是否表示行业/概念板块。"""

    text = " ".join(
        str(value or "")
        for value in (
            universe.universe_id,
            universe.name,
            universe.source,
            universe.strategy_context,
        )
    ).lower()
    if "p0:all_a" in text:
        return False
    return any(token in text for token in ("concept", "industry", "概念", "行业"))


def daily_pct_change(bar: MarketBarORM | None) -> float | None:
    """由最新日 K 估算当日涨跌幅。"""

    if bar is None:
        return None
    open_price = decimal_to_float(bar.open)
    close_price = decimal_to_float(bar.close)
    if open_price is None or close_price is None or open_price == 0:
        return None
    return round((close_price - open_price) / open_price * 100, 6)


def decimal_to_float(value: Decimal | int | float | None) -> float | None:
    """把数据库数值转换为 float。"""

    if value is None:
        return None
    return float(value)


def first_int(payload: JsonDict, *keys: str) -> int | None:
    """从 payload 里按多个候选 key 提取整数。"""

    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def first_str(payload: JsonDict, *keys: str) -> str | None:
    """从 payload 里按多个候选 key 提取字符串。"""

    for key in keys:
        value = payload.get(key)
        if value:
            return str(value)
    return None


def first_float(payload: JsonDict, *keys: str) -> float | None:
    """从 payload 里按多个候选 key 提取浮点数。"""

    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def optional_bool(value: Any) -> bool | None:
    """解析可选布尔值，无法识别时保持缺失。"""

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return None


def return_horizon_map(value: Any) -> dict[int, float]:
    """规范化 1/3/5/10/20 日收益映射。"""

    if not isinstance(value, dict):
        return {}
    result: dict[int, float] = {}
    for raw_horizon, raw_return in value.items():
        try:
            horizon = int(raw_horizon)
            parsed = float(raw_return)
        except (TypeError, ValueError):
            continue
        if horizon in {1, 3, 5, 10, 20}:
            result[horizon] = parsed
    return result


def dedupe_evidence_ids(values: Any) -> list[str]:
    """按出现顺序清理证据 ID。"""

    result: list[str] = []
    for value in values:
        if not value:
            continue
        text = str(value)
        if text not in result:
            result.append(text)
    return result
