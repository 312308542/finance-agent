"""持仓监控状态与事件仓储。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from finance_agent.monitoring.models import PositionAction, PositionMonitoringState
from finance_agent.storage.orm import PositionMonitoringEventORM, PositionMonitoringStateORM


class PositionMonitoringRepository:
    """提供数据库实现，同时支持无数据库的纯内存测试适配。"""

    def __init__(self, session: Session | None = None) -> None:
        self.session = session
        self._states: dict[str, PositionMonitoringState] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}

    def get_state(self, position_id: str) -> PositionMonitoringStateORM | PositionMonitoringState | None:
        """读取一个持仓的当前状态。"""

        if self.session is None:
            return self._states.get(position_id)
        statement = select(PositionMonitoringStateORM).where(PositionMonitoringStateORM.position_id == position_id)
        return self.session.scalars(statement).one_or_none()

    def list_active_states(self, owner_id: str) -> tuple[Any, ...]:
        """读取用户所有仍有数量的监控状态。"""

        if self.session is None:
            return tuple(
                state for state in self._states.values() if state.owner_id == owner_id and state.total_quantity > 0
            )
        statement = select(PositionMonitoringStateORM).where(
            PositionMonitoringStateORM.owner_id == owner_id,
            PositionMonitoringStateORM.total_quantity > 0,
        )
        return tuple(self.session.scalars(statement))

    def list_states_by_position_ids(self, position_ids: Sequence[str]) -> tuple[Any, ...]:
        """按持仓 ID 批量读取监控状态，避免 Dashboard 逐仓查询。"""

        normalized = tuple(dict.fromkeys(str(item) for item in position_ids if item))
        if not normalized:
            return ()
        if self.session is None:
            return tuple(self._states[item] for item in normalized if item in self._states)
        statement = select(PositionMonitoringStateORM).where(PositionMonitoringStateORM.position_id.in_(normalized))
        return tuple(self.session.scalars(statement))

    def save(self, action: PositionAction, *, state: PositionMonitoringState | None = None) -> Any:
        """更新当前动作，并仅在动作或原因变化时追加事件。"""

        current = self.get_state(action.position_id)
        base = state or _state_from_row(current, action)
        previous_action = getattr(current, "current_action", None) if current is not None else None
        previous_reasons = tuple((getattr(current, "payload", {}) or {}).get("reason_codes", ())) if current else ()
        changed = previous_action != action.action or previous_reasons != action.reason_codes
        next_state = _apply_action(base, action)
        if self.session is None:
            self._states[action.position_id] = next_state
            if changed:
                evaluated_at = action.evaluated_at.isoformat() if action.evaluated_at else "unknown"
                event_id = f"monitoring-event:{action.position_id}:{evaluated_at}:{action.action}"
                self._events.setdefault(action.position_id, []).append(
                    {"event_id": event_id, **action.to_dict(), "event_type": "action_changed"}
                )
            return next_state
        values = _orm_values(next_state, action)
        statement = insert(PositionMonitoringStateORM).values(**values)
        self.session.execute(
            statement.on_conflict_do_update(
                constraint="uq_position_monitoring_position",
                set_={key: statement.excluded[key] for key in values if key != "position_id"},
            )
        )
        if changed:
            event_values = {
                "event_id": (
                    f"monitoring-event:{action.position_id}:"
                    f"{action.evaluated_at.isoformat() if action.evaluated_at else 'unknown'}:"
                    f"{action.action}"
                ),
                "monitoring_state_id": values["monitoring_state_id"],
                "position_id": action.position_id,
                "event_type": "action_changed",
                "severity": action.severity,
                "action": action.action,
                "reason_codes": list(action.reason_codes),
                "quote_snapshot_id": action.quote_snapshot_id or None,
                "decision_snapshot_id": action.decision_snapshot_id,
                "occurred_at": action.evaluated_at or datetime.now().astimezone(),
                "payload": action.payload,
            }
            self.session.execute(
                insert(PositionMonitoringEventORM)
                .values(**event_values)
                .on_conflict_do_nothing(index_elements=[PositionMonitoringEventORM.event_id])
            )
        self.session.flush()
        return self.get_state(action.position_id)

    def apply_execution(self, **kwargs: Any) -> Any:
        """把外部执行登记同步为当前监控状态，不代表系统自动下单。"""

        position = kwargs["position"]
        action_name = str(kwargs.get("action") or "buy")
        payload = dict(getattr(position, "payload", {}) or {})
        payload.update(
            {
                "owner_id": kwargs.get("owner_id"),
                "portfolio_id": kwargs.get("portfolio_id"),
                "asset_id": kwargs.get("asset_id"),
                "symbol": getattr(position, "symbol", ""),
                "sellable_quantity": str(kwargs.get("sellable_quantity", "0")),
                "opened_on": (
                    kwargs.get("opened_on").isoformat()
                    if hasattr(kwargs.get("opened_on"), "isoformat")
                    else kwargs.get("opened_on")
                ),
            }
        )
        state = PositionMonitoringState(
            position_id=str(position.position_id),
            owner_id=str(kwargs.get("owner_id") or "default-owner"),
            portfolio_id=str(kwargs.get("portfolio_id") or position.portfolio_id),
            asset_id=str(kwargs.get("asset_id") or position.asset_id),
            symbol=str(getattr(position, "symbol", "")),
            market=str(getattr(position, "market", "ashare")),
            total_quantity=Decimal(str(kwargs.get("total_quantity", position.quantity))),
            sellable_quantity=Decimal(str(kwargs.get("sellable_quantity", "0"))),
            opened_on=kwargs.get("opened_on"),
            current_action="watch" if action_name in {"buy", "add"} and not payload.get("setup_id") else "hold",
            previous_valid_action="hold",
            setup_id=payload.get("setup_id"),
            planned_horizon_days=int(payload.get("planned_horizon_days", 10) or 10),
            invalidation_price=_decimal(payload.get("invalidation_price")),
            protective_price=_decimal(payload.get("protective_price")),
            payload=payload,
        )
        if self.session is None:
            self._states[state.position_id] = state
            return state
        values = _orm_values(
            state,
            PositionAction(
                position_id=state.position_id,
                action=state.current_action,
                reason_codes=("execution_registered",),
                evaluated_at=getattr(kwargs.get("execution"), "executed_at", None),
                payload=payload,
            ),
        )
        statement = insert(PositionMonitoringStateORM).values(**values)
        self.session.execute(
            statement.on_conflict_do_update(
                constraint="uq_position_monitoring_position",
                set_={key: statement.excluded[key] for key in values if key != "position_id"},
            )
        )
        self.session.flush()
        return self.get_state(state.position_id)

    def list_events(self, monitoring_state_id: str, limit: int = 100) -> tuple[Any, ...]:
        """按时间倒序读取监控事件。"""

        if self.session is None:
            position_id = monitoring_state_id
            return tuple(self._events.get(position_id, ())[-max(1, int(limit)) :])
        statement = (
            select(PositionMonitoringEventORM)
            .where(PositionMonitoringEventORM.monitoring_state_id == monitoring_state_id)
            .order_by(PositionMonitoringEventORM.occurred_at.desc())
            .limit(max(1, int(limit)))
        )
        return tuple(self.session.scalars(statement))


def _state_from_row(row: Any, action: PositionAction) -> PositionMonitoringState:
    if isinstance(row, PositionMonitoringState):
        return row
    payload = dict(getattr(row, "payload", {}) or {})
    payload.update(action.payload)
    return PositionMonitoringState(
        position_id=action.position_id,
        owner_id=str(payload.get("owner_id", "default-owner")),
        portfolio_id=str(payload.get("portfolio_id", "")),
        asset_id=str(payload.get("asset_id", "")),
        symbol=str(payload.get("symbol", "")),
        market=str(payload.get("market", "ashare")),
        total_quantity=Decimal(str(getattr(row, "total_quantity", payload.get("total_quantity", "0")))),
        sellable_quantity=Decimal(str(getattr(row, "sellable_quantity", payload.get("sellable_quantity", "0")))),
        current_action=getattr(row, "current_action", "hold"),
        previous_valid_action=getattr(row, "previous_valid_action", "hold"),
        payload=payload,
    )


def _apply_action(state: PositionMonitoringState, action: PositionAction) -> PositionMonitoringState:
    valid_action = action.action if action.action != "unexecutable" else state.previous_valid_action
    payload = dict(state.payload)
    payload.update(action.payload)
    payload["reason_codes"] = list(action.reason_codes)
    return PositionMonitoringState(
        **{
            **state.__dict__,
            "current_action": action.action,
            "previous_valid_action": valid_action,
            "protective_price": action.protective_price or state.protective_price,
            "last_quote_at": action.evaluated_at or state.last_quote_at,
            "last_evaluated_at": action.evaluated_at or state.last_evaluated_at,
            "payload": payload,
        }
    )


def _orm_values(state: PositionMonitoringState, action: PositionAction) -> dict[str, Any]:
    return {
        "monitoring_state_id": f"monitoring:{state.position_id}",
        "position_id": state.position_id,
        "owner_id": state.owner_id,
        "portfolio_id": state.portfolio_id,
        "asset_id": state.asset_id,
        "symbol": state.symbol or state.asset_id.split(":")[-1],
        "market": state.market,
        "setup_id": state.setup_id,
        "decision_snapshot_id": action.decision_snapshot_id,
        "current_action": action.action,
        "previous_valid_action": state.previous_valid_action,
        "cost_price": _decimal(state.payload.get("cost_price")),
        "opened_on": state.opened_on,
        "total_quantity": state.total_quantity,
        "sellable_quantity": state.sellable_quantity,
        "active_days": state.active_days,
        "planned_horizon_days": state.planned_horizon_days,
        "invalidation_price": state.invalidation_price,
        "protective_price": action.protective_price or state.protective_price,
        "highest_price": state.highest_price,
        "sector_id": state.sector_id,
        "sector_regime": state.sector_regime,
        "last_quote_at": action.evaluated_at,
        "last_evaluated_at": action.evaluated_at,
        "payload": {**state.payload, **action.payload, "reason_codes": list(action.reason_codes)},
    }


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return None
