from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from finance_agent.scoring.service import ScoringService, compute_asset_score
from finance_agent.scoring.strategies import (
    default_scoring_strategy_seeds,
    validate_scoring_strategy_payload,
)


@dataclass(frozen=True)
class _Factor:
    asset_id: str = "ashare:000001"
    symbol: str = "000001"
    market: str = "ashare"
    horizon: str = "swing"
    factor_frame_id: str = "factor:ashare:000001:swing"
    status: str = "available"
    missing_groups: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ("indicator:000001",)
    as_of: datetime = datetime(2026, 6, 12, 9, 30, tzinfo=UTC)
    payload: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.payload is None:
            object.__setattr__(
                self,
                "payload",
                {
                    "factor_groups": [
                        {"group": "technical", "score": 90, "status": "available"},
                        {"group": "fundamental", "score": 50, "status": "available"},
                        {"group": "valuation", "score": 30, "status": "available"},
                        {"group": "capital_flow", "score": 70, "status": "available"},
                        {"group": "liquidity", "score": 60, "status": "available"},
                        {"group": "event", "score": 40, "status": "available"},
                        {"group": "event_decay", "score": 80, "status": "available"},
                        {"group": "risk", "score": 90, "status": "available"},
                    ],
                    "partial_groups": [],
                },
            )


class _Screenings:
    def list_items(self, *, screening_id: str, passed_only: bool = False) -> list[SimpleNamespace]:
        assert screening_id == "screen:ashare"
        assert passed_only is True
        return [
            SimpleNamespace(
                universe_id="universe:merged:ashare:recommendation",
                asset_id="ashare:000001",
                symbol="000001",
                market="ashare",
            )
        ]


class _Factors:
    def __init__(self, factor: _Factor) -> None:
        self.factor = factor

    def get_latest_factor_frame(self, *, asset_id: str, horizon: str) -> _Factor:
        assert asset_id == "ashare:000001"
        assert horizon == "swing"
        return self.factor


class _Scores:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def upsert_asset_score(self, **kwargs: Any) -> SimpleNamespace:
        self.records.append(kwargs)
        return SimpleNamespace(score_id=kwargs["score_id"])


class _Strategies:
    def __init__(self, strategy: SimpleNamespace | None) -> None:
        self.strategy = strategy
        self.calls: list[str] = []

    def get_active_strategy(self, strategy_id: str) -> SimpleNamespace | None:
        self.calls.append(strategy_id)
        return self.strategy


def _strategy() -> SimpleNamespace:
    return SimpleNamespace(
        strategy_id="strategy:ashare:short_swing",
        market="ashare",
        name="A 股短线波段",
        description="提高技术和资金流权重。",
        group_weights={
            "technical": 0.8,
            "fundamental": 0.2,
        },
        missing_penalty={
            "per_missing_group": 2,
            "per_partial_group": 1,
        },
        status="active",
    )


def test_default_scoring_strategy_seeds_are_valid() -> None:
    """默认策略应覆盖 A 股和数字货币，并通过权重校验。"""

    seeds = default_scoring_strategy_seeds()
    strategy_ids = {seed["strategy_id"] for seed in seeds}

    assert {
        "strategy:ashare:balanced_growth",
        "strategy:ashare:short_swing",
        "strategy:ashare:defensive",
        "strategy:crypto:crypto_swing",
    }.issubset(strategy_ids)
    for seed in seeds:
        validate_scoring_strategy_payload(seed)


def test_validate_scoring_strategy_rejects_invalid_weight_sum() -> None:
    """策略权重和必须接近 1，避免评分不可解释。"""

    payload = {
        "strategy_id": "strategy:ashare:broken",
        "market": "ashare",
        "name": "错误策略",
        "description": "权重和错误。",
        "group_weights": {"technical": 0.8, "fundamental": 0.8},
        "missing_penalty": {"per_missing_group": 4, "per_partial_group": 1.5},
        "status": "draft",
    }

    with pytest.raises(ValueError, match="权重和"):
        validate_scoring_strategy_payload(payload)


def test_compute_asset_score_accepts_strategy_weights() -> None:
    """透明评分应能使用外部策略权重，同时保持公式确定性。"""

    score = compute_asset_score(_Factor(), strategy=_strategy())

    assert score["total_score"] == 82.0
    assert score["confidence"] == 1.0
    assert score["group_scores"] == {"technical": 90.0, "fundamental": 50.0}
    assert score["weight_snapshot"] == {
        "strategy_id": "strategy:ashare:short_swing",
        "group_weights": {"technical": 0.8, "fundamental": 0.2},
        "missing_penalty": {"per_missing_group": 2.0, "per_partial_group": 1.0},
    }


def test_scoring_service_records_strategy_snapshot_in_score_payload() -> None:
    """评分落库 payload 应保存 strategy_id 和权重快照，便于推荐结果复现。"""

    scores = _Scores()
    strategies = _Strategies(_strategy())
    service = ScoringService.__new__(ScoringService)
    service.screenings = _Screenings()
    service.factors = _Factors(_Factor())
    service.scores = scores
    service.strategies = strategies

    result = service.score_screening(
        screening_id="screen:ashare",
        horizon="swing",
        strategy_id="strategy:ashare:short_swing",
    )

    assert result.status == "available"
    assert strategies.calls == ["strategy:ashare:short_swing"]
    assert scores.records[0]["total_score"] == Decimal("82.0")
    assert scores.records[0]["payload"]["strategy_id"] == "strategy:ashare:short_swing"
    assert scores.records[0]["payload"]["weight_snapshot"] == {
        "strategy_id": "strategy:ashare:short_swing",
        "group_weights": {"technical": 0.8, "fundamental": 0.2},
        "missing_penalty": {"per_missing_group": 2.0, "per_partial_group": 1.0},
    }
