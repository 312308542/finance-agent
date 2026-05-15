"""Redis 缓存实现。"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field

from redis import Redis

DEFAULT_REDIS_URL = "redis://localhost:6379/0"


def get_redis_url() -> str:
    """读取 Redis 连接地址。"""

    return os.getenv("FINANCE_AGENT_REDIS_URL", DEFAULT_REDIS_URL)


def create_redis_cache(redis_url: str | None = None, *, namespace: str = "finance_agent"):
    """创建 Redis 缓存客户端。"""

    client = Redis.from_url(redis_url or get_redis_url(), decode_responses=True)
    return RedisCacheClient(client=client, namespace=namespace)


@dataclass
class RedisCacheClient:
    """基于 Redis 的 JSON 缓存和轻量锁实现。"""

    client: Redis
    namespace: str = "finance_agent"
    _lock_tokens: dict[str, str] = field(default_factory=dict)

    def get_json(self, key: str) -> dict | list | str | int | float | bool | None:
        """读取 JSON 缓存。"""

        raw = self.client.get(self._key(key))
        if raw is None:
            return None
        return json.loads(raw)

    def set_json(self, key: str, value: object, *, ttl_seconds: int | None = None) -> None:
        """写入 JSON 缓存。"""

        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        self.client.set(self._key(key), payload, ex=ttl_seconds)

    def delete(self, key: str) -> None:
        """删除缓存键。"""

        self.client.delete(self._key(key))

    def acquire_lock(self, key: str, *, ttl_seconds: int) -> bool:
        """使用 `SET NX EX` 获取锁。"""

        token = secrets.token_urlsafe(24)
        redis_key = self._key(f"lock:{key}")
        acquired = bool(self.client.set(redis_key, token, nx=True, ex=ttl_seconds))
        if acquired:
            self._lock_tokens[redis_key] = token
        return acquired

    def release_lock(self, key: str) -> None:
        """仅释放本客户端持有的锁，避免误删其他任务的锁。"""

        redis_key = self._key(f"lock:{key}")
        token = self._lock_tokens.pop(redis_key, None)
        if token is None:
            return
        release_script = """
        if redis.call("GET", KEYS[1]) == ARGV[1] then
            return redis.call("DEL", KEYS[1])
        end
        return 0
        """
        self.client.eval(release_script, 1, redis_key, token)

    def _key(self, key: str) -> str:
        """加上项目命名空间，避免多项目共用 Redis 时键冲突。"""

        return f"{self.namespace}:{key}"

    def ping(self) -> bool:
        """检查 Redis 是否可用。"""

        return bool(self.client.ping())
