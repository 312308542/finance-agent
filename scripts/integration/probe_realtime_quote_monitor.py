"""只读观测 GoTDX 实时通道，并生成连续交易时段验收报告。"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as day_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
CHANNEL_INTERVAL_SECONDS = {"held": 1.0, "radar": 5.0}


@dataclass(frozen=True)
class ProbeObservation:
    """一次通道请求的只读观测结果。"""

    channel: str
    observed_at: datetime
    requested_symbols: tuple[str, ...]
    received_timestamps: tuple[tuple[str, datetime], ...]
    latency_seconds: float
    http_ok: bool
    quality_statuses: tuple[str, ...]
    connection_reopened: bool


def build_parser() -> argparse.ArgumentParser:
    """构建连续压力探针参数。"""

    parser = argparse.ArgumentParser(description="只读观测分层实时行情质量。")
    parser.add_argument("--duration-minutes", type=int, default=120)
    parser.add_argument("--held-symbols", nargs="+", required=True)
    parser.add_argument("--radar-symbols", nargs="*", default=[])
    parser.add_argument("--output", default="output/realtime-monitor-probe.json")
    parser.add_argument(
        "--gotdx-url",
        default=os.getenv("FINANCE_AGENT_GOTDX_GATEWAY_URL", "http://127.0.0.1:8790"),
    )
    parser.add_argument("--database-url", default=os.getenv("FINANCE_AGENT_DATABASE_URL"))
    parser.add_argument("--request-timeout-seconds", type=float, default=3.0)
    return parser


def build_probe_report(
    observations: Sequence[ProbeObservation],
    *,
    expected_bar_count: int,
    observed_bar_count: int,
    full_market_captured_at: Sequence[datetime],
    started_at: datetime,
    completed_at: datetime,
    observed_bar_timestamps: Sequence[datetime] = (),
) -> dict[str, Any]:
    """把请求观测压缩为可审计的通道、分钟 K 和全市场指标。"""

    channels = {
        channel: _channel_metrics(
            tuple(item for item in observations if item.channel == channel)
        )
        for channel in CHANNEL_INTERVAL_SECONDS
        if any(item.channel == channel for item in observations)
    }
    statuses = {
        status
        for item in observations
        for status in item.quality_statuses
        if status
    }
    status = (
        "after_hours_snapshot"
        if statuses and statuses <= {"after_hours_snapshot"}
        else "trading_session"
    )
    expected = max(int(expected_bar_count), 0)
    observed = max(int(observed_bar_count), 0)
    full_market_times = sorted(_utc(item) for item in full_market_captured_at)
    full_market_intervals = [
        (current - previous).total_seconds()
        for previous, current in zip(full_market_times, full_market_times[1:], strict=False)
    ]
    bar_times = sorted({_utc(item) for item in observed_bar_timestamps})
    bar_gaps = [
        (current - previous).total_seconds()
        for previous, current in zip(bar_times, bar_times[1:], strict=False)
    ]
    duration_seconds = max(
        0.0,
        (_utc(completed_at) - _utc(started_at)).total_seconds(),
    )
    minimum_market_snapshots = (
        max(2, math.floor(duration_seconds / 300) - 1)
        if duration_seconds >= 600
        else 0
    )
    return {
        "status": status,
        "started_at": _utc(started_at).isoformat(),
        "completed_at": _utc(completed_at).isoformat(),
        "duration_seconds": duration_seconds,
        "channels": channels,
        "one_minute_bars": {
            "expected_count": expected,
            "observed_count": observed,
            "completeness_ratio": min(observed / expected, 1.0) if expected else None,
            "longest_gap_seconds": max(bar_gaps, default=0.0),
        },
        "full_market": {
            "snapshot_count": len(full_market_times),
            "minimum_expected_count": minimum_market_snapshots,
            "maximum_interval_seconds": max(full_market_intervals, default=None),
        },
    }


def evaluate_probe_report(report: dict[str, Any]) -> dict[str, Any]:
    """按实时事实层硬门槛评估一份交易时段报告。"""

    if report.get("status") == "after_hours_snapshot":
        return {
            "status": "not_applicable",
            "reason_codes": ["outside_trading_session"],
        }
    reasons: list[str] = []
    channels = dict(report.get("channels") or {})
    held = dict(channels.get("held") or {})
    radar = dict(channels.get("radar") or {})
    if _metric(held, "end_to_end_latency_seconds", "p95") > 1.0:
        reasons.append("held_latency_p95_above_1s")
    if _metric(radar, "end_to_end_latency_seconds", "p95") > 5.0:
        reasons.append("radar_latency_p95_above_5s")
    if max(
        _metric(held, "server_freshness_seconds", "p99"),
        _metric(radar, "server_freshness_seconds", "p99"),
    ) > 3.0:
        reasons.append("server_freshness_p99_above_3s")
    for channel, metrics in channels.items():
        if float(metrics.get("http_success_rate") or 0.0) < 0.999:
            reasons.append(f"{channel}_http_loss_above_0_1pct")
        if float(metrics.get("return_coverage_ratio") or 0.0) < 0.999:
            reasons.append(f"{channel}_coverage_below_99_9pct")
        if int(metrics.get("clock_regression_count") or 0) > 0:
            reasons.append(f"{channel}_clock_regression_detected")
    bar_ratio = report.get("one_minute_bars", {}).get("completeness_ratio")
    if bar_ratio is None or float(bar_ratio) < 0.999:
        reasons.append("one_minute_bar_completeness_below_99_9pct")
    market_interval = report.get("full_market", {}).get("maximum_interval_seconds")
    market_count = int(report.get("full_market", {}).get("snapshot_count") or 0)
    minimum_market_count = int(
        report.get("full_market", {}).get("minimum_expected_count") or 0
    )
    if market_count < minimum_market_count:
        reasons.append("full_market_snapshot_count_below_expected")
    if market_interval is not None and float(market_interval) > 300.0:
        reasons.append("full_market_interval_above_300s")
    return {
        "status": "passed" if not reasons else "failed",
        "reason_codes": reasons,
    }


def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    """按通道频率执行只读 HTTP 观测并返回聚合报告。"""

    import requests

    from finance_agent.data.providers.gotdx_gateway import (
        parse_gateway_quotes,
        split_quote_symbols,
    )
    from finance_agent.intraday.bar_aggregation import aggregate_closed_bars

    held = _gateway_symbols(args.held_symbols)
    radar = tuple(symbol for symbol in _gateway_symbols(args.radar_symbols) if symbol not in held)
    symbols_by_channel = {"held": held, "radar": radar}
    started_at = datetime.now(UTC)
    started_monotonic = time.monotonic()
    deadline = started_monotonic + max(args.duration_minutes, 0) * 60
    next_due = {channel: started_monotonic for channel in CHANNEL_INTERVAL_SECONDS}
    observations: list[ProbeObservation] = []
    quotes: list[Any] = []
    http = requests.Session()

    try:
        while True:
            monotonic_now = time.monotonic()
            if monotonic_now >= deadline:
                break
            for channel, interval in CHANNEL_INTERVAL_SECONDS.items():
                channel_symbols = symbols_by_channel[channel]
                if not channel_symbols or monotonic_now < next_due[channel]:
                    continue
                request_started = time.perf_counter()
                received: list[Any] = []
                http_ok = True
                reopened = False
                try:
                    for batch in split_quote_symbols(channel_symbols):
                        response = http.post(
                            f"{str(args.gotdx_url).rstrip('/')}/quotes",
                            json={"symbols": list(batch)},
                            timeout=args.request_timeout_seconds,
                        )
                        response.raise_for_status()
                        received.extend(parse_gateway_quotes(response.json()))
                except Exception:  # noqa: BLE001 - 探针必须记录失败并继续采样
                    http_ok = False
                    reopened = True
                    http.close()
                    http = requests.Session()
                observed_at = datetime.now(UTC)
                latency = time.perf_counter() - request_started
                quotes.extend(received)
                observations.append(
                    ProbeObservation(
                        channel=channel,
                        observed_at=observed_at,
                        requested_symbols=channel_symbols,
                        received_timestamps=tuple(
                            (quote.symbol, quote.server_timestamp) for quote in received
                        ),
                        latency_seconds=latency,
                        http_ok=http_ok,
                        quality_statuses=tuple(
                            sorted({quote.quality_status for quote in received})
                        ),
                        connection_reopened=reopened,
                    )
                )
                next_due[channel] = monotonic_now + interval
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.1, remaining))
    finally:
        http.close()

    completed_at = datetime.now(UTC)
    expected_minutes = _expected_closed_minutes(started_at, completed_at)
    expected_bar_count = len(expected_minutes) * len((*held, *radar))
    observed_bars = aggregate_closed_bars(
        quotes,
        timeframe="1m",
        close_before=completed_at,
    )
    report = build_probe_report(
        observations,
        expected_bar_count=expected_bar_count,
        observed_bar_count=len(observed_bars),
        full_market_captured_at=_full_market_snapshot_times(
            args.database_url,
            started_at=started_at,
            completed_at=completed_at,
        ),
        started_at=started_at,
        completed_at=completed_at,
        observed_bar_timestamps=tuple(bar.timestamp for bar in observed_bars),
    )
    report["acceptance"] = evaluate_probe_report(report)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.duration_minutes <= 0:
        raise SystemExit("--duration-minutes 必须大于 0")
    report = run_probe(args)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(output)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["acceptance"]["status"] == "failed" else 0


def _channel_metrics(observations: Sequence[ProbeObservation]) -> dict[str, Any]:
    successful = [item for item in observations if item.http_ok]
    requested_count = sum(len(item.requested_symbols) for item in observations)
    received_count = sum(
        len({symbol for symbol, _timestamp in item.received_timestamps})
        for item in observations
    )
    freshness = [
        max(0.0, (_utc(item.observed_at) - _utc(timestamp)).total_seconds())
        for item in observations
        for _symbol, timestamp in item.received_timestamps
    ]
    previous_by_symbol: dict[str, datetime] = {}
    duplicate_count = 0
    regression_count = 0
    for item in sorted(observations, key=lambda value: value.observed_at):
        for symbol, timestamp in item.received_timestamps:
            normalized = _utc(timestamp)
            previous = previous_by_symbol.get(symbol)
            if previous is not None and normalized == previous:
                duplicate_count += 1
            elif previous is not None and normalized < previous:
                regression_count += 1
            previous_by_symbol[symbol] = normalized
    successful_times = sorted(_utc(item.observed_at) for item in successful)
    gaps = [
        (current - previous).total_seconds()
        for previous, current in zip(successful_times, successful_times[1:], strict=False)
    ]
    return {
        "request_count": len(observations),
        "http_success_rate": len(successful) / len(observations) if observations else 0.0,
        "return_coverage_ratio": received_count / requested_count if requested_count else 0.0,
        "server_freshness_seconds": _percentiles(freshness),
        "end_to_end_latency_seconds": _percentiles(
            [item.latency_seconds for item in observations]
        ),
        "duplicate_timestamp_count": duplicate_count,
        "clock_regression_count": regression_count,
        "reconnect_count": sum(item.connection_reopened for item in observations),
        "longest_gap_seconds": max(gaps, default=0.0),
        "quality_statuses": sorted(
            {
                status
                for item in observations
                for status in item.quality_statuses
                if status
            }
        ),
    }


def _percentiles(values: Sequence[float]) -> dict[str, float | None]:
    ordered = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not ordered:
        return {"p50": None, "p95": None, "p99": None}

    def nearest_rank(quantile: float) -> float:
        index = max(0, math.ceil(quantile * len(ordered)) - 1)
        return round(ordered[index], 6)

    return {
        "p50": nearest_rank(0.50),
        "p95": nearest_rank(0.95),
        "p99": nearest_rank(0.99),
    }


def _metric(metrics: dict[str, Any], group: str, field: str) -> float:
    value = dict(metrics.get(group) or {}).get(field)
    return float(value) if value is not None else 0.0


def _gateway_symbols(symbols: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for raw in symbols:
        value = str(raw or "").strip().upper()
        code = value.split(".", 1)[0]
        if len(code) != 6 or not code.isdigit():
            raise ValueError(f"A 股代码无效: {raw}")
        suffix = "BJ" if code[0] in {"4", "8"} else "SH" if code[0] == "6" else "SZ"
        normalized = f"{code}.{suffix}"
        if normalized not in result:
            result.append(normalized)
    return tuple(result)


def _expected_closed_minutes(started_at: datetime, completed_at: datetime) -> tuple[datetime, ...]:
    start = _utc(started_at).astimezone(SHANGHAI_TZ).replace(second=0, microsecond=0)
    end = _utc(completed_at).astimezone(SHANGHAI_TZ)
    result: list[datetime] = []
    cursor = start
    while cursor + timedelta(minutes=1) <= end:
        current_time = cursor.timetz().replace(tzinfo=None)
        if day_time(9, 30) <= current_time < day_time(11, 30) or day_time(
            13, 0
        ) <= current_time < day_time(15, 0):
            result.append(cursor)
        cursor += timedelta(minutes=1)
    return tuple(result)


def _full_market_snapshot_times(
    database_url: str | None,
    *,
    started_at: datetime,
    completed_at: datetime,
) -> tuple[datetime, ...]:
    if not database_url:
        return ()
    from sqlalchemy import select

    from finance_agent.storage.db import create_session_factory
    from finance_agent.storage.orm import DataSnapshotORM

    session_factory = create_session_factory(database_url)
    with session_factory() as session:
        statement = (
            select(DataSnapshotORM.captured_at)
            .where(
                DataSnapshotORM.snapshot_type == "ashare_realtime_quotes",
                DataSnapshotORM.provider == "akshare:stock_zh_a_spot",
                DataSnapshotORM.captured_at >= _utc(started_at) - timedelta(minutes=5),
                DataSnapshotORM.captured_at <= _utc(completed_at),
            )
            .order_by(DataSnapshotORM.captured_at)
        )
        return tuple(session.scalars(statement))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("探针时间必须包含时区")
    return value.astimezone(UTC)


if __name__ == "__main__":
    raise SystemExit(main())
