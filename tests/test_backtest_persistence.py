from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy.dialects import postgresql

from finance_agent.storage.orm import BacktestResultORM
from finance_agent.storage.repositories import BacktestRepository


class _FakeResult:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []

    def scalars(self) -> _FakeResult:
        return self

    def one_or_none(self) -> Any | None:
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class _FakeSession:
    def __init__(self) -> None:
        self.results: dict[str, Any] = {}
        self.executed: list[Any] = []
        self.flushed = False

    def execute(self, statement: Any) -> _FakeResult:
        self.executed.append(statement)
        sql = str(statement)
        params = statement.compile(dialect=postgresql.dialect()).params
        if "INSERT INTO backtest_results" in sql:
            self.add(BacktestResultORM(**params))
        return _FakeResult()

    def scalars(self, statement: Any) -> _FakeResult:
        sql = str(statement)
        params = statement.compile(dialect=postgresql.dialect()).params
        if "backtest_results" not in sql:
            return _FakeResult()
        rows = list(self.results.values())
        market = params.get("market_1")
        strategy_id = params.get("strategy_id_1")
        universe_id = params.get("universe_id_1")
        status = params.get("status_1")
        if market:
            rows = [row for row in rows if row.market == market]
        if strategy_id:
            rows = [row for row in rows if row.strategy_id == strategy_id]
        if universe_id:
            rows = [row for row in rows if row.universe_id == universe_id]
        if status:
            rows = [row for row in rows if row.status == status]
        return _FakeResult(sorted(rows, key=lambda row: row.created_at, reverse=True))

    def get_one(self, model: Any, key: str) -> Any:
        if model is BacktestResultORM:
            return self.results[key]
        raise LookupError(key)

    def add(self, instance: Any) -> None:
        if isinstance(instance, BacktestResultORM):
            self.results[instance.backtest_id] = instance

    def flush(self) -> None:
        self.flushed = True


def _compiled(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def test_backtest_result_orm_matches_0019_contract() -> None:
    """回测结果表应保存可复现指标、数据版本和明细 payload。"""

    table = BacktestResultORM.__table__

    assert table.name == "backtest_results"
    assert {column.name for column in table.primary_key.columns} == {"backtest_id"}
    assert "idx_backtest_results_strategy_created" in {index.name for index in table.indexes}
    assert "idx_backtest_results_universe_created" in {index.name for index in table.indexes}
    assert "metrics" in table.c
    assert "data_versions" in table.c
    assert "payload" in table.c


def test_backtest_result_migration_has_chinese_comments() -> None:
    """0019 迁移应创建 backtest_results 并补齐中文 DDL 注释。"""

    migration = Path(
        "src/finance_agent/storage/migrations/versions/"
        "20260614_0019_create_backtest_tables.py"
    )
    content = migration.read_text(encoding="utf-8")

    assert 'revision = "20260614_0019"' in content
    assert 'down_revision = "20260613_0018"' in content
    assert "backtest_results" in content
    assert "COMMENT ON TABLE backtest_results" in content
    assert "回测结果表" in content
    assert "数据版本" in content


def test_backtest_repository_upserts_result_and_lists_latest() -> None:
    """BacktestRepository 应幂等保存回测摘要，并按策略查询最近结果。"""

    session = _FakeSession()
    created_at = datetime(2026, 6, 14, 9, 30, tzinfo=UTC)

    row = BacktestRepository(session).upsert_result(
        backtest_id="backtest:factor_score_topn:ashare:1",
        market="ashare",
        strategy_id="strategy:ashare:short_swing",
        universe_id="universe:tradeable:ashare:main_board",
        start_at=datetime(2021, 1, 1, tzinfo=UTC),
        end_at=datetime(2026, 1, 1, tzinfo=UTC),
        rebalance_frequency="monthly",
        metrics={
            "cagr": Decimal("0.1234"),
            "max_drawdown": Decimal("-0.1800"),
            "generated_at": created_at,
        },
        data_versions={"score_mode": "replayed", "bars_watermark": created_at},
        payload={"monthly_returns": [{"month": "2026-01", "return": Decimal("0.0100")}]},
        status="available",
        created_at=created_at,
    )

    latest = BacktestRepository(session).get_latest_result(
        market="ashare",
        strategy_id="strategy:ashare:short_swing",
        universe_id="universe:tradeable:ashare:main_board",
    )

    assert row is session.results["backtest:factor_score_topn:ashare:1"]
    assert latest is row
    assert row.metrics["cagr"] == "0.1234"
    assert row.metrics["generated_at"] == created_at.isoformat()
    assert row.data_versions["bars_watermark"] == created_at.isoformat()
    assert row.payload["monthly_returns"][0]["return"] == "0.0100"
    assert session.flushed is True
    assert "INSERT INTO backtest_results" in _compiled(session.executed[0])


def test_backtest_repository_lists_recent_results() -> None:
    """列表查询应支持按市场、策略、候选池和状态筛选。"""

    session = _FakeSession()
    older = BacktestResultORM(
        backtest_id="backtest:old",
        market="ashare",
        strategy_id="strategy:ashare:short_swing",
        universe_id="universe:tradeable:ashare:main_board",
        start_at=datetime(2021, 1, 1, tzinfo=UTC),
        end_at=datetime(2025, 1, 1, tzinfo=UTC),
        rebalance_frequency="monthly",
        metrics={},
        data_versions={},
        payload={},
        status="available",
        created_at=datetime(2026, 6, 13, tzinfo=UTC),
    )
    newer = SimpleNamespace(
        backtest_id="backtest:new",
        market="ashare",
        strategy_id="strategy:ashare:short_swing",
        universe_id="universe:tradeable:ashare:main_board",
        status="available",
        created_at=datetime(2026, 6, 14, tzinfo=UTC),
    )
    session.results = {older.backtest_id: older, newer.backtest_id: newer}

    rows = BacktestRepository(session).list_results(
        market="ashare",
        strategy_id="strategy:ashare:short_swing",
        universe_id="universe:tradeable:ashare:main_board",
        status="available",
        limit=10,
    )

    assert [row.backtest_id for row in rows] == ["backtest:new", "backtest:old"]
