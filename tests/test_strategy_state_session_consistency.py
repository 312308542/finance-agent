"""真实 Session 中策略状态写入后不得继续使用 identity map 的旧值。"""

from __future__ import annotations

from argparse import Namespace
from collections.abc import Iterator
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import JSON, Column, MetaData, Table, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from finance_agent.cli import data_sync
from finance_agent.pipelines.recommendation import recommendation_strategy_gate
from finance_agent.research.strategy_observation_service import (
    SqlStrategyObservationStore,
    StrategyObservationService,
)
from finance_agent.research.validation_gate import StrategyValidationGate
from finance_agent.scheduler.base_data_scheduler import BaseDataScheduler, BaseDataSchedulerConfig
from finance_agent.storage import db
from finance_agent.storage.orm import StrategyTrialStateORM
from finance_agent.storage.repositories import StrategyObservationRepository

STRATEGY_ID = "strategy:ashare:session_consistency"
NOW = datetime(2026, 9, 7, tzinfo=UTC)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    original = StrategyTrialStateORM.__table__
    # 只替换建表时的 PostgreSQL JSONB 类型，仓储 upsert 和 ORM 查询仍执行真实 SQL。
    table = Table(
        original.name,
        MetaData(),
        *(
            Column(
                column.name,
                JSON() if isinstance(column.type, JSONB) else column.type,
                primary_key=column.primary_key,
                nullable=column.nullable,
            )
            for column in original.columns
        ),
    )
    table.create(engine)
    try:
        with Session(engine, expire_on_commit=False) as current:
            yield current
    finally:
        engine.dispose()


def _metrics(*, rolling_excess: float = 0.02) -> dict[str, Any]:
    return {"t20_count": 60, "median_excess": 0.02, "rolling_excess": rolling_excess}


def _seed_state(session: Session, *, state: str) -> StrategyTrialStateORM:
    row = StrategyObservationRepository(session).upsert_trial_state(
        strategy_id=STRATEGY_ID,
        strategy_version="v1",
        state=state,
        historical_evidence_id="bt:qualified",
        forward_metrics=_metrics(),
        consecutive_failure_count=0,
        disabled_reason=None,
    )
    session.commit()
    return row


@pytest.mark.parametrize("transition", ["disable", "promote", "revoke_evidence"])
@pytest.mark.parametrize("consumer", ["service", "pipeline", "cli"])
def test_same_transaction_consumers_observe_updated_trial_state(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    transition: str,
    consumer: str,
) -> None:
    cached = _seed_state(session, state="trial" if transition == "promote" else "validated")
    service = StrategyObservationService(repository=SqlStrategyObservationStore(session), source=None)
    if transition == "revoke_evidence":
        saved = service.apply_historical_result(
            strategy_id=STRATEGY_ID, result={"status": "unavailable", "metrics": {}}
        )
    else:
        saved = service.evaluate_weekly(
            strategy_id=STRATEGY_ID,
            as_of=NOW,
            metrics=_metrics(rolling_excess=-0.02 if transition == "disable" else 0.02),
        )

    expected_state = "disabled" if transition == "disable" else "validated"
    expected_evidence = None if transition == "revoke_evidence" else "bt:qualified"
    expected_allowed = transition == "promote"
    persisted = session.execute(
        select(StrategyTrialStateORM.state, StrategyTrialStateORM.historical_evidence_id)
    ).one()
    assert tuple(persisted) == (expected_state, expected_evidence)
    assert session.in_transaction()

    if consumer == "service":
        assert saved.state == expected_state
        assert saved.historical_evidence_id == expected_evidence
        assert StrategyValidationGate().evaluate_runtime(saved, action="buy").allowed is expected_allowed
    elif consumer == "pipeline":
        result = recommendation_strategy_gate(
            market="ashare",
            strategy_id=STRATEGY_ID,
            trial_states=StrategyObservationRepository(session),
        )
        assert result["allowed"] is expected_allowed
        assert result["trial_state"] == expected_state
        assert result["validation_evidence_id"] == expected_evidence
    else:
        monkeypatch.setattr(data_sync, "create_session_factory", lambda _url: object())
        monkeypatch.setattr(data_sync, "session_scope", lambda _factory: nullcontext(session))
        result = data_sync.dispatch_data_strategy(
            Namespace(
                database_url="sqlite://", subcommand="validate", strategy_id=STRATEGY_ID, market="ashare"
            )
        )
        assert result["data"]["allow_new_buys"] is expected_allowed
        assert result["data"]["strategy_state"] == expected_state
        assert result["data"]["historical_evidence_id"] == expected_evidence

    # 保持先前实例的强引用，避免弱引用缓存恰巧回收而掩盖问题。
    assert saved is cached
    assert cached.state == expected_state
    assert cached.historical_evidence_id == expected_evidence


def test_scheduler_returns_buy_block_immediately_after_persisting_disable(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    cached = _seed_state(session, state="validated")
    monkeypatch.setattr(db, "create_session_factory", lambda: object())
    monkeypatch.setattr(db, "session_scope", lambda _factory: nullcontext(session))
    monkeypatch.setattr(
        StrategyObservationService,
        "build_forward_metrics",
        lambda _self, **_kwargs: _metrics(rolling_excess=-0.02),
    )
    scheduler = BaseDataScheduler(BaseDataSchedulerConfig(cache_backend="null", jobs=()))

    result = scheduler.run_backtest(strategy="strategy_validation_gate", strategy_id=STRATEGY_ID, as_of=NOW)

    assert session.scalar(select(StrategyTrialStateORM.state)) == "disabled"
    assert result["state"] == "disabled"
    assert result["allow_new_buys"] is False
    assert cached.state == "disabled"
