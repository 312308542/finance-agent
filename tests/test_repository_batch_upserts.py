from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from sqlalchemy.dialects import postgresql

from finance_agent.storage.orm import AssetUniverseORM
from finance_agent.storage.repositories import (
    AssetRepository,
    CapitalFlowRepository,
    DerivativeDataRepository,
    EventRepository,
    FundamentalDataRepository,
    FundNavRepository,
    MarketCalendarRepository,
    RiskRepository,
    UniverseRepository,
)


class _FakeScalarResult:
    def __iter__(self):
        return iter(())

    def one(self) -> Any:
        return SimpleNamespace()

    def one_or_none(self) -> Any:
        return None


class _FakeSession:
    def __init__(self) -> None:
        self.executed: list[Any] = []
        self.scalars_statements: list[Any] = []
        self.flush_count = 0

    def execute(self, statement: Any) -> None:
        self.executed.append(statement)

    def scalars(self, statement: Any) -> _FakeScalarResult:
        self.scalars_statements.append(statement)
        return _FakeScalarResult()

    def flush(self) -> None:
        self.flush_count += 1

    def get_one(self, model: Any, key: Any) -> Any:
        if model is AssetUniverseORM:
            return SimpleNamespace(universe_id=key, market="ashare")
        return SimpleNamespace(key=key)


class _RowCountResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _RowCountSession(_FakeSession):
    def __init__(self, rowcounts: list[int]) -> None:
        super().__init__()
        self.rowcounts = rowcounts

    def execute(self, statement: Any) -> _RowCountResult:
        self.executed.append(statement)
        return _RowCountResult(self.rowcounts.pop(0))


