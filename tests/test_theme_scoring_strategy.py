from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from finance_agent.scoring.service import compute_asset_score
from finance_agent.scoring.strategies import (
    default_scoring_strategy_seeds,
    validate_scoring_strategy_payload,
)
from tests.test_scoring_strategies import _Factor


def test_theme_momentum_strategy_seed_is_valid() -> None:
    seeds = default_scoring_strategy_seeds()
    theme = next(
        seed for seed in seeds if seed["strategy_id"] == "strategy:ashare:theme_momentum"
    )

    assert theme["market"] == "ashare"
    assert theme["group_weights"]["sector_strength"] > theme["group_weights"]["valuation"]
    assert theme["group_weights"]["leadership"] > theme["group_weights"]["fundamental"]
    validate_scoring_strategy_payload(theme)


def test_compute_asset_score_consumes_sector_and_leadership_groups() -> None:
    factor = replace(
        _Factor(),
        payload={
            "factor_groups": [
                {"group": "sector_strength", "score": 92, "status": "available"},
                {"group": "leadership", "score": 88, "status": "available"},
                {"group": "capital_flow", "score": 72, "status": "available"},
                {"group": "technical", "score": 70, "status": "available"},
                {"group": "event", "score": 66, "status": "available"},
                {"group": "risk", "score": 80, "status": "available"},
            ],
            "partial_groups": [],
        },
    )
    strategy = SimpleNamespace(
        strategy_id="strategy:ashare:theme_momentum",
        group_weights={
            "sector_strength": 0.28,
            "leadership": 0.24,
            "capital_flow": 0.18,
            "technical": 0.16,
            "event": 0.08,
            "risk": 0.06,
        },
        missing_penalty={"per_missing_group": 4, "per_partial_group": 1.5},
    )

    score = compute_asset_score(factor, strategy=strategy)

    assert score["group_scores"]["sector_strength"] == 92.0
    assert score["group_scores"]["leadership"] == 88.0
    assert score["total_score"] == pytest.approx(81.12)
    assert score["status"] == "available"
    assert score["weight_snapshot"]["strategy_id"] == "strategy:ashare:theme_momentum"
