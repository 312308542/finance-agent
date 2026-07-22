"""盘中临时行情覆盖式仓储测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from sqlalchemy.dialects import postgresql

from finance_agent.storage.repositories import AssetRepository

NOW = datetime(2026, 7, 20, 9, 40, tzinfo=UTC)


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
