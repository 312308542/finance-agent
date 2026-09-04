from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from finance_agent.intraday.models import QuoteQualityResult, quote_channel_policy
from finance_agent.intraday.quote_monitor import (
    QuoteChannelCollection,
    RealtimeQuoteBatchPersister,
    RealtimeQuoteMonitor,
)
from scripts.data.run_realtime_quote_monitor import build_parser

NOW = datetime(2026, 9, 7, 1, 30, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.current = NOW
        self.elapsed = 0.0

    def now(self) -> datetime:
        return self.current

    def monotonic(self) -> float:
        return self.elapsed

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)
        self.elapsed += seconds


class _SymbolSource:
    def __init__(
        self,
        *,
        held: tuple[str, ...] = (),
        radar: tuple[str, ...] = (),
        verification: tuple[str, ...] = (),
    ) -> None:
        self.symbols = {
            "held": held,
            "radar": radar,
            "verification": verification,
        }

    def symbols_for(self, channel: str, *, owner_id: str) -> tuple[str, ...]:
        assert owner_id == "default-owner"
        return self.symbols[channel]


class _Collector:
    def __init__(self, *, latency_seconds: float = 0.1) -> None:
        self.latency_seconds = latency_seconds
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.failures_remaining = 0

    def collect(
        self,
        *,
        channel: str,
        symbols: tuple[str, ...],
        captured_at: datetime,
    ) -> QuoteChannelCollection:
        assert captured_at.tzinfo is not None
        self.calls.append((channel, symbols))
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise RuntimeError("temporary gateway failure")
        return QuoteChannelCollection(
            status="available",
            requested_count=len(symbols),
            received_count=len(symbols),
            rows_written=len(symbols),
            bars_written=0,
            latency_seconds=self.latency_seconds,
            data_snapshot_id=f"snapshot:{channel}",
        )


def test_quote_channels_have_fixed_frequency_and_batch_limits() -> None:
    held = quote_channel_policy("held")
    radar = quote_channel_policy("radar")
    market = quote_channel_policy("market")
    verification = quote_channel_policy("verification")

    assert (held.interval_seconds, held.batch_size, held.primary_source) == (1, 50, "gotdx")
    assert held.maximum_freshness_seconds == 3
    assert (radar.interval_seconds, radar.batch_size, radar.primary_source) == (5, 50, "gotdx")
    assert radar.maximum_freshness_seconds == 10
    assert (market.interval_seconds, market.batch_size, market.primary_source) == (300, 500, "akshare")
    assert market.maximum_freshness_seconds == 300
    assert (verification.interval_seconds, verification.batch_size, verification.primary_source) == (
        30,
        50,
        "akshare",
    )
    assert verification.maximum_freshness_seconds == 90


def test_only_available_quote_quality_is_executable() -> None:
    available = QuoteQualityResult(
        status="available",
        requested_count=2,
        received_count=2,
        fresh_count=2,
        maximum_lag_seconds=0.8,
        duplicate_timestamp_count=0,
        clock_regression_count=0,
        source_errors=(),
    )
    partial = QuoteQualityResult(
        status="partial",
        requested_count=2,
        received_count=1,
        fresh_count=1,
        maximum_lag_seconds=0.8,
        duplicate_timestamp_count=0,
        clock_regression_count=0,
        source_errors=("gotdx:timeout",),
    )

    assert available.is_executable is True
    assert partial.is_executable is False


def test_monitor_fetches_held_symbol_once_at_highest_priority() -> None:
    source = _SymbolSource(held=("600519",), radar=("600519", "000001"))
    collector = _Collector()
    monitor = RealtimeQuoteMonitor(symbol_source=source, collector=collector, clock=_Clock())

    summary = monitor.run_due_channels()

    assert summary.requested_by_channel == {
        "held": ("600519",),
        "radar": ("000001",),
    }
    assert collector.calls == [
        ("held", ("600519",)),
        ("radar", ("000001",)),
    ]


