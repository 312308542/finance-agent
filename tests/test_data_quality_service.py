from datetime import UTC, datetime
from typing import Any

from sqlalchemy.dialects import postgresql

from finance_agent.application.data_quality_service import market_bar_coverage


class _FakeResult:
    def one(self) -> tuple[int, datetime]:
        return 1, datetime(2026, 6, 3, tzinfo=UTC)


class _FakeSession:
    def __init__(self) -> None:
        self.executed: list[Any] = []

    def execute(self, statement: Any) -> _FakeResult:
        self.executed.append(statement)
        return _FakeResult()


def _compiled(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def test_market_bar_coverage_only_counts_closed_final_bars() -> None:
    """正式数据质量覆盖率不能把盘中 partial 日 K 当成可用日 K。"""

    session = _FakeSession()

    count, latest_at = market_bar_coverage(
        session,
        asset_id="ashare:000001",
        timeframe="1d",
    )

    sql = _compiled(session.executed[0])

    assert count == 1
    assert latest_at == datetime(2026, 6, 3, tzinfo=UTC)
    assert "market_bars.is_closed IS true" in sql
    assert "market_bars.status IN" in sql
