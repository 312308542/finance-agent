from __future__ import annotations

from datetime import UTC, datetime, timedelta

from scripts.data.check_base_data_health import (
    build_recommendation_readiness,
    freshness_row_is_stale,
    infer_gaps,
    load_table_counts,
    load_table_freshness,
)


def test_build_recommendation_readiness_marks_ready_when_core_dimensions_pass() -> None:
    checked_at = datetime(2026, 6, 30, 10, tzinfo=UTC)
    readiness = build_recommendation_readiness(
        checked_at=checked_at,
        table_counts={
            "market_bars": 2_200_000,
            "asset_scores": 4_000,
            "factor_frames": 4_000,
            "capital_flow_snapshots": 4_000,
            "fundamental_snapshots": 4_000,
            "event_records": 500,
            "screening_results": 1_000,
        },
        freshness_rows=[
            freshness("market_bars", checked_at - timedelta(hours=2), 12, checked_at),
            freshness("asset_scores", checked_at - timedelta(hours=3), 24, checked_at),
            freshness("factor_frames", checked_at - timedelta(hours=3), 24, checked_at),
            freshness("capital_flow_snapshots", checked_at - timedelta(hours=4), 24, checked_at),
            freshness("fundamental_snapshots", checked_at - timedelta(hours=24), 72, checked_at),
            freshness("event_records", checked_at - timedelta(hours=8), 48, checked_at),
        ],
        universe_counts=[{"universe_id": "ashare:mainboard:tradable", "member_count": 3200}],
        gaps=[],
    )

    assert readiness["status"] == "ready"
    assert readiness["executable"] is True
    assert readiness["reasons"] == []
    assert readiness["dimensions"]["market_bars"]["status"] == "ready"
    assert readiness["dimensions"]["asset_scores"]["status"] == "ready"


def test_build_recommendation_readiness_prefers_tradeable_included_universe_count() -> None:
    """覆盖基数应优先取可交易主板池 included 数，避免历史残留成员抬高基数。"""

    checked_at = datetime(2026, 6, 30, 10, tzinfo=UTC)
    readiness = build_recommendation_readiness(
        checked_at=checked_at,
        table_counts={
            "market_bars": 2_200_000,
            "asset_scores": 4_000,
            "factor_frames": 4_000,
        },
        freshness_rows=[
            freshness("market_bars", checked_at - timedelta(hours=2), 12, checked_at),
            freshness("asset_scores", checked_at - timedelta(hours=3), 24, checked_at),
            freshness("factor_frames", checked_at - timedelta(hours=3), 24, checked_at),
        ],
        universe_counts=[
            {
                "universe_id": "universe:merged:ashare:recommendation",
                "member_count": 5_855,
                "included_member_count": 3_482,
            },
            {
                "universe_id": "universe:tradeable:ashare:main_board",
                "member_count": 3_482,
                "included_member_count": 3_482,
            },
        ],
        gaps=[],
    )

    assert readiness["coverage"]["mainboard_universe_members"] == 3_482


def test_build_recommendation_readiness_keeps_daily_bars_ready_until_next_close() -> None:
    """日 K 应按交易日收盘语义判断，不能在次日凌晨被 12 小时阈值误判过期。"""

    checked_at = datetime(2026, 7, 1, 6, tzinfo=UTC)
    latest_daily_bar = datetime(2026, 6, 30, 15, tzinfo=UTC)
    readiness = build_recommendation_readiness(
        checked_at=checked_at,
        table_counts={
            "market_bars": 2_200_000,
            "asset_scores": 4_000,
            "factor_frames": 4_000,
        },
        freshness_rows=[
            freshness("market_bars", latest_daily_bar, 12, checked_at)
            | {
                "timeframe": "1d",
                "market": "ashare",
                "expected_latest_as_of": latest_daily_bar.isoformat(),
                "expected_close_at": datetime(2026, 7, 1, 7, tzinfo=UTC).isoformat(),
            },
            freshness("asset_scores", checked_at - timedelta(hours=3), 24, checked_at),
            freshness("factor_frames", checked_at - timedelta(hours=3), 24, checked_at),
        ],
        universe_counts=[{"universe_id": "ashare:mainboard:tradable", "member_count": 3200}],
        gaps=[],
    )

    assert readiness["status"] == "ready"
    assert "market_bars_stale" not in readiness["reasons"]
    assert readiness["dimensions"]["market_bars"]["status"] == "ready"
    assert readiness["dimensions"]["market_bars"]["freshness_policy"] == "trading_day_close"