def _compiled(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def _assert_chunked_batch(session: _FakeSession, *, row_count: int) -> None:
    assert len(session.executed) == 3
    assert session.flush_count == 1
    assert row_count == 1001


def _asset_row(index: int) -> dict[str, Any]:
    symbol = f"{index:06d}"
    return {
        "asset_id": f"ashare:{symbol}",
        "symbol": symbol,
        "name": f"测试股票{index}",
        "market": "ashare",
        "asset_type": "stock",
        "exchange": "SSE",
        "currency": "CNY",
        "payload": {"source": "test"},
    }


def test_asset_repository_batch_methods_write_in_chunks() -> None:
    as_of = datetime(2026, 6, 8, tzinfo=UTC)
    rows = [_asset_row(index) for index in range(1001)]

    session = _FakeSession()
    row_count = AssetRepository(session).upsert_asset_masters(rows)
    _assert_chunked_batch(session, row_count=row_count)

    session = _FakeSession()
    row_count = AssetRepository(session).ensure_assets(rows)
    _assert_chunked_batch(session, row_count=row_count)

    session = _FakeSession()
    row_count = AssetRepository(session).upsert_asset_profiles(
        [
            {
                "asset_id": row["asset_id"],
                "symbol": row["symbol"],
                "name": row["name"],
                "market": row["market"],
                "source": "test:asset_profile",
                "as_of": as_of,
            }
            for row in rows
        ]
    )
    _assert_chunked_batch(session, row_count=row_count)

    session = _FakeSession()
    row_count = AssetRepository(session).upsert_asset_provider_mappings(
        [
            {
                "asset_id": row["asset_id"],
                "symbol": row["symbol"],
                "market": row["market"],
                "provider": "test",
                "provider_symbol": row["symbol"],
                "source": "test:mapping",
            }
            for row in rows
        ]
    )
    _assert_chunked_batch(session, row_count=row_count)

    session = _FakeSession()
    row_count = AssetRepository(session).upsert_asset_status_snapshots(
        [
            {
                "asset_id": row["asset_id"],
                "symbol": row["symbol"],
                "market": row["market"],
                "source": "test:status",
                "as_of": as_of,
                "tradable": True,
                "trading_status": "available",
            }
            for row in rows
        ]
    )
    _assert_chunked_batch(session, row_count=row_count)

    session = _FakeSession()
    row_count = AssetRepository(session).upsert_realtime_quote_snapshots(
        [
            {
                "asset_id": row["asset_id"],
                "symbol": row["symbol"],
                "market": row["market"],
                "source": "test:quote",
                "as_of": as_of + timedelta(seconds=index),
                "last_price": Decimal("10.00"),
            }
            for index, row in enumerate(rows)
        ]
    )
    _assert_chunked_batch(session, row_count=row_count)


def test_universe_and_calendar_batch_methods_write_in_chunks() -> None:
    as_of = datetime(2026, 6, 8, tzinfo=UTC)

    session = _FakeSession()
    UniverseRepository(session).replace_members(
        universe_id="universe:test:ashare",
        members=[
            {
                "member_id": f"member:{index}",
                "asset_id": f"ashare:{index:06d}",
                "symbol": f"{index:06d}",
                "market": "ashare",
                "as_of": as_of,
            }
            for index in range(1001)
        ],
    )
    assert len(session.executed) == 3
    assert session.flush_count == 1

    session = _FakeSession()
    MarketCalendarRepository(session).replace_calendar_entries(
        [
            {
                "calendar_id": f"calendar:{index}",
                "market": "ashare",
                "exchange": "SSE",
                "trade_date": date(2026, 1, 1) + timedelta(days=index),
                "is_trading_day": True,
                "session_type": "regular",
                "timezone": "Asia/Shanghai",
                "source": "test:calendar",
            }
            for index in range(1001)
        ]
    )
    assert len(session.executed) == 3
    assert session.flush_count == 1


def test_universe_repository_prunes_missing_members() -> None:
    """显式 prune 应只把本轮缺席的旧成员标记为 excluded，保留 upsert 默认语义。"""

    session = _RowCountSession(rowcounts=[2])
    result = UniverseRepository(session).prune_missing_members(
        universe_id="universe:test:ashare",
        current_asset_ids=["ashare:000001", "ashare:600519"],
        as_of=datetime(2026, 6, 30, tzinfo=UTC),
        removed_reason="not_in_latest_merge",
    )

    sql = _compiled(session.executed[0])

    assert result == 2
    assert "UPDATE asset_universe_members" in sql
    assert "asset_universe_members.universe_id = " in sql
    assert "asset_universe_members.included IS true" in sql
    assert "asset_universe_members.asset_id NOT IN" in sql
    assert session.flush_count == 1


def test_fact_repository_batch_methods_write_in_chunks() -> None:
    as_of = datetime(2026, 6, 8, tzinfo=UTC)

    cases: list[tuple[Any, str, list[dict[str, Any]]]] = [
        (
            FundNavRepository,
            "upsert_snapshots",
            [
                {
                    "snapshot_id": f"fund_nav:{index}",
                    "asset_id": "fund:open:000001",
                    "symbol": "000001",
                    "market": "fund",
                    "nav_date": date(2026, 1, 1) + timedelta(days=index),
                    "source": "test:fund_nav",
                    "unit_nav": Decimal("1.0000"),
                }
                for index in range(1001)
            ],
        ),
        (
            FundamentalDataRepository,
            "upsert_fundamental_snapshots",
            [
                {
                    "snapshot_id": f"fundamental:{index}",
                    "asset_id": f"ashare:{index:06d}",
                    "symbol": f"{index:06d}",
                    "source": "test:fundamental",
                    "status": "available",
                    "as_of": as_of + timedelta(seconds=index),
                }
                for index in range(1001)
            ],
        ),
        (
            CapitalFlowRepository,
            "upsert_capital_flow_snapshots",
            [
                {
                    "snapshot_id": f"capital_flow:{index}",
                    "asset_id": f"ashare:{index:06d}",
                    "symbol": f"{index:06d}",
                    "market": "ashare",
                    "window": "today",
                    "source": "test:capital_flow",
                    "status": "available",
                    "as_of": as_of + timedelta(seconds=index),
                    "amount": Decimal("1000"),
                }
                for index in range(1001)
            ],
        ),
        (
            DerivativeDataRepository,
            "upsert_crypto_derivative_snapshots",
            [
                {
                    "snapshot_id": f"derivative:{index}",
                    "asset_id": f"crypto_future:BTCUSDT:{index}",
                    "symbol": "BTCUSDT",
                    "market": "crypto_future",
                    "source": "test:derivative",
                    "as_of": as_of + timedelta(seconds=index),
                }
                for index in range(1001)
            ],
        ),
        (
            RiskRepository,
            "upsert_risk_findings",
            [
                {
                    "risk_id": f"risk:{index}",
                    "scope": "asset",
                    "risk_type": "test",
                    "severity": "medium",
                    "title": "测试风险",
                    "as_of": as_of + timedelta(seconds=index),
                }
                for index in range(1001)
            ],
        ),
    ]

    for repo_cls, method_name, rows in cases:
        session = _FakeSession()
        row_count = getattr(repo_cls(session), method_name)(rows)
        _assert_chunked_batch(session, row_count=row_count)


def test_event_repository_batch_methods_write_in_chunks() -> None:
    as_of = datetime(2026, 6, 8, tzinfo=UTC)

    session = _FakeSession()
    row_count = EventRepository(session).upsert_events(
        [
            {
                "event_id": f"event:{index}",
                "market": "ashare",
                "event_type": "news",
                "title": "测试新闻",
                "sentiment": "neutral",
                "importance": "medium",
                "source": "test:event",
                "collected_at": as_of + timedelta(seconds=index),
            }
            for index in range(1001)
        ]
    )
    _assert_chunked_batch(session, row_count=row_count)

    session = _FakeSession()
    row_count = EventRepository(session).upsert_evidence_items(
        [
            {
                "evidence_id": f"evidence:{index}",
                "evidence_type": "news",
                "source": "test:evidence",
                "title": "测试证据",
                "reliability": "medium",
                "collected_at": as_of + timedelta(seconds=index),
            }
            for index in range(1001)
        ]
    )
    _assert_chunked_batch(session, row_count=row_count)


def test_event_repository_article_payload_updates_write_in_chunks() -> None:
    rows = [
        {
            "event_id": f"event:news:{index}",
            "article_payload": {
                "status": "available",
                "full_text": f"正文 {index}",
                "text_length": index,
            },
        }
        for index in range(1001)
    ]

    session = _FakeSession()
    row_count = EventRepository(session).update_event_article_payloads(rows)
    _assert_chunked_batch(session, row_count=row_count)

    session = _FakeSession()
    row_count = EventRepository(session).update_evidence_article_payloads_by_events(rows)
    _assert_chunked_batch(session, row_count=row_count)


def test_event_repository_recent_events_filters_by_default_signal_window() -> None:
    """最近事件默认只返回 90 天内的新闻/公告信号，避免旧新闻污染当前判断。"""

    now = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)
    session = _FakeSession()

    EventRepository(session).list_recent_events(
        asset_id="ashare:600519",
        limit=5,
        now=now,
    )

    statement = session.scalars_statements[0]
    sql = _compiled(statement)
    params = statement.compile(dialect=postgresql.dialect()).params

    assert "event_records.published_at >=" in sql
    assert "event_records.published_at IS NULL" in sql
    assert "event_records.collected_at >=" in sql
    assert now - timedelta(days=90) in params.values()


def test_event_repository_recent_events_can_disable_signal_window() -> None:
    """审计或排查场景可以显式关闭事件时间窗口。"""

    session = _FakeSession()

    EventRepository(session).list_recent_events(
        asset_id="ashare:600519",
        limit=5,
        max_age_days=None,
    )

    sql = _compiled(session.scalars_statements[0])

    assert "event_records.published_at >=" not in sql
    assert "event_records.collected_at >=" not in sql


def test_event_repository_deletes_expired_article_events() -> None:
    """过期新闻/公告应删除事件和证据整行，但保留 raw_records 审计。"""

    cutoff = datetime(2026, 3, 18, tzinfo=UTC)
    session = _RowCountSession(rowcounts=[2, 3])

    result = EventRepository(session).delete_expired_article_events(cutoff=cutoff)

    sql_text = "\n".join(str(statement) for statement in session.executed)

    assert result == {"event_records": 2, "evidence": 3, "total": 5}
    assert len(session.executed) == 2
    assert "DELETE FROM event_records" in sql_text
    assert "DELETE FROM evidence" in sql_text
    assert "payload #>" not in sql_text
    assert "raw_records" not in sql_text
    assert session.flush_count == 1
