"""PostgreSQL Outbox 到 Redis Streams 的可靠投递边界。

数据库 Outbox 是事实源；Redis Streams 只负责至少一次传输。消费者必须使用
`event_id` 做幂等，不能把 Redis 的消费状态当作业务事实。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Protocol


def event_stream_name(event_type: str, *, prefix: str = "finance-agent:events") -> str:
    """按事件类型分组 Stream，避免不同业务共享一条无界队列。"""

    normalized = str(event_type).strip()
    if not normalized:
        raise ValueError("event_type 不能为空")
    return f"{prefix}:{normalized}"


class EventTransport(Protocol):
    """Outbox 发布器依赖的最小传输接口。"""

    def publish(self, event: Any) -> str:
        """发布一个事件并返回 Redis Stream ID。"""


class RedisStreamsTransport:
    """Redis Streams 写入适配器。"""

    def __init__(
        self,
        client: Any,
        *,
        stream_prefix: str = "finance-agent:events",
        maxlen: int = 10_000,
    ) -> None:
        if client is None:
            raise ValueError("Redis client 不能为空")
        self.client = client
        self.stream_prefix = stream_prefix
        self.maxlen = max(1, int(maxlen))

    def publish(self, event: Any) -> str:
        """以结构化字段写入 Stream，payload 保留 JSON 原文。"""

        fields = {
            "event_id": str(event.event_id),
            "event_type": str(event.event_type),
            "aggregate_type": str(event.aggregate_type),
            "aggregate_id": str(event.aggregate_id),
            "payload": json.dumps(
                dict(event.payload or {}),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ),
        }
        stream = event_stream_name(str(event.event_type), prefix=self.stream_prefix)
        return str(self.client.xadd(stream, fields, maxlen=self.maxlen, approximate=True))


class RedisStreamConsumer:
    """Redis Streams Consumer Group 的最小封装。"""

    def __init__(self, client: Any) -> None:
        if client is None:
            raise ValueError("Redis client 不能为空")
        self.client = client

    def ensure_group(self, stream: str, group: str, *, start_id: str = "0-0") -> None:
        """幂等创建消费组；已存在时保持现有游标。"""

        try:
            self.client.xgroup_create(stream, group, id=start_id, mkstream=True)
        except Exception as exc:  # redis.exceptions.ResponseError 在不同版本路径不同
            if "BUSYGROUP" not in str(exc).upper():
                raise

    def read(
        self,
        stream: str,
        *,
        group: str,
        consumer: str,
        count: int = 10,
        block_ms: int = 1_000,
    ) -> Any:
        """读取新消息；业务处理成功后必须显式 `ack`。"""

        return self.client.xreadgroup(
            group,
            consumer,
            {stream: ">"},
            count=max(1, int(count)),
            block=max(0, int(block_ms)),
        )

    def ack(self, stream: str, group: str, *message_ids: str) -> int:
        """确认已完成的消息。"""

        if not message_ids:
            return 0
        return int(self.client.xack(stream, group, *message_ids))

    def autoclaim(
        self,
        stream: str,
        *,
        group: str,
        consumer: str,
        min_idle_ms: int = 60_000,
        start_id: str = "0-0",
        count: int = 10,
    ) -> Any:
        """接管崩溃消费者遗留的 pending 消息。"""

        return self.client.xautoclaim(
            stream,
            group,
            consumer,
            min_idle_time=max(0, int(min_idle_ms)),
            start_id=start_id,
            count=max(1, int(count)),
        )


class OutboxPublisher:
    """从数据库领取 Outbox 并发布到 Redis。"""

    def __init__(self, repository: Any, transport: EventTransport) -> None:
        self.repository = repository
        self.transport = transport

    def publish_batch(
        self,
        *,
        publisher_id: str,
        limit: int = 100,
        lease_seconds: int = 60,
        retry_after: timedelta = timedelta(seconds=30),
        now: datetime | None = None,
    ) -> int:
        """发布一批事件；单条失败不影响同批其他事件。"""

        occurred_at = now or datetime.now().astimezone()
        events = self.repository.claim_pending(
            publisher_id=publisher_id,
            limit=limit,
            lease_seconds=lease_seconds,
            now=occurred_at,
        )
        published = 0
        for event in events:
            try:
                stream_id = self.transport.publish(event)
            except Exception as exc:  # noqa: BLE001 - 投递边界必须回写失败并继续批处理
                self.repository.mark_failed(
                    event_id=event.event_id,
                    lease_token=event.publish_lease_token,
                    error_message=str(exc),
                    retry_after=retry_after,
                    now=occurred_at,
                )
                continue
            if self.repository.mark_published(
                event_id=event.event_id,
                lease_token=event.publish_lease_token,
                stream_id=stream_id,
                now=occurred_at,
            ):
                published += 1
        return published
