"""推荐设置、当前状态和状态迁移的数据契约。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

JsonDict = dict[str, Any]
RecommendationStateName = Literal[
    "discovered",
    "watch",
    "setup_confirming",
    "buy_ready",
    "active",
    "weakening",
    "exit_pending",
    "exited",
    "cooldown",
]
RECOMMENDATION_STATES: tuple[RecommendationStateName, ...] = (
    "discovered",
    "watch",
    "setup_confirming",
    "buy_ready",
    "active",
    "weakening",
    "exit_pending",
    "exited",
    "cooldown",
)


@dataclass(frozen=True)
class StockSetup:
    """绑定统一决策快照的单股交易设置。"""

    setup_id: str
    owner_id: str
    decision_snapshot_id: str
    asset_id: str
    strategy_id: str
    setup_type: str
    planned_horizon_days: int
    entry_zone: JsonDict
    invalidation_price: Decimal | None
    target_zone: JsonDict
    expected_net_return: Decimal | None
    downside_risk: Decimal | None
    confidence: Decimal
    as_of: datetime
    payload: JsonDict


@dataclass(frozen=True)
class RecommendationState:
    """一个用户、策略和资产唯一的一份当前生命周期状态。"""

    state_id: str
    owner_id: str
    strategy_id: str
    asset_id: str
    setup_id: str | None
    current_state: RecommendationStateName
    previous_state: RecommendationStateName | None
    decision_snapshot_id: str
    state_changed_at: datetime
    consecutive_valid_closes: int
    active_days: int
    cooldown_until: date | None
    payload: JsonDict


@dataclass(frozen=True)
class RecommendationTransition:
    """一次可审计且可幂等重放的生命周期迁移。"""

    event_id: str
    state_id: str
    owner_id: str
    strategy_id: str
    asset_id: str
    setup_id: str | None
    from_state: RecommendationStateName | None
    to_state: RecommendationStateName
    reason_codes: tuple[str, ...]
    decision_snapshot_id: str
    occurred_at: datetime
    consecutive_valid_closes: int
    active_days: int
    cooldown_until: date | None
    payload: JsonDict
