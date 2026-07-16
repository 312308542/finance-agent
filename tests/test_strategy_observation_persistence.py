from __future__ import annotations

import importlib
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from sqlalchemy.dialects import postgresql

from finance_agent.research.strategy_observation_service import (
    SqlStrategyObservationStore,
)
from finance_agent.storage import StrategyObservationRepository
from finance_agent.storage.orm import (
    StrategyObservationOutcomeORM,
    StrategyObservationPositionORM,
    StrategyObservationRunORM,
    StrategyTrialStateORM,
)


class _FakeScalarResult:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []

    def __iter__(self):
        return iter(self.rows)


class _RowCountResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _FakeSession:
    def __init__(self) -> None:
        self.executed: list[Any] = []
        self.scalars_statements: list[Any] = []
        self.flush_count = 0
        self.objects: dict[tuple[Any, Any], Any] = {}

    def execute(self, statement: Any) -> _RowCountResult:
        self.executed.append(statement)
        return _RowCountResult(1)

    def scalars(self, statement: Any) -> _FakeScalarResult:
        self.scalars_statements.append(statement)
        return _FakeScalarResult()

    def flush(self) -> None:
        self.flush_count += 1

    def get_one(self, model: Any, key: Any) -> Any:
        if model is StrategyObservationRunORM:
            return SimpleNamespace(observation_id=key)
        if model is StrategyTrialStateORM:
            return SimpleNamespace(strategy_id=key)
        raise AssertionError(f"不支持的 get_one: {model}")

    def get(self, model: Any, key: Any) -> Any | None:
        return self.objects.get((model, key))


