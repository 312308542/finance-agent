"""Agent 上下文缓存冒烟验证。

验证内容：
- Redis 可连接。
- JSON 缓存可写可读。
- 轻量锁可获取和释放。
- `AgentContextBuilder` 能从事实库构建单标的上下文，并写入 Redis TTL 缓存。
"""

from __future__ import annotations

from finance_agent.agents import AgentContextBuilder
from finance_agent.cache import create_redis_cache
from finance_agent.storage.db import create_session_factory, session_scope


def main() -> None:
    """执行 Agent 上下文缓存冒烟验证。"""

    cache = create_redis_cache()
    cache.set_json("smoke:json", {"ok": True, "name": "agent_context_cache"}, ttl_seconds=60)
    lock_ok = cache.acquire_lock("smoke:lock", ttl_seconds=60)
    cache.release_lock("smoke:lock")

    session_factory = create_session_factory()
    with session_scope(session_factory) as session:
        builder = AgentContextBuilder(session, cache, ttl_seconds=60)
        context = builder.build_asset_context(
            asset_id="ashare:000001",
            horizon="short_term",
            bar_limit=5,
        )

    print(
        {
            "redis_ping": cache.ping(),
            "cache_value": cache.get_json("smoke:json"),
            "lock_ok": lock_ok,
            "asset_id": context["asset_id"],
            "asset": context["asset"],
            "bar_count": len(context["latest_market_bars"]),
            "event_count": len(context["recent_events"]),
            "evidence_count": len(context["evidence"]),
        }
    )


if __name__ == "__main__":
    main()
