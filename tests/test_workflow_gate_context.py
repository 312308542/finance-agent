from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from finance_agent.agents.interfaces import build_workflow_gate_context


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def first(self) -> Any:
        return self.value


class _Session:
    def __init__(self, snapshot: Any) -> None:
        self.snapshot = snapshot
        self.executed: list[Any] = []
        self.flush_count = 0

    def scalars(self, _statement: Any) -> _ScalarResult:
        return _ScalarResult(self.snapshot)

    def execute(self, statement: Any) -> SimpleNamespace:
        self.executed.append(statement)
        return SimpleNamespace(rowcount=1)

    def flush(self) -> None:
        self.flush_count += 1

    def get_one(self, _model: Any, key: Any) -> Any:
        return SimpleNamespace(decision_gate_id=key)


def _snapshot() -> SimpleNamespace:
    as_of = datetime(2026, 7, 20, 9, 35, tzinfo=UTC)
    return SimpleNamespace(
        data_snapshot_id="snapshot:quotes:1",
        snapshot_type="ashare_realtime_quotes",
        market="ashare",
        as_of=as_of,
        captured_at=as_of,
        provider="gotdx:tdx_main",
        provider_version="gateway-v1",
        quality_status="available",
        schema_version="1",
        content_hash="hash",
        raw_record_ids=[],
        payload={},
        snapshot_metadata={},
    )


def test_workflow_gate_context_persists_approved_gate_for_fresh_snapshot() -> None:
    session = _Session(_snapshot())
    context = build_workflow_gate_context(
        session=session,
        workflow_type="portfolio_monitoring",
        as_of=datetime(2026, 7, 20, 9, 36, tzinfo=UTC),
    )

    assert context["data_snapshot_id"] == "snapshot:quotes:1"
    assert context["decision_gate_status"] == "approved"
    assert context["decision_gate_id"].startswith("gate:portfolio_monitoring:")
    assert session.flush_count == 1


def test_workflow_gate_context_fails_closed_without_snapshot() -> None:
    context = build_workflow_gate_context(
        session=_Session(None),
        workflow_type="recommendation_decision",
        as_of=datetime(2026, 7, 20, 9, 36, tzinfo=UTC),
    )

    assert context["data_snapshot_id"] is None
    assert context["decision_gate_status"] == "data_unavailable"
