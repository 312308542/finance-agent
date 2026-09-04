"""持仓监控的数据类型。

这些类型不依赖数据库，便于在行情回放、盘中服务和前端读模型之间复用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

PositionActionName = Literal[
    "hold",
    "add_blocked",
    "watch",
    "reduce",
    "exit",
    "unexecutable",
]
Severity = Literal["low", "medium", "high", "critical"]


@dataclass(frozen=True)
class PositionMonitoringState:
    """单个持仓的当前监控状态。"""

    position_id: str
    owner_id: str = "default-owner"
    portfolio_id: str = ""
    asset_id: str = ""
    symbol: str = ""
    market: str = "ashare"
    total_quantity: Decimal = Decimal("0")
    sellable_quantity: Decimal = Decimal("0")
    opened_on: date | None = None
    active_days: int = 0
    setup_id: str | None = None
    current_action: PositionActionName = "hold"
    previous_valid_action: PositionActionName = "hold"
    planned_horizon_days: int = 10
    invalidation_price: Decimal | None = None
    protective_price: Decimal | None = None
    highest_price: Decimal | None = None
    sector_id: str | None = None
    sector_regime: str = "unknown"
    last_quote_at: datetime | None = None
    last_evaluated_at: datetime | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IntradayPositionSnapshot:
    """同一时点的持仓行情、结构和交易状态事实。"""

    position_id: str = ""
    asset_id: str = ""
    price: Decimal | None = None
    quote_snapshot_id: str = ""
    as_of: datetime | None = None
    quality_status: str = "available"
    quote_age_seconds: float = 0.0
    suspended: bool = False
    limit_up: bool = False
    limit_down: bool = False
    daily_structure: str = "unknown"
    structure_invalidated: bool = False
    sector_regime: str = "unknown"
    sector_breadth: float | None = None
    flow_streak: int = 0
    volume_confirmed: bool = False
    volume_price_divergence: bool = False
    capital_flow_negative_streak: int = 0
    risk_level: str | None = None
    risk_event: bool = False
    acceleration: bool = False
    profit_r_multiple: float | None = None
    new_protective_price: Decimal | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PositionAction:
    """监控引擎输出的建议动作，不代表已经执行。"""

    position_id: str
    action: PositionActionName
    intended_action: PositionActionName | None = None
    severity: Severity = "low"
    reason_codes: tuple[str, ...] = ()
    protective_price: Decimal | None = None
    suggested_quantity: Decimal = Decimal("0")
    evaluated_at: datetime | None = None
    quote_snapshot_id: str = ""
    decision_snapshot_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为可写入事件和 API 的 JSON 结构。"""

        return {
            "position_id": self.position_id,
            "action": self.action,
            "intended_action": self.intended_action,
            "severity": self.severity,
            "reason_codes": list(self.reason_codes),
            "protective_price": str(self.protective_price) if self.protective_price is not None else None,
            "suggested_quantity": str(self.suggested_quantity),
            "evaluated_at": self.evaluated_at.isoformat() if self.evaluated_at else None,
            "quote_snapshot_id": self.quote_snapshot_id,
            "decision_snapshot_id": self.decision_snapshot_id,
            "payload": self.payload,
        }
