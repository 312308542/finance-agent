from datetime import UTC, datetime
from typing import Any

import pytest

from finance_agent.data.collectors import AshareP1Collector
from finance_agent.data.models import UniverseSeedData, UniverseSeedsResult


def test_index_member_collection_does_not_write_asset_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """指数成分只应写入资产身份和候选池成员关系，不应把成员关系展开到资产画像表。"""

    asset_calls: dict[str, list[dict[str, Any]]] = {
        "assets": [],
        "profiles": [],
        "mappings": [],
    }
    universe_calls: dict[str, list[Any]] = {"universes": [], "members": []}

    class FakeAssetRepository:
        def __init__(self, _session: Any) -> None:
            pass

        def ensure_asset(self, **kwargs: Any) -> None:
            asset_calls["assets"].append(kwargs)

        def upsert_asset_profile(self, **kwargs: Any) -> None:
            asset_calls["profiles"].append(kwargs)

        def upsert_asset_provider_mapping(self, **kwargs: Any) -> None:
            asset_calls["mappings"].append(kwargs)

    class FakeUniverseRepository:
        def __init__(self, _session: Any) -> None:
            pass

        def upsert_universe(self, **kwargs: Any) -> None:
            universe_calls["universes"].append(kwargs)

        def replace_members(self, *, universe_id: str, members: list[dict[str, Any]]) -> None:
            universe_calls["members"].append((universe_id, members))

    class FakeRawRecordRepository:
        def __init__(self, _session: Any) -> None:
            pass

    class FakeSectorProvider:
        def fetch_index_members(
            self,
            *,
            index_code: str,
            index_name: str | None = None,
            limit: int | None = None,
        ) -> UniverseSeedsResult:
            as_of = datetime(2026, 5, 29, 9, 30, tzinfo=UTC)
            return UniverseSeedsResult(
                provider_name="akshare",
                status="available",
                collected_at=as_of,
                payload={"index_code": index_code, "index_name": index_name, "limit": limit},
                seeds=[
                    UniverseSeedData(
                        seed_id="seed:000001",
                        source_name=index_name or index_code,
                        source_type="index",
                        symbol="000001",
                        name="平安银行",
                        market="ashare",
                        asset_id="ashare:000001",
                        rank_hint=1,
                        as_of=as_of,
                    ),
                    UniverseSeedData(
                        seed_id="seed:600519",
                        source_name=index_name or index_code,
                        source_type="index",
                        symbol="600519",
                        name="贵州茅台",
                        market="ashare",
                        asset_id="ashare:600519",
                        rank_hint=2,
                        as_of=as_of,
                    ),
                ],
            )

    monkeypatch.setattr("finance_agent.data.collectors.AssetRepository", FakeAssetRepository)
    monkeypatch.setattr("finance_agent.data.collectors.UniverseRepository", FakeUniverseRepository)
    monkeypatch.setattr("finance_agent.data.collectors.RawRecordRepository", FakeRawRecordRepository)
    monkeypatch.setattr(
        "finance_agent.data.collectors.archive_provider_result",
        lambda *args, **kwargs: "raw:test",
    )

    collector = AshareP1Collector(object(), sector_provider=FakeSectorProvider())

    collector.collect_index_members(
        index_code="000300",
        index_name="沪深300",
        universe_id="universe:base:ashare:p1:index:000300",
        universe_name="沪深300成分股",
        strategy_context="ashare-p1",
        limit=2,
    )

    assert len(asset_calls["assets"]) == 2
    assert len(asset_calls["mappings"]) == 2
    assert {item["source"] for item in asset_calls["mappings"]} == {"akshare:universe_seed"}
    assert {
        item["payload"]["universe_source"] for item in asset_calls["mappings"]
    } == {"akshare:index_stock_cons_csindex:000300"}
    assert asset_calls["profiles"] == []
    assert universe_calls["universes"][0]["source"] == "akshare:index_stock_cons_csindex:000300"
    assert len(universe_calls["members"][0][1]) == 2