def test_build_recommendation_readiness_keeps_daily_factors_and_scores_ready_until_next_close() -> None:
    """日级因子和评分继承日 K 交易日时间戳，下一次收盘前不应被固定小时阈值误判过期。"""

    checked_at = datetime(2026, 7, 1, 6, tzinfo=UTC)
    latest_daily_snapshot = datetime(2026, 6, 30, 0, tzinfo=UTC)
    freshness_policy = {
        "timeframe": "1d",
        "market": "ashare",
        "freshness_policy": "trading_day_close",
        "expected_latest_as_of": latest_daily_snapshot.isoformat(),
        "expected_close_at": datetime(2026, 7, 1, 7, tzinfo=UTC).isoformat(),
    }
    readiness = build_recommendation_readiness(
        checked_at=checked_at,
        table_counts={
            "market_bars": 2_200_000,
            "asset_scores": 4_000,
            "factor_frames": 4_000,
        },
        freshness_rows=[
            freshness("market_bars", latest_daily_snapshot, 12, checked_at)
            | freshness_policy,
            freshness("asset_scores", latest_daily_snapshot, 24, checked_at)
            | freshness_policy,
            freshness("factor_frames", latest_daily_snapshot, 24, checked_at)
            | freshness_policy,
        ],
        universe_counts=[{"universe_id": "ashare:mainboard:tradable", "member_count": 3200}],
        gaps=[],
    )

    assert readiness["status"] == "ready"
    assert readiness["reasons"] == []
    assert readiness["dimensions"]["asset_scores"]["status"] == "ready"
    assert readiness["dimensions"]["factor_frames"]["status"] == "ready"


def test_daily_derived_snapshot_freshness_detects_missing_expected_trade_date() -> None:
    """日级衍生截面缺少最近应收盘交易日时，应按日期缺口判定 stale。"""

    assert freshness_row_is_stale(
        {
            "table_name": "asset_scores",
            "timeframe": "1d",
            "freshness_policy": "trading_day_close",
            "latest_as_of": "2026-06-29T00:00:00+00:00",
            "expected_latest_as_of": "2026-06-30T00:00:00+00:00",
        }
    )


def test_daily_market_bar_freshness_detects_missing_expected_trade_date() -> None:
    """日 K 缺少最近应收盘交易日时，应按日期缺口判定 stale。"""

    assert freshness_row_is_stale(
        {
            "table_name": "market_bars",
            "timeframe": "1d",
            "latest_as_of": "2026-06-29T00:00:00+00:00",
            "expected_latest_as_of": "2026-06-30T00:00:00+00:00",
        }
    )


def test_build_recommendation_readiness_blocks_when_scores_are_missing_or_stale() -> None:
    checked_at = datetime(2026, 6, 30, 10, tzinfo=UTC)
    readiness = build_recommendation_readiness(
        checked_at=checked_at,
        table_counts={
            "market_bars": 2_200_000,
            "asset_scores": 0,
            "factor_frames": 4_000,
            "capital_flow_snapshots": 10,
            "fundamental_snapshots": 4_000,
            "event_records": 0,
        },
        freshness_rows=[
            freshness("market_bars", checked_at - timedelta(days=3), 12, checked_at),
            freshness("factor_frames", checked_at - timedelta(hours=2), 24, checked_at),
        ],
        universe_counts=[{"universe_id": "ashare:mainboard:tradable", "member_count": 3200}],
        gaps=["market_bars 最近数据已过期，建议补采"],
    )

    assert readiness["status"] == "blocked"
    assert readiness["executable"] is False
    assert "market_bars_stale" in readiness["reasons"]
    assert "asset_scores_empty" in readiness["reasons"]
    assert "event_records_empty" in readiness["warnings"]


