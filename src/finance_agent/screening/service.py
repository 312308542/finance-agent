"""候选池初筛服务。

第一版只执行硬过滤：是否有因子快照、因子快照是否可用、可用因子组是否达到
最低要求，以及是否出现明确的严重风险。初筛不生成推荐理由。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.storage.orm import FactorFrameORM
from finance_agent.storage.repositories import (
    FactorFrameRepository,
    ScreeningRepository,
    UniverseRepository,
)

JsonDict = dict[str, Any]

RULE_VERSION = "screening_v1.0.0"


@dataclass(frozen=True)
class ScreeningRunResult:
    """一次候选池初筛摘要。"""

    status: str
    screening_id: str
    universe_id: str
    strategy: str
    market: str
    passed_count: int
    removed_count: int


class ScreeningService:
    """对候选池成员执行第一版硬过滤。"""

    def __init__(self, session: Session) -> None:
        self.universes = UniverseRepository(session)
        self.factors = FactorFrameRepository(session)
        self.screenings = ScreeningRepository(session)

    def apply_rules(
        self,
        *,
        universe_id: str,
        strategy: str = "balanced_swing_v1",
        horizon: str = "swing",
        min_available_groups: int = 1,
        rule_version: str = RULE_VERSION,
    ) -> ScreeningRunResult:
        """执行候选池初筛并落库。"""

        universe = self.universes.get_universe(universe_id)
        members = self.universes.list_members(universe_id)
        as_of = datetime.now(tz=UTC)
        screening_id = build_screening_id(
            universe_id=universe_id,
            strategy=strategy,
            horizon=horizon,
            as_of=as_of,
        )
        passed_count = 0
        removed_count = 0
        item_summaries: list[JsonDict] = []

        for member in members:
            factor = self.factors.get_latest_factor_frame(
                asset_id=member.asset_id,
                horizon=horizon,
            )
            decision = evaluate_member(
                factor=factor,
                min_available_groups=min_available_groups,
            )
            if decision["passed"]:
                passed_count += 1
            else:
                removed_count += 1
            item_id = f"{screening_id}:{member.asset_id}"
            self.screenings.upsert_screening_item(
                screening_item_id=item_id,
                screening_id=screening_id,
                universe_id=universe_id,
                asset_id=member.asset_id,
                symbol=member.symbol,
                market=member.market,
                passed=decision["passed"],
                removed_reason=decision["removed_reason"],
                failed_rules=decision["failed_rules"],
                passed_rules=decision["passed_rules"],
                data_status=decision["data_status"],
                liquidity_status=decision["liquidity_status"],
                as_of=as_of,
                payload={
                    "rule_version": rule_version,
                    "factor_frame_id": factor.factor_frame_id if factor else None,
                    "available_groups": factor.total_available_groups if factor else 0,
                    "missing_groups": factor.missing_groups if factor else ["factor_frame"],
                },
            )
            item_summaries.append(
                {
                    "asset_id": member.asset_id,
                    "symbol": member.symbol,
                    "passed": decision["passed"],
                    "failed_rules": decision["failed_rules"],
                    "removed_reason": decision["removed_reason"],
                }
            )

        status = "available" if members else "unavailable"
        saved = self.screenings.upsert_screening_result(
            screening_id=screening_id,
            universe_id=universe_id,
            strategy=strategy,
            market=universe.market,
            passed_count=passed_count,
            removed_count=removed_count,
            rules={
                "rule_version": rule_version,
                "horizon": horizon,
                "min_available_groups": min_available_groups,
                "hard_rules": [
                    "factor_frame_exists",
                    "factor_frame_available",
                    "available_groups_min",
                    "critical_risk_block",
                ],
            },
            status=status,
            as_of=as_of,
            payload={
                "schema_version": "1.0",
                "items": item_summaries,
            },
        )
        return ScreeningRunResult(
            status=saved.status,
            screening_id=saved.screening_id,
            universe_id=saved.universe_id,
            strategy=saved.strategy,
            market=saved.market,
            passed_count=saved.passed_count,
            removed_count=saved.removed_count,
        )


def evaluate_member(
    *,
    factor: FactorFrameORM | None,
    min_available_groups: int,
) -> JsonDict:
    """对单个候选标的执行硬过滤。"""

    passed_rules: list[str] = []
    failed_rules: list[str] = []
    removed_reasons: list[str] = []

    if factor is None:
        return {
            "passed": False,
            "removed_reason": "缺少因子快照",
            "failed_rules": ["factor_frame_exists"],
            "passed_rules": [],
            "data_status": "missing_factor_frame",
            "liquidity_status": "unknown",
        }

    passed_rules.append("factor_frame_exists")
    if factor.status == "unavailable":
        failed_rules.append("factor_frame_available")
        removed_reasons.append("因子快照不可用")
    else:
        passed_rules.append("factor_frame_available")

    if factor.total_available_groups < min_available_groups:
        failed_rules.append("available_groups_min")
        removed_reasons.append(f"可用因子组少于 {min_available_groups} 个")
    else:
        passed_rules.append("available_groups_min")

    if has_critical_risk(factor):
        failed_rules.append("critical_risk_block")
        removed_reasons.append("存在严重风险因子")
    else:
        passed_rules.append("critical_risk_block")

    return {
        "passed": not failed_rules,
        "removed_reason": "；".join(removed_reasons) if removed_reasons else None,
        "failed_rules": failed_rules,
        "passed_rules": passed_rules,
        "data_status": factor.status,
        "liquidity_status": liquidity_status(factor),
    }


def has_critical_risk(factor: FactorFrameORM) -> bool:
    """判断是否存在硬阻断风险。"""

    risk_group = find_group(factor, "risk")
    if not risk_group:
        return False
    factors = risk_group.get("factors") or {}
    risk_penalty = factors.get("risk_penalty")
    return isinstance(risk_penalty, int | float) and risk_penalty >= 80


def liquidity_status(factor: FactorFrameORM) -> str:
    """输出轻量流动性状态。"""

    technical = find_group(factor, "technical")
    if not technical:
        return "unknown"
    factors = technical.get("factors") or {}
    if factors.get("amount_avg_20d") is None and factors.get("volatility_20d") is None:
        return "partial"
    return "available"


def find_group(factor: FactorFrameORM, group_name: str) -> JsonDict | None:
    """从因子快照中查找指定因子组。"""

    for group in factor.payload.get("factor_groups") or []:
        if group.get("group") == group_name:
            return group
    return None


def build_screening_id(
    *,
    universe_id: str,
    strategy: str,
    horizon: str,
    as_of: datetime,
) -> str:
    """生成稳定初筛 ID。"""

    normalized_time = as_of.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"screen:{universe_id}:{strategy}:{horizon}:{normalized_time}"
