from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from finance_agent.application.data_production_service import (
    ProductionUniverseService,
    UniverseMergeService,
)


def test_production_universe_merge_prunes_members_missing_from_latest_run() -> None:
    """合并池重建后应显式剔除本轮缺席的旧成员，避免历史候选继续参与推荐。"""

    as_of = datetime(2026, 6, 30, tzinfo=UTC)
    universe_repository = _FakeUniverseRepository(as_of=as_of)
    service = ProductionUniverseService.__new__(ProductionUniverseService)
    service.universes = universe_repository
    service.merge_service = UniverseMergeService()

    plans = service.merge_universes(
        target_universe_id="universe:merged:ashare:recommendation",
        name="推荐合并池",
        source_universe_ids=["universe:tradeable:ashare:main_board"],
        strategy_context="recommendation",
        as_of=as_of,
    )

    assert [plan.asset_id for plan in plans] == ["ashare:000001", "ashare:600519"]
    assert universe_repository.replaced_universe_id == "universe:merged:ashare:recommendation"
    assert universe_repository.pruned == {
        "universe_id": "universe:merged:ashare:recommendation",
        "current_asset_ids": ["ashare:000001", "ashare:600519"],
        "as_of": as_of,
        "removed_reason": "not_in_latest_merge",
    }


class _FakeUniverseRepository:
    def __init__(self, *, as_of: datetime) -> None:
        self.as_of = as_of
        self.replaced_universe_id: str | None = None
        self.pruned: dict[str, Any] | None = None

    def list_universes(self, universe_ids: list[str]) -> list[Any]:
        return [
            SimpleNamespace(
                universe_id=universe_ids[0],
                source="test:main_board",
                market="ashare",
            )
        ]

    def list_members(self, universe_id: str, *, included_only: bool = True) -> list[Any]:
        return [
            SimpleNamespace(
                asset_id="ashare:000001",
                symbol="000001",
                market="ashare",
                rank_hint=1,
            ),
            SimpleNamespace(
                asset_id="ashare:600519",
                symbol="600519",
                market="ashare",
                rank_hint=2,
            ),
        ]

    def upsert_universe(self, **_: Any) -> None:
        return None

    def replace_members(self, *, universe_id: str, members: list[dict[str, Any]]) -> None:
        self.replaced_universe_id = universe_id
        assert [member["asset_id"] for member in members] == ["ashare:000001", "ashare:600519"]

    def prune_missing_members(
        self,
        *,
        universe_id: str,
        current_asset_ids: list[str],
        as_of: datetime,
        removed_reason: str,
    ) -> None:
        self.pruned = {
            "universe_id": universe_id,
            "current_asset_ids": current_asset_ids,
            "as_of": as_of,
            "removed_reason": removed_reason,
        }
