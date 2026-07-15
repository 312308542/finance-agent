"""A 股财务和估值新鲜度公共规则。"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo


ASHARE_TIMEZONE = ZoneInfo("Asia/Shanghai")
REPORTING_PERIOD_FRESHNESS_POLICY = "reporting_period"
ASHARE_SPOT_VALUATION_SOURCE = "akshare:stock_zh_a_spot"
ASHARE_HISTORICAL_VALUATION_SOURCE = "akshare:stock_value_em"
ASHARE_FINANCIAL_INDICATOR_SOURCE = "akshare:stock_financial_analysis_indicator_em"


def expected_ashare_report_period(checked_at: datetime) -> date:
    """按法定披露截止日返回 A 股最低应有报告期。"""

    local = checked_at.astimezone(ASHARE_TIMEZONE)
    if local.month <= 4:
        return date(local.year - 1, 9, 30)
    if local.month <= 8:
        return date(local.year, 3, 31)
    if local.month <= 10:
        return date(local.year, 6, 30)
    return date(local.year, 9, 30)


def ashare_daily_snapshot_at(collected_at: datetime) -> datetime:
    """把采集时刻归一化为上海自然日对应的 UTC 零点。"""

    local_date = collected_at.astimezone(ASHARE_TIMEZONE).date()
    return datetime.combine(local_date, time.min, tzinfo=UTC)
