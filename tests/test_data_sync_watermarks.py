from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.dialects import postgresql

from finance_agent.storage.repositories import DataSyncWatermarkRepository


class _FakeResult:
    def __init__(self, value: Any = None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value


class _FakeSession:
    def __init__(self, scalar_value: Any = None) -> None:
        self.executed: list[Any] = []
        self.flushed = False
        self.scalar_value = scalar_value

    def execute(self, statement: Any) -> _FakeResult:
        self.executed.append(statement)
        return _FakeResult(self.scalar_value)

    def flush(self) -> None:
        self.flushed = True


def _compiled(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def test_data_sync_watermark_success_resets_failure_state() -> None:
    """采集成功后应记录最新水位，并清空失败和重试状态。"""

    session = _FakeSession()
    occurred_at = datetime(2026, 6, 4, 15, 10, tzinfo=UTC)
    watermark_at = datetime(2026, 6, 3, 15, 0, tzinfo=UTC)

    DataSyncWatermarkRepository(session).record_success(
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        data_domain="market_bars",
        provider="akshare:stock_zh_a_hist_tx",
        timeframe="1d",
        watermark_at=watermark_at,
        occurred_at=occurred_at,
        payload={"row_count": 117},
    )

    sql = _compiled(session.executed[0])

    assert "INSERT INTO data_sync_watermarks" in sql
    assert "ON CONFLICT" in sql
    assert "fail_count" in sql
    assert "next_retry_at" in sql
    assert session.flushed is True


def test_data_sync_watermark_failure_schedules_retry() -> None:
    """网络失败不应阻塞整轮采集，而应写入下次可重试时间。"""

    session = _FakeSession()
    occurred_at = datetime(2026, 6, 4, 15, 10, tzinfo=UTC)

    DataSyncWatermarkRepository(session).record_failure(
        asset_id="ashare:301611",
        symbol="301611",
        market="ashare",
        data_domain="market_bars",
        provider="akshare:stock_zh_a_hist_tx",
        timeframe="1d",
        occurred_at=occurred_at,
        retry_after=timedelta(minutes=15),
        error_message="curl: (56) Connection closed abruptly",
    )

    sql = _compiled(session.executed[0])

    assert "INSERT INTO data_sync_watermarks" in sql
    assert "data_sync_watermarks.fail_count + %(fail_count_" in sql
    assert "next_retry_at" in sql
    assert "curl: (56)" in str(session.executed[0].compile(dialect=postgresql.dialect()).params)
    assert session.flushed is True


def test_data_sync_watermark_failure_can_skip_retry_schedule() -> None:
    """手动全量任务失败时允许只记录错误，不设置下一次自动重试时间。"""

    session = _FakeSession()
    occurred_at = datetime(2026, 6, 4, 15, 10, tzinfo=UTC)

    DataSyncWatermarkRepository(session).record_failure(
        asset_id="ashare:301611",
        symbol="301611",
        market="ashare",
        data_domain="market_bars",
        provider="akshare:stock_zh_a_hist_tx",
        timeframe="1d",
        occurred_at=occurred_at,
        retry_after=None,
        error_message="curl: (56) Connection closed abruptly",
    )

    params = session.executed[0].compile(dialect=postgresql.dialect()).params

    assert params["next_retry_at"] is None
    assert session.flushed is True


def test_data_sync_watermark_reads_due_retry_time() -> None:
    """仓储层应能读取某个资产在某个数据域的下次重试时间。"""

    retry_at = datetime(2026, 6, 4, 15, 25, tzinfo=UTC)
    session = _FakeSession(scalar_value=retry_at)

    result = DataSyncWatermarkRepository(session).get_next_retry_at(
        asset_id="ashare:301611",
        data_domain="market_bars",
        provider="akshare:stock_zh_a_hist_tx",
        timeframe="1d",
    )

    sql = _compiled(session.executed[0])

    assert "FROM data_sync_watermarks" in sql
    assert "next_retry_at" in sql
    assert result == retry_at
