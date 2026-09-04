"""盘中实时行情通道和质量契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

QuoteChannelName = Literal["held", "radar", "market", "verification"]
QuoteSourceName = Literal["gotdx", "akshare"]
QuoteQualityStatus = Literal[
    "available",
    "partial",
    "stale",
    "unavailable",
    "degraded",
    "after_hours_snapshot",
]


@dataclass(frozen=True)
class QuoteChannelPolicy:
    """单个实时行情通道不可放宽的采集约束。"""

    name: QuoteChannelName
    interval_seconds: int
    batch_size: int
    primary_source: QuoteSourceName
    maximum_freshness_seconds: int


@dataclass(frozen=True)
class QuoteQualityResult:
    """一次通道采集的完整性和时效性结果。"""

    status: QuoteQualityStatus
    requested_count: int
    received_count: int
    fresh_count: int
    maximum_lag_seconds: float | None
    duplicate_timestamp_count: int
    clock_regression_count: int
    source_errors: tuple[str, ...]

    @property
    def is_executable(self) -> bool:
        """仅完整可用的实时行情可以进入可执行动作。"""

        return self.status == "available"


POLICIES: dict[QuoteChannelName, QuoteChannelPolicy] = {
    "held": QuoteChannelPolicy("held", 1, 50, "gotdx", 3),
    "radar": QuoteChannelPolicy("radar", 5, 50, "gotdx", 10),
    "market": QuoteChannelPolicy("market", 300, 500, "akshare", 300),
    "verification": QuoteChannelPolicy("verification", 30, 50, "akshare", 90),
}


def quote_channel_policy(name: QuoteChannelName) -> QuoteChannelPolicy:
    """返回不可由运行参数放宽的实时通道策略。"""

    return POLICIES[name]
