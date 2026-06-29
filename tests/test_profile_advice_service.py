from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from finance_agent.application.profile_advice_service import (
    ProfileAdviceService,
    UserInvestmentProfileService,
)


class FakeProfileRepository:
    def __init__(self) -> None:
        self.upserts: list[dict[str, Any]] = []

    def get_or_default(self, *, owner_id: str) -> Any:
        return type(
            "Profile",
            (),
            {
                "profile_id": f"profile:{owner_id}",
                "owner_id": owner_id,
                "risk_appetite": "balanced",
                "horizon": "swing",
                "capital_scale": "unknown",
                "style_tendency": {"value": 0.6, "theme": 0.4},
                "timing_posture": "neutral",
                "dimension_confidence": {"risk_appetite": 0.1},
                "source": {"risk_appetite": "default"},
                "status": "active",
                "payload": {},
                "updated_at": datetime(2026, 6, 30, tzinfo=UTC),
            },
        )()

    def upsert_profile(self, **kwargs: Any) -> Any:
        self.upserts.append(kwargs)
        return self.get_or_default(owner_id=kwargs["owner_id"])


class FakeDecisionStore:
    def __init__(self, decisions: list[dict[str, Any]], reviews: list[dict[str, Any]]) -> None:
        self.decisions = decisions
        self.reviews = reviews

    def list_recent_decision_feedback(self, *, owner_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return self.decisions[:limit]

    def list_recent_review_outcomes(self, *, owner_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return self.reviews[:limit]


def test_profile_service_get_returns_default_profile_payload() -> None:
    service = UserInvestmentProfileService(repository=FakeProfileRepository())

    payload = service.get_profile(owner_id="owner:demo")

    assert payload["profile_id"] == "profile:owner:demo"
    assert payload["risk_appetite"] == "balanced"
    assert payload["style_tendency"] == {"value": 0.6, "theme": 0.4}
    assert payload["source"]["risk_appetite"] == "default"


def test_profile_service_upsert_requires_source_and_evidence() -> None:
    repo = FakeProfileRepository()
    service = UserInvestmentProfileService(repository=repo)

    payload = service.upsert_profile(
        owner_id="owner:demo",
        updates={"risk_appetite": "conservative"},
        source={"risk_appetite": "elicited"},
        evidence=[{"type": "chat", "id": "chat:turn:1"}],
    )

    assert payload["profile_id"] == "profile:owner:demo"
    assert repo.upserts[0]["risk_appetite"] == "conservative"
    assert repo.upserts[0]["source"] == {"risk_appetite": "elicited"}
    assert repo.upserts[0]["evidence"][0]["id"] == "chat:turn:1"


def test_profile_service_upsert_rejects_missing_source_or_evidence() -> None:
    service = UserInvestmentProfileService(repository=FakeProfileRepository())

    try:
        service.upsert_profile(
            owner_id="owner:demo",
            updates={"risk_appetite": "aggressive"},
            source={},
            evidence=[],
        )
    except ValueError as exc:
        assert "source" in str(exc)
    else:
        raise AssertionError("profile.upsert 必须拒绝缺少 source/evidence 的写入")


def test_advice_suggest_style_uses_rejections_and_reviews_with_traceable_evidence() -> None:
    store = FakeDecisionStore(
        decisions=[
            {"decision_id": "decision:1", "action": "reject", "style": "theme"},
            {"decision_id": "decision:2", "action": "reject", "style": "theme"},
            {"decision_id": "decision:3", "action": "reject", "style": "theme"},
        ],
        reviews=[
            {
                "review_task_id": "review:1",
                "outcome": "loss",
                "tags": ["chased_high"],
                "realized_return": Decimal("-0.08"),
            }
        ],
    )
    service = ProfileAdviceService(profile_service=UserInvestmentProfileService(FakeProfileRepository()), store=store)

    result = service.suggest_style(owner_id="owner:demo")

    assert result["suggested_risk_appetite"] == "conservative"
    assert result["suggested_timing_posture"] == "defensive"
    assert result["suggested_style_tendency"]["value"] > result["suggested_style_tendency"]["theme"]
    evidence_ids = {item["id"] for item in result["evidence"]}
    assert {"decision:1", "decision:2", "decision:3", "review:1"}.issubset(evidence_ids)
    assert result["llm_role"] == "explanation_only"
    assert result["deterministic_fields_unchanged"] == [
        "asset_scores.total_score",
        "signal_snapshots.direction",
        "risk_findings.severity",
    ]

