"""分层实时行情监控、退避和事务内持久化。"""

from __future__ import annotations

import time as time_module
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from finance_agent.intraday.bar_aggregation import (
    SHANGHAI_TZ,
    SUPPORTED_INTRADAY_TIMEFRAMES,
    aggregate_closed_bars,
)
from finance_agent.intraday.models import QuoteChannelName, quote_channel_policy

JsonDict = dict[str, Any]
ACTIVE_CHANNELS: tuple[QuoteChannelName, ...] = ("held", "radar", "verification")
FAILURE_BACKOFF_SECONDS = (1, 2, 5, 10, 30)
HELD_COLD_START_LATENCY_SECONDS = 5.0


class QuoteSymbolSource(Protocol):
    """按用户和通道提供实时监控代码。"""

    def symbols_for(
        self,
        channel: QuoteChannelName,
        *,
        owner_id: str,
    ) -> Sequence[str]: ...


class QuoteCollector(Protocol):
    """采集并持久化一个通道批次。"""

    def collect(
        self,
        *,
        channel: QuoteChannelName,
        symbols: tuple[str, ...],
        captured_at: datetime,
    ) -> QuoteChannelCollection: ...


class MonitorClock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class SystemMonitorClock:
    """生产环境使用的 UTC 与单调时钟。"""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time_module.monotonic()


@dataclass(frozen=True)
class QuoteChannelCollection:
    """一个通道完成采集与持久化后的摘要。"""

    status: str
    requested_count: int
    received_count: int
    rows_written: int
    bars_written: int
    latency_seconds: float
    data_snapshot_id: str | None


@dataclass(frozen=True)
class QuotePersistenceResult:
    """一次实时批次的各事实表写入数量。"""

    history_rows_written: int
    latest_rows_written: int
    bars_written: int


@dataclass(frozen=True)
class RealtimeMonitorSummary:
    """一次到期通道扫描的可序列化结果。"""

    started_at: datetime
    completed_at: datetime
    executed_channels: tuple[str, ...]
    requested_by_channel: dict[str, tuple[str, ...]]
    channel_status: dict[str, str]
    collections: dict[str, QuoteChannelCollection]
    errors: dict[str, str]

    @property
    def error_count(self) -> int:
        return len(self.errors)

    def to_dict(self) -> JsonDict:
        """生成健康文件可直接写入的 JSON 数据。"""

        return {
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "executed_channels": list(self.executed_channels),
            "requested_by_channel": {
                channel: list(symbols)
                for channel, symbols in self.requested_by_channel.items()
            },
            "channel_status": dict(self.channel_status),
            "collections": {
                channel: {
                    "status": result.status,
                    "requested_count": result.requested_count,
                    "received_count": result.received_count,
                    "rows_written": result.rows_written,
                    "bars_written": result.bars_written,
                    "latency_seconds": result.latency_seconds,
                    "data_snapshot_id": result.data_snapshot_id,
                }
                for channel, result in self.collections.items()
            },
            "errors": dict(self.errors),
            "error_count": self.error_count,
        }


class RealtimeQuoteMonitor:
    """按持仓、重点池和校验通道优先级执行到期采集。"""

    def __init__(
        self,
        *,
        symbol_source: QuoteSymbolSource,
        collector: QuoteCollector,
        clock: MonitorClock | None = None,
    ) -> None:
        self.symbol_source = symbol_source
        self.collector = collector
        self.clock = clock or SystemMonitorClock()
        self._next_due = {channel: float("-inf") for channel in ACTIVE_CHANNELS}
        self._failure_counts = {channel: 0 for channel in ACTIVE_CHANNELS}
        self._next_intervals = {
            channel: quote_channel_policy(channel).interval_seconds
            for channel in ACTIVE_CHANNELS
        }
        self._last_channel_status: dict[str, str] = {}

    def run_due_channels(self, owner_id: str = "default-owner") -> RealtimeMonitorSummary:
        """执行当前到期通道；单通道失败不会阻断其他通道。"""

        started_at = self.clock.now()
        monotonic_now = self.clock.monotonic()
        requested_by_channel: dict[str, tuple[str, ...]] = {}
        channel_status: dict[str, str] = {}
        collections: dict[str, QuoteChannelCollection] = {}
        errors: dict[str, str] = {}
        claimed_symbols: set[str] = set()
        executed_channels: list[str] = []

        for channel in ACTIVE_CHANNELS:
            if monotonic_now < self._next_due[channel]:
                continue
            executed_channels.append(channel)
            try:
                source_symbols = self.symbol_source.symbols_for(channel, owner_id=owner_id)
                symbols = tuple(
                    symbol
                    for symbol in _stable_symbols(source_symbols)
                    if symbol not in claimed_symbols
                )
                claimed_symbols.update(symbols)
                if not symbols:
                    channel_status[channel] = "idle"
                    self._mark_success(channel)
                    self._schedule(channel, monotonic_now)
                    continue
                requested_by_channel[channel] = symbols
                result = self.collector.collect(
                    channel=channel,
                    symbols=symbols,
                    captured_at=self.clock.now(),
                )
                collections[channel] = result
                status = result.status
                if status == "available" and result.received_count != result.requested_count:
                    status = "partial"
                if (
                    channel == "held"
                    and status == "available"
                    and result.latency_seconds > HELD_COLD_START_LATENCY_SECONDS
                ):
                    status = "degraded"
                    self._mark_success(channel, interval_seconds=5)
                else:
                    self._mark_success(channel)
                channel_status[channel] = status
            except Exception as exc:  # noqa: BLE001 - 通道必须互相隔离
                errors[channel] = str(exc)
                channel_status[channel] = "error"
                self._mark_failure(channel)
            self._schedule(channel, monotonic_now)

        self._last_channel_status.update(channel_status)
        return RealtimeMonitorSummary(
            started_at=started_at,
            completed_at=self.clock.now(),
            executed_channels=tuple(executed_channels),
            requested_by_channel=requested_by_channel,
            channel_status=dict(self._last_channel_status),
            collections=collections,
            errors=errors,
        )

    def next_interval(self, channel: QuoteChannelName) -> int:
        """返回通道当前使用的默认或退避间隔。"""

        return self._next_intervals[channel]

    def _mark_success(
        self,
        channel: QuoteChannelName,
        *,
        interval_seconds: int | None = None,
    ) -> None:
        self._failure_counts[channel] = 0
        self._next_intervals[channel] = (
            interval_seconds
            if interval_seconds is not None
            else quote_channel_policy(channel).interval_seconds
        )

    def _mark_failure(self, channel: QuoteChannelName) -> None:
        failure_count = self._failure_counts[channel] + 1
        self._failure_counts[channel] = failure_count
        self._next_intervals[channel] = FAILURE_BACKOFF_SECONDS[
            min(failure_count - 1, len(FAILURE_BACKOFF_SECONDS) - 1)
        ]

    def _schedule(self, channel: QuoteChannelName, monotonic_now: float) -> None:
        self._next_due[channel] = monotonic_now + self._next_intervals[channel]


