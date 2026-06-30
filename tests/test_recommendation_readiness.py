from __future__ import annotations

from datetime import UTC, datetime, timedelta

from scripts.data.check_base_data_health import (
    build_recommendation_readiness,
    infer_gaps,
    load_table_counts,
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

    def execute(self, statement: object) -> "RecordingResult":
        self.sql = str(statement)
        return RecordingResult()


class RecordingResult:
    def mappings(self) -> list[dict[str, object]]:
        return []
