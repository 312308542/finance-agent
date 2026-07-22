"""Outbox 事实层和 Redis Streams 投递行为测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy.dialects import postgresql

from finance_agent.events.outbox import (
    OutboxPublisher,
    RedisStreamsTransport,
    event_stream_name,
)
from finance_agent.storage.repositories import OutboxEventRepository

NOW = datetime(2026, 7, 20, 9, 40, tzinfo=UTC)


class _Result:
    def __init__(self, rowcount: int = 1) -> None:
        self.rowcount = rowcount


class _ScalarResult:
    def __init__(self, values: list[Any]) -> None:
        self.values = values

    def first(self) -> Any:
        return self.values[0] if self.values else None

    def all(self) -> list[Any]:
        return list(self.values)


class _Session:
    def __init__(self, *, values: list[Any] | None = None, rowcount: int = 1) -> None:
        self.values = values or []
        self.rowcount = rowcount
        self.executed: list[Any] = []
        self.flush_count = 0

    def execute(self, statement: Any) -> _Result:
        self.executed.append(statement)
        return _Result(self.rowcount)

    def flush(self) -> None:
        self.flush_count += 1

    def get_one(self, _model: Any, key: Any) -> Any:
        return SimpleNamespace(event_id=key)

    def scalars(self, statement: Any) -> _ScalarResult:
        self.executed.append(statement)
        return _ScalarResult(self.values)


def _compiled(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def test_outbox_append_is_idempotent_by_key() -> None:
    session = _Session()

    event = OutboxEventRepository(session).append(
        event_type="scheduler.task.completed",
        aggregate_type="scheduler_task",
        aggregate_id="task:1",
        idempotency_key="scheduler.task.completed:task:1:attempt-1",
        payload={"job_name": "ashare.realtime_quotes"},
        occurred_at=NOW,
    )

    sql = _compiled(session.executed[0])
    assert "ON CONFLICT ON CONSTRAINT uq_outbox_events_idempotency DO NOTHING" in sql
    assert event.event_id.startswith("outbox:")
    assert session.flush_count == 1


def test_outbox_claim_uses_skip_locked_and_sets_lease() -> None:
    event = SimpleNamespace(
        event_id="outbox:1",
        event_type="scheduler.task.completed",
        aggregate_type="scheduler_task",
        aggregate_id="task:1",
        payload={"status": "completed"},
        attempts=0,
        publish_lease_token=None,
        publish_lease_expires_at=None,
    )
    session = _Session(values=[event])

    claimed = OutboxEventRepository(session).claim_pending(
        publisher_id="publisher-1",
        limit=1,
        lease_seconds=30,
        now=NOW,
    )

    sql = _compiled(session.executed[0])
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert claimed == [event]
    assert event.attempts == 1
    assert event.publish_lease_owner == "publisher-1"
    assert event.publish_lease_token
    assert event.publish_lease_expires_at == NOW + timedelta(seconds=30)


def test_event_stream_name_groups_by_event_type() -> None:
    assert event_stream_name("scheduler.task.completed") == (
        "finance-agent:events:scheduler.task.completed"
    )


class _FakeOutbox:
    def __init__(self, event: Any) -> None:
        self.event = event
        self.published: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []

    def claim_pending(self, **_: Any) -> list[Any]:
        return [self.event]

    def mark_published(self, **kwargs: Any) -> bool:
        self.published.append(kwargs)
        return True

    def mark_failed(self, **kwargs: Any) -> bool:
        self.failed.append(kwargs)
        return False


class _FakeTransport:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def publish(self, event: Any) -> str:
        self.events.append(event)
        return "1740000000000-0"


def test_outbox_publisher_marks_event_after_stream_publish() -> None:
    event = SimpleNamespace(
        event_id="outbox:1",
        event_type="scheduler.task.completed",
        aggregate_type="scheduler_task",
        aggregate_id="task:1",
        payload={"status": "completed"},
        publish_lease_token="lease-1",
    )
    repository = _FakeOutbox(event)
    transport = _FakeTransport()

    published = OutboxPublisher(repository, transport).publish_batch(
        publisher_id="publisher-1",
        now=NOW,
    )

    assert published == 1
    assert transport.events == [event]
    assert repository.published == [
        {
            "event_id": "outbox:1",
            "lease_token": "lease-1",
            "stream_id": "1740000000000-0",
            "now": NOW,
        }
    ]


class _FakeRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def xadd(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append(("xadd", args, kwargs))
        return "1-0"


def test_redis_stream_transport_serializes_event_payload() -> None:
    client = _FakeRedis()
    event = SimpleNamespace(
        event_id="outbox:1",
        event_type="scheduler.task.completed",
        aggregate_type="scheduler_task",
        aggregate_id="task:1",
        payload={"status": "completed"},
    )

    stream_id = RedisStreamsTransport(client).publish(event)

    assert stream_id == "1-0"
    stream, fields = client.calls[0][1]
    assert stream == "finance-agent:events:scheduler.task.completed"
    assert fields["event_id"] == "outbox:1"
    assert '"status":"completed"' in fields["payload"]
