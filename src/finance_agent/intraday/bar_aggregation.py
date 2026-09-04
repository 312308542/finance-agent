"""把追加式实时行情快照聚合为闭合的盘中 K 线。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

JsonDict = dict[str, Any]
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
SUPPORTED_INTRADAY_TIMEFRAMES = {"1m": 1, "5m": 5, "15m": 15, "60m": 60}
TRADING_SESSIONS = ((time(9, 30), time(11, 30)), (time(13, 0), time(15, 0)))


@dataclass(frozen=True)
class IntradayBar:
    """一根已经闭合、可持久化的盘中 K 线。"""

    asset_id: str
    symbol: str
    market: str
    timeframe: str
    timestamp: datetime
    end_timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    amount: Decimal | None
    source: str
    adjustment: str = ""
    is_closed: bool = True
    raw_record_id: str | None = None
    status: str = "available"

    def to_row(self) -> JsonDict:
        """转换为盘中 K 仓储接受的标准行。"""

        return {
            "asset_id": self.asset_id,
            "symbol": self.symbol,
            "market": self.market,
            "timeframe": self.timeframe,
            "timestamp": self.timestamp,
            "end_timestamp": self.end_timestamp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "amount": self.amount,
            "source": self.source,
            "adjustment": self.adjustment,
            "is_closed": self.is_closed,
            "raw_record_id": self.raw_record_id,
            "status": self.status,
        }


@dataclass(frozen=True)
class _Tick:
    asset_id: str
    symbol: str
    market: str
    timestamp: datetime
    price: Decimal
    cumulative_volume: Decimal | None
    cumulative_amount: Decimal | None
    source: str
    quality_status: str
    volume_delta: Decimal = Decimal("0")
    amount_delta: Decimal | None = None


def aggregate_closed_bars(
    ticks: Sequence[Any],
    *,
    timeframe: str,
    close_before: datetime,
) -> tuple[IntradayBar, ...]:
    """按上海交易时段聚合已经闭合的行情桶。"""

    try:
        minutes = SUPPORTED_INTRADAY_TIMEFRAMES[timeframe]
    except KeyError as exc:
        raise ValueError(f"不支持的盘中周期: {timeframe}") from exc
    cutoff = _as_shanghai_time(close_before, field_name="close_before")
    grouped: dict[tuple[str, str, date], list[_Tick]] = {}
    for tick in _dedupe_and_sort_ticks(ticks):
        if _session_bounds(tick.timestamp) is None:
            continue
        key = (tick.asset_id, tick.source, tick.timestamp.date())
        grouped.setdefault(key, []).append(tick)

    bars: list[IntradayBar] = []
    for group_ticks in grouped.values():
        bucketed: dict[tuple[datetime, datetime], list[_Tick]] = {}
        for tick in _attach_positive_cumulative_deltas(group_ticks):
            bounds = _bucket_bounds(tick.timestamp, minutes=minutes)
            if bounds is None:
                continue
            bucketed.setdefault(bounds, []).append(tick)
        for (start_at, end_at), bucket in bucketed.items():
            if end_at <= cutoff:
                bars.append(_build_bar(bucket, timeframe=timeframe, start_at=start_at, end_at=end_at))
    return tuple(sorted(bars, key=lambda item: (item.asset_id, item.timestamp, item.source)))


class IntradayBarAggregator:
    """闭合分钟 K 聚合器的稳定对象接口。"""

    def aggregate(
        self,
        ticks: Sequence[Any],
        *,
        timeframe: str,
        close_before: datetime,
    ) -> tuple[IntradayBar, ...]:
        """委托纯函数完成聚合，实例本身不保存可变状态。"""

        return aggregate_closed_bars(ticks, timeframe=timeframe, close_before=close_before)


def _dedupe_and_sort_ticks(ticks: Sequence[Any]) -> tuple[_Tick, ...]:
    deduped: dict[tuple[str, str, datetime], _Tick] = {}
    for raw in ticks:
        tick = _normalize_tick(raw)
        deduped[(tick.asset_id, tick.source, tick.timestamp)] = tick
    return tuple(
        sorted(
            deduped.values(),
            key=lambda item: (item.asset_id, item.source, item.timestamp),
        )
    )


def _normalize_tick(raw: Any) -> _Tick:
    asset_id = str(_value(raw, "asset_id") or "").strip()
    source = str(_value(raw, "source") or "").strip()
    symbol = str(_value(raw, "symbol") or "").strip()
    market = str(_value(raw, "market") or "").strip()
    timestamp = _as_shanghai_time(
        _value(raw, "server_timestamp") or _value(raw, "as_of"),
        field_name="as_of",
    )
    price = _decimal(_value(raw, "last_price"), field_name="last_price", required=True)
    if not asset_id or not source or not symbol or not market:
        raise ValueError("实时行情缺少资产、代码、市场或来源")
    return _Tick(
        asset_id=asset_id,
        symbol=symbol,
        market=market,
        timestamp=timestamp,
        price=price,
        cumulative_volume=_decimal(_value(raw, "volume"), field_name="volume"),
        cumulative_amount=_decimal(_value(raw, "amount"), field_name="amount"),
        source=source,
        quality_status=str(
            _value(raw, "quality_status") or _value(raw, "status") or "available"
        ),
    )


def _attach_positive_cumulative_deltas(ticks: Sequence[_Tick]) -> tuple[_Tick, ...]:
    previous_volume: Decimal | None = None
    previous_amount: Decimal | None = None
    result: list[_Tick] = []
    for tick in ticks:
        volume_delta = _positive_delta(tick.cumulative_volume, previous_volume)
        amount_delta = (
            _positive_delta(tick.cumulative_amount, previous_amount)
            if tick.cumulative_amount is not None
            else None
        )
        result.append(replace(tick, volume_delta=volume_delta, amount_delta=amount_delta))
        if tick.cumulative_volume is not None:
            previous_volume = tick.cumulative_volume
        if tick.cumulative_amount is not None:
            previous_amount = tick.cumulative_amount
    return tuple(result)


def _positive_delta(current: Decimal | None, previous: Decimal | None) -> Decimal:
    if current is None or previous is None or current < previous:
        return Decimal("0")
    return current - previous


def _session_bounds(value: datetime) -> tuple[datetime, datetime] | None:
    for start, end in TRADING_SESSIONS:
        start_at = datetime.combine(value.date(), start, tzinfo=SHANGHAI_TZ)
        end_at = datetime.combine(value.date(), end, tzinfo=SHANGHAI_TZ)
        if start_at <= value <= end_at:
            return start_at, end_at
    return None


def _bucket_bounds(value: datetime, *, minutes: int) -> tuple[datetime, datetime] | None:
    session = _session_bounds(value)
    if session is None:
        return None
    session_start, session_end = session
    effective = value - timedelta(microseconds=1) if value == session_end else value
    bucket_index = int((effective - session_start).total_seconds() // (minutes * 60))
    start_at = session_start + timedelta(minutes=bucket_index * minutes)
    return start_at, min(start_at + timedelta(minutes=minutes), session_end)


def _build_bar(
    ticks: Sequence[_Tick],
    *,
    timeframe: str,
    start_at: datetime,
    end_at: datetime,
) -> IntradayBar:
    first = ticks[0]
    prices = tuple(tick.price for tick in ticks)
    quality_statuses = {tick.quality_status for tick in ticks}
    amount_deltas = tuple(tick.amount_delta for tick in ticks if tick.amount_delta is not None)
    return IntradayBar(
        asset_id=first.asset_id,
        symbol=first.symbol,
        market=first.market,
        timeframe=timeframe,
        timestamp=start_at,
        end_timestamp=end_at,
        open=prices[0],
        high=max(prices),
        low=min(prices),
        close=prices[-1],
        volume=sum((tick.volume_delta for tick in ticks), Decimal("0")),
        amount=sum(amount_deltas, Decimal("0")) if amount_deltas else None,
        source=first.source,
        status="available" if quality_statuses == {"available"} else "partial",
    )


def _as_shanghai_time(value: Any, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} 必须是 datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} 必须包含时区")
    return value.astimezone(SHANGHAI_TZ)


def _decimal(value: Any, *, field_name: str, required: bool = False) -> Decimal | None:
    if value is None or str(value).strip() == "":
        if required:
            raise ValueError(f"{field_name} 不能为空")
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} 不是有效数值") from exc


def _value(raw: Any, field_name: str) -> Any:
    if isinstance(raw, Mapping):
        return raw.get(field_name)
    return getattr(raw, field_name, None)
