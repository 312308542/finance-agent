"""实时行情连续压力探针的指标与门槛测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from scripts.integration.probe_realtime_quote_monitor import (
    ProbeObservation,
    build_parser,
    build_probe_report,
    evaluate_probe_report,
)

NOW = datetime(2026, 9, 7, 1, 30, tzinfo=UTC)


def test_probe_report_counts_quality_latency_and_timestamp_anomalies() -> None:
    observations = (
        ProbeObservation(
            channel="held",
            observed_at=NOW,
            requested_symbols=("600519.SH", "000001.SZ"),
            received_timestamps=(("600519", NOW - timedelta(seconds=1)), ("000001", NOW - timedelta(seconds=1))),
            latency_seconds=0.4,
            http_ok=True,
            quality_statuses=("available",),
            connection_reopened=False,
        ),
        ProbeObservation(
            channel="held",
            observed_at=NOW + timedelta(seconds=1),
            requested_symbols=("600519.SH", "000001.SZ"),
            received_timestamps=(("600519", NOW - timedelta(seconds=1)), ("000001", NOW - timedelta(seconds=2))),
            latency_seconds=0.6,
            http_ok=True,
            quality_statuses=("available",),
            connection_reopened=True,
        ),
    )

    report = build_probe_report(
        observations,
        expected_bar_count=4,
        observed_bar_count=3,
        observed_bar_timestamps=(NOW, NOW + timedelta(minutes=1), NOW + timedelta(minutes=3)),
        full_market_captured_at=(NOW, NOW + timedelta(minutes=5)),
        started_at=NOW,
        completed_at=NOW + timedelta(seconds=2),
    )
    held = report["channels"]["held"]

    assert held["request_count"] == 2
    assert held["http_success_rate"] == 1.0
    assert held["return_coverage_ratio"] == 1.0
    assert held["duplicate_timestamp_count"] == 1
    assert held["clock_regression_count"] == 1
    assert held["reconnect_count"] == 1
    assert held["longest_gap_seconds"] == 1.0
    assert held["end_to_end_latency_seconds"]["p95"] == 0.6
    assert report["one_minute_bars"]["completeness_ratio"] == 0.75
    assert report["one_minute_bars"]["longest_gap_seconds"] == 120.0
    assert report["full_market"]["maximum_interval_seconds"] == 300.0


def test_probe_gate_reports_each_failed_trading_threshold() -> None:
    report = build_probe_report(
        (
            ProbeObservation(
                channel="held",
                observed_at=NOW,
                requested_symbols=("600519.SH",),
                received_timestamps=(("600519", NOW - timedelta(seconds=4)),),
                latency_seconds=1.2,
                http_ok=True,
                quality_statuses=("available",),
                connection_reopened=False,
            ),
        ),
        expected_bar_count=1000,
        observed_bar_count=998,
        full_market_captured_at=(NOW, NOW + timedelta(minutes=6)),
        started_at=NOW,
        completed_at=NOW + timedelta(minutes=1),
    )

    result = evaluate_probe_report(report)

    assert result["status"] == "failed"
    assert set(result["reason_codes"]) == {
        "held_latency_p95_above_1s",
        "server_freshness_p99_above_3s",
        "one_minute_bar_completeness_below_99_9pct",
        "full_market_interval_above_300s",
    }


def test_after_hours_probe_is_not_misclassified_as_trading_acceptance() -> None:
    report = build_probe_report(
        (
            ProbeObservation(
                channel="held",
                observed_at=NOW,
                requested_symbols=("600519.SH",),
                received_timestamps=(("600519", NOW - timedelta(hours=12)),),
                latency_seconds=0.2,
                http_ok=True,
                quality_statuses=("after_hours_snapshot",),
                connection_reopened=False,
            ),
        ),
        expected_bar_count=0,
        observed_bar_count=0,
        full_market_captured_at=(),
        started_at=NOW,
        completed_at=NOW + timedelta(minutes=5),
    )

    assert report["status"] == "after_hours_snapshot"
    assert report["one_minute_bars"]["completeness_ratio"] is None
    assert evaluate_probe_report(report) == {
        "status": "not_applicable",
        "reason_codes": ["outside_trading_session"],
    }


def test_probe_gate_rejects_insufficient_full_market_snapshots() -> None:
    report = build_probe_report(
        (
            ProbeObservation(
                channel="held",
                observed_at=NOW,
                requested_symbols=("600519.SH",),
                received_timestamps=(("600519", NOW),),
                latency_seconds=0.2,
                http_ok=True,
                quality_statuses=("available",),
                connection_reopened=False,
            ),
        ),
        expected_bar_count=1,
        observed_bar_count=1,
        full_market_captured_at=(NOW,),
        started_at=NOW,
        completed_at=NOW + timedelta(minutes=11),
    )

    assert evaluate_probe_report(report)["reason_codes"] == [
        "full_market_snapshot_count_below_expected"
    ]


def test_probe_cli_defaults_to_two_hours_and_requires_held_symbols() -> None:
    args = build_parser().parse_args(["--held-symbols", "601222", "601330"])

    assert args.duration_minutes == 120
    assert args.held_symbols == ["601222", "601330"]
    assert args.radar_symbols == []
    assert args.output == "output/realtime-monitor-probe.json"
