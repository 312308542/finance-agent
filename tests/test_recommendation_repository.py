from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy.dialects import postgresql

from finance_agent.recommendations.lifecycle import RecommendationTransition
from finance_agent.recommendations.state_repository import RecommendationStateRepository
from finance_agent.storage.orm import (
    RecommendationLifecycleEventORM,
    RecommendationLifecycleStateORM,
    StockSetupORM,
)
from finance_agent.storage.repositories import RecommendationRepository

MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "src"
    / "finance_agent"
    / "storage"
    / "migrations"
    / "versions"
    / "20260904_0029_create_recommendation_lifecycle.py"
)


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


class _LifecycleScalarRows:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def __iter__(self):
        return iter(self.rows)

    def one_or_none(self) -> Any | None:
        return self.rows[0] if self.rows else None


class _LifecycleSession:
    def __init__(self) -> None:
        self.current: Any | None = None
        self.events: list[Any] = []
        self.flush_count = 0

    def execute(self, statement: Any) -> None:
        table_name = statement.table.name
        values = statement.compile(dialect=postgresql.dialect()).params
        if table_name == "recommendation_lifecycle_states":
            self.current = SimpleNamespace(**values)
        elif table_name == "recommendation_lifecycle_events":
            if not any(event.event_id == values["event_id"] for event in self.events):
                self.events.append(SimpleNamespace(**values))

    def scalars(self, statement: Any) -> _LifecycleScalarRows:
        sql = str(statement)
        if "recommendation_lifecycle_events" in sql:
            return _LifecycleScalarRows(self.events)
        return _LifecycleScalarRows([self.current] if self.current is not None else [])

    def get_one(self, model: Any, _key: Any) -> Any:
        if model is RecommendationLifecycleStateORM:
            return self.current
        raise AssertionError(f"未预期的 get_one: {model}")

    def flush(self) -> None:
        self.flush_count += 1


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


def test_recommendation_lifecycle_schema_and_migration_contract() -> None:
    assert StockSetupORM.__table__.name == "stock_setups"
    assert RecommendationLifecycleStateORM.__table__.name == "recommendation_lifecycle_states"
    assert RecommendationLifecycleEventORM.__table__.name == "recommendation_lifecycle_events"
    assert {column.name for column in RecommendationLifecycleStateORM.__table__.columns} >= {
        "owner_id",
        "strategy_id",
        "asset_id",
        "current_state",
        "previous_state",
        "decision_snapshot_id",
    }
    content = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision = "20260904_0029"' in content
    assert 'down_revision = "20260904_0028"' in content
    assert "uq_recommendation_lifecycle_owner_strategy_asset" in content


def test_lifecycle_repository_keeps_one_current_state_and_append_only_events() -> None:
    session = _LifecycleSession()
    repository = RecommendationStateRepository(session)  # type: ignore[arg-type]
    first = _transition(None, "watch", occurred_at=datetime(2026, 9, 7, tzinfo=UTC))
    second = _transition(
        "watch",
        "setup_confirming",
        occurred_at=datetime(2026, 9, 7, tzinfo=UTC) + timedelta(days=1),
    )

    repository.save_transition(first)
    repository.save_transition(second)

    current = repository.get_state(
        owner_id="default-owner",
        strategy_id="strategy:ashare:adaptive_v1",
        asset_id="ashare:600519",
    )
    events = repository.list_events("state:default-owner:adaptive:600519")
    assert current.current_state == "setup_confirming"
    assert current.previous_state == "watch"
    assert [event.to_state for event in events] == ["watch", "setup_confirming"]
    assert session.flush_count == 2


def test_lifecycle_repository_does_not_append_unchanged_action_reason() -> None:
    session = _LifecycleSession()
    repository = RecommendationStateRepository(session)  # type: ignore[arg-type]
    first = _transition(None, "watch", occurred_at=datetime(2026, 9, 7, tzinfo=UTC))
    repeated = RecommendationTransition(
        **{
            **first.__dict__,
            "event_id": "event:watch:repeat",
            "from_state": "watch",
            "decision_snapshot_id": "decision:2",
            "occurred_at": datetime(2026, 9, 8, tzinfo=UTC),
            "payload": {"quote": "refreshed"},
        }
    )

    repository.save_transition(first)
    repository.save_transition(repeated)

    assert [event.to_state for event in session.events] == ["watch"]
    assert session.current.decision_snapshot_id == "decision:2"
    assert session.current.payload == {
        "quote": "refreshed",
        "last_reason_codes": ["to_watch"],
    }


def _transition(
    from_state: str | None,
    to_state: str,
    *,
    occurred_at: datetime,
) -> RecommendationTransition:
    return RecommendationTransition(
        event_id=f"event:{to_state}",
        state_id="state:default-owner:adaptive:600519",
        owner_id="default-owner",
        strategy_id="strategy:ashare:adaptive_v1",
        asset_id="ashare:600519",
        setup_id="setup:1",
        from_state=from_state,
        to_state=to_state,  # type: ignore[arg-type]
        reason_codes=(f"to_{to_state}",),
        decision_snapshot_id="decision:1",
        occurred_at=occurred_at,
        consecutive_valid_closes=1 if to_state == "setup_confirming" else 0,
        active_days=0,
        cooldown_until=None,
        payload={},
    )
