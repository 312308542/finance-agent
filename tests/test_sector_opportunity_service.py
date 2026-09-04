"""多日热门板块生命周期与弱市覆盖测试。"""

from __future__ import annotations

import pytest

from finance_agent.application.sector_opportunity_service import (
    SectorOpportunityHistory,
    SectorOpportunityService,
)


def _history(**overrides: object) -> SectorOpportunityHistory:
    values: dict[str, object] = {
        "sector_id": "concept:robot",
        "excess_returns": {1: 0.015, 3: 0.04, 5: 0.07, 10: 0.09, 20: 0.12},
        "breadth": 0.72,
        "ma20_ratio": 0.68,
        "flow_streak": 4,
        "leader_asset_ids": ("ashare:600001",),
        "challenger_asset_ids": ("ashare:600002", "ashare:600003"),
        "breadth_change": 0.08,
        "valid_cross_sections": 2,
        "previous_regime": None,
        "evidence_ids": ("decision:1",),
    }
    values.update(overrides)
    return SectorOpportunityHistory(**values)  # type: ignore[arg-type]


def test_weak_market_sector_override_requires_healthy_diffusion() -> None:
    result = SectorOpportunityService().evaluate(
        _history(),
        market_regime="trend_down",
    )

    assert result.regime == "diffusion"
    assert result.override_eligible is True
    assert result.maximum_sector_positions == 3
    assert result.exposure_multiplier == pytest.approx(0.5)


def test_single_leader_without_diffusion_cannot_override_weak_market() -> None:
    result = SectorOpportunityService().evaluate(
        _history(challenger_asset_ids=(), breadth=0.30),
        market_regime="trend_down",
    )

    assert result.override_eligible is False
    assert "healthy_diffusion_missing" in result.reason_codes


def test_acceleration_with_contracting_breadth_is_marked_as_chase_risk() -> None:
    result = SectorOpportunityService().evaluate(
        _history(excess_returns={1: 0.04, 3: 0.09, 5: 0.10}, breadth_change=-0.12),
        market_regime="trend_up",
    )

    assert result.regime == "acceleration"
    assert result.chase_risk is True


def test_new_sector_needs_two_valid_cross_sections_before_diffusion() -> None:
    result = SectorOpportunityService().evaluate(
        _history(valid_cross_sections=1),
        market_regime="trend_down",
    )

    assert result.regime == "ignition"
    assert result.override_eligible is False
    assert "entry_confirmation_missing" in result.reason_codes


def test_existing_diffusion_uses_lower_retention_threshold() -> None:
    retained = SectorOpportunityService().evaluate(
        _history(
            breadth=0.52,
            ma20_ratio=0.53,
            flow_streak=2,
            excess_returns={3: 0.015, 5: 0.025},
            previous_regime="diffusion",
        ),
        market_regime="trend_down",
    )
    fresh = SectorOpportunityService().evaluate(
        _history(
            breadth=0.52,
            ma20_ratio=0.53,
            flow_streak=2,
            excess_returns={3: 0.015, 5: 0.025},
            previous_regime=None,
        ),
        market_regime="trend_down",
    )

    assert retained.regime == "diffusion"
    assert fresh.regime == "cooling"


def test_risk_off_never_allows_sector_override() -> None:
    result = SectorOpportunityService().evaluate(_history(), market_regime="risk_off")

    assert result.override_eligible is False
    assert result.exposure_multiplier == 0
    assert "market_risk_off" in result.reason_codes
