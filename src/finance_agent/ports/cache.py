"""缓存端口定义。

Redis 是可重建缓存和任务协调层，不是事实数据源。业务代码依赖本端口，
避免在采集、因子和 Agent 层散落 Redis SDK 调用。
"""

from __future__ import annotations

from typing import Protocol


class CacheClient(Protocol):
    """通用缓存客户端接口。"""

    def get_json(self, key: str) -> dict | list | str | int | float | bool | None:
        """读取 JSON 缓存。"""

    def set_json(self, key: str, value: object, *, ttl_seconds: int | None = None) -> None:
        """写入 JSON 缓存。"""

    def delete(self, key: str) -> None:
        """删除缓存键。"""


class LockClient(Protocol):
    """分布式锁接口，用于采集任务去重和 Provider 熔断控制。"""

    def acquire_lock(self, key: str, *, ttl_seconds: int) -> bool:
        """尝试获取锁。"""

    def release_lock(self, key: str) -> None:
        """释放锁。"""
