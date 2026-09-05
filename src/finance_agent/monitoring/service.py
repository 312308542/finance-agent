"""盘中持仓监控服务：批量加载事实、逐仓计算、幂等保存动作。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from finance_agent.monitoring.models import (
    IntradayPositionSnapshot,
    PositionAction,
    PositionMonitoringState,
)
from finance_agent.monitoring.position_engine import PositionMonitoringEngine
from finance_agent.monitoring.repository import PositionMonitoringRepository
from finance_agent.storage.repositories import AssetRepository, PortfolioRepository


@dataclass(frozen=True)
class PositionMonitoringSummary:
    """一次用户持仓监控的结果摘要。"""

    owner_id: str
    evaluated_at: datetime
    actions: tuple[PositionAction, ...]
    error_count: int = 0
    changed_actions: tuple[PositionAction, ...] = ()


class PositionMonitoringService:
    """只消费数据库事实，不直接访问行情供应商。"""

    def __init__(
        self,
        session: Any | None = None,
        *,
        portfolio_repository: Any | None = None,
        asset_repository: Any | None = None,
        state_repository: PositionMonitoringRepository | None = None,
        engine: PositionMonitoringEngine | None = None,
    ) -> None:
        self.session = session
        self.portfolios = portfolio_repository or (PortfolioRepository(session) if session is not None else None)
        self.assets = asset_repository or (AssetRepository(session) if session is not None else None)
        self.states = state_repository or PositionMonitoringRepository(session)
        self.engine = engine or PositionMonitoringEngine()

    def evaluate_owner(self, owner_id: str, *, as_of: datetime) -> PositionMonitoringSummary:
        """批量读取持仓和最新行情，单仓失败不会阻断其他持仓。"""

        positions = self._list_positions(owner_id)
        asset_ids = [str(getattr(position, "asset_id", "")) for position in positions]
        quotes = self._list_quotes(asset_ids)
        actions: list[PositionAction] = []
        changed_actions: list[PositionAction] = []
        error_count = 0
        for position in positions:
            try:
                state = self._state_from_position(position)
                quote = quotes.get(state.asset_id)
                if quote is None:
                    action = PositionAction(
                        position_id=state.position_id,
                        action="unexecutable",
                        intended_action="exit" if state.total_quantity > 0 else "watch",
                        severity="high",
                        reason_codes=("quote_missing",),
                        protective_price=state.protective_price,
                        evaluated_at=as_of,
                        quote_snapshot_id="",
                        decision_snapshot_id=state.payload.get("decision_snapshot_id"),
                    )
                else:
                    snapshot = self._snapshot_from_quote(state, quote, as_of)
                    action = self.engine.evaluate(state, snapshot)
                saved = self.states.save_with_change(action, state=state)
                actions.append(action)
                if saved.changed:
                    changed_actions.append(action)
            except Exception as exc:  # noqa: BLE001 - 单仓隔离，继续监控其他持仓
                error_count += 1
                state = self._state_from_position(position)
                action = PositionAction(
                    position_id=state.position_id,
                    action="unexecutable",
                    intended_action="exit" if state.total_quantity > 0 else "watch",
                    severity="high",
                    reason_codes=("monitor_error",),
                    evaluated_at=as_of,
                    payload={"error": str(exc)[:240]},
                )
                saved = self.states.save_with_change(action, state=state)
                actions.append(action)
                if saved.changed:
                    changed_actions.append(action)
        return PositionMonitoringSummary(owner_id, as_of, tuple(actions), error_count, tuple(changed_actions))

    def _list_positions(self, owner_id: str) -> list[Any]:
        if self.portfolios is None:
            return []
        if hasattr(self.portfolios, "list_active_positions_by_owner"):
            return list(self.portfolios.list_active_positions_by_owner(owner_id=owner_id, market="ashare"))
        return []

    def _list_quotes(self, asset_ids: list[str]) -> dict[str, Any]:
        if self.assets is None or not asset_ids or not hasattr(self.assets, "list_intraday_quote_latest"):
            return {}
        rows = self.assets.list_intraday_quote_latest(
            asset_ids=asset_ids,
            quality_statuses=("available", "partial", "conflict"),
        )
        return {str(row.asset_id): row for row in rows}

    @staticmethod
    def _state_from_position(position: Any) -> PositionMonitoringState:
        payload = dict(getattr(position, "payload", {}) or {})
        return PositionMonitoringState(
            position_id=str(position.position_id),
            owner_id=str(payload.get("owner_id", "default-owner")),
            portfolio_id=str(position.portfolio_id),
            asset_id=str(position.asset_id),
            symbol=str(getattr(position, "symbol", "")),
            market=str(getattr(position, "market", "ashare")),
            total_quantity=Decimal(str(getattr(position, "quantity", "0") or "0")),
            sellable_quantity=Decimal(str(payload.get("sellable_quantity", getattr(position, "quantity", "0")) or "0")),
            opened_on=payload.get("opened_on"),
            setup_id=payload.get("setup_id"),
            planned_horizon_days=int(payload.get("planned_horizon_days", 10) or 10),
            invalidation_price=_decimal(payload.get("invalidation_price")),
            protective_price=_decimal(payload.get("protective_price")),
            highest_price=_decimal(payload.get("highest_price")),
            sector_id=payload.get("sector_id"),
            sector_regime=str(payload.get("sector_regime", "unknown")),
            payload=payload,
        )

    @staticmethod
    def _snapshot_from_quote(
        state: PositionMonitoringState,
        quote: Any,
        as_of: datetime,
    ) -> IntradayPositionSnapshot:
        quote_time = getattr(quote, "as_of", None) or getattr(quote, "captured_at", None)
        age = max(0.0, (as_of - quote_time).total_seconds()) if quote_time is not None else 999.0
        payload = dict(getattr(quote, "payload", {}) or {})
        payload.update({"state": state.position_id})
        return IntradayPositionSnapshot(
            position_id=state.position_id,
            asset_id=state.asset_id,
            price=_decimal(getattr(quote, "last_price", None) or getattr(quote, "price", None)),
            quote_snapshot_id=str(getattr(quote, "quote_snapshot_id", "") or getattr(quote, "snapshot_id", "")),
            as_of=quote_time or as_of,
            quality_status=str(getattr(quote, "quality_status", "available")),
            quote_age_seconds=age,
            suspended=bool(payload.get("suspended", False)),
            limit_up=bool(payload.get("limit_up", False)),
            limit_down=bool(payload.get("limit_down", False)),
            daily_structure=str(payload.get("daily_structure", "unknown")),
            structure_invalidated=bool(payload.get("structure_invalidated", False)),
            sector_regime=str(payload.get("sector_regime", state.sector_regime)),
            volume_confirmed=bool(payload.get("volume_confirmed", False)),
            volume_price_divergence=bool(payload.get("volume_price_divergence", False)),
            capital_flow_negative_streak=int(payload.get("capital_flow_negative_streak", 0) or 0),
            risk_level=payload.get("risk_level"),
            risk_event=bool(payload.get("risk_event", False)),
            acceleration=bool(payload.get("acceleration", False)),
            profit_r_multiple=float(payload.get("profit_r_multiple", 0) or 0),
            new_protective_price=_decimal(payload.get("new_protective_price")),
            payload=payload,
        )


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return None
