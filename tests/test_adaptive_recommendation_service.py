"""自适应决策接入推荐持久化入口的测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from finance_agent.indicators.structural_methodology_adapters import (
    StructuralMethodologyAdapter,
    StructuralPriceBar,
)
from finance_agent.recommendations.decision_snapshot import DecisionSnapshot
from finance_agent.recommendations.lifecycle import RecommendationState
from finance_agent.recommendations.portfolio_construction import PortfolioRiskBudget
from finance_agent.recommendations.service import RecommendationService
from finance_agent.recommendations.structural_decision import StructuralDecisionEngine

NOW = datetime(2026, 9, 8, 7, 0, tzinfo=UTC)
STRATEGY_ID = "strategy:ashare:adaptive_v1"


def test_adaptive_rank_persists_lifecycle_action_and_snapshot_id() -> None:
    service, store, states = _service(previous=_state("setup_confirming"))

    result = service.rank_from_screening(
        screening_id="screen:adaptive",
        score_strategy_id=STRATEGY_ID,
        decision_snapshot=_snapshot(structure_confirmed=True),
        portfolio_budget=_budget(),
    )

    payload = store.assets[0]["payload"]
    assert result.status == "available"
    assert result.buy_ready_count == 1
    assert result.decision_snapshot_id == "decision:ashare:2026-09-08:test"
    assert payload["action"] == "buy_candidate"
    assert payload["recommendation_state"] == "buy_ready"
    assert payload["decision_snapshot_id"] == result.decision_snapshot_id
    assert store.assets[0]["total_score"] == Decimal("50.000000")
    assert payload["total_score"] == 50.0
    assert payload["decision_context"] is None
    assert payload["action_source"] == "adaptive_lifecycle_portfolio"
    assert states.setups[0].setup_id == "setup:ashare:600519"
    assert states.setups[0].invalidation_price == Decimal("9.50")
    assert states.transitions[0].to_state == "buy_ready"


def test_adaptive_rank_keeps_available_run_when_buy_count_is_zero() -> None:
    service, store, _states = _service(previous=None)

    result = service.rank_from_screening(
        screening_id="screen:adaptive",
        score_strategy_id=STRATEGY_ID,
        decision_snapshot=_snapshot(structure_confirmed=False),
        portfolio_budget=_budget(),
    )

    assert result.status == "available"
    assert result.recommendation_count == 1
    assert result.buy_ready_count == 0
    assert store.run["summary"] == "本次没有满足新增买入门槛的标的。"
    assert store.assets[0]["action"] == "watch"


def test_adaptive_rank_freezes_repository_facts_when_snapshot_is_not_supplied() -> None:
    service, store, _states = _service(previous=None)
    snapshots = _SnapshotStore()
    service.factors = _FactorStore()
    service.indicators = _IndicatorStore.with_structure()
    service.snapshots = snapshots
    service.market_data = _MarketStore()

    result = service.rank_from_screening(
        screening_id="screen:adaptive",
        score_strategy_id=STRATEGY_ID,
        market_regime={
            "regime": "trend_up",
            "risk_budget": {
                "total_exposure": 1.0,
                "per_position_risk": 0.01,
                "allow_new_buys": True,
            },
        },
    )

    assert result.status == "available"
    assert result.decision_snapshot_id is not None
    assert result.buy_ready_count == 0
    assert len(snapshots.inserted) == 1
    assert store.assets[0]["payload"]["decision_snapshot_id"] == result.decision_snapshot_id


def test_snapshot_builder_consumes_persisted_market_and_sector_context() -> None:
    service, _store, _states = _service(previous=None)
    snapshots = _SnapshotStore(
        latest={
            "market_regime": SimpleNamespace(
                data_snapshot_id="snapshot:market:1",
                as_of=NOW,
                quality_status="available",
                payload={
                    "regime": "trend_down",
                    "risk_budget": {
                        "total_exposure": 0.35,
                        "per_position_risk": 0.005,
                        "allow_new_buys": True,
                        "allow_sector_override": True,
                    },
                },
            ),
            "sector_opportunities": SimpleNamespace(
                data_snapshot_id="snapshot:sector:1",
                as_of=NOW,
                quality_status="available",
                payload={
                    "sector_opportunities": [
                        {"sector_id": "sector:liquor", "sector_regime": "diffusion"}
                    ]
                },
            ),
        }
    )
    service.factors = _FactorStore()
    service.indicators = _IndicatorStore.with_structure()
    service.snapshots = snapshots
    scores = service.scores.list_scores_for_screening(
        "screen:adaptive",
        strategy_id=STRATEGY_ID,
    )

    snapshot = service.build_adaptive_decision_snapshot(
        scores=scores,
        market="ashare",
        horizon="swing",
        market_regime=None,
    )

    assert snapshot.market_regime["regime"] == "trend_down"
    assert snapshot.sector_opportunities[0]["sector_id"] == "sector:liquor"
    assert snapshot.data_versions["market_snapshot_id"] == "snapshot:market:1"
    assert snapshot.data_versions["sector_snapshot_id"] == "snapshot:sector:1"


def test_partial_or_stale_context_keeps_research_run_but_blocks_buy() -> None:
    service, _store, _states = _service(previous=_state("setup_confirming"))
    service.factors = _FactorStore()
    service.indicators = _IndicatorStore.with_structure()
    service.snapshots = _SnapshotStore(
        latest={
            "market_regime": SimpleNamespace(
                data_snapshot_id="snapshot:market:partial",
                as_of=NOW,
                quality_status="partial",
                payload={"regime": "trend_up"},
            ),
            "sector_opportunities": SimpleNamespace(
                data_snapshot_id="snapshot:sector:stale",
                as_of=NOW - timedelta(days=1),
                quality_status="available",
                payload={"sector_opportunities": []},
            ),
        }
    )

    result = service.rank_from_screening(
        screening_id="screen:adaptive",
        score_strategy_id=STRATEGY_ID,
    )

    assert result.status == "available"
    assert result.recommendation_count == 1
    assert result.buy_ready_count == 0
    assert service.snapshots.inserted[-1].quality_status == "partial"


def test_adaptive_rank_uses_actual_positions_for_portfolio_capacity() -> None:
    service, store, _states = _service(previous=_state("setup_confirming"))
    service.portfolios = _PortfolioStore(position_count=10)

    result = service.rank_from_screening(
        screening_id="screen:adaptive",
        score_strategy_id=STRATEGY_ID,
        decision_snapshot=_snapshot(structure_confirmed=True),
    )

    assert result.buy_ready_count == 0
    assert store.assets[0]["action"] == "watch"
    assert "maximum_position_count_reached" in store.assets[0]["payload"][
        "lifecycle_reason_codes"
    ]


def test_adaptive_cross_section_is_not_truncated_by_legacy_output_limit() -> None:
    service, store, _states = _service(previous=None)
    service.scores = _TwoScoreStore()
    first = _snapshot(structure_confirmed=False)
    second_asset = {
        **first.assets[0],
        "asset_id": "ashare:000001",
        "symbol": "000001",
        "setup_id": "setup:ashare:000001",
        "sector_id": "sector:bank",
    }
    snapshot = replace(first, assets=(first.assets[0], second_asset))

    result = service.rank_from_screening(
        screening_id="screen:adaptive",
        score_strategy_id=STRATEGY_ID,
        decision_snapshot=snapshot,
        portfolio_budget=_budget(),
        limit=1,
    )

    assert result.recommendation_count == 2
    assert {item["asset_id"] for item in store.assets} == {
        "ashare:600519",
        "ashare:000001",
    }


def test_stale_structure_frame_cannot_confirm_new_buy() -> None:
    service, store, _states = _service(previous=_state("setup_confirming"))
    service.factors = _FactorStore()
    indicators = _IndicatorStore.with_structure()
    assert indicators.frame is not None
    indicators.frame.as_of = NOW - timedelta(days=1)
    service.indicators = indicators
    service.snapshots = _SnapshotStore()

    result = service.rank_from_screening(
        screening_id="screen:adaptive",
        score_strategy_id=STRATEGY_ID,
        market_regime={"regime": "trend_up"},
    )

    assert result.buy_ready_count == 0
    assert store.assets[0]["action"] == "watch"
    assert store.assets[0]["payload"]["data_quality"] == "unavailable"


def test_fresh_hourly_structure_cannot_mask_stale_daily_structure() -> None:
    service, _store, _states = _service(previous=None)
    service.factors = _FactorStore()
    service.indicators = _MixedFreshnessIndicatorStore()
    service.snapshots = _SnapshotStore()
    scores = service.scores.list_scores_for_screening(
        "screen:adaptive",
        strategy_id=STRATEGY_ID,
    )

    snapshot = service.build_adaptive_decision_snapshot(
        scores=scores,
        market="ashare",
        horizon="swing",
        market_regime={"regime": "trend_up"},
    )

    assert snapshot.quality_status == "partial"
    assert snapshot.assets[0]["data_quality"] == "unavailable"


def test_closed_position_moves_active_lifecycle_into_cooldown() -> None:
    service, store, _states = _service(previous=_state("active"))
    service.portfolios = _ClosedPortfolioStore(position_count=0)

    service.rank_from_screening(
        screening_id="screen:adaptive",
        score_strategy_id=STRATEGY_ID,
        decision_snapshot=_snapshot(structure_confirmed=True),
    )

    first_cooldown = _states.transitions[-1].cooldown_until
    next_day_snapshot = replace(
        _snapshot(structure_confirmed=True),
        decision_snapshot_id="decision:ashare:2026-09-09:test",
        as_of=NOW + timedelta(days=1),
    )
    service.rank_from_screening(
        screening_id="screen:adaptive",
        score_strategy_id=STRATEGY_ID,
        decision_snapshot=next_day_snapshot,
    )

    assert store.assets[0]["payload"]["recommendation_state"] == "cooldown"
    assert "sold_entered_cooldown" in store.assets[0]["payload"][
        "lifecycle_reason_codes"
    ]
    assert _states.transitions[-1].cooldown_until == first_cooldown


def test_partial_context_retains_previous_buy_ready_state_as_stale() -> None:
    previous_asset = dict(_snapshot(structure_confirmed=True).assets[0])
    service, store, _states = _service(
        previous=_state(
            "buy_ready",
            payload={"decision_asset": previous_asset},
        )
    )
    service.factors = _FactorStore()
    service.indicators = _IndicatorStore.with_structure()
    service.snapshots = _SnapshotStore(
        latest={
            "market_regime": SimpleNamespace(
                data_snapshot_id="snapshot:market:partial",
                as_of=NOW,
                quality_status="partial",
                payload={"regime": "trend_up"},
            ),
            "sector_opportunities": SimpleNamespace(
                data_snapshot_id="snapshot:sector:partial",
                as_of=NOW,
                quality_status="partial",
                payload={"sector_opportunities": []},
            ),
        }
    )

    result = service.rank_from_screening(
        screening_id="screen:adaptive",
        score_strategy_id=STRATEGY_ID,
    )

    payload = store.assets[0]["payload"]
    assert result.buy_ready_count == 0
    assert payload["recommendation_state"] == "buy_ready"
    assert payload["action"] == "watch"
    assert payload["data_quality"] == "stale"


def test_missing_point_in_time_trading_status_fails_closed() -> None:
    service, store, _states = _service(previous=None)
    service.assets = _AssetStoreWithoutStatuses()
    service.factors = _FactorStore()
    service.indicators = _MixedFreshnessIndicatorStore()
    service.snapshots = _SnapshotStore()

    service.rank_from_screening(
        screening_id="screen:adaptive",
        score_strategy_id=STRATEGY_ID,
        market_regime={"regime": "trend_up"},
    )

    tradability = store.assets[0]["payload"]["tradability"]
    assert tradability["tradable"] is False
    assert tradability["reasons"] == ["trading_status_missing"]


def test_owner_without_account_equity_cannot_receive_sized_buy_plan() -> None:
    service, store, _states = _service(previous=_state("setup_confirming"))
    service.portfolios = _NoPortfolioStore(position_count=0)

    result = service.rank_from_screening(
        screening_id="screen:adaptive",
        score_strategy_id=STRATEGY_ID,
        decision_snapshot=_snapshot(structure_confirmed=True),
    )

    assert result.buy_ready_count == 0
    assert store.assets[0]["action"] == "watch"
    assert "new_buys_disabled" in store.assets[0]["payload"][
        "lifecycle_reason_codes"
    ]


def test_real_adapter_snapshot_uses_point_in_time_close_for_structure_verdict() -> None:
    service, _store, _states = _service(previous=None)
    prices = [10.0, 15.0, 12.0, 18.0, 15.0, 21.0, 18.0, 24.0, 22.0, 24.5]
    bars = [
        StructuralPriceBar(
            timestamp=NOW - timedelta(hours=len(prices) - 1 - index),
            open=price - 0.1,
            high=price + 0.2,
            low=price - 0.2,
            close=price,
            volume=1000 + index * 10,
        )
        for index, price in enumerate(prices)
    ]
    adapter = StructuralMethodologyAdapter(swing_window=1)
    service.factors = _FactorStore()
    service.indicators = _RealIndicatorBatchStore(
        daily=adapter.compute_swings(
            asset_id="ashare:600519",
            symbol="600519",
            market="ashare",
            timeframe="1d",
            bars=bars,
        ),
        hourly=adapter.compute_smc(
            asset_id="ashare:600519",
            symbol="600519",
            market="ashare",
            timeframe="60m",
            bars=bars,
        ),
    )
    service.market_data = _MarketStore(price="24.5")
    service.snapshots = _SnapshotStore()
    scores = service.scores.list_scores_for_screening(
        "screen:adaptive",
        strategy_id=STRATEGY_ID,
    )

    snapshot = service.build_adaptive_decision_snapshot(
        scores=scores,
        market="ashare",
        horizon="swing",
        market_regime={"regime": "trend_up"},
    )

    asset = snapshot.assets[0]
    assert asset["current_price"] == "24.5"
    verdict = StructuralDecisionEngine().evaluate(
        frames=asset["structure_frames"],
        current_price=Decimal(asset["current_price"]),
    )
    assert verdict.status == "confirmed"


def test_daily_utc_midnight_bar_is_interpreted_as_same_day_market_close() -> None:
    service, _store, _states = _service(previous=None)
    service.factors = _FactorStore()
    service.indicators = _IndicatorStore.with_structure()
    service.snapshots = _SnapshotStore()
    service.market_data = _MarketStore(price="10", timestamp=NOW.replace(hour=0))

    result = service.rank_from_screening(
        screening_id="screen:adaptive",
        score_strategy_id=STRATEGY_ID,
        market_regime={"regime": "trend_up"},
    )

    assert result.recommendation_count == 1
    assert result.decision_snapshot_id is not None


def test_active_lifecycle_outside_current_screening_is_still_maintained() -> None:
    previous_asset = {
        **dict(_snapshot(structure_confirmed=True).assets[0]),
        "asset_id": "ashare:000001",
        "symbol": "000001",
        "setup_id": "setup:ashare:000001",
    }
    active = RecommendationState(
        state_id="state:ashare:000001",
        owner_id="default-owner",
        strategy_id=STRATEGY_ID,
        asset_id="ashare:000001",
        setup_id="setup:ashare:000001",
        current_state="active",
        previous_state="buy_ready",
        decision_snapshot_id="decision:previous",
        state_changed_at=NOW - timedelta(days=2),
        consecutive_valid_closes=2,
        active_days=2,
        cooldown_until=None,
        payload={"decision_asset": previous_asset},
    )
    service, store, _states = _service(previous=None)
    service.lifecycle_states = _ExpandedStateStore(active)
    service.factors = _FactorStore()
    service.indicators = _IndicatorStore.with_structure()
    service.snapshots = _SnapshotStore()

    result = service.rank_from_screening(
        screening_id="screen:adaptive",
        score_strategy_id=STRATEGY_ID,
        market_regime={"regime": "trend_up"},
        portfolio_budget=_budget(),
    )

    assert result.recommendation_count == 2
    by_asset = {item["asset_id"]: item["payload"] for item in store.assets}
    assert by_asset["ashare:000001"]["recommendation_state"] == "active"
    assert by_asset["ashare:000001"]["action"] == "hold"


def _service(
    *,
    previous: RecommendationState | None,
) -> tuple[RecommendationService, _RecommendationStore, _StateStore]:
    service = RecommendationService.__new__(RecommendationService)
    service.assets = _AssetStore()
    service.market_data = _MarketStore()
    service.screenings = _ScreeningStore()
    service.scores = _ScoreStore()
    service.signals = _SignalStore()
    service.risks = _RiskStore()
    service.indicators = _IndicatorStore()
    store = _RecommendationStore()
    states = _StateStore(previous)
    service.recommendations = store
    service.lifecycle_states = states
    return service, store, states


def _snapshot(*, structure_confirmed: bool) -> DecisionSnapshot:
    frames: tuple[dict[str, object], ...] = ()
    if structure_confirmed:
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
                "evidence_id": "structure:ashare:600519",
            },
        )
    return DecisionSnapshot(
        decision_snapshot_id="decision:ashare:2026-09-08:test",
        market="ashare",
        as_of=NOW,
        market_regime={
            "regime": "trend_up",
            "risk_budget": {"allow_new_buys": True},
        },
        sector_opportunities=(),
        assets=(
            {
                "asset_id": "ashare:600519",
                "symbol": "600519",
                "setup_id": "setup:ashare:600519",
                "sector_id": "sector:liquor",
                "data_quality": "available",
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
                "current_price": "10.00",
                "entry_threshold": 45.0,
                "retention_threshold": 40.0,
                "tradable": True,
            },
        ),
        data_versions={"test": "v1"},
        quality_status="available",
    )


def _state(state: str, *, payload: dict[str, Any] | None = None) -> RecommendationState:
    return RecommendationState(
        state_id="state:ashare:600519",
        owner_id="default-owner",
        strategy_id=STRATEGY_ID,
        asset_id="ashare:600519",
        setup_id="setup:ashare:600519",
        current_state=state,  # type: ignore[arg-type]
        previous_state="watch",
        decision_snapshot_id="decision:previous",
        state_changed_at=datetime(2026, 9, 7, 7, 0, tzinfo=UTC),
        consecutive_valid_closes=1,
        active_days=0,
        cooldown_until=None,
        payload=payload or {},
    )


def _budget() -> PortfolioRiskBudget:
    return PortfolioRiskBudget(
        equity=Decimal("100000"),
        total_exposure=1.0,
        per_position_risk=0.01,
    )


class _ScreeningStore:
    def get_screening_result(self, _screening_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            universe_id="universe:adaptive",
            market="ashare",
            passed_count=1,
        )


class _ScoreStore:
    def list_scores_for_screening(
        self,
        _screening_id: str,
        *,
        strategy_id: str | None = None,
    ) -> list[SimpleNamespace]:
        assert strategy_id == STRATEGY_ID
        return [
            SimpleNamespace(
                score_id="score:1",
                asset_id="ashare:600519",
                symbol="600519",
                market="ashare",
                horizon="swing",
                total_score=Decimal("80"),
                confidence=Decimal("0.85"),
                missing_penalty=Decimal("0"),
                factor_frame_id="factor:1",
                as_of=NOW,
                payload={"strategy_id": STRATEGY_ID},
            )
        ]


class _TwoScoreStore(_ScoreStore):
    def list_scores_for_screening(
        self,
        screening_id: str,
        *,
        strategy_id: str | None = None,
    ) -> list[SimpleNamespace]:
        first = super().list_scores_for_screening(
            screening_id,
            strategy_id=strategy_id,
        )[0]
        return [
            first,
            SimpleNamespace(
                **{
                    **first.__dict__,
                    "score_id": "score:2",
                    "asset_id": "ashare:000001",
                    "symbol": "000001",
                    "factor_frame_id": "factor:2",
                }
            ),
        ]


class _SignalStore:
    def get_latest_signal(self, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            signal_id="signal:1",
            direction="bullish",
            score=Decimal("80"),
            status="available",
        )


class _RiskStore:
    def list_recent_risks(self, **_kwargs: Any) -> list[Any]:
        return []


class _AssetStore:
    def get_asset_or_none(self, _asset_id: str) -> SimpleNamespace:
        raise AssertionError("自适应路径不应逐股查询资产名称")

    def find_by_ids(self, asset_ids: tuple[str, ...]) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                asset_id=asset_id,
                name="贵州茅台" if asset_id == "ashare:600519" else asset_id,
                symbol=asset_id.split(":")[-1],
            )
            for asset_id in asset_ids
        ]

    def list_latest_statuses(
        self,
        *,
        asset_ids: tuple[str, ...],
        as_of: datetime,
    ) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                asset_id=asset_id,
                as_of=as_of,
                tradable=True,
                trading_status="available",
                reason=None,
                payload={},
            )
            for asset_id in asset_ids
        ]


class _AssetStoreWithoutStatuses(_AssetStore):
    def list_latest_statuses(self, **_kwargs: Any) -> list[Any]:
        return []


class _IndicatorStore:
    def __init__(self, frame: SimpleNamespace | None = None) -> None:
        self.frame = frame

    @classmethod
    def with_structure(cls) -> _IndicatorStore:
        return cls(
            SimpleNamespace(
                horizon="smc_lite_v2",
                timeframe="60m",
                status="available",
                confidence=Decimal("0.85"),
                as_of=NOW,
                payload={
                    "status": "available",
                    "direction": "bullish",
                    "entry_setup": "breakout_confirmed",
                    "entry_zone": {"low": "9.90", "high": "10.10"},
                    "invalidation_price": "9.50",
                    "target_price": "11.00",
                    "evidence_id": "structure:ashare:600519",
                },
            )
        )

    def get_latest_indicator_frame(self, **kwargs: Any) -> SimpleNamespace | None:
        if kwargs.get("horizon") == "smc_lite_v2":
            return self.frame
        return None


class _MixedFreshnessIndicatorStore:
    def list_latest_indicator_frames(self, **_kwargs: Any) -> list[Any]:
        return [
            SimpleNamespace(
                asset_id="ashare:600519",
                horizon="structural_swings_v2",
                timeframe="1d",
                status="available",
                as_of=NOW - timedelta(days=1),
                payload={
                    "schema_version": "structural_swings_v2",
                    "status": "available",
                    "direction": "bullish",
                    "invalidation_price": "9.5",
                    "target_price": "11",
                    "evidence_id": "daily:stale",
                },
            ),
            SimpleNamespace(
                asset_id="ashare:600519",
                horizon="smc_lite_v2",
                timeframe="60m",
                status="available",
                as_of=NOW,
                payload={
                    "schema_version": "smc_lite_v2",
                    "status": "available",
                    "direction": "bullish",
                    "entry_setup": "breakout_confirmed",
                    "entry_zone": {"low": "9.9", "high": "10.1"},
                    "evidence_id": "hourly:fresh",
                },
            ),
        ]


class _FactorStore:
    def get_latest_factor_frame(self, **_kwargs: Any) -> SimpleNamespace:
        groups = [
            {"group": "technical", "status": "available", "score": 80},
            {"group": "fundamental", "status": "available", "score": 70},
            {"group": "valuation", "status": "available", "score": 72},
            {"group": "capital_flow", "status": "available", "score": 75},
            {
                "group": "sector_strength",
                "status": "available",
                "score": 82,
                "factors": {"sector_id": "sector:liquor", "sector_regime": "diffusion"},
            },
            {"group": "leadership", "status": "available", "score": 85},
            {"group": "liquidity", "status": "available", "score": 80},
            {"group": "risk", "status": "available", "score": 90},
        ]
        return SimpleNamespace(
            asset_id="ashare:600519",
            factor_frame_id="factor:1",
            status="available",
            as_of=NOW,
            missing_groups=[],
            source_ids=["source:1"],
            payload={"factor_groups": groups, "partial_groups": []},
        )


class _MarketStore:
    def __init__(
        self,
        *,
        price: str = "10",
        timestamp: datetime | None = None,
    ) -> None:
        self.price = Decimal(price)
        self.timestamp = timestamp

    def list_latest_closed_bars(
        self,
        *,
        asset_ids: tuple[str, ...],
        timeframe: str,
        as_of: datetime,
    ) -> list[Any]:
        assert timeframe == "1d"
        return [
            SimpleNamespace(
                asset_id=asset_id,
                timestamp=self.timestamp or as_of,
                close=self.price,
            )
            for asset_id in asset_ids
        ]


class _RealIndicatorBatchStore:
    def __init__(self, *, daily: dict[str, Any], hourly: dict[str, Any]) -> None:
        self.daily = daily
        self.hourly = hourly

    def list_latest_indicator_frames(self, **_kwargs: Any) -> list[Any]:
        return [
            SimpleNamespace(
                asset_id="ashare:600519",
                horizon=self.daily["schema_version"],
                timeframe="1d",
                status=self.daily["status"],
                as_of=NOW,
                payload=self.daily,
            ),
            SimpleNamespace(
                asset_id="ashare:600519",
                horizon=self.hourly["schema_version"],
                timeframe="60m",
                status=self.hourly["status"],
                as_of=NOW,
                payload=self.hourly,
            ),
        ]


class _SnapshotStore:
    def __init__(self, latest: dict[str, Any] | None = None) -> None:
        self.inserted: list[Any] = []
        self.latest = latest or {}

    def get_snapshot(self, _snapshot_id: str) -> None:
        return None

    def insert_snapshot(self, snapshot: Any) -> Any:
        self.inserted.append(snapshot)
        return snapshot

    def get_latest(self, *, snapshot_type: str, market: str) -> Any | None:
        assert market == "ashare"
        return self.latest.get(snapshot_type)


class _PortfolioStore:
    def __init__(self, *, position_count: int) -> None:
        self.position_count = position_count

    def list_portfolios(self, *, owner_id: str, status: str) -> list[Any]:
        assert owner_id == "default-owner"
        assert status == "active"
        return [SimpleNamespace(total_equity=Decimal("100000"), payload={})]

    def list_active_positions_by_owner(self, *, owner_id: str, market: str) -> list[Any]:
        assert owner_id == "default-owner"
        assert market == "ashare"
        return [
            SimpleNamespace(
                position_id=f"position:{index}",
                asset_id=f"ashare:{index:06d}",
                quantity=Decimal("100"),
                last_price=Decimal("10"),
                avg_cost=Decimal("9"),
                payload={
                    "sector_id": f"sector:{index}",
                    "sellable_quantity": "100",
                    "expected_net_return": 0.02,
                },
            )
            for index in range(self.position_count)
        ]


class _ClosedPortfolioStore(_PortfolioStore):
    def list_positions_by_owner(
        self,
        *,
        owner_id: str,
        market: str,
        status: str | None,
    ) -> list[Any]:
        assert owner_id == "default-owner"
        assert market == "ashare"
        assert status is None
        return [
            SimpleNamespace(
                asset_id="ashare:600519",
                status="closed",
                as_of=NOW - timedelta(days=1),
            )
        ]


class _NoPortfolioStore(_PortfolioStore):
    def list_portfolios(self, *, owner_id: str, status: str) -> list[Any]:
        return []


class _StateStore:
    def __init__(self, previous: RecommendationState | None) -> None:
        self.previous = previous
        self.setups: list[Any] = []
        self.transitions: list[Any] = []

    def get_state(self, **_kwargs: Any) -> RecommendationState | None:
        raise AssertionError("自适应路径不应逐股查询生命周期状态")

    def list_states(self, **_kwargs: Any) -> list[RecommendationState]:
        return [self.previous] if self.previous is not None else []

    def save_transition(self, transition: Any, **_kwargs: Any) -> Any:
        self.transitions.append(transition)
        self.previous = transition.state
        return transition.state

    def save_setup(self, setup: Any) -> Any:
        self.setups.append(setup)
        return setup


class _ExpandedStateStore(_StateStore):
    def __init__(self, extra: RecommendationState) -> None:
        super().__init__(None)
        self.extra = extra

    def list_open_states(self, **_kwargs: Any) -> list[RecommendationState]:
        return [self.extra]

    def list_states(self, **_kwargs: Any) -> list[RecommendationState]:
        return [self.extra]


class _RecommendationStore:
    def __init__(self) -> None:
        self.assets: list[dict[str, Any]] = []
        self.run: dict[str, Any] = {}

    def upsert_run_universe(self, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(**kwargs)

    def upsert_asset_recommendation(self, **kwargs: Any) -> SimpleNamespace:
        self.assets.append(kwargs)
        return SimpleNamespace(recommendation_id=kwargs["recommendation_id"])

    def upsert_run(self, **kwargs: Any) -> SimpleNamespace:
        self.run = kwargs
        return SimpleNamespace(**kwargs)
