"""推荐设置和生命周期状态的 SQLAlchemy 仓储。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from finance_agent.recommendations.lifecycle import RecommendationTransition, StockSetup
from finance_agent.storage.orm import (
    RecommendationLifecycleEventORM,
    RecommendationLifecycleStateORM,
    StockSetupORM,
)

JsonDict = dict[str, Any]


class RecommendationStateRepository:
    """维护唯一当前状态，并把实际迁移追加为不可变事件。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_state(
        self,
        *,
        owner_id: str,
        strategy_id: str,
        asset_id: str,
    ) -> RecommendationLifecycleStateORM | None:
        """按用户、策略和资产读取唯一当前状态。"""

        statement = select(RecommendationLifecycleStateORM).where(
            RecommendationLifecycleStateORM.owner_id == owner_id,
            RecommendationLifecycleStateORM.strategy_id == strategy_id,
            RecommendationLifecycleStateORM.asset_id == asset_id,
        )
        return self.session.scalars(statement).one_or_none()

    def save_setup(self, setup: StockSetup) -> StockSetupORM:
        """幂等保存绑定决策快照的股票设置。"""

        values = {
            "setup_id": setup.setup_id,
            "owner_id": setup.owner_id,
            "decision_snapshot_id": setup.decision_snapshot_id,
            "asset_id": setup.asset_id,
            "strategy_id": setup.strategy_id,
            "setup_type": setup.setup_type,
            "planned_horizon_days": setup.planned_horizon_days,
            "entry_zone": _json_safe(setup.entry_zone),
            "invalidation_price": setup.invalidation_price,
            "target_zone": _json_safe(setup.target_zone),
            "expected_net_return": setup.expected_net_return,
            "downside_risk": setup.downside_risk,
            "confidence": setup.confidence,
            "as_of": setup.as_of,
            "payload": _json_safe(setup.payload),
        }
        statement = insert(StockSetupORM).values(**values).on_conflict_do_nothing(
            index_elements=[StockSetupORM.setup_id]
        )
        self.session.execute(statement)
        self.session.flush()
        return self.session.get_one(StockSetupORM, setup.setup_id)

    def save_transition(
        self,
        transition: RecommendationTransition,
    ) -> RecommendationLifecycleStateORM:
        """刷新当前状态，仅在状态或原因变化时追加审计事件。"""

        current = self.get_state(
            owner_id=transition.owner_id,
            strategy_id=transition.strategy_id,
            asset_id=transition.asset_id,
        )
        current_payload = dict(current.payload or {}) if current is not None else {}
        last_reason_codes = tuple(current_payload.get("last_reason_codes", ()))
        should_append_event = (
            current is None
            or current.current_state != transition.to_state
            or last_reason_codes != transition.reason_codes
        )
        state_id = current.state_id if current is not None else transition.state_id
        state_payload = dict(transition.payload)
        state_payload["last_reason_codes"] = list(transition.reason_codes)
        if current is not None and not should_append_event:
            previous_state = current.previous_state
            state_changed_at = current.state_changed_at
        else:
            previous_state = transition.from_state
            state_changed_at = transition.occurred_at

        state_values = {
            "state_id": state_id,
            "owner_id": transition.owner_id,
            "strategy_id": transition.strategy_id,
            "asset_id": transition.asset_id,
            "setup_id": transition.setup_id,
            "current_state": transition.to_state,
            "previous_state": previous_state,
            "decision_snapshot_id": transition.decision_snapshot_id,
            "state_changed_at": state_changed_at,
            "consecutive_valid_closes": transition.consecutive_valid_closes,
            "active_days": transition.active_days,
            "cooldown_until": transition.cooldown_until,
            "payload": _json_safe(state_payload),
            "updated_at": transition.occurred_at,
        }
        state_statement = insert(RecommendationLifecycleStateORM).values(**state_values)
        self.session.execute(
            state_statement.on_conflict_do_update(
                constraint="uq_recommendation_lifecycle_owner_strategy_asset",
                set_={
                    key: state_statement.excluded[key]
                    for key in state_values
                    if key not in {"state_id", "owner_id", "strategy_id", "asset_id"}
                },
            )
        )
        if should_append_event:
            event_values = {
                "event_id": transition.event_id,
                "state_id": state_id,
                "owner_id": transition.owner_id,
                "strategy_id": transition.strategy_id,
                "asset_id": transition.asset_id,
                "setup_id": transition.setup_id,
                "from_state": transition.from_state,
                "to_state": transition.to_state,
                "reason_codes": list(transition.reason_codes),
                "decision_snapshot_id": transition.decision_snapshot_id,
                "occurred_at": transition.occurred_at,
                "payload": _json_safe(transition.payload),
            }
            self.session.execute(
                insert(RecommendationLifecycleEventORM)
                .values(**event_values)
                .on_conflict_do_nothing(
                    index_elements=[RecommendationLifecycleEventORM.event_id]
                )
            )
        self.session.flush()
        if current is not None and hasattr(self.session, "expire"):
            self.session.expire(current)
        return self.session.get_one(RecommendationLifecycleStateORM, state_id)

    def list_events(
        self,
        state_id: str,
        *,
        limit: int = 100,
    ) -> tuple[RecommendationLifecycleEventORM, ...]:
        """按发生时间升序读取一份状态的追加事件。"""

        statement = (
            select(RecommendationLifecycleEventORM)
            .where(RecommendationLifecycleEventORM.state_id == state_id)
            .order_by(
                RecommendationLifecycleEventORM.occurred_at,
                RecommendationLifecycleEventORM.event_id,
            )
            .limit(max(1, int(limit)))
        )
        return tuple(self.session.scalars(statement))


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, Decimal | datetime | date):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_safe(item) for item in value]
    return str(value)