def test_monitor_with_no_symbols_does_not_call_collector() -> None:
    collector = _Collector()
    monitor = RealtimeQuoteMonitor(
        symbol_source=_SymbolSource(),
        collector=collector,
        clock=_Clock(),
    )

    summary = monitor.run_due_channels()

    assert collector.calls == []
    assert summary.requested_by_channel == {}
    assert summary.channel_status == {
        "held": "idle",
        "radar": "idle",
        "verification": "idle",
    }


def test_monitor_runs_only_channels_whose_interval_is_due() -> None:
    clock = _Clock()
    collector = _Collector()
    monitor = RealtimeQuoteMonitor(
        symbol_source=_SymbolSource(held=("600519",), radar=("000001",)),
        collector=collector,
        clock=clock,
    )

    monitor.run_due_channels()
    collector.calls.clear()
    monitor.run_due_channels()
    assert collector.calls == []

    clock.advance(1)
    monitor.run_due_channels()
    assert collector.calls == [("held", ("600519",))]


def test_monitor_cold_start_timeout_degrades_held_channel_to_five_seconds() -> None:
    collector = _Collector(latency_seconds=5.5)
    monitor = RealtimeQuoteMonitor(
        symbol_source=_SymbolSource(held=("600519",)),
        collector=collector,
        clock=_Clock(),
    )

    summary = monitor.run_due_channels()

    assert summary.channel_status["held"] == "degraded"
    assert monitor.next_interval("held") == 5


def test_slow_partial_collection_keeps_partial_quality_status() -> None:
    class _PartialCollector(_Collector):
        def collect(self, **kwargs: Any) -> QuoteChannelCollection:
            result = super().collect(**kwargs)
            return QuoteChannelCollection(
                status="partial",
                requested_count=result.requested_count,
                received_count=result.received_count - 1,
                rows_written=result.rows_written - 1,
                bars_written=result.bars_written,
                latency_seconds=result.latency_seconds,
                data_snapshot_id=result.data_snapshot_id,
            )

    monitor = RealtimeQuoteMonitor(
        symbol_source=_SymbolSource(held=("600519", "000001")),
        collector=_PartialCollector(latency_seconds=5.5),
        clock=_Clock(),
    )

    summary = monitor.run_due_channels()

    assert summary.channel_status["held"] == "partial"


def test_monitor_failure_backoff_is_bounded_and_success_restores_default() -> None:
    clock = _Clock()
    collector = _Collector()
    collector.failures_remaining = 5
    monitor = RealtimeQuoteMonitor(
        symbol_source=_SymbolSource(held=("600519",)),
        collector=collector,
        clock=clock,
    )

    intervals: list[int] = []
    for _ in range(5):
        monitor.run_due_channels()
        intervals.append(monitor.next_interval("held"))
        clock.advance(monitor.next_interval("held"))

    monitor.run_due_channels()

    assert intervals == [1, 2, 5, 10, 30]
    assert monitor.next_interval("held") == 1


def test_batch_persister_writes_snapshot_history_latest_and_closed_bars() -> None:
    calls: list[str] = []

    class _Snapshots:
        def insert_snapshot(self, _snapshot: Any) -> Any:
            calls.append("snapshot")
            return _snapshot

    class _Assets:
        def upsert_realtime_quote_snapshots(self, rows: Any) -> int:
            calls.append("history")
            return len(rows)

        def upsert_intraday_quote_latest(self, rows: Any) -> int:
            calls.append("latest")
            return len(rows)

        def list_realtime_quote_snapshots(self, **_: Any) -> list[Any]:
            calls.append("load-history")
            return [
                _history_tick("2026-09-07T09:30:05+08:00", "10.00", "100"),
                _history_tick("2026-09-07T09:30:40+08:00", "10.20", "200"),
            ]

    class _Bars:
        rows: list[Any] = []

        def upsert_intraday_bars(self, rows: Any) -> int:
            calls.append("bars")
            self.rows = list(rows)
            return len(self.rows)

    bars = _Bars()
    persister = RealtimeQuoteBatchPersister(
        snapshot_repository=_Snapshots(),
        asset_repository=_Assets(),
        market_repository=bars,
    )

    result = persister.persist(
        snapshot=SimpleNamespace(data_snapshot_id="snapshot:held"),
        rows=(_history_tick("2026-09-07T09:30:40+08:00", "10.20", "200"),),
        close_before=datetime.fromisoformat("2026-09-07T10:30:00+08:00"),
    )

    assert calls == ["snapshot", "history", "latest", "load-history", "bars"]
    assert result.history_rows_written == 1
    assert result.latest_rows_written == 1
    assert result.bars_written == 4
    assert {row["timeframe"] for row in bars.rows} == {"1m", "5m", "15m", "60m"}


