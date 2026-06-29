from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from finance_agent.recommendations.readiness import evaluate_recommendation_readiness


def test_recommendation_readiness_blocks_smoke_runs() -> None:
    readiness = evaluate_recommendation_readiness(
        run=run(run_id="run:smoke:1", payload={"source": "smoke"}),
        recommendations=[recommendation()],
        as_of=datetime(2026, 6, 30, 9, 30, tzinfo=UTC),
    )

    assert readiness.status == "blocked"
    assert "smoke" in readiness.reasons
    assert readiness.executable is False


def test_recommendation_readiness_blocks_stale_runs() -> None:
    readiness = evaluate_recommendation_readiness(
        run=run(finished_at=datetime(2026, 6, 1, 15, 0, tzinfo=UTC)),
        recommendations=[recommendation()],
        as_of=datetime(2026, 6, 30, 9, 30, tzinfo=UTC),
        max_age=timedelta(days=7),
    )

    assert readiness.status == "blocked"
    assert "stale" in readiness.reasons


def test_recommendation_readiness_blocks_future_timestamps() -> None:
    readiness = evaluate_recommendation_readiness(
        run=run(finished_at=datetime(2026, 7, 1, 15, 0, tzinfo=UTC)),
        recommendations=[recommendation()],
        as_of=datetime(2026, 6, 30, 9, 30, tzinfo=UTC),
    )

    assert readiness.status == "blocked"
    assert "future_timestamp" in readiness.reasons


def test_recommendation_readiness_blocks_bad_quality_payloads() -> None:
    readiness = evaluate_recommendation_readiness(
        run=run(),
        recommendations=[
            recommendation(
                payload={
                    "data_quality": {"status": "stale"},
                    "backtest_evidence": {"status": "missing"},
                }
            )
        ],
        as_of=datetime(2026, 6, 30, 9, 30, tzinfo=UTC),
    )

    assert readiness.status == "blocked"
    assert "data_quality" in readiness.reasons
    assert "missing_backtest_evidence" in readiness.reasons


def test_recommendation_readiness_blocks_absent_backtest_evidence() -> None:
    readiness = evaluate_recommendation_readiness(
        run=run(),
        recommendations=[
            recommendation(
                payload={
                    "data_quality": {"status": "available"},
                }
            )
        ],
        as_of=datetime(2026, 6, 30, 9, 30, tzinfo=UTC),
    )

    assert readiness.status == "blocked"
    assert "missing_backtest_evidence" in readiness.reasons


def test_recommendation_readiness_allows_fresh_real_runs() -> None:
    readiness = evaluate_recommendation_readiness(
        run=run(),
        recommendations=[
            recommendation(
                payload={
                    "data_quality": {"status": "available"},
                    "backtest_evidence": {"status": "available"},
                }
            )
        ],
        as_of=datetime(2026, 6, 30, 9, 30, tzinfo=UTC),
    )

    assert readiness.status == "ready"
    assert readiness.reasons == []
    assert readiness.executable is True


def run(
    *,
    run_id: str = "run:real:1",
    finished_at: datetime = datetime(2026, 6, 30, 8, 30, tzinfo=UTC),
    payload: dict[str, object] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        run_id=run_id,
        strategy="balanced_swing_v1",
        universe_id="universe:ashare:recommendation",
        status="available",
        finished_at=finished_at,
        started_at=finished_at - timedelta(minutes=5),
        payload=payload or {},
    )


def recommendation(payload: dict[str, object] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        recommendation_id="rec:1",
        asset_id="ashare:600519",
        payload=payload or {},
    )
