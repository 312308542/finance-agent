from __future__ import annotations

from argparse import Namespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from finance_agent.agents.context_builder import AgentContextBuilder
from finance_agent.agents.tools.runtime import list_recent_evidence
from finance_agent.graph.sync import GraphSyncService
from finance_agent.scheduler.base_data_scheduler import import_collection_module
from finance_agent.storage.event_validation import (
    STOCK_NEWS_SOURCE,
    active_event_predicate,
    active_evidence_predicate,
)
from finance_agent.storage.orm import EventRecordORM, EvidenceORM
from finance_agent.storage.repositories import EventRepository


class _FakeScalarResult:
    def __iter__(self):
        return iter(())


class _FakeSession:
    def __init__(self) -> None:
        self.scalars_statements: list[Any] = []

    def scalars(self, statement: Any) -> _FakeScalarResult:
        self.scalars_statements.append(statement)
        return _FakeScalarResult()


class _FakeCache:
    def get_json(self, _key: str) -> None:
        return None

    def set_json(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _compiled(statement: Any) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def _assert_stock_news_gate(sql: str, *, table_name: str) -> None:
    assert f"{table_name}.source != '{STOCK_NEWS_SOURCE}'" in sql
    assert "entity_validation" in sql
    assert "status" in sql
    assert "passed" in sql


def test_active_predicates_fail_closed_only_for_stock_news_source() -> None:
    """关键词新闻仅允许 passed，公告等其他来源不受实体门控影响。"""

    event_sql = _compiled(select(EventRecordORM).where(active_event_predicate(EventRecordORM)))
    evidence_sql = _compiled(select(EvidenceORM).where(active_evidence_predicate(EvidenceORM)))

    _assert_stock_news_gate(event_sql, table_name="event_records")
    _assert_stock_news_gate(evidence_sql, table_name="evidence")
    assert "failed" not in event_sql
    assert "ambiguous" not in event_sql


def test_event_repository_applies_active_news_gate() -> None:
    session = _FakeSession()

    EventRepository(session).list_recent_events(
        asset_id="ashare:000685",
        limit=5,
        max_age_days=None,
    )

    _assert_stock_news_gate(
        _compiled(session.scalars_statements[0]),
        table_name="event_records",
    )


def test_agent_context_applies_gate_to_events_and_evidence() -> None:
    session = _FakeSession()
    builder = AgentContextBuilder(session, _FakeCache(), event_lookback_days=None)

    builder._load_recent_events("ashare:000685", limit=5)
    builder._load_recent_evidence("ashare:000685", limit=5)

    _assert_stock_news_gate(
        _compiled(session.scalars_statements[0]),
        table_name="event_records",
    )
    _assert_stock_news_gate(
        _compiled(session.scalars_statements[1]),
        table_name="evidence",
    )


def test_finance_tool_runtime_evidence_query_applies_active_news_gate() -> None:
    session = _FakeSession()

    list_recent_evidence(session, asset_id="ashare:000685", limit=5)

    _assert_stock_news_gate(
        _compiled(session.scalars_statements[0]),
        table_name="evidence",
    )


def test_graph_sync_evidence_query_applies_active_news_gate() -> None:
    session = _FakeSession()
    service = object.__new__(GraphSyncService)
    service.session = session

    service._list_evidence(decisions=[], risks=[], asset_id="ashare:000685")

    _assert_stock_news_gate(
        _compiled(session.scalars_statements[0]),
        table_name="evidence",
    )


def test_news_article_candidates_apply_active_news_gate() -> None:
    collect_base_data = import_collection_module()
    session = _FakeSession()
    args = Namespace(priority_symbol_limit=1, source_limit=None, batch_size=10)

    candidates = collect_base_data.resolve_ashare_news_article_candidates(session, args)

    assert candidates == []
    _assert_stock_news_gate(
        _compiled(session.scalars_statements[0]),
        table_name="event_records",
    )
