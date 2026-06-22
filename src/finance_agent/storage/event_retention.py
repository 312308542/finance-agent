"""事件和新闻正文保留策略。

本模块只定义跨仓储、Agent 上下文和因子链路共享的默认时效参数，
不直接执行数据库操作。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

DEFAULT_EVENT_SIGNAL_LOOKBACK_DAYS = 90
DEFAULT_ARTICLE_FULL_TEXT_RETENTION_DAYS = 90
NEWS_ARTICLE_EVENT_TYPES = ("news", "announcement")


def event_signal_cutoff(
    max_age_days: int | None,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """计算事件信号窗口的 UTC cutoff，`None` 表示关闭窗口。"""

    if max_age_days is None:
        return None
    if max_age_days <= 0:
        raise ValueError("max_age_days 必须大于 0，或传入 None 关闭时间窗口")
    current = now or datetime.now(tz=UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    else:
        current = current.astimezone(UTC)
    return current - timedelta(days=max_age_days)
