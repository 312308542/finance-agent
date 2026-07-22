"""运行 PostgreSQL Outbox 到 Redis Streams 的发布器。"""

from __future__ import annotations

import argparse
import json
import os
import socket
import time
from datetime import timedelta
from typing import Any

from finance_agent.events import OutboxPublisher, RedisStreamsTransport
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.repositories import OutboxEventRepository


def publish_once(
    *,
    database_url: str | None = None,
    redis_url: str | None = None,
    publisher_id: str | None = None,
    limit: int = 100,
    lease_seconds: int = 60,
    retry_after_seconds: int = 30,
    stream_prefix: str = "finance-agent:events",
    redis_client: Any | None = None,
) -> int:
    """在一个数据库事务中领取并回写一批事件。"""

    if redis_client is None:
        import redis

        redis_client = redis.Redis.from_url(
            redis_url or os.getenv("FINANCE_AGENT_REDIS_URL", "redis://127.0.0.1:6379/0"),
            decode_responses=True,
        )
    session_factory = create_session_factory(database_url)
    resolved_publisher_id = publisher_id or f"outbox-publisher:{socket.gethostname()}"
    with session_scope(session_factory) as session:
        publisher = OutboxPublisher(
            OutboxEventRepository(session),
            RedisStreamsTransport(redis_client, stream_prefix=stream_prefix),
        )
        return publisher.publish_batch(
            publisher_id=resolved_publisher_id,
            limit=limit,
            lease_seconds=lease_seconds,
            retry_after=timedelta(seconds=max(1, retry_after_seconds)),
        )


def build_parser() -> argparse.ArgumentParser:
    """创建发布器命令行参数。"""

    parser = argparse.ArgumentParser(description="发布 finance-agent Outbox 事件")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--redis-url", default=None)
    parser.add_argument("--publisher-id", default=None)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--lease-seconds", type=int, default=60)
    parser.add_argument("--retry-after-seconds", type=int, default=30)
    parser.add_argument("--stream-prefix", default="finance-agent:events")
    parser.add_argument("--loop", action="store_true", help="持续运行")
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    return parser


def main(argv: list[str] | None = None) -> None:
    """执行一次或持续发布。"""

    args = build_parser().parse_args(argv)
    while True:
        published = publish_once(
            database_url=args.database_url,
            redis_url=args.redis_url,
            publisher_id=args.publisher_id,
            limit=args.limit,
            lease_seconds=args.lease_seconds,
            retry_after_seconds=args.retry_after_seconds,
            stream_prefix=args.stream_prefix,
        )
        print(json.dumps({"status": "available", "published": published}, ensure_ascii=False))
        if not args.loop:
            return
        time.sleep(max(0.1, args.interval_seconds))


if __name__ == "__main__":
    main()
