"""确定性的盘中持仓动作引擎。"""

from __future__ import annotations

from decimal import Decimal

from finance_agent.monitoring.models import (
    IntradayPositionSnapshot,
    PositionAction,
    PositionActionName,
    PositionMonitoringState,
)


class PositionMonitoringEngine:
    """按固定优先级把持仓事实转换为建议动作。"""

    def evaluate(
        self,
        state: PositionMonitoringState,
        snapshot: IntradayPositionSnapshot,
    ) -> PositionAction:
        """数据不足或交易限制优先于收益和排名规则。"""

        evaluated_at = snapshot.as_of or state.last_evaluated_at
        protective_price = _raise_protective_price(
            state.protective_price,
            snapshot.new_protective_price,
        )
        if snapshot.quality_status != "available" or snapshot.quote_age_seconds > 3:
            return _action(
                state,
                snapshot,
                "unexecutable",
                "high",
                ("quote_stale" if snapshot.quality_status == "available" else "quote_unavailable",),
                protective_price=protective_price,
                intended_action=_intended_exit(state),
                evaluated_at=evaluated_at,
            )
        if snapshot.suspended:
            return _action(
                state,
                snapshot,
                "unexecutable",
                "high",
                ("suspended",),
                protective_price=protective_price,
                intended_action=_intended_exit(state),
                evaluated_at=evaluated_at,
            )
        if snapshot.limit_down and state.sellable_quantity > 0:
            return _action(
                state,
                snapshot,
                "unexecutable",
                "critical",
                ("limit_down_no_liquidity",),
                protective_price=protective_price,
                intended_action="exit",
                evaluated_at=evaluated_at,
            )
        if state.sellable_quantity <= 0:
            if snapshot.structure_invalidated or snapshot.daily_structure == "bearish":
                return _action(
                    state,
                    snapshot,
                    "unexecutable",
                    "critical",
                    ("t1_not_sellable",),
                    protective_price=protective_price,
                    intended_action="exit",
                    evaluated_at=evaluated_at,
                )
        if snapshot.risk_event or snapshot.risk_level in {"critical", "high"}:
            return _action(
                state,
                snapshot,
                "exit" if state.sellable_quantity > 0 else "unexecutable",
                "critical",
                ("material_risk_event",),
                protective_price=protective_price,
                intended_action="exit",
                evaluated_at=evaluated_at,
            )
        if snapshot.structure_invalidated or snapshot.daily_structure == "bearish":
            return _action(
                state,
                snapshot,
                "exit" if state.sellable_quantity > 0 else "unexecutable",
                "critical",
                ("structure_broken",),
                protective_price=protective_price,
                intended_action="exit",
                evaluated_at=evaluated_at,
            )
        if (
            state.invalidation_price is not None
            and snapshot.price is not None
            and snapshot.price <= state.invalidation_price
        ):
            return _action(
                state,
                snapshot,
                "exit" if state.sellable_quantity > 0 else "unexecutable",
                "high",
                ("invalidation_price_broken",),
                protective_price=protective_price,
                intended_action="exit",
                evaluated_at=evaluated_at,
            )
        if snapshot.sector_regime in {"cooling", "divergence"}:
            return _action(
                state,
                snapshot,
                "add_blocked",
                "medium",
                ("sector_cooling",),
                protective_price=protective_price,
                evaluated_at=evaluated_at,
            )
        if snapshot.capital_flow_negative_streak >= 3 or snapshot.volume_price_divergence:
            return _action(
                state,
                snapshot,
                "reduce" if state.sellable_quantity > 0 else "watch",
                "medium",
                ("capital_flow_weakening",),
                protective_price=protective_price,
                intended_action="reduce" if state.sellable_quantity > 0 else None,
                evaluated_at=evaluated_at,
            )
        if snapshot.acceleration and (snapshot.profit_r_multiple or 0) >= 1.5:
            return _action(
                state,
                snapshot,
                "reduce" if state.sellable_quantity > 0 else "watch",
                "medium",
                ("acceleration_profit_taking",),
                protective_price=protective_price,
                intended_action="reduce" if state.sellable_quantity > 0 else None,
                evaluated_at=evaluated_at,
                suggested_quantity=_half_quantity(state.sellable_quantity),
            )
        return _action(
            state,
            snapshot,
            "hold",
            "low",
            ("thesis_and_structure_hold",),
            protective_price=protective_price,
            evaluated_at=evaluated_at,
        )


def _action(
    state: PositionMonitoringState,
    snapshot: IntradayPositionSnapshot,
    action: PositionActionName,
    severity: str,
    reason_codes: tuple[str, ...],
    *,
    protective_price: Decimal | None,
    intended_action: PositionActionName | None = None,
    evaluated_at: object = None,
    suggested_quantity: Decimal = Decimal("0"),
) -> PositionAction:
    return PositionAction(
        position_id=state.position_id,
        action=action,
        intended_action=intended_action,
        severity=severity,  # type: ignore[arg-type]
        reason_codes=reason_codes,
        protective_price=protective_price,
        suggested_quantity=suggested_quantity,
        evaluated_at=evaluated_at,  # type: ignore[arg-type]
        quote_snapshot_id=snapshot.quote_snapshot_id,
        decision_snapshot_id=state.payload.get("decision_snapshot_id"),
        payload={"sector_regime": snapshot.sector_regime},
    )


def _intended_exit(state: PositionMonitoringState) -> PositionActionName:
    return "exit" if state.sellable_quantity > 0 or state.total_quantity > 0 else "watch"


def _raise_protective_price(
    current: Decimal | None,
    candidate: Decimal | None,
) -> Decimal | None:
    if current is None:
        return candidate
    if candidate is None:
        return current
    return max(current, candidate)


def _half_quantity(quantity: Decimal) -> Decimal:
    if quantity <= 0:
        return Decimal("0")
    return (quantity / Decimal("2")).quantize(Decimal("1"))
