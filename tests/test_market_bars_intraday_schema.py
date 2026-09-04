from __future__ import annotations

from pathlib import Path

from finance_agent.storage.orm import MarketBarIntradayORM


def test_market_bars_intraday_has_separate_orm_table() -> None:
    """分钟线应使用独立 ORM 和独立表，避免和长期日 K 共用生命周期策略。"""

    table = MarketBarIntradayORM.__table__

    assert table.name == "market_bars_intraday"
    assert {column.name for column in table.primary_key.columns} == {
        "asset_id",
        "timeframe",
        "timestamp",
        "source",
        "adjustment",
    }
    assert "idx_market_bars_intraday_asset_tf_time" in {index.name for index in table.indexes}
    assert "idx_market_bars_intraday_closed" in {index.name for index in table.indexes}


def test_market_bars_intraday_migration_has_independent_timescale_policy() -> None:
    """分钟线迁移应创建独立 hypertable，并使用独立压缩策略。"""

    migration = Path(
        "src/finance_agent/storage/migrations/versions/"
        "20260605_0015_create_market_bars_intraday.py"
    )

    content = migration.read_text(encoding="utf-8")

    assert "market_bars_intraday" in content
    assert "create_hypertable" in content
    assert "chunk_time_interval => INTERVAL '7 days'" in content
    assert "add_compression_policy" in content
    assert "INTERVAL '7 days'" in content


def test_market_bars_intraday_documents_closed_bar_writer() -> None:
    """数据库设计必须明确分钟线只由闭合聚合器写入。"""

    content = Path("docs/数据库设计.md").read_text(encoding="utf-8")

    assert "market_bars_intraday" in content
    assert "IntradayBarAggregator" in content
    assert "只写入已经闭合的时间桶" in content
