from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from finance_agent.data.freshness import (
    ashare_daily_snapshot_at,
    expected_ashare_report_period,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


@pytest.mark.parametrize(
    ("checked_at", "expected"),
    [
        (datetime(2026, 4, 30, 23, tzinfo=SHANGHAI), date(2025, 9, 30)),
        (datetime(2026, 5, 1, 0, tzinfo=SHANGHAI), date(2026, 3, 31)),
        (datetime(2026, 8, 31, 23, 59, tzinfo=SHANGHAI), date(2026, 3, 31)),
        (datetime(2026, 9, 1, 0, tzinfo=SHANGHAI), date(2026, 6, 30)),
        (datetime(2026, 10, 31, 23, 59, tzinfo=SHANGHAI), date(2026, 6, 30)),
        (datetime(2026, 11, 1, 0, tzinfo=SHANGHAI), date(2026, 9, 30)),
        (datetime(2027, 1, 1, 0, tzinfo=SHANGHAI), date(2026, 9, 30)),
    ],
)
def test_expected_ashare_report_period_follows_disclosure_deadlines(
    checked_at: datetime,
    expected: date,
) -> None:
    """A 股最低应有报告期应随法定披露截止日推进。"""

    assert expected_ashare_report_period(checked_at) == expected


def test_ashare_daily_snapshot_uses_shanghai_calendar_day() -> None:
    """日级快照应按上海自然日生成稳定的 UTC 零点。"""

    collected_at = datetime(2026, 7, 15, 16, 30, tzinfo=UTC)

    assert ashare_daily_snapshot_at(collected_at) == datetime(2026, 7, 16, tzinfo=UTC)