class RealtimeQuoteBatchPersister:
    """在同一 Session 事务中写入实时事实和闭合分钟 K。"""

    def __init__(
        self,
        *,
        snapshot_repository: Any,
        asset_repository: Any,
        market_repository: Any,
    ) -> None:
        self.snapshot_repository = snapshot_repository
        self.asset_repository = asset_repository
        self.market_repository = market_repository

    def persist(
        self,
        *,
        snapshot: Any,
        rows: Sequence[Any],
        close_before: datetime,
    ) -> QuotePersistenceResult:
        """追加历史、覆盖最新值，并在有限窗口内重算最近闭合周期。"""

        normalized_rows = tuple(_row_mapping(row) for row in rows)
        if not normalized_rows:
            raise ValueError("实时行情批次不能为空")
        self.snapshot_repository.insert_snapshot(snapshot)
        history_count = self.asset_repository.upsert_realtime_quote_snapshots(
            normalized_rows
        )
        latest_count = self.asset_repository.upsert_intraday_quote_latest(normalized_rows)

        local_cutoff = close_before.astimezone(SHANGHAI_TZ)
        start_at = local_cutoff - timedelta(
            minutes=max(SUPPORTED_INTRADAY_TIMEFRAMES.values()) + 1
        )
        history = self.asset_repository.list_realtime_quote_snapshots(
            asset_ids=tuple(sorted({str(row["asset_id"]) for row in normalized_rows})),
            sources=tuple(sorted({str(row["source"]) for row in normalized_rows})),
            start_at=start_at,
            end_at=close_before,
        )
        latest_bars: dict[tuple[str, str, str], JsonDict] = {}
        for timeframe in SUPPORTED_INTRADAY_TIMEFRAMES:
            for bar in aggregate_closed_bars(
                history,
                timeframe=timeframe,
                close_before=close_before,
            ):
                latest_bars[(bar.asset_id, bar.source, timeframe)] = bar.to_row()
        bar_rows = tuple(
            latest_bars[key]
            for key in sorted(latest_bars, key=lambda item: (item[0], item[1], item[2]))
        )
        bars_written = self.market_repository.upsert_intraday_bars(bar_rows)
        return QuotePersistenceResult(
            history_rows_written=history_count,
            latest_rows_written=latest_count,
            bars_written=bars_written,
        )


def _stable_symbols(symbols: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(symbol).strip() for symbol in symbols if str(symbol).strip()))


def _row_mapping(row: Any) -> JsonDict:
    if isinstance(row, Mapping):
        return dict(row)
    fields = (
        "asset_id",
        "symbol",
        "market",
        "as_of",
        "source",
        "data_snapshot_id",
        "captured_at",
        "freshness_ms",
        "last_price",
        "prev_close",
        "open",
        "high",
        "low",
        "volume",
        "amount",
        "turnover_rate",
        "change_amount",
        "change_percent",
        "bid_price",
        "ask_price",
        "status",
        "quality_status",
        "payload",
    )
    return {field: getattr(row, field) for field in fields if hasattr(row, field)}