def _compiled(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def test_observation_models_define_append_only_unique_keys() -> None:
    assert StrategyObservationRunORM.__tablename__ == "strategy_observation_runs"
    assert StrategyObservationPositionORM.__tablename__ == "strategy_observation_positions"
    assert StrategyObservationOutcomeORM.__tablename__ == "strategy_observation_outcomes"
    assert StrategyTrialStateORM.__tablename__ == "strategy_trial_states"

    run_constraints = {item.name for item in StrategyObservationRunORM.__table__.constraints}
    position_constraints = {
        item.name for item in StrategyObservationPositionORM.__table__.constraints
    }
    outcome_constraints = {
        item.name for item in StrategyObservationOutcomeORM.__table__.constraints
    }
    state_constraints = {item.name for item in StrategyTrialStateORM.__table__.constraints}

    assert "uq_strategy_observation_run_day_universe" in run_constraints
    assert "uq_strategy_position_asset" in position_constraints
    assert "uq_strategy_outcome_horizon" in outcome_constraints
    assert "ck_strategy_outcome_horizon" in outcome_constraints
    assert "ck_strategy_trial_state" in state_constraints
    assert StrategyObservationPositionORM.entry_price.type.precision == 18
    assert StrategyObservationPositionORM.entry_price.type.scale == 8
    assert StrategyObservationOutcomeORM.net_return.type.precision == 18
    assert StrategyObservationOutcomeORM.net_return.type.scale == 8


def test_0022_migration_follows_0021_and_seeds_fixed_mixed_strategy() -> None:
    migration = importlib.import_module(
        "finance_agent.storage.migrations.versions."
        "20260716_0022_create_strategy_observation_tables"
    )

    assert migration.revision == "20260716_0022"
    assert migration.down_revision == "20260716_0021"
    assert migration.MIXED_STRATEGY_ID == "strategy:ashare:short_theme_mixed_v1"
    assert migration.MIXED_STRATEGY_SEED["group_weights"]["technical"] == 0.27
    assert migration.MIXED_STRATEGY_SEED["missing_penalty"]["per_missing_group"] == 3.5


def test_repository_upserts_same_trade_date_without_overwriting_previous_date() -> None:
    session = _FakeSession()
    repository = StrategyObservationRepository(session)
    common = {
        "universe_id": "universe:merged:ashare:recommendation",
        "status": "captured",
        "data_versions": {"commit": "abc123"},
        "payload": {},
    }

    first = repository.upsert_run(
        observation_id="obs:20260716",
        trade_date=date(2026, 7, 16),
        screening_id="screen:20260716",
        **common,
    )
    same = repository.upsert_run(
        observation_id="obs:20260716",
        trade_date=date(2026, 7, 16),
        screening_id="screen:20260716",
        **common,
    )
    next_day = repository.upsert_run(
        observation_id="obs:20260717",
        trade_date=date(2026, 7, 17),
        screening_id="screen:20260717",
        **common,
    )

    assert first.observation_id == same.observation_id
    assert next_day.observation_id != first.observation_id
    assert all("ON CONFLICT" in _compiled(statement) for statement in session.executed)


def test_repository_writes_positions_outcomes_and_trial_state_idempotently() -> None:
    session = _FakeSession()
    repository = StrategyObservationRepository(session)
    now = datetime(2026, 7, 16, 15, 0, tzinfo=UTC)

    position_count = repository.upsert_positions(
        [
            {
                "position_id": "position:1",
                "observation_id": "obs:20260716",
                "strategy_id": "strategy:ashare:short_swing",
                "asset_id": "ashare:000001",
                "symbol": "000001",
                "rank": 1,
                "score_id": "score:1",
                "signal_date": date(2026, 7, 16),
                "status": "pending",
                "payload": {},
                "created_at": now,
            }
        ]
    )
    outcome_count = repository.ensure_outcomes(
        [
            {
                "outcome_id": "outcome:1:5",
                "position_id": "position:1",
                "horizon_days": 5,
                "due_trade_date": date(2026, 7, 23),
                "status": "pending",
                "payload": {},
                "created_at": now,
            }
        ]
    )
    state = repository.upsert_trial_state(
        strategy_id="strategy:ashare:short_theme_mixed_v1",
        strategy_version="v1",
        state="research",
        historical_evidence_id=None,
        forward_metrics={},
        consecutive_failure_count=0,
        disabled_reason=None,
        payload={},
    )

    assert position_count == 1
    assert outcome_count == 1
    assert state.strategy_id == "strategy:ashare:short_theme_mixed_v1"
    assert "ON CONFLICT" in _compiled(session.executed[0])
    assert "ON CONFLICT" in _compiled(session.executed[1])
    assert "ON CONFLICT" in _compiled(session.executed[2])


def test_repository_due_and_matured_queries_preserve_pending_history() -> None:
    session = _FakeSession()
    repository = StrategyObservationRepository(session)

    due = repository.list_due_outcomes(as_of=date(2026, 7, 23), limit=20)
    matured = repository.mature_outcomes(
        [
            {
                "outcome_id": "outcome:1:5",
                "status": "matured",
                "exit_date": date(2026, 7, 23),
                "exit_price": Decimal("12.00000000"),
                "gross_return": Decimal("0.20000000"),
                "net_return": Decimal("0.19700000"),
                "benchmark_return": Decimal("0.10000000"),
                "excess_return": Decimal("0.09700000"),
                "reason": None,
                "payload": {"cost": "0.003"},
            }
        ]
    )

    due_sql = _compiled(session.scalars_statements[0])
    mature_sql = _compiled(session.executed[0])

    assert due == []
    assert "strategy_observation_outcomes.status" in due_sql
    assert "strategy_observation_outcomes.due_trade_date" in due_sql
    assert matured == 1
    assert "UPDATE strategy_observation_outcomes" in mature_sql


def test_sql_observation_store_only_updates_existing_position_entry() -> None:
    """结算适配器只补写既有仓位入场事实，不插入或覆盖观察批次。"""

    session = _FakeSession()
    store = SqlStrategyObservationStore(session)

    updated = store.update_position_entries(
        [
            {
                "position_id": "position:1",
                "entry_date": date(2026, 7, 17),
                "entry_price": Decimal("10.00000000"),
                "benchmark_entry_price": None,
                "status": "entered",
            }
        ]
    )

    sql = _compiled(session.executed[0])
    assert updated == 1
    assert "UPDATE strategy_observation_positions" in sql
    assert "entry_price" in sql
    assert "WHERE strategy_observation_positions.position_id" in sql
