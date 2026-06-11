from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

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
