from __future__ import annotations

from dataclasses import replace

from finance_agent.scoring.service import compute_asset_score
from tests.test_scoring_strategies import _Factor


def test_missing_groups_reduce_data_confidence_not_total_score() -> None:
    full = compute_asset_score(_Factor())
    missing = compute_asset_score(
        replace(
            _Factor(),
            missing_groups=("valuation", "event"),
            payload={
                "factor_groups": [
                    {"group": "technical", "score": 90, "status": "available"},
                    {"group": "fundamental", "score": 50, "status": "available"},
                    {"group": "capital_flow", "score": 70, "status": "available"},
                    {"group": "liquidity", "score": 60, "status": "available"},
                    {"group": "event_decay", "score": 80, "status": "available"},
                    {"group": "risk", "score": 90, "status": "available"},
                ],
                "partial_groups": [],
            },
        )
    )

    assert missing["missing_penalty"] == 0
    assert missing["total_score"] > full["total_score"]
    assert missing["data_confidence"] < full["data_confidence"]
    assert missing["confidence"] == missing["data_confidence"]
    assert missing["status"] == "partial"


def test_legacy_score_remains_unchanged_when_no_missing_groups() -> None:
    score = compute_asset_score(_Factor())

    assert score["total_score"] == 63.1
    assert score["confidence"] == 1.0
    assert score["data_confidence"] == 1.0
    assert score["missing_penalty"] == 0
