from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.dialects import postgresql

from finance_agent.storage.repositories import MarketDataRepository


class _FakeScalarResult:
    def __iter__(self):
        return iter(())


class _FakeSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []
        self.execute_statements: list[Any] = []
        self.flush_count = 0

    def scalars(self, statement: Any) -> _FakeScalarResult:
        self.statements.append(statement)
        return _FakeScalarResult()

    def execute(self, statement: Any) -> None:
        self.execute_statements.append(statement)

    def flush(self) -> None:
        self.flush_count += 1


def _compiled(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def _market_bar_row(index: int) -> dict[str, Any]:
    return {
        "asset_id": "ashare:000001",
        "symbol": "000001",
        "market": "ashare",
        "timeframe": "1d",
        "timestamp": datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index),
        "end_timestamp": None,
        "open": Decimal("10.00"),
        "high": Decimal("10.50"),
        "low": Decimal("9.90"),
        "close": Decimal("10.20"),
        "volume": Decimal("1000"),
        "amount": Decimal("10200"),
        "source": "canonical:ashare:kline",
        "adjustment": "qfq",
        "is_closed": True,
        "raw_record_id": "raw:market-bar",
        "status": "available",
    }


def test_list_recent_bars_excludes_non_final_statuses_by_default() -> None:
    """最近 K 线读取默认只使用正式可用数据，避免 partial/error 污染指标。"""

    session = _FakeSession()
    repository = MarketDataRepository(session)

    rows = repository.list_recent_bars(
        asset_id="ashare:000001",
        timeframe="1d",
        limit=120,
    )

    sql = _compiled(session.statements[0])

    assert rows == []
    assert "market_bars.is_closed IS true" in sql
    assert "market_bars.status IN" in sql


def test_list_window_bars_excludes_non_final_statuses_by_default() -> None:
    """窗口 K 线读取也应默认排除 partial/error，保持批量链路口径一致。"""

    session = _FakeSession()
    repository = MarketDataRepository(session)

    rows = repository.list_window_bars(
        asset_ids=["ashare:000001"],
        timeframe="1d",
        start_at=datetime(2026, 6, 1, tzinfo=UTC),
        end_at=datetime(2026, 6, 5, tzinfo=UTC),
    )

    sql = _compiled(session.statements[0])

    assert rows == []
    assert "market_bars.status IN" in sql


def test_upsert_bars_writes_rows_in_500_row_chunks() -> None:
    """批量写入 K 线时应按 500 条分块，避免单根 K 线一次数据库往返。"""

    session = _FakeSession()
    repository = MarketDataRepository(session)

    row_count = repository.upsert_bars([_market_bar_row(index) for index in range(1001)])

    assert row_count == 1001
    assert len(session.execute_statements) == 3
    assert session.flush_count == 1


def test_upsert_bars_deduplicates_conflict_keys_before_batch_write() -> None:
    """同一批内重复 K 线应先去重，避免 PostgreSQL 同一行被 ON CONFLICT 更新两次。"""

    session = _FakeSession()
    repository = MarketDataRepository(session)
    first_row = _market_bar_row(0)
    revised_row = first_row | {"close": Decimal("10.80")}

    row_count = repository.upsert_bars([first_row, revised_row])

    assert row_count == 1
    assert len(session.execute_statements) == 1
    assert session.flush_count == 1