def test_batch_persister_rejects_empty_rows_before_any_write() -> None:
    class _MustNotWrite:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"不应调用 {name}")

    repository = _MustNotWrite()
    persister = RealtimeQuoteBatchPersister(
        snapshot_repository=repository,
        asset_repository=repository,
        market_repository=repository,
    )

    with pytest.raises(ValueError, match="实时行情批次不能为空"):
        persister.persist(
            snapshot=object(),
            rows=(),
            close_before=datetime.fromisoformat("2026-09-07T10:30:00+08:00"),
        )


def test_batch_persister_limits_history_window_and_rewrites_only_latest_bars() -> None:
    class _Snapshots:
        def insert_snapshot(self, snapshot: Any) -> Any:
            return snapshot

    class _Assets:
        query: dict[str, Any] = {}

        def upsert_realtime_quote_snapshots(self, rows: Any) -> int:
            return len(rows)

        def upsert_intraday_quote_latest(self, rows: Any) -> int:
            return len(rows)

        def list_realtime_quote_snapshots(self, **kwargs: Any) -> list[Any]:
            self.query = kwargs
            start = datetime.fromisoformat("2026-09-07T09:29:00+08:00")
            return [
                _history_tick(
                    (start + timedelta(minutes=index)).isoformat(),
                    str(10 + index / 100),
                    str(index * 100),
                )
                for index in range(62)
            ]

    class _Bars:
        rows: list[Any] = []

        def upsert_intraday_bars(self, rows: Any) -> int:
            self.rows = list(rows)
            return len(self.rows)

    assets = _Assets()
    bars = _Bars()
    persister = RealtimeQuoteBatchPersister(
        snapshot_repository=_Snapshots(),
        asset_repository=assets,
        market_repository=bars,
    )
    cutoff = datetime.fromisoformat("2026-09-07T10:30:00+08:00")

    persister.persist(
        snapshot=object(),
        rows=(_history_tick("2026-09-07T10:30:00+08:00", "10.61", "6100"),),
        close_before=cutoff,
    )

    assert assets.query["start_at"] == datetime.fromisoformat("2026-09-07T09:29:00+08:00")
    assert len(bars.rows) == 4
    assert {row["timeframe"] for row in bars.rows} == {"1m", "5m", "15m", "60m"}


def test_realtime_monitor_cli_supports_once_and_loop_modes() -> None:
    parser = build_parser()

    once = parser.parse_args(["--once", "--owner-id", "owner-a"])
    loop = parser.parse_args(["--loop", "--status-file", "runtime/test/status.json"])

    assert once.once is True
    assert once.owner_id == "owner-a"
    assert loop.loop is True
    assert loop.status_file == "runtime/test/status.json"


def _history_tick(value: str, price: str, volume: str) -> SimpleNamespace:
    timestamp = datetime.fromisoformat(value)
    return SimpleNamespace(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        as_of=timestamp,
        source="gotdx:tdx_main",
        last_price=Decimal(price),
        volume=Decimal(volume),
        amount=None,
        quality_status="available",
    )
