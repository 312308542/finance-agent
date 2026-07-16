from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from finance_agent.data.news_entity_revalidation import (
    StockNewsEntityRevalidationService,
)
from finance_agent.storage.repositories import EventRepository


def _import_revalidation_script() -> Any:
    script_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "data"
        / "revalidate_stock_news_entities.py"
    )
    spec = importlib.util.spec_from_file_location(
        "revalidate_stock_news_entities",
        script_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeRepository:
    def __init__(self, rows: list[tuple[Any, str | None]]) -> None:
        self.rows = rows
        self.event_update_calls: list[list[dict[str, Any]]] = []
        self.evidence_update_calls: list[list[dict[str, Any]]] = []

    def list_stock_news_for_entity_revalidation(
        self,
        *,
        limit: int | None = None,
    ) -> list[tuple[Any, str | None]]:
        return self.rows[:limit] if limit is not None else self.rows

    def update_news_entity_validations(
        self,
        rows: list[dict[str, Any]],
        *,
        chunk_size: int = 500,
    ) -> int:
        del chunk_size
        copied_rows = [dict(row) for row in rows]
        self.event_update_calls.append(copied_rows)
        payload_by_id = {row.event_id: row.payload for row, _asset_name in self.rows}
        for item in copied_rows:
            payload_by_id[item["event_id"]]["entity_validation"] = item[
                "entity_validation"
            ]
        return len(copied_rows)

    def update_evidence_entity_validations(
        self,
        rows: list[dict[str, Any]],
        *,
        chunk_size: int = 500,
    ) -> int:
        del chunk_size
        copied_rows = [dict(row) for row in rows]
        self.evidence_update_calls.append(copied_rows)
        return len(copied_rows)


class _RowCountResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _FakeSession:
    def __init__(self, *, rowcounts: list[int] | None = None) -> None:
        self.rowcounts = list(rowcounts or [])
        self.executed: list[Any] = []
        self.flush_count = 0

    def execute(self, statement: Any) -> _RowCountResult:
        self.executed.append(statement)
        rowcount = self.rowcounts.pop(0) if self.rowcounts else 0
        return _RowCountResult(rowcount)

    def flush(self) -> None:
        self.flush_count += 1


def _event(
    event_id: str,
    *,
    symbol: str,
    title: str,
    summary: str | None = None,
) -> Any:
    return SimpleNamespace(
        event_id=event_id,
        asset_id=f"ashare:{symbol}",
        symbol=symbol,
        title=title,
        summary=summary,
        payload={
            "raw": {"新闻标题": title},
            "article": {"status": "available"},
        },
    )


def _sample_rows() -> list[tuple[Any, str | None]]:
    return [
        (
            _event("event:passed", symbol="000685", title="中山公用发布经营公告"),
            "中山公用",
        ),
        (
            _event("event:failed", symbol="000685", title="000685.SH 指数午后上涨"),
            "中山公用",
        ),
        (
            _event("event:ambiguous", symbol="000685", title="000685 发布经营提示"),
            "中山公用",
        ),
        (
            _event("event:missing", symbol="000001", title="平安银行发布经营公告"),
            None,
        ),
    ]


def test_revalidation_dry_run_reports_decisions_without_updates() -> None:
    repository = _FakeRepository(_sample_rows())

    result = StockNewsEntityRevalidationService(repository).run(apply=False)

    assert result.scanned == 4
    assert result.passed == 1
    assert result.failed == 1
    assert result.ambiguous == 2
    assert result.missing_asset == 1
    assert result.updated == 0
    assert result.reason_counts == {
        "bare_symbol_only": 1,
        "canonical_name": 1,
        "conflicting_exchange_suffix": 1,
        "missing_asset_name": 1,
    }
    assert repository.event_update_calls == []
    assert repository.evidence_update_calls == []


def test_revalidation_cli_requires_explicit_apply_flag() -> None:
    script = _import_revalidation_script()

    assert script.parse_args([]).apply is False
    assert script.parse_args(["--dry-run"]).apply is False
    assert script.parse_args(["--apply"]).apply is True
    with pytest.raises(SystemExit):
        script.parse_args(["--dry-run", "--apply"])


def test_revalidation_apply_updates_event_and_evidence_idempotently() -> None:
    rows = _sample_rows()
    repository = _FakeRepository(rows)
    service = StockNewsEntityRevalidationService(repository)

    first = service.run(apply=True, chunk_size=2)
    first_revalidated_at = rows[0][0].payload["entity_validation"].get("revalidated_at")
    second = service.run(apply=True, chunk_size=2)

    assert first.updated == second.updated == 4
    assert first.updated_events == second.updated_events == 4
    assert first.updated_evidence == second.updated_evidence == 4
    assert rows[1][0].payload["entity_validation"]["status"] == "failed"
    assert rows[2][0].payload["entity_validation"]["status"] == "ambiguous"
    assert rows[3][0].payload["entity_validation"]["status"] == "ambiguous"
    assert first_revalidated_at
    assert rows[0][0].payload["entity_validation"]["revalidated_at"] == first_revalidated_at
    assert rows[0][0].payload["raw"] == {"新闻标题": "中山公用发布经营公告"}
    assert rows[0][0].payload["article"] == {"status": "available"}


def test_repository_entity_validation_updates_only_merge_payload_subobject() -> None:
    update = {
        "event_id": "event:failed",
        "entity_validation": {
            "status": "failed",
            "reason": "conflicting_exchange_suffix",
        },
    }

    event_session = _FakeSession(rowcounts=[1])
    event_count = EventRepository(event_session).update_news_entity_validations([update])
    event_sql = str(event_session.executed[0])

    evidence_session = _FakeSession(rowcounts=[2])
    evidence_count = EventRepository(evidence_session).update_evidence_entity_validations(
        [update]
    )
    evidence_sql = str(evidence_session.executed[0])

    assert event_count == 1
    assert evidence_count == 2
    for sql in (event_sql, evidence_sql):
        assert "jsonb_set" in sql
        assert "entity_validation" in sql
        assert "DELETE" not in sql.upper()


def test_repository_revalidation_query_joins_asset_name_and_keeps_audit_rows() -> None:
    class _RowsResult(_RowCountResult):
        def all(self) -> list[Any]:
            return []

    class _ReadSession(_FakeSession):
        def execute(self, statement: Any) -> _RowsResult:
            self.executed.append(statement)
            return _RowsResult(0)

    session = _ReadSession()

    rows = EventRepository(session).list_stock_news_for_entity_revalidation(limit=10)
    sql = str(
        session.executed[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert rows == []
    assert "LEFT OUTER JOIN assets" in sql
    assert "akshare:stock_news_em" in sql
    assert "DELETE" not in sql.upper()
