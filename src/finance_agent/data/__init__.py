"""数据采集和归一化模块。"""

from finance_agent.data.akshare_capabilities import (
    AKSHARE_CAPABILITIES,
    AkshareCapability,
    get_capability,
    iter_capabilities,
)

__all__ = [
    "AKSHARE_CAPABILITIES",
    "AkshareCapability",
    "get_capability",
    "iter_capabilities",
]
