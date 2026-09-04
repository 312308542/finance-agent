"""收盘市场状态和板块机会快照刷新测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from finance_agent.application.closing_decision_context_service import (
    ClosingDecisionContextService,
)

NOW = datetime(2026, 9, 8, 7, 0, tzinfo=UTC)


def test_market_refresh_builds_trend_up_snapshot_from_point_in_time_bars() -> None:
    snapshots = _Snapshots()
    service = ClosingDecisionContextService(
        universe_repository=_Universes(["ashare:000001", "ashare:600519"]),
        market_data_repository=_MarketData(_rising_bars()),
        snapshot_repository=snapshots,
        theme_contexts=_Themes({}),
        minimum_market_assets=2,
    )

    first = service.refresh(
        context_type="market",
        market="ashare",
        universe_id="universe:all",
        lookback_bars=61,
    )
    replayed = service.refresh(
        context_type="market",
        market="ashare",
        universe_id="universe:all",
        lookback_bars=61,
    )

    assert first["status"] == "available"
    assert first["regime"] == "trend_up"
    assert first["sample_count"] == 2
    assert replayed["data_snapshot_id"] == first["data_snapshot_id"]
    assert snapshots.rows[-1].payload["risk_budget"]["allow_new_buys"] is True
    assert snapshots.rows[-1].as_of.hour == 7


def test_market_refresh_fails_closed_when_history_coverage_is_insufficient() -> None:
    snapshots = _Snapshots()
    service = ClosingDecisionContextService(
        universe_repository=_Universes(["ashare:000001", "ashare:600519"]),
        market_data_repository=_MarketData(_rising_bars(asset_ids=("ashare:000001",))),
        snapshot_repository=snapshots,
        theme_contexts=_Themes({}),
        minimum_market_assets=2,
    )

    result = service.refresh(
        context_type="market",
        market="ashare",
        universe_id="universe:all",
        lookback_bars=61,
    )

    assert result["status"] == "partial"
    assert result["regime"] == "risk_off"
    assert snapshots.rows[-1].payload["reason_codes"] == [
        "market_history_coverage_insufficient"
    ]
    assert snapshots.rows[-1].payload["risk_budget"]["allow_new_buys"] is False


def test_market_refresh_excludes_assets_with_stale_latest_close() -> None:
    snapshots = _Snapshots()
    bars = _rising_bars()
    for row in bars:
        if row.asset_id == "ashare:600519":
            row.timestamp -= timedelta(days=1)
    service = ClosingDecisionContextService(
        universe_repository=_Universes(["ashare:000001", "ashare:600519"]),
        market_data_repository=_MarketData(bars),
        snapshot_repository=snapshots,
        theme_contexts=_Themes({}),
        minimum_market_assets=2,
    )

    result = service.refresh(
        context_type="market",
        market="ashare",
        universe_id="universe:all",
        lookback_bars=61,
    )

    assert result["status"] == "partial"
    assert snapshots.rows[-1].payload["stale_asset_count"] == 1
    assert snapshots.rows[-1].payload["sample_count"] == 1


def test_market_refresh_requires_ninety_five_percent_history_coverage() -> None:
    snapshots = _Snapshots()
    service = ClosingDecisionContextService(
        universe_repository=_Universes(["ashare:000001", "ashare:600519"]),
        market_data_repository=_MarketData(_rising_bars(asset_ids=("ashare:000001",))),
        snapshot_repository=snapshots,
        theme_contexts=_Themes({}),
        minimum_market_assets=1,
        minimum_market_coverage_ratio=0.95,
    )

    result = service.refresh(
        context_type="market",
        market="ashare",
        universe_id="universe:all",
        lookback_bars=61,
    )

    assert result["status"] == "partial"
    assert snapshots.rows[-1].payload["coverage_ratio"] == 0.5
    assert snapshots.rows[-1].payload["missing_history_asset_count"] == 1


def test_sector_refresh_persists_opportunities_and_leadership() -> None:
    snapshots = _Snapshots(
        latest_market=SimpleNamespace(
            as_of=NOW,
            quality_status="available",
            data_snapshot_id="snapshot:market:1",
            payload={"regime": "trend_down"},
        )
    )
    contexts = {
        "ashare:600519": SimpleNamespace(
            sectors=(
                {
                    "sector_id": "industry:liquor",
                    "sector_name": "白酒",
                    "strength_score": 88.0,
                    "sector_regime": "diffusion",
                    "override_eligible": True,
                },
            ),
            leadership={
                "sector_id": "industry:liquor",
                "role": "leader",
                "score": 91.0,
            },
            factor_groups=(),
            evidence_ids=("sector:e1", "leader:e1"),
        )
    }
    service = ClosingDecisionContextService(
        universe_repository=_Universes(["ashare:600519"]),
        market_data_repository=_MarketData(_rising_bars(asset_ids=("ashare:600519",))),
        snapshot_repository=snapshots,
        theme_contexts=_Themes(contexts),
        minimum_market_assets=1,
    )

    result = service.refresh(
        context_type="sector",
        market="ashare",
        universe_id="universe:merged",
        lookback_bars=5,
    )

    payload = snapshots.rows[-1].payload
    assert result["status"] == "available"
    assert result["sector_count"] == 1
    assert payload["sector_opportunities"][0]["sector_id"] == "industry:liquor"
    assert payload["sector_opportunities"][0]["leader_asset_ids"] == [
        "ashare:600519"
    ]
    assert payload["assets"][0]["leadership"]["role"] == "leader"
    assert payload["evidence_ids"] == ["leader:e1", "sector:e1"]


def test_sector_refresh_uses_persisted_market_regime() -> None:
    snapshots = _Snapshots(
        latest_market=SimpleNamespace(
            as_of=NOW,
            quality_status="available",
            data_snapshot_id="snapshot:market:same-generation",
            payload={"regime": "trend_down"},
        )
    )
    themes = _Themes({})
    service = ClosingDecisionContextService(
        universe_repository=_Universes(["ashare:600519"]),
        market_data_repository=_MarketData(_rising_bars(asset_ids=("ashare:600519",))),
        snapshot_repository=snapshots,
        theme_contexts=themes,
        minimum_market_assets=1,
    )

    service.refresh(
        context_type="sector",
        market="ashare",
        universe_id="universe:merged",
        lookback_bars=5,
    )

    assert themes.market_regimes == ["trend_down"]


def _rising_bars(
    *,
    asset_ids: tuple[str, ...] = ("ashare:000001", "ashare:600519"),
) -> list[SimpleNamespace]:
    rows: list[SimpleNamespace] = []
    for asset_id in asset_ids:
        price = Decimal("10")
        for offset in range(61):
            next_price = price * Decimal("1.01")
            rows.append(
                SimpleNamespace(
                    asset_id=asset_id,
                    timestamp=NOW - timedelta(days=60 - offset),
                    close=next_price,
                    source="test",
                )
            )
            price = next_price
    return rows


class _Universes:
    def __init__(self, asset_ids: list[str]) -> None:
        self.members = [
            SimpleNamespace(asset_id=asset_id, symbol=asset_id.split(":")[-1])
            for asset_id in asset_ids
        ]

    def list_members(self, _universe_id: str, included_only: bool = True) -> list[Any]:
        assert included_only is True
        return self.members


class _MarketData:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows

    def list_recent_bars_for_assets(self, **_kwargs: Any) -> list[SimpleNamespace]:
        return self.rows


class _Snapshots:
    def __init__(self, *, latest_market: Any | None = None) -> None:
        self.rows: list[Any] = []
        self.latest_market = latest_market

    def insert_snapshot(self, snapshot: Any) -> Any:
        self.rows.append(snapshot)
        return snapshot

    def get_latest(self, *, snapshot_type: str, market: str) -> Any | None:
        assert market == "ashare"
        return self.latest_market if snapshot_type == "market_regime" else None


class _Themes:
    def __init__(self, contexts: dict[str, Any]) -> None:
        self.contexts = contexts
        self.market_regimes: list[str] = []

    def build_for_members(
        self,
        _members: list[Any],
        *,
        market_regime: str = "range",
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        assert as_of is not None
        self.market_regimes.append(market_regime)
        return self.contexts
