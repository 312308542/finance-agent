"""缓存实现模块。

这里避免在包导入阶段强制导入 Redis 依赖。离线 smoke、dry-run 和空缓存
场景只需要 `create_cache_client` / `NullCacheClient`，不应该因为本地未安装
或未启动 Redis 而失败。
"""

from typing import Any

from finance_agent.cache.factory import CacheStartupStatus, create_cache_client
from finance_agent.cache.null_cache import NullCacheClient

__all__ = [
    "CacheStartupStatus",
    "NullCacheClient",
    "RedisCacheClient",
    "create_cache_client",
    "create_redis_cache",
]


def create_redis_cache(*args: Any, **kwargs: Any) -> Any:
    """懒加载 Redis 缓存实现。"""

    from finance_agent.cache.redis_cache import create_redis_cache as factory

    return factory(*args, **kwargs)


def __getattr__(name: str) -> Any:
    """按需暴露 RedisCacheClient，避免普通导入触发 redis 包加载。"""

    if name == "RedisCacheClient":
        from finance_agent.cache.redis_cache import RedisCacheClient

        return RedisCacheClient
    raise AttributeError(name)
