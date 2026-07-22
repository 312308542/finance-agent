from datetime import UTC, datetime, timedelta

from finance_agent.application.decision_gate import DecisionGateInput, DecisionGateService
from finance_agent.storage.snapshot_contracts import build_data_snapshot

NOW = datetime(2026, 7, 20, 9, 35, tzinfo=UTC)


def _snapshot(*, quality_status: str = "available", as_of: datetime = NOW) -> object:
    return build_data_snapshot(
        snapshot_type="ashare_realtime_quotes",
        market="ashare",
        as_of=as_of,
        captured_at=as_of,
        provider="gotdx:tdx_main",
        quality_status=quality_status,
        payload={"quotes": [{"symbol": "600519.SH", "last_price": "1500.00"}]},
    )


def test_gate_approves_fresh_low_risk_input_with_evidence() -> None:
    result = DecisionGateService().evaluate(
        DecisionGateInput(
            decision_type="portfolio_monitoring",
            action="hold",
            snapshot=_snapshot(),
            evaluated_at=NOW,
            evidence_ids=("evidence:quote:1",),
        )
    )

    assert result.status == "approved"
    assert result.reason_codes == ()
    assert result.data_snapshot_id.startswith("snapshot:")


def test_gate_rejects_missing_snapshot_as_data_unavailable() -> None:
    result = DecisionGateService().evaluate(
        DecisionGateInput(
            decision_type="recommendation_decision",
            action="buy",
            snapshot=None,
            evaluated_at=NOW,
            evidence_ids=("evidence:recommendation:1",),
        )
    )

    assert result.status == "data_unavailable"
    assert "snapshot_missing" in result.reason_codes


def test_gate_rejects_stale_snapshot() -> None:
    result = DecisionGateService(max_age=timedelta(seconds=10)).evaluate(
        DecisionGateInput(
            decision_type="portfolio_monitoring",
            action="sell",
            snapshot=_snapshot(as_of=NOW - timedelta(seconds=11)),
            evaluated_at=NOW,
            evidence_ids=("evidence:risk:1",),
        )
    )

    assert result.status == "expired"
    assert "snapshot_expired" in result.reason_codes


def test_gate_rejects_conflicting_or_incomplete_snapshot() -> None:
    for quality_status, expected in (("conflict", "snapshot_conflict"), ("partial", "snapshot_incomplete")):
        result = DecisionGateService().evaluate(
            DecisionGateInput(
                decision_type="recommendation_decision",
                action="buy",
                snapshot=_snapshot(quality_status=quality_status),
                evaluated_at=NOW,
                evidence_ids=("evidence:recommendation:1",),
            )
        )
        assert result.status == "rejected"
        assert expected in result.reason_codes


def test_gate_requires_review_for_high_risk_action_without_confirmation() -> None:
    result = DecisionGateService().evaluate(
        DecisionGateInput(
            decision_type="portfolio_monitoring",
            action="sell",
            snapshot=_snapshot(),
            evaluated_at=NOW,
            evidence_ids=("evidence:risk:1",),
            requires_human_confirmation=True,
            human_confirmed=False,
        )
    )

    assert result.status == "pending_review"
    assert "human_confirmation_required" in result.reason_codes


def test_gate_rejects_missing_evidence_when_required() -> None:
    result = DecisionGateService().evaluate(
        DecisionGateInput(
            decision_type="recommendation_decision",
            action="buy",
            snapshot=_snapshot(),
            evaluated_at=NOW,
            require_evidence=True,
        )
    )

    assert result.status == "rejected"
    assert "evidence_missing" in result.reason_codes
