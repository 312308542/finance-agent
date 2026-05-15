"""空缓存实现。

用于测试、离线脚本或 Redis 暂不可用时的降级。空缓存不会保存任何数据，
因此不能用于需要任务去重的生产采集任务。
"""

from __future__ import annotations


class NullCacheClient:
    """不持久化任何内容的缓存客户端。"""

    def get_json(self, key: str) -> dict | list | str | int | float | bool | None:
        """始终返回空缓存。"""

        return None

    def set_json(self, key: str, value: object, *, ttl_seconds: int | None = None) -> None:
        """忽略写入请求。"""

    def delete(self, key: str) -> None:
        """忽略删除请求。"""

    def acquire_lock(self, key: str, *, ttl_seconds: int) -> bool:
        """空缓存下总是视为获取锁成功。"""

        return True

    def release_lock(self, key: str) -> None:
        """忽略释放锁请求。"""
