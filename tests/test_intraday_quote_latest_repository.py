"""盘中临时行情覆盖式仓储测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy.dialects import postgresql

from finance_agent.storage.orm import RealtimeQuoteSnapshotORM
from finance_agent.storage.repositories import AssetRepository

NOW = datetime(2026, 7, 20, 9, 40, tzinfo=UTC)
MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "src"
    / "finance_agent"
    / "storage"
    / "migrations"
    / "versions"
    / "20260904_0028_harden_realtime_quote_history.py"
)


class _Result:
    rowcount = 2


class _Session:
    def __init__(self) -> None:
        self.executed: list[Any] = []
        self.flush_count = 0

    def execute(self, statement: Any) -> _Result:
        self.executed.append(statement)
        return _Result()

    def flush(self) -> None:
        self.flush_count += 1

    def get_one(self, _model: Any, key: Any) -> Any:
        return SimpleNamespace(key=key)

    def scalars(self, _statement: Any) -> list[Any]:
        self.executed.append(_statement)
        return []


def _compiled(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def test_realtime_history_schema_tracks_capture_time_and_quality() -> None:
    columns = RealtimeQuoteSnapshotORM.__table__.c
    indexes = {index.name for index in RealtimeQuoteSnapshotORM.__table__.indexes}

    assert {"captured_at", "freshness_ms", "quality_status"}.issubset(columns.keys())
    assert columns.captured_at.nullable is False
    assert columns.quality_status.nullable is False
    assert "idx_realtime_quotes_quality_asof" in indexes


def test_realtime_history_migration_adds_retention_policies() -> None:
    content = MIGRATION_PATH.read_text(encoding="utf-8")

    assert 'revision = "20260904_0028"' in content
    assert 'down_revision = "20260831_0027"' in content
    assert "create_hypertable('realtime_quote_snapshots'" in content
    assert "add_retention_policy('realtime_quote_snapshots', INTERVAL '7 days'" in content
    assert "add_retention_policy('market_bars_intraday', INTERVAL '180 days'" in content


def test_realtime_history_upsert_writes_capture_and_quality_columns() -> None:
    session = _Session()

    count = AssetRepository(session).upsert_realtime_quote_snapshots(
        [
            {
                "asset_id": "ashare:600519",
                "source": "gotdx:tdx_main",
                "symbol": "600519",
                "market": "ashare",
                "as_of": NOW,
                "captured_at": NOW,
                "freshness_ms": 250,
                "quality_status": "available",
                "last_price": Decimal("1500.25"),
            }
        ]
    )

    sql = _compiled(session.executed[0])
    assert count == 1
    assert "captured_at" in sql
    assert "freshness_ms" in sql
    assert "quality_status" in sql


def test_intraday_latest_upsert_overwrites_by_asset_and_source() -> None:
    session = _Session()

    count = AssetRepository(session).upsert_intraday_quote_latest(
        [
            {
                "asset_id": "ashare:600519",
                "source": "gotdx:tdx_main",
                "symbol": "600519",
                "market": "ashare",
                "as_of": NOW,
                "captured_at": NOW,
                "last_price": Decimal("1500.25"),
                "quality_status": "available",
            }
        ]
    )

    sql = _compiled(session.executed[0])
    assert count == 1
    assert "ON CONFLICT (asset_id, source) DO UPDATE SET" in sql
    assert session.flush_count == 1


def test_clear_intraday_latest_can_limit_to_market() -> None:
    session = _Session()

    count = AssetRepository(session).clear_intraday_quote_latest(market="ashare")

    assert count == 2
    assert "DELETE FROM intraday_quote_latest" in _compiled(session.executed[0])
    assert "intraday_quote_latest.market =" in _compiled(session.executed[0])


def test_list_intraday_latest_reads_latest_rows_for_risk_and_position_consumers() -> None:
    """盘中临时表必须提供统一读取入口，避免消费者继续依赖历史高频表。"""

    session = _Session()
    rows = AssetRepository(session).list_intraday_quote_latest(
        asset_ids=("ashare:600519",),
        market="ashare",
    )

    assert rows == []
    assert "SELECT" in _compiled(session.executed[0])
    assert "intraday_quote_latest" in _compiled(session.executed[0])


def test_list_realtime_history_filters_assets_sources_and_time_window() -> None:
    session = _Session()

    rows = AssetRepository(session).list_realtime_quote_snapshots(
        asset_ids=("ashare:600519",),
        sources=("gotdx:tdx_main",),
        start_at=NOW - timedelta(minutes=5),
        end_at=NOW,
        quality_statuses=("available",),
    )

    sql = _compiled(session.executed[0])
    assert rows == []
    assert "FROM realtime_quote_snapshots" in sql
    assert "realtime_quote_snapshots.asset_id IN" in sql
    assert "realtime_quote_snapshots.source IN" in sql
    assert "realtime_quote_snapshots.as_of >=" in sql
    assert "realtime_quote_snapshots.as_of <=" in sql
    assert "realtime_quote_snapshots.quality_status IN" in sql
    assert "ORDER BY realtime_quote_snapshots.asset_id" in sql
