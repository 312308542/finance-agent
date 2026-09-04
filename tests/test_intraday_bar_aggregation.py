"""实时行情快照聚合闭合分钟 K 的行为测试。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from finance_agent.intraday.bar_aggregation import (
    SUPPORTED_INTRADAY_TIMEFRAMES,
    aggregate_closed_bars,
)


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _tick(
    value: str,
    *,
    price: str,
    volume: str,
    amount: str | None = None,
    asset_id: str = "ashare:600519",
) -> SimpleNamespace:
    return SimpleNamespace(
        asset_id=asset_id,
        symbol=asset_id.split(":", 1)[1],
        market="ashare",
        as_of=_datetime(value),
        source="gotdx:tdx_main",
        last_price=Decimal(price),
        volume=Decimal(volume),
        amount=Decimal(amount) if amount is not None else None,
        quality_status="available",
    )


def test_one_minute_bar_uses_price_ohlc_and_positive_volume_deltas() -> None:
    ticks = [
        _tick("2026-09-07T09:30:05+08:00", price="10.00", volume="1000", amount="10000"),
        _tick("2026-09-07T09:30:40+08:00", price="10.20", volume="1300", amount="13000"),
        _tick("2026-09-07T09:31:00+08:00", price="10.10", volume="1500", amount="15000"),
    ]

    bars = aggregate_closed_bars(
        ticks,
        timeframe="1m",
        close_before=_datetime("2026-09-07T09:32:00+08:00"),
    )

    assert [(bar.open, bar.high, bar.low, bar.close, bar.volume) for bar in bars] == [
        (Decimal("10.00"), Decimal("10.20"), Decimal("10.00"), Decimal("10.20"), Decimal("300")),
        (Decimal("10.10"), Decimal("10.10"), Decimal("10.10"), Decimal("10.10"), Decimal("200")),
    ]
    assert [bar.amount for bar in bars] == [Decimal("3000"), Decimal("2000")]
    assert all(bar.is_closed for bar in bars)


def test_bars_do_not_cross_the_midday_break() -> None:
    ticks = [
        _tick("2026-09-07T11:29:00+08:00", price="10.00", volume="100"),
        _tick("2026-09-07T11:29:30+08:00", price="10.10", volume="150"),
        _tick("2026-09-07T13:00:00+08:00", price="10.20", volume="150"),
        _tick("2026-09-07T13:00:30+08:00", price="10.30", volume="210"),
    ]

    bars = aggregate_closed_bars(
        ticks,
        timeframe="5m",
        close_before=_datetime("2026-09-07T13:05:00+08:00"),
    )

    assert [bar.timestamp.strftime("%H:%M") for bar in bars] == ["11:25", "13:00"]
    assert [bar.volume for bar in bars] == [Decimal("50"), Decimal("60")]


def test_cumulative_volume_regression_contributes_zero_for_that_tick() -> None:
    ticks = [
        _tick("2026-09-07T09:30:00+08:00", price="10.00", volume="1000"),
        _tick("2026-09-07T09:30:20+08:00", price="9.90", volume="900"),
        _tick("2026-09-07T09:30:40+08:00", price="10.10", volume="950"),
    ]

    bars = aggregate_closed_bars(
        ticks,
        timeframe="1m",
        close_before=_datetime("2026-09-07T09:31:00+08:00"),
    )

    assert bars[0].volume == Decimal("50")


def test_duplicate_server_timestamp_keeps_the_last_snapshot() -> None:
    ticks = [
        _tick("2026-09-07T09:30:00+08:00", price="10.00", volume="100"),
        _tick("2026-09-07T09:30:30+08:00", price="10.10", volume="150"),
        _tick("2026-09-07T09:30:30+08:00", price="10.40", volume="180"),
        _tick("2026-09-07T09:30:50+08:00", price="10.20", volume="200"),
    ]

    bars = aggregate_closed_bars(
        ticks,
        timeframe="1m",
        close_before=_datetime("2026-09-07T09:31:00+08:00"),
    )

    assert (bars[0].open, bars[0].high, bars[0].low, bars[0].close) == (
        Decimal("10.00"),
        Decimal("10.40"),
        Decimal("10.00"),
        Decimal("10.20"),
    )
    assert bars[0].volume == Decimal("100")


def test_unclosed_bucket_is_not_returned() -> None:
    bars = aggregate_closed_bars(
        [_tick("2026-09-07T09:31:05+08:00", price="10.00", volume="100")],
        timeframe="1m",
        close_before=_datetime("2026-09-07T09:31:59+08:00"),
    )

    assert bars == ()


def test_supported_intraday_timeframes_are_fixed() -> None:
    assert SUPPORTED_INTRADAY_TIMEFRAMES == {"1m": 1, "5m": 5, "15m": 15, "60m": 60}
