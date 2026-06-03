"""空缓存实现。"""

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

    def append_json(
        self,
        key: str,
        value: object,
        *,
        ttl_seconds: int | None = None,
        max_length: int | None = None,
    ) -> None:
        """忽略列表追加请求。"""

    def list_json(self, key: str, *, limit: int | None = None) -> list[object]:
        """始终返回空列表。"""

        return []

    def expire(self, key: str, ttl_seconds: int) -> None:
        """忽略 TTL 刷新请求。"""

    def acquire_lock(self, key: str, *, ttl_seconds: int) -> bool:
        """空缓存下总是视为成功获取锁。"""

        return True

    def release_lock(self, key: str) -> None:
        """忽略释放锁请求。"""
