"""统一快照到推荐生命周期和组合计划的编排测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from finance_agent.recommendations.adaptive_decision import (
    AdaptiveRecommendationDecisionEngine,
)
from finance_agent.recommendations.decision_snapshot import DecisionSnapshot
from finance_agent.recommendations.lifecycle import RecommendationState
from finance_agent.recommendations.portfolio_construction import (
    PortfolioPosition,
    PortfolioRiskBudget,
)

NOW = datetime(2026, 9, 8, 7, 0, tzinfo=UTC)


def test_blocked_structure_and_partial_data_publish_zero_buy_decision() -> None:
    snapshot = _snapshot(
        assets=(
            _asset("ashare:000001", data_quality="partial", structure_frames=()),
            _asset("ashare:600519", structure_frames=()),
        )
    )

    result = AdaptiveRecommendationDecisionEngine().decide(
        snapshot,
        previous_states={},
        positions=(),
        budget=_budget(),
    )

    assert result.status == "available"
    assert result.buy_ready_count == 0
    assert {item.action for item in result.decisions} == {"watch"}
    assert {item.decision_snapshot_id for item in result.decisions} == {
        snapshot.decision_snapshot_id
    }


def test_second_valid_close_becomes_buy_candidate_without_rank_quota() -> None:
    snapshot = _snapshot(assets=(_asset("ashare:600519"),))
    previous = _state("setup_confirming", valid_closes=1)

    result = AdaptiveRecommendationDecisionEngine().decide(
        snapshot,
        previous_states={"ashare:600519": previous},
        positions=(),
        budget=_budget(),
    )

    decision = result.decisions[0]
    assert decision.transition.to_state == "buy_ready"
    assert decision.action == "buy_candidate"
    assert result.buy_ready_count == 1
    assert decision.reason_codes == ("two_valid_closes_confirmed",)


def test_active_and_exit_pending_states_map_to_hold_and_exit() -> None:
    snapshot = _snapshot(
        assets=(
            _asset("ashare:000001"),
            _asset("ashare:600519", structure_invalidated=True),
        )
    )

    result = AdaptiveRecommendationDecisionEngine().decide(
        snapshot,
        previous_states={
            "ashare:000001": _state("active", asset_id="ashare:000001", active_days=4),
            "ashare:600519": _state("active", asset_id="ashare:600519", active_days=4),
        },
        positions=(),
        budget=_budget(),
    )

    by_asset = {item.asset_id: item for item in result.decisions}
    assert by_asset["ashare:000001"].action == "hold"
    assert by_asset["ashare:600519"].action == "exit"
    assert result.active_count == 1
    assert result.exit_pending_count == 1


def test_trend_down_requires_qualified_sector_override_for_new_buy() -> None:
    snapshot = _snapshot(assets=(_asset("ashare:600519"),))
    snapshot = DecisionSnapshot(
        decision_snapshot_id=snapshot.decision_snapshot_id,
        market=snapshot.market,
        as_of=snapshot.as_of,
        market_regime={
            "regime": "trend_down",
            "risk_budget": {"allow_new_buys": True, "allow_sector_override": True},
        },
        sector_opportunities=({
            "sector_id": "sector:other",
            "sector_regime": "cooling",
            "override_eligible": False,
            "chase_risk": False,
            "leader_asset_ids": (),
            "challenger_asset_ids": (),
        },),
        assets=snapshot.assets,
        data_versions=snapshot.data_versions,
        quality_status=snapshot.quality_status,
    )

    result = AdaptiveRecommendationDecisionEngine().decide(
        snapshot,
        previous_states={},
        positions=(),
        budget=_budget(),
    )

    assert result.buy_ready_count == 0
    assert result.decisions[0].action == "watch"
    assert "sector_override_not_eligible" in result.decisions[0].reason_codes


def test_trend_down_allows_confirmed_leader_in_healthy_diffusion_sector() -> None:
    snapshot = _snapshot(assets=(_asset("ashare:600519"),))
    snapshot = DecisionSnapshot(
        decision_snapshot_id=snapshot.decision_snapshot_id,
        market=snapshot.market,
        as_of=snapshot.as_of,
        market_regime={
            "regime": "trend_down",
            "risk_budget": {"allow_new_buys": True, "allow_sector_override": True},
        },
        sector_opportunities=({
            "sector_id": "sector:ashare:600519",
            "sector_regime": "diffusion",
            "override_eligible": True,
            "chase_risk": False,
            "leader_asset_ids": ("ashare:600519",),
            "challenger_asset_ids": ("ashare:000001",),
        },),
        assets=snapshot.assets,
        data_versions=snapshot.data_versions,
        quality_status=snapshot.quality_status,
    )

    result = AdaptiveRecommendationDecisionEngine().decide(
        snapshot,
        previous_states={"ashare:600519": _state("setup_confirming")},
        positions=(),
        budget=_budget(),
    )

    assert result.buy_ready_count == 1
    assert result.decisions[0].action == "buy_candidate"


def test_daily_evidence_id_change_keeps_same_setup_for_two_close_confirmation() -> None:
    engine = AdaptiveRecommendationDecisionEngine()
    first_asset = _asset("ashare:600519")
    first_asset.pop("setup_id")
    first = _snapshot(assets=(first_asset,))
    first_result = engine.decide(
        first,
        previous_states={},
        positions=(),
        budget=_budget(),
    )
    second_asset = dict(first_asset)
    second_frames = [dict(item) for item in first_asset["structure_frames"]]  # type: ignore[arg-type]
    second_frames[0]["evidence_id"] = "structure:next-day-frame"
    second_asset["structure_frames"] = tuple(second_frames)
    second = DecisionSnapshot(
        decision_snapshot_id="decision:ashare:2026-09-09:test",
        market="ashare",
        as_of=NOW + timedelta(days=1),
        market_regime=first.market_regime,
        sector_opportunities=first.sector_opportunities,
        assets=(second_asset,),
        data_versions={"test": "v2"},
        quality_status="available",
    )

    second_result = engine.decide(
        second,
        previous_states={"ashare:600519": first_result.decisions[0].transition.state},
        positions=(),
        budget=_budget(),
    )

    assert first_result.decisions[0].transition.to_state == "setup_confirming"
    assert (
        second_result.decisions[0].transition.setup_id
        == first_result.decisions[0].transition.setup_id
    )
    assert second_result.decisions[0].transition.to_state == "buy_ready"


def test_registered_position_promotes_buy_ready_lifecycle_to_active() -> None:
    snapshot = _snapshot(assets=(_asset("ashare:600519"),))
    position = _position(asset_id="ashare:600519", sellable="100")

    result = AdaptiveRecommendationDecisionEngine().decide(
        snapshot,
        previous_states={"ashare:600519": _state("buy_ready")},
        positions=(position,),
        budget=_budget(),
    )

    decision = result.decisions[0]
    assert decision.transition.to_state == "active"
    assert decision.action == "hold"
    assert decision.reason_codes == ("position_execution_registered",)


def test_t1_blocks_exit_execution_but_keeps_exit_intent() -> None:
    snapshot = _snapshot(
        assets=(_asset("ashare:600519", structure_invalidated=True),)
    )
    position = _position(asset_id="ashare:600519", sellable="0")

    result = AdaptiveRecommendationDecisionEngine().decide(
        snapshot,
        previous_states={"ashare:600519": _state("active", active_days=4)},
        positions=(position,),
        budget=_budget(),
    )

    decision = result.decisions[0]
    assert decision.transition.to_state == "exit_pending"
    assert decision.action == "watch"
    assert decision.intended_action == "exit"
    assert decision.execution_status == "blocked"
    assert "t1_not_sellable" in decision.reason_codes


def _snapshot(*, assets: tuple[dict[str, object], ...]) -> DecisionSnapshot:
    return DecisionSnapshot(
        decision_snapshot_id="decision:ashare:2026-09-08:test",
        market="ashare",
        as_of=NOW,
        market_regime={
            "regime": "trend_up",
            "risk_budget": {"allow_new_buys": True},
        },
        sector_opportunities=(),
        assets=assets,
        data_versions={"test": "v1"},
        quality_status="available",
    )


def _asset(
    asset_id: str,
    *,
    data_quality: str = "available",
    structure_frames: tuple[dict[str, object], ...] | None = None,
    structure_invalidated: bool = False,
) -> dict[str, object]:
    frames = structure_frames
    if frames is None:
        frames = (
            {
                "horizon": "smc_lite_v2",
                "timeframe": "60m",
                "status": "available",
                "direction": "bullish",
                "entry_setup": "breakout_confirmed",
                "entry_zone": {"low": "9.90", "high": "10.10"},
                "invalidation_price": "9.50",
                "target_price": "11.00",
                "evidence_id": f"structure:{asset_id}",
            },
        )
    return {
        "asset_id": asset_id,
        "symbol": asset_id.split(":")[-1],
        "setup_id": f"setup:{asset_id}",
        "sector_id": f"sector:{asset_id}",
        "data_quality": data_quality,
        "group_scores": {
            "trend": 80,
            "structure": 85,
            "sector_leadership": 80,
            "capital_flow": 75,
            "fundamental_valuation": 70,
            "tradability_return_risk": 80,
        },
        "factor_as_of": {},
        "missing_groups": (),
        "partial_groups": (),
        "expected_return_hint": 0.08,
        "downside_risk": 0.02,
        "structure_frames": frames,
        "structure_invalidated": structure_invalidated,
        "current_price": "10.00",
        "entry_threshold": 45.0,
        "retention_threshold": 40.0,
        "tradable": True,
        "tradability_reasons": (),
    }


def _state(
    state: str,
    *,
    asset_id: str = "ashare:600519",
    valid_closes: int = 1,
    active_days: int = 0,
) -> RecommendationState:
    return RecommendationState(
        state_id=f"state:{asset_id}",
        owner_id="default-owner",
        strategy_id="strategy:ashare:adaptive_v1",
        asset_id=asset_id,
        setup_id=f"setup:{asset_id}",
        current_state=state,  # type: ignore[arg-type]
        previous_state="watch",
        decision_snapshot_id="decision:previous",
        state_changed_at=datetime(2026, 9, 7, 7, 0, tzinfo=UTC),
        consecutive_valid_closes=valid_closes,
        active_days=active_days,
        cooldown_until=None,
        payload={},
    )


def _budget() -> PortfolioRiskBudget:
    return PortfolioRiskBudget(
        equity=Decimal("100000"),
        total_exposure=1.0,
        per_position_risk=0.01,
    )


def _position(*, asset_id: str, sellable: str) -> PortfolioPosition:
    return PortfolioPosition(
        position_id=f"position:{asset_id}",
        asset_id=asset_id,
        sector_id=f"sector:{asset_id}",
        quantity=Decimal("100"),
        sellable_quantity=Decimal(sellable),
        price=Decimal("10"),
        expected_net_return=0.02,
    )
