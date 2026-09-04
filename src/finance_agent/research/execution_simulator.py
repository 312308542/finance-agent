"""A 股点时成交与交易限制仿真。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_DOWN, Decimal
from typing import Literal

import pandas as pd

Side = Literal["buy", "sell"]
ExecutionStatus = Literal["filled", "partial", "unexecutable"]


@dataclass(frozen=True)
class SimulatedOrder:
    """单笔研究订单；成交日始终晚于信号日。"""

    signal_date: date
    side: Side
    quantity: int
    bought_today: bool = False
    exit_date: date | None = None


@dataclass(frozen=True)
class SimulatedExecution:
    """仿真结果，无法证明成交时返回 ``unexecutable``。"""

    status: ExecutionStatus
    entry_date: date | None
    exit_date: date | None
    entry_price: Decimal | None
    exit_price: Decimal | None
    gross_return: float | None
    net_return: float | None
    total_cost_rate: Decimal
    reason: str | None
    filled_quantity: int = 0


class AshareExecutionSimulator:
    """按 A 股 100 股一手、T+1 和涨跌停规则模拟成交。"""

    def __init__(self, *, limit_pct: Decimal = Decimal("0.10")) -> None:
        self.limit_pct = limit_pct

    def simulate(self, order: SimulatedOrder, *, bars: pd.DataFrame) -> SimulatedExecution:
        """使用信号日之后的第一根可交易 K 线进行成交。"""

        if order.quantity <= 0:
            return self._blocked("invalid_quantity")
        if order.side not in ("buy", "sell"):
            return self._blocked("invalid_side")
        quantity = (int(order.quantity) // 100) * 100
        if quantity <= 0:
            return self._blocked("quantity_below_lot")
        prepared = self._prepare_bars(bars)
        if prepared.empty:
            return self._blocked("missing_market_bars")
        entry = self._first_after(prepared, order.signal_date)
        if entry is None:
            return self._blocked("missing_next_trading_day")
        if order.side == "sell" and order.bought_today:
            return self._blocked("t1_not_sellable")
        if bool(entry.get("is_suspended", False)):
            return self._blocked("suspended")
        if order.side == "buy" and self._is_limit_up(entry):
            return self._blocked("limit_up_no_liquidity")
        if order.side == "sell" and self._is_limit_down(entry):
            return self._blocked("limit_down_no_liquidity")
        price = self._price(entry, order.side)
        if price is None or price <= 0:
            return self._blocked("invalid_price")
        cost = Decimal("0.003") if order.side == "buy" else Decimal("0.0015")
        if order.exit_date is None or order.side == "sell":
            return SimulatedExecution(
                status="filled",
                entry_date=entry["timestamp"].date() if order.side == "buy" else None,
                exit_date=entry["timestamp"].date() if order.side == "sell" else None,
                entry_price=price if order.side == "buy" else None,
                exit_price=price if order.side == "sell" else None,
                gross_return=None,
                net_return=None,
                total_cost_rate=cost,
                reason=None,
                filled_quantity=quantity,
            )
        exit_row = self._on_or_after(prepared, order.exit_date)
        if exit_row is None:
            return self._blocked("missing_exit_trading_day")
        if bool(exit_row.get("is_suspended", False)) or self._is_limit_down(exit_row):
            return self._blocked("limit_down_no_liquidity")
        exit_price = self._price(exit_row, "sell")
        if exit_price is None or exit_price <= 0:
            return self._blocked("invalid_exit_price")
        gross = float(exit_price / price - Decimal("1"))
        net = gross - float(Decimal("0.003"))
        return SimulatedExecution(
            status="filled",
            entry_date=entry["timestamp"].date(),
            exit_date=exit_row["timestamp"].date(),
            entry_price=price,
            exit_price=exit_price,
            gross_return=gross,
            net_return=net,
            total_cost_rate=Decimal("0.003"),
            reason=None,
            filled_quantity=quantity,
        )

    @staticmethod
    def _prepare_bars(bars: pd.DataFrame) -> pd.DataFrame:
        required = {"timestamp", "open", "high", "low", "close"}
        missing = sorted(required.difference(bars.columns))
        if missing:
            raise ValueError(f"K 线缺少必要列：{', '.join(missing)}")
        frame = bars.copy(deep=True)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
        frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp", kind="stable")
        return frame.reset_index(drop=True)

    @staticmethod
    def _first_after(frame: pd.DataFrame, signal_date: date) -> pd.Series | None:
        rows = frame.loc[frame["timestamp"].dt.date > signal_date]
        return rows.iloc[0] if not rows.empty else None

    @staticmethod
    def _on_or_after(frame: pd.DataFrame, target_date: date) -> pd.Series | None:
        rows = frame.loc[frame["timestamp"].dt.date >= target_date]
        return rows.iloc[0] if not rows.empty else None

    @staticmethod
    def _price(row: pd.Series, side: Side) -> Decimal | None:
        value = row.get("open" if side == "buy" else "close")
        try:
            parsed = Decimal(str(value))
        except Exception:
            return None
        return parsed.quantize(Decimal("0.01"), rounding=ROUND_DOWN) if math.isfinite(float(parsed)) else None

    def _is_limit_up(self, row: pd.Series) -> bool:
        prev = self._decimal(row.get("prev_close"))
        opening = self._decimal(row.get("open"))
        high = self._decimal(row.get("high"))
        low = self._decimal(row.get("low"))
        return bool(
            prev
            and opening
            and high
            and low
            and opening >= prev * (1 + self.limit_pct)
            and high == low == opening
        )

    def _is_limit_down(self, row: pd.Series) -> bool:
        prev = self._decimal(row.get("prev_close"))
        opening = self._decimal(row.get("open"))
        high = self._decimal(row.get("high"))
        low = self._decimal(row.get("low"))
        return bool(
            prev
            and opening
            and high
            and low
            and opening <= prev * (1 - self.limit_pct)
            and high == low == opening
        )

    @staticmethod
    def _decimal(value: object) -> Decimal | None:
        try:
            parsed = Decimal(str(value))
            return parsed if parsed.is_finite() else None
        except Exception:
            return None

    @staticmethod
    def _blocked(reason: str) -> SimulatedExecution:
        return SimulatedExecution(
            status="unexecutable",
            entry_date=None,
            exit_date=None,
            entry_price=None,
            exit_price=None,
            gross_return=None,
            net_return=None,
            total_cost_rate=Decimal("0"),
            reason=reason,
        )
