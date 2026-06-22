from __future__ import annotations

from typing import Any

from sqlalchemy.dialects import postgresql

from finance_agent.agents.context_builder import AgentContextBuilder


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
    return str(statement.compile(dialect=postgresql.dialect()))


def test_agent_context_recent_events_uses_default_signal_window() -> None:
    """Agent 上下文默认只读取近期事件，旧新闻不应继续进入模型输入。"""

    session = _FakeSession()
    builder = AgentContextBuilder(session, _FakeCache())

    builder._load_recent_events("ashare:600519", limit=5)

    sql = _compiled(session.scalars_statements[0])

    assert "event_records.published_at >=" in sql
    assert "event_records.published_at IS NULL" in sql
    assert "event_records.collected_at >=" in sql


def test_agent_context_recent_events_can_disable_signal_window() -> None:
    """历史审计场景可以关闭 Agent 上下文事件窗口。"""

    session = _FakeSession()
    builder = AgentContextBuilder(session, _FakeCache(), event_lookback_days=None)

    builder._load_recent_events("ashare:600519", limit=5)

    sql = _compiled(session.scalars_statements[0])

    assert "event_records.published_at >=" not in sql
    assert "event_records.collected_at >=" not in sql
