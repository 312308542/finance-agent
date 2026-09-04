"""组合风险分配和换股缓冲测试。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from finance_agent.recommendations.portfolio_construction import (
    PortfolioCandidate,
    PortfolioConstructionEngine,
    PortfolioPosition,
    PortfolioRiskBudget,
    should_replace,
)


def test_position_size_uses_invalidation_distance_and_account_risk() -> None:
    plan = PortfolioConstructionEngine().allocate(
        candidates=(_candidate(price="10", invalidation="9.5", sector="power"),),
        positions=(),
        budget=_budget(equity="100000", per_position_risk=0.01),
    )

    assert plan.orders[0].maximum_quantity == 2_000
    assert plan.orders[0].risk_amount == Decimal("1000.00")


def test_new_candidate_does_not_replace_position_without_cost_buffer() -> None:
    decision = should_replace(
        held_expected_return=0.025,
        candidate_expected_return=0.030,
        round_trip_cost=0.003,
        uncertainty_buffer=0.004,
    )

    assert decision.allowed is False
    assert decision.required_improvement == pytest.approx(0.007)


def test_sector_exposure_never_exceeds_thirty_percent() -> None:
    plan = PortfolioConstructionEngine().allocate(
        candidates=(
            _candidate(asset_id="ashare:000001", price="10", invalidation="9", sector="bank"),
            _candidate(asset_id="ashare:600000", price="10", invalidation="9", sector="bank"),
        ),
        positions=(),
        budget=_budget(equity="100000", per_position_risk=0.02),
    )

    bank_notional = sum(order.notional for order in plan.orders if order.sector_id == "bank")
    assert bank_notional <= Decimal("30000")
    assert any("sector_concentration_capped" in order.reason_codes for order in plan.orders)


def test_weak_market_sector_override_halves_total_exposure() -> None:
    plan = PortfolioConstructionEngine().allocate(
        candidates=tuple(
            _candidate(
                asset_id=f"ashare:{index:06d}",
                price="10",
                invalidation="9",
                sector=f"sector-{index}",
            )
            for index in range(10)
        ),
        positions=(),
        budget=_budget(
            equity="100000",
            per_position_risk=0.02,
            weak_market_sector_override=True,
        ),
    )

    assert plan.target_exposure == pytest.approx(0.5)
    assert sum(order.notional for order in plan.orders) <= Decimal("50000")


def test_allocation_limits_target_to_ten_positions_without_fabricating_minimum() -> None:
    many = PortfolioConstructionEngine().allocate(
        candidates=tuple(
            _candidate(
                asset_id=f"ashare:{index:06d}",
                price="5",
                invalidation="4.8",
                sector=f"sector-{index}",
            )
            for index in range(12)
        ),
        positions=(),
        budget=_budget(equity="1000000", per_position_risk=0.001),
    )
    sparse = PortfolioConstructionEngine().allocate(
        candidates=(_candidate(),),
        positions=(),
        budget=_budget(),
    )

    assert len(many.orders) == 10
    assert len(sparse.orders) == 1
    assert "target_position_count_below_minimum" in sparse.reason_codes


def test_turnover_limit_blocks_new_buys_but_keeps_sellable_exit() -> None:
    plan = PortfolioConstructionEngine().allocate(
        candidates=(_candidate(asset_id="ashare:600519"),),
        positions=(
            _position(
                asset_id="ashare:000001",
                quantity="1000",
                sellable_quantity="1000",
                required_action="exit",
            ),
        ),
        budget=_budget(weekly_turnover_ratio=0.36),
    )

    assert [(order.asset_id, order.side) for order in plan.orders] == [
        ("ashare:000001", "sell")
    ]
    assert any(
        item.asset_id == "ashare:600519" and item.reason_codes == ("turnover_limit_reached",)
        for item in plan.blocked_candidates
    )


def test_t1_or_suspended_position_does_not_emit_invalid_sell_order() -> None:
    positions = (
        _position(
            asset_id="ashare:000001",
            sellable_quantity="0",
            required_action="exit",
        ),
        _position(
            asset_id="ashare:000002",
            sellable_quantity="1000",
            required_action="exit",
            tradable=False,
            tradability_reasons=("suspended",),
        ),
    )

    plan = PortfolioConstructionEngine().allocate(
        candidates=(),
        positions=positions,
        budget=_budget(),
    )

    assert plan.orders == ()
    assert {item.reason_codes for item in plan.blocked_candidates} == {
        ("t1_not_sellable",),
        ("suspended",),
    }


def _candidate(
    *,
    asset_id: str = "ashare:600519",
    price: str = "10",
    invalidation: str = "9.5",
    sector: str = "liquor",
    tradable: bool = True,
    tradability_reasons: tuple[str, ...] = (),
) -> PortfolioCandidate:
    return PortfolioCandidate(
        asset_id=asset_id,
        setup_id=f"setup:{asset_id}",
        sector_id=sector,
        price=Decimal(price),
        invalidation_price=Decimal(invalidation),
        expected_net_return=0.08,
        downside_risk=0.03,
        confidence=0.85,
        tradable=tradable,
        tradability_reasons=tradability_reasons,
    )


def _position(
    *,
    asset_id: str,
    quantity: str = "1000",
    sellable_quantity: str = "1000",
    required_action: str = "hold",
    tradable: bool = True,
    tradability_reasons: tuple[str, ...] = (),
) -> PortfolioPosition:
    return PortfolioPosition(
        position_id=f"position:{asset_id}",
        asset_id=asset_id,
        sector_id="bank",
        quantity=Decimal(quantity),
        sellable_quantity=Decimal(sellable_quantity),
        price=Decimal("10"),
        expected_net_return=0.02,
        required_action=required_action,  # type: ignore[arg-type]
        tradable=tradable,
        tradability_reasons=tradability_reasons,
    )


def _budget(
    *,
    equity: str = "100000",
    total_exposure: float = 1.0,
    per_position_risk: float = 0.01,
    weekly_turnover_ratio: float = 0.0,
    weak_market_sector_override: bool = False,
) -> PortfolioRiskBudget:
    return PortfolioRiskBudget(
        equity=Decimal(equity),
        total_exposure=total_exposure,
        per_position_risk=per_position_risk,
        weekly_turnover_ratio=weekly_turnover_ratio,
        weak_market_sector_override=weak_market_sector_override,
    )
