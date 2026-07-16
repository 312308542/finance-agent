from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.dialects import postgresql

from finance_agent.storage.orm import AssistantMemoryORM, UserInvestmentProfileORM
from finance_agent.storage.repositories import UserInvestmentProfileRepository


class _FakeResult:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []

    def scalars(self) -> _FakeResult:
        return self

    def one_or_none(self) -> Any | None:
        return self.rows[0] if self.rows else None


class _FakeSession:
    def __init__(self) -> None:
        self.profiles: dict[str, UserInvestmentProfileORM] = {}
        self.memories: dict[str, AssistantMemoryORM] = {}
        self.executed: list[Any] = []
        self.flush_count = 0

    def execute(self, statement: Any) -> _FakeResult:
        self.executed.append(statement)
        sql = str(statement)
        params = statement.compile(dialect=postgresql.dialect()).params
        if "INSERT INTO user_investment_profiles" in sql:
            self.add(UserInvestmentProfileORM(**params))
        if "INSERT INTO assistant_memories" in sql:
            self.add(AssistantMemoryORM(**params))
        return _FakeResult()

    def get(self, model: Any, key: str) -> Any | None:
        if model is UserInvestmentProfileORM:
            return self.profiles.get(key)
        if model is AssistantMemoryORM:
            return self.memories.get(key)
        return None

    def get_one(self, model: Any, key: str) -> Any:
        row = self.get(model, key)
        if row is None:
            raise LookupError(key)
        return row

    def refresh(self, instance: Any) -> None:
        return None

    def add(self, instance: Any) -> None:
        if isinstance(instance, UserInvestmentProfileORM):
            self.profiles[instance.profile_id] = instance
        if isinstance(instance, AssistantMemoryORM):
            self.memories[instance.memory_id] = instance

    def flush(self) -> None:
        self.flush_count += 1


def _compiled(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def test_profile_repository_returns_default_profile_for_new_owner() -> None:
    session = _FakeSession()
    repo = UserInvestmentProfileRepository(session)

    profile = repo.get_or_default(owner_id="owner:demo")

    assert profile.profile_id == "profile:owner:demo"
    assert profile.owner_id == "owner:demo"
    assert profile.risk_appetite == "balanced"
    assert profile.horizon == "swing"
    assert profile.style_tendency == {"value": 0.6, "theme": 0.4}
    assert profile.timing_posture == "neutral"
    assert profile.dimension_confidence["risk_appetite"] == 0.1
    assert profile.source["risk_appetite"] == "default"
    assert profile.status == "active"
    assert session.executed == []


def test_profile_repository_upserts_dimension_confidence_and_audit_memory() -> None:
    session = _FakeSession()
    repo = UserInvestmentProfileRepository(session)
    updated_at = datetime(2026, 6, 30, 9, 30, tzinfo=UTC)

    profile = repo.upsert_profile(
        owner_id="owner:demo",
        risk_appetite="conservative",
        horizon="mid_long",
        style_tendency={"value": 0.8, "theme": 0.2},
        timing_posture="defensive",
        source={"risk_appetite": "elicited", "timing_posture": "inferred"},
        evidence=[
            {"type": "decision", "id": "decision:reject:1"},
            {"type": "review", "id": "review:loss:1"},
        ],
        confidence_delta=Decimal("0.25"),
        updated_at=updated_at,
    )

    assert profile.risk_appetite == "conservative"
    assert profile.dimension_confidence["risk_appetite"] == 0.35
    assert profile.dimension_confidence["timing_posture"] == 0.35
    assert profile.source["risk_appetite"] == "elicited"
    assert profile.payload["evidence"][0]["id"] == "decision:reject:1"
    assert session.flush_count >= 1
    assert "INSERT INTO user_investment_profiles" in _compiled(session.executed[0])
    memory = session.memories["memory:investment_profile:owner:demo"]
    assert memory.memory_type == "investment_profile"
    assert memory.owner_id == "owner:demo"
    assert memory.payload["profile_id"] == "profile:owner:demo"
    assert memory.payload["evidence"][1]["id"] == "review:loss:1"


def test_profile_repository_decay_marks_profile_stale_when_confidence_drops() -> None:
    session = _FakeSession()
    repo = UserInvestmentProfileRepository(session)
    profile = repo.upsert_profile(
        owner_id="owner:demo",
        source={"risk_appetite": "elicited"},
        confidence_delta=Decimal("0.20"),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert profile.status == "active"

    decayed = repo.apply_confidence_decay(
        owner_id="owner:demo",
        as_of=datetime(2026, 6, 30, tzinfo=UTC),
        half_life_days=30,
        stale_threshold=Decimal("0.05"),
    )

    assert decayed is not None
    assert decayed.status == "stale"
    assert decayed.dimension_confidence["risk_appetite"] < 0.05
