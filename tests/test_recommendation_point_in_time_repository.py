"""推荐点时批量事实读取契约测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.dialects import postgresql

from finance_agent.storage.repositories import (
    AssetRepository,
    FactorFrameRepository,
    IndicatorFrameRepository,
    MarketDataRepository,
)

AS_OF = datetime(2026, 9, 8, 7, 0, tzinfo=UTC)


class _Rows:
    def __iter__(self):
        return iter(())

    def one_or_none(self) -> None:
        return None


class _Session:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    def scalars(self, statement: Any) -> _Rows:
        self.statements.append(statement)
        return _Rows()


def _sql(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def test_factor_batch_reader_filters_at_or_before_decision_time() -> None:
    session = _Session()

    rows = FactorFrameRepository(session).list_latest_factor_frames(
        asset_ids=("ashare:000001", "ashare:600519"),
        horizon="swing",
        as_of=AS_OF,
    )

    sql = _sql(session.statements[0])
    assert rows == []
    assert "factor_frames.as_of <=" in sql
    assert "DISTINCT ON (factor_frames.asset_id)" in sql


def test_structure_batch_reader_filters_input_end_and_as_of() -> None:
    session = _Session()

    rows = IndicatorFrameRepository(session).list_latest_indicator_frames(
        asset_ids=("ashare:000001", "ashare:600519"),
        timeframes=("1d", "60m"),
        horizons=("smc_lite_v2", "ichimoku_v1"),
        library="structural-lite",
        as_of=AS_OF,
    )

    sql = _sql(session.statements[0])
    assert rows == []
    assert "indicator_frames.input_end_at <=" in sql
    assert "indicator_frames.as_of <=" in sql
    assert "DISTINCT ON (indicator_frames.asset_id, indicator_frames.timeframe" in sql


def test_trading_status_batch_reader_filters_at_or_before_decision_time() -> None:
    session = _Session()

    rows = AssetRepository(session).list_latest_statuses(
        asset_ids=("ashare:000001", "ashare:600519"),
        as_of=AS_OF,
    )

    sql = _sql(session.statements[0])
    assert rows == []
    assert "asset_status_snapshots.as_of <=" in sql
    assert "DISTINCT ON (asset_status_snapshots.asset_id)" in sql


def test_latest_close_batch_reader_is_point_in_time_and_closed_only() -> None:
    session = _Session()

    rows = MarketDataRepository(session).list_latest_closed_bars(
        asset_ids=("ashare:000001", "ashare:600519"),
        timeframe="1d",
        as_of=AS_OF,
    )

    sql = _sql(session.statements[0])
    assert rows == []
    assert "market_bars.timestamp <=" in sql
    assert "market_bars.is_closed IS true" in sql
    assert "DISTINCT ON (market_bars.asset_id)" in sql
