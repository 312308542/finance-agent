from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.dialects import postgresql

from finance_agent.storage.repositories import AssetRepository, RawRecordRepository


class _FakeSession:
    def __init__(self) -> None:
        self.executed: list[Any] = []
        self.flushed = False

    def execute(self, statement: Any) -> None:
        self.executed.append(statement)

    def flush(self) -> None:
        self.flushed = True

    def get_one(self, _model: Any, key: Any) -> Any:
        return {"key": key}


class _RawRecordSession(_FakeSession):
    def __init__(self, returned_id: str) -> None:
        super().__init__()
        self.returned_id = returned_id

    def execute(self, statement: Any) -> Any:
        self.executed.append(statement)

        class _Result:
            def __init__(self, value: str) -> None:
                self.value = value

            def scalar_one(self) -> str:
                return self.value

        return _Result(self.returned_id)


def _compiled(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def test_ensure_asset_uses_do_nothing_to_avoid_hot_row_updates() -> None:
    """高频采集路径只确保资产身份存在，不覆盖 assets 主表上的既有资料。"""

    session = _FakeSession()

    AssetRepository(session).ensure_asset(
        asset_id="ashare:600519",
        symbol="600519",
        name="贵州茅台",
        market="ashare",
        asset_type="stock",
        payload={"source": "akshare"},
    )

    sql = _compiled(session.executed[0])

    assert "ON CONFLICT (asset_id) DO NOTHING" in sql
    assert "DO UPDATE" not in sql
    assert session.flushed is True


def test_upsert_asset_master_updates_only_changed_stable_identity_fields() -> None:
    """低频主数据刷新应允许修复占位资产，同时避免无差异时反复更新主表。"""

    session = _FakeSession()

    AssetRepository(session).upsert_asset_master(
        asset_id="ashare:300001",
        symbol="300001",
        name="特锐德",
        market="ashare",
        asset_type="stock",
        exchange="SZSE",
        currency="CNY",
        payload={"source": "akshare:stock_zh_a_spot"},
    )

    sql = _compiled(session.executed[0])

    assert "ON CONFLICT (asset_id) DO UPDATE" in sql
    assert "assets.name IS DISTINCT FROM excluded.name" in sql
    assert "assets.exchange IS DISTINCT FROM excluded.exchange" in sql
    assert "assets.currency IS DISTINCT FROM excluded.currency" in sql
    assert session.flushed is True


def test_asset_detail_repositories_write_side_tables() -> None:
    """资产动态资料写入附表，避免多个采集任务争抢 assets 同一行更新锁。"""

    session = _FakeSession()
    repo = AssetRepository(session)
    as_of = datetime(2026, 5, 29, 9, 30, tzinfo=UTC)

    repo.upsert_asset_profile(
        asset_id="ashare:600519",
        name="贵州茅台",
        market="ashare",
        symbol="600519",
        source="akshare:stock_zh_a_spot",
        sector="白酒",
        as_of=as_of,
    )
    repo.upsert_asset_provider_mapping(
        asset_id="ashare:600519",
        market="ashare",
        symbol="600519",
        provider="akshare",
        provider_symbol="600519",
        source="stock_zh_a_spot",
    )
    repo.upsert_asset_status_snapshot(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        as_of=as_of,
        source="akshare:stock_zh_a_spot",
        tradable=True,
        trading_status="available",
    )
    repo.upsert_realtime_quote_snapshot(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        as_of=as_of,
        source="akshare:stock_zh_a_spot",
        last_price=Decimal("1720.50"),
        volume=Decimal("1234"),
    )

    sql_text = "\n".join(_compiled(statement) for statement in session.executed)

    assert "INSERT INTO asset_profiles" in sql_text
    assert "INSERT INTO asset_provider_mappings" in sql_text
    assert "INSERT INTO asset_status_snapshots" in sql_text
    assert "INSERT INTO realtime_quote_snapshots" in sql_text
    assert "ON CONFLICT" in sql_text


def test_raw_record_repository_deduplicates_exact_provider_payloads() -> None:
    """原始响应按 Provider、接口、请求、内容和状态精确去重，避免重复采集无限追加大 payload。"""

    session = _RawRecordSession(returned_id="raw:canonical")
    collected_at = datetime(2026, 5, 30, 9, 30, tzinfo=UTC)

    result = RawRecordRepository(session).insert_raw_record(
        provider="akshare",
        endpoint="stock_zh_a_hist",
        request_params={"symbol": "600519", "period": "daily"},
        response_payload={"rows": [{"close": 100}]},
        status="available",
        collected_at=collected_at,
        symbol="600519",
        market="ashare",
    )

    sql = _compiled(session.executed[0])

    assert "ON CONFLICT (provider, endpoint, request_hash, content_hash, status) DO UPDATE" in sql
    assert "RETURNING raw_records.raw_record_id" in sql
    assert result == {"key": "raw:canonical"}
    assert session.flushed is True
