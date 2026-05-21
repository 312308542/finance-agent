"""缓存客户端工厂。"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module

from finance_agent.cache.null_cache import NullCacheClient
from finance_agent.ports.cache import CacheClient, LockClient


@dataclass(frozen=True)
class CacheStartupStatus:
    """缓存启动结果。"""

    backend: str
    status: str
    error_message: str | None = None


def create_cache_client(
    *,
    backend: str = "auto",
) -> tuple[CacheClient, LockClient, CacheStartupStatus]:
    """创建缓存和锁客户端。

    `auto` 用于本地命令行体验：优先 Redis，连接失败时降级为空缓存。
    调度器和生产任务应显式使用 `redis`，避免任务锁和熔断状态失效。
    """

    if backend not in {"auto", "redis", "null"}:
        raise ValueError(f"不支持的缓存后端: {backend}")
    if backend == "null":
        cache = NullCacheClient()
        return cache, cache, CacheStartupStatus(backend="null", status="available")

    try:
        create_redis_cache = import_module("finance_agent.cache.redis_cache").create_redis_cache
        cache = create_redis_cache()
        cache.ping()
        return cache, cache, CacheStartupStatus(backend="redis", status="available")
    except Exception as exc:
        if backend == "redis":
            raise
        cache = NullCacheClient()
        return (
            cache,
            cache,
            CacheStartupStatus(
                backend="null",
                status="fallback",
                error_message=str(exc),
            ),
        )
