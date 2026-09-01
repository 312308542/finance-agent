"""补跑规则单元测试：cutoff/报告期/窗口/区间压缩/指纹/合并策略。"""

from datetime import UTC, date, datetime

from finance_agent.data_recovery.gap_detector import (
    compress_consecutive_dates,
    latest_closed_trading_date,
    report_period_to_date,
)
from finance_agent.data_recovery.gate import classify_recovery_policy
from finance_agent.data_recovery.models import RECOVERY_STRATEGY_VERSION, GapTarget
from finance_agent.data_recovery.repository import plan_fingerprint


def test_report_period_to_date_formats() -> None:
    assert report_period_to_date("20260630") == date(2026, 6, 30)
    assert report_period_to_date("2026-06-30") == date(2026, 6, 30)
    assert report_period_to_date("") is None
    assert report_period_to_date(None) is None
    assert report_period_to_date("bad") is None


def test_compress_consecutive_dates_merges_runs() -> None:
    dates = [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5), date(2026, 8, 10)]
    ranges = compress_consecutive_dates(dates)
    assert ranges == [
        (date(2026, 8, 3), date(2026, 8, 5)),
        (date(2026, 8, 10), date(2026, 8, 10)),
    ]
    assert compress_consecutive_dates([]) == []


def test_latest_closed_trading_date_ignores_future_and_partial() -> None:
    from types import SimpleNamespace

    now = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)

    def row(day: int, *, trading: bool = True, close_hour: int = 16) -> SimpleNamespace:
        return SimpleNamespace(
            trade_date=date(2026, 8, day),
            is_trading_day=trading,
            status="active",
            close_at=datetime(2026, 8, day, close_hour, tzinfo=UTC),
        )

    rows = [row(21), row(24, trading=False), row(25)]
    assert latest_closed_trading_date(rows, now=now) == date(2026, 8, 21)


def _scope(asset: str = "ashare:600000", start: str = "2026-08-18") -> str:
    target = GapTarget(
        data_domain="market_bars",
        asset_id=asset,
        gap_start_at=date.fromisoformat(start),
        gap_end_at=date.fromisoformat("2026-08-21"),
        granularity="1d",
        expected_count=4,
    )
    return target.scope_key()


def test_plan_fingerprint_stable_and_sensitive() -> None:
    base = plan_fingerprint(
        market="ashare",
        universe_snapshot_hash="u1",
        cutoff_date=date(2026, 8, 21),
        gap_scope_keys=[_scope(), _scope("ashare:000001")],
        strategy_version=RECOVERY_STRATEGY_VERSION,
    )
    reordered = plan_fingerprint(
        market="ashare",
        universe_snapshot_hash="u1",
        cutoff_date=date(2026, 8, 21),
        gap_scope_keys=[_scope("ashare:000001"), _scope()],
        strategy_version=RECOVERY_STRATEGY_VERSION,
    )
    changed = plan_fingerprint(
        market="ashare",
        universe_snapshot_hash="u1",
        cutoff_date=date(2026, 8, 22),
        gap_scope_keys=[_scope()],
        strategy_version=RECOVERY_STRATEGY_VERSION,
    )
    assert base == reordered
    assert base != changed
    assert len(base) == 32


def test_classify_recovery_policy_prefixes() -> None:
    assert classify_recovery_policy("quality.ashare") == "requires_open"
    assert classify_recovery_policy("analytics.recommendations") == "requires_open"
    assert classify_recovery_policy("ashare.realtime_quotes") == "always"
    assert classify_recovery_policy("ashare.events") == "always"
    assert classify_recovery_policy("crypto_future.derivatives") == "always"
    assert classify_recovery_policy("ashare.market_bars") == "merge"