def test_build_recommendation_readiness_blocks_when_required_freshness_unknown() -> None:
    checked_at = datetime(2026, 6, 30, 10, tzinfo=UTC)
    readiness = build_recommendation_readiness(
        checked_at=checked_at,
        table_counts={
            "market_bars": 2_200_000,
            "asset_scores": 4_000,
            "factor_frames": 4_000,
        },
        freshness_rows=[
            freshness("asset_scores", checked_at - timedelta(hours=3), 24, checked_at),
            freshness("factor_frames", checked_at - timedelta(hours=3), 24, checked_at),
        ],
        universe_counts=[{"universe_id": "ashare:mainboard:tradable", "member_count": 3200}],
        gaps=[],
    )

    assert readiness["status"] == "blocked"
    assert "market_bars_freshness_unknown" in readiness["reasons"]
    assert readiness["dimensions"]["market_bars"]["status"] == "unknown"


def test_infer_gaps_includes_recommendation_readiness_gap_when_requested() -> None:
    gaps = infer_gaps(
        {
            "assets": 10,
            "asset_universe_members": 10,
            "market_calendars": 1,
            "market_bars": 10,
            "capital_flow_snapshots": 0,
            "fundamental_snapshots": 10,
            "event_records": 10,
            "evidence": 10,
            "risk_findings": 10,
            "crypto_derivative_snapshots": 10,
        },
        provider_rows=[],
        freshness_rows=[],
        recommendation_readiness={"status": "blocked", "reasons": ["capital_flow_snapshots_empty"]},
    )

    assert "推荐就绪度未通过：capital_flow_snapshots_empty" in gaps


def test_load_table_counts_includes_recommendation_readiness_tables() -> None:
    session = RecordingSession()

    load_table_counts(session)

    sql = session.sql.lower()
    assert "indicator_frames" in sql
    assert "factor_frames" in sql
    assert "asset_scores" in sql
    assert "signal_snapshots" in sql
    assert "screening_results" in sql


def test_load_table_freshness_caps_future_event_time_at_collection_time() -> None:
    """事件 freshness 不应被晚于采集时刻的源发布时间推到未来。"""

    session = FreshnessRecordingSession()

    load_table_freshness(session)

    sql = session.sql_statements[0].lower()
    assert "least(coalesce(published_at, collected_at), collected_at)" in sql


def freshness(
    table_name: str,
    latest_as_of: datetime,
    threshold_hours: int,
    checked_at: datetime,
) -> dict[str, object]:
    return {
        "table_name": table_name,
        "latest_as_of": latest_as_of.isoformat(),
        "age_hours": round((checked_at - latest_as_of).total_seconds() / 3600, 2),
        "threshold_hours": threshold_hours,
    }


class RecordingSession:
    def __init__(self) -> None:
        self.sql = ""

    def execute(self, statement: object) -> RecordingResult:
        self.sql = str(statement)
        return RecordingResult()


class RecordingResult:
    def mappings(self) -> list[dict[str, object]]:
        return []


class FreshnessRecordingSession:
    def __init__(self) -> None:
        self.sql_statements: list[str] = []

    def execute(self, statement: object, *_args: object) -> FreshnessRecordingResult:
        self.sql_statements.append(str(statement))
        return FreshnessRecordingResult(is_daily_query=len(self.sql_statements) > 1)


class FreshnessRecordingResult:
    def __init__(self, *, is_daily_query: bool) -> None:
        self.is_daily_query = is_daily_query

    def mappings(self) -> FreshnessRecordingResult:
        return self

    def __iter__(self):
        return iter(())

    def one(self) -> dict[str, object | None]:
        assert self.is_daily_query
        return {
            "latest_as_of": None,
            "expected_latest_as_of": None,
            "expected_close_at": None,
        }
