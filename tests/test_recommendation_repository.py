from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from finance_agent.storage.repositories import RecommendationRepository


class _FakeScalarRows:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def __iter__(self):
        return iter(self.rows)


class _FakeSession:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.statements: list[Any] = []

    def scalars(self, statement: Any) -> _FakeScalarRows:
        self.statements.append(statement)
        return _FakeScalarRows(self.rows)


def test_list_available_runs_since_excludes_smoke_runs_by_default() -> None:
    """默认推荐查询不能把冒烟样例运行返回给 Agent 或 Dashboard。"""

    real_run = SimpleNamespace(
        run_id="run:balanced_swing_v1:swing:20260528T030000Z:real",
        strategy="balanced_swing_v1",
        universe_id="universe:base:ashare:p0:all_a",
        payload={"source": {"universe_id": "universe:base:ashare:p0:all_a"}},
    )
    smoke_run = SimpleNamespace(
        run_id="run:balanced_swing_v1:swing:20260520T210702Z:smoke",
        strategy="balanced_swing_v1",
        universe_id="universe:smoke:ashare:batch",
        payload={"source": "universe_pipeline_smoke"},
    )
    session = _FakeSession([smoke_run, real_run])

    runs = RecommendationRepository(session).list_available_runs_since(
        since=datetime(2026, 5, 1, tzinfo=UTC),
        market="ashare",
        limit=5,
    )

    assert runs == [real_run]
    compiled = str(session.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "%smoke%" in compiled.lower()


def test_list_available_runs_since_can_include_smoke_for_diagnostics() -> None:
    """诊断脚本显式声明时仍可查看 smoke 推荐运行。"""

    smoke_run = SimpleNamespace(
        run_id="run:smoke:v12:20260520034131",
        strategy="trigger_smoke",
        universe_id=None,
        payload={"source": "smoke_v12_trigger_events"},
    )
    session = _FakeSession([smoke_run])

    runs = RecommendationRepository(session).list_available_runs_since(
        since=datetime(2026, 5, 1, tzinfo=UTC),
        limit=5,
        include_smoke=True,
    )

    assert runs == [smoke_run]
