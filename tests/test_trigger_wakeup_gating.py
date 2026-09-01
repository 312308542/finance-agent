from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from finance_agent.agents.loop.runner import InternalFinanceAgentLoopRunner
from finance_agent.triggers.service import TriggerService
from finance_agent.triggers.webhook import HermesWebhookPublisher


def _event(*, runtime: str = "hermes_agent") -> SimpleNamespace:
    return SimpleNamespace(
        trigger_event_id="trigger:test:1",
        owner_id="owner-1",
        trigger_type="position_drawdown",
        trigger_ref=None,
        dedup_key="owner-1:position_drawdown:portfolio_monitoring:asset-1",
        severity="high",
        status="pending",
        agent_runtime=runtime,
        agent_task_id=None,
        requested_workflow_type="portfolio_monitoring",
        portfolio_id="portfolio-1",
        watchlist_id=None,
        recommendation_run_id=None,
        asset_id="asset-1",
        cooldown_until=None,
        triggered_at=datetime(2026, 7, 24, 2, 0, tzinfo=UTC),
        dispatched_at=None,
        payload={},
    )


class _TriggerRepository:
    def __init__(self, event: SimpleNamespace) -> None:
        self.event = event
        self.marked_dispatched = 0
        self.marked_failed = 0

    def list_pending_events(
        self, *, owner_id: str | None, agent_runtime: str | None, limit: int
    ):
        if agent_runtime and self.event.agent_runtime != agent_runtime:
            return []
        return [self.event]

    def mark_dispatched(self, **kwargs):
        self.marked_dispatched += 1
        self.event.status = "dispatched"
        self.event.agent_task_id = kwargs["agent_task_id"]
        return self.event

    def mark_dispatch_failed(self, **kwargs):
        self.marked_failed += 1
        self.event.payload = {
            "dispatch_retry_at": kwargs["retry_at"].isoformat(),
            "last_dispatch_error": kwargs["error_message"],
        }
        return self.event

    def mark_skipped(self, **kwargs):  # pragma: no cover - not used by these fixtures
        raise AssertionError("测试事件不应被跳过")


def test_dispatch_queries_only_selected_runtime() -> None:
    repository = _TriggerRepository(_event(runtime="internal_agent_loop"))
    service = _service(repository)

    result = service.dispatch_pending(
        owner_id="owner-1",
        agent_runtime="hermes_agent",
        publisher=lambda event: None,
    )

    assert result.dispatched_events == ()
    assert result.failed_events == ()
    assert repository.marked_dispatched == 0


def _service(repository: _TriggerRepository) -> TriggerService:
    service = object.__new__(TriggerService)
    service.triggers = repository
    return service


def test_hermes_event_without_publisher_is_not_marked_dispatched() -> None:
    repository = _TriggerRepository(_event())

    result = _service(repository).dispatch_pending(
        owner_id="owner-1",
        as_of=datetime(2026, 7, 24, 2, 1, tzinfo=UTC),
    )

    assert result.dispatched_events == ()
    assert result.failed_events == (repository.event,)
    assert repository.marked_dispatched == 0
    assert repository.marked_failed == 1


def test_successful_publisher_marks_event_dispatched_once() -> None:
    repository = _TriggerRepository(_event())
    published: list[str] = []

    result = _service(repository).dispatch_pending(
        owner_id="owner-1",
        as_of=datetime(2026, 7, 24, 2, 1, tzinfo=UTC),
        publisher=lambda event: published.append(event.trigger_event_id),
    )

    assert published == ["trigger:test:1"]
    assert len(result.dispatched_events) == 1
    assert result.failed_events == ()
    assert repository.marked_dispatched == 1


def test_publisher_failure_keeps_event_retryable() -> None:
    repository = _TriggerRepository(_event())

    def failing_publisher(event):
        raise RuntimeError("Hermes gateway unavailable")

    result = _service(repository).dispatch_pending(
        owner_id="owner-1",
        as_of=datetime(2026, 7, 24, 2, 1, tzinfo=UTC),
        publisher=failing_publisher,
    )

    assert result.dispatched_events == ()
    assert result.failed_events == (repository.event,)
    assert repository.event.status == "pending"
    assert repository.event.payload["last_dispatch_error"] == "Hermes gateway unavailable"


def test_internal_loop_filters_events_to_its_runtime() -> None:
    calls: dict[str, object] = {}

    class _Repository:
        def list_agent_wakeup_events(self, **kwargs):
            calls.update(kwargs)
            return []

    runner = object.__new__(InternalFinanceAgentLoopRunner)
    runner.triggers = _Repository()

    result = runner.run_once(owner_id="owner-1", limit=5)

    assert result.processed == ()
    assert calls["agent_runtime"] == "internal_agent_loop"


def test_webhook_publisher_uses_hermes_hmac_header() -> None:
    calls: list[dict[str, object]] = []

    class _Response:
        def raise_for_status(self) -> None:
            return None

    class _Session:
        def post(self, url, **kwargs):
            kwargs["url"] = url
            calls.append(kwargs)
            return _Response()

    HermesWebhookPublisher(
        url="http://hermes.test/webhooks/finance-agent",
        secret="secret",
        session=_Session(),
    ).publish(_event())

    assert calls[0]["headers"]["X-Hub-Signature-256"].startswith("sha256=")
    assert calls[0]["headers"]["X-Finance-Agent-Event"] == "trigger:test:1"
