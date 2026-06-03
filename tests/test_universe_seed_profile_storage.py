from datetime import UTC, datetime
from typing import Any

import pytest

from finance_agent.data.collectors import AshareP0Collector, AshareP1Collector, CryptoDataCollector
from finance_agent.data.models import (
    AssetData,
    AssetListResult,
    UniverseSeedData,
    UniverseSeedsResult,
)


def test_ashare_full_asset_collection_repairs_asset_master(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全 A 主数据刷新应修复 assets 主表占位信息，并继续写入附表。"""

    asset_calls: dict[str, list[dict[str, Any]]] = {
        "master": [],
        "ensure": [],
        "profiles": [],
        "mappings": [],
        "statuses": [],
        "quotes": [],
    }
    universe_calls: dict[str, list[Any]] = {"universes": [], "members": []}

    class FakeAssetRepository:
        def __init__(self, _session: Any) -> None:
            pass

        def upsert_asset_master(self, **kwargs: Any) -> None:
            asset_calls["master"].append(kwargs)

        def ensure_asset(self, **kwargs: Any) -> None:
            asset_calls["ensure"].append(kwargs)

        def upsert_asset_profile(self, **kwargs: Any) -> None:
            asset_calls["profiles"].append(kwargs)

        def upsert_asset_provider_mapping(self, **kwargs: Any) -> None:
            asset_calls["mappings"].append(kwargs)

        def upsert_asset_status_snapshot(self, **kwargs: Any) -> None:
            asset_calls["statuses"].append(kwargs)

        def upsert_realtime_quote_snapshot(self, **kwargs: Any) -> None:
            asset_calls["quotes"].append(kwargs)

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

    class FakeProvider:
        def fetch_assets(self, *, limit: int | None = None) -> AssetListResult:
            as_of = datetime(2026, 6, 4, 9, 30, tzinfo=UTC)
            return AssetListResult(
                provider_name="akshare",
                status="available",
                collected_at=as_of,
                payload={"actual_source": "akshare:stock_info_a_code_name"},
                assets=[
                    AssetData(
                        asset_id="ashare:300001",
                        symbol="300001",
                        name="特锐德",
                        market="ashare",
                        asset_type="stock",
                        exchange="SZSE",
                        currency="CNY",
                    )
                ],
            )

    monkeypatch.setattr("finance_agent.data.collectors.AssetRepository", FakeAssetRepository)
    monkeypatch.setattr("finance_agent.data.collectors.UniverseRepository", FakeUniverseRepository)
    monkeypatch.setattr(
        "finance_agent.data.collectors.RawRecordRepository", FakeRawRecordRepository
    )
    monkeypatch.setattr(
        "finance_agent.data.collectors.archive_provider_result",
        lambda *args, **kwargs: "raw:test",
    )

    collector = AshareP0Collector(object(), provider=FakeProvider())

    collector.collect_assets(
        universe_id="universe:base:ashare:p0:all_a",
        universe_name="全 A",
        strategy_context="base_data_collect",
    )

    assert [item["name"] for item in asset_calls["master"]] == ["特锐德"]
    assert asset_calls["master"][0]["exchange"] == "SZSE"
    assert asset_calls["master"][0]["currency"] == "CNY"
    assert asset_calls["ensure"] == []
    assert [item["name"] for item in asset_calls["profiles"]] == ["特锐德"]
    assert len(universe_calls["members"][0][1]) == 1


def test_crypto_market_collection_writes_asset_master(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Binance universe 刷新应写入 crypto assets 主表和资产池成员。"""

    asset_calls: dict[str, list[dict[str, Any]]] = {
        "master": [],
        "ensure": [],
        "profiles": [],
        "mappings": [],
        "statuses": [],
    }
    universe_calls: dict[str, list[Any]] = {"universes": [], "members": []}

    class FakeAssetRepository:
        def __init__(self, _session: Any) -> None:
            pass

        def upsert_asset_master(self, **kwargs: Any) -> None:
            asset_calls["master"].append(kwargs)

        def ensure_asset(self, **kwargs: Any) -> None:
            asset_calls["ensure"].append(kwargs)

        def upsert_asset_profile(self, **kwargs: Any) -> None:
            asset_calls["profiles"].append(kwargs)

        def upsert_asset_provider_mapping(self, **kwargs: Any) -> None:
            asset_calls["mappings"].append(kwargs)

        def upsert_asset_status_snapshot(self, **kwargs: Any) -> None:
            asset_calls["statuses"].append(kwargs)

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

    class FakeProvider:
        def fetch_assets(self, *, limit: int | None = None) -> AssetListResult:
            as_of = datetime(2026, 6, 4, 9, 30, tzinfo=UTC)
            return AssetListResult(
                provider_name="ccxt_binance",
                status="available",
                collected_at=as_of,
                payload={"row_count": 1, "default_type": "spot"},
                assets=[
                    AssetData(
                        asset_id="crypto_spot:BTCUSDT",
                        symbol="BTCUSDT",
                        name="BTC / USDT",
                        market="crypto_spot",
                        asset_type="crypto",
                        exchange="Binance",
                        currency="USDT",
                        base_asset="BTC",
                        quote_asset="USDT",
                    )
                ],
            )

    monkeypatch.setattr("finance_agent.data.collectors.AssetRepository", FakeAssetRepository)
    monkeypatch.setattr("finance_agent.data.collectors.UniverseRepository", FakeUniverseRepository)
    monkeypatch.setattr(
        "finance_agent.data.collectors.RawRecordRepository", FakeRawRecordRepository
    )
    monkeypatch.setattr(
        "finance_agent.data.collectors.archive_provider_result",
        lambda *args, **kwargs: "raw:crypto",
    )

    collector = CryptoDataCollector(
        object(),
        spot_provider=FakeProvider(),
        future_provider=FakeProvider(),
    )

    collector.collect_markets(
        market_type="spot",
        universe_id="universe:base:crypto:spot:binance",
        universe_name="Binance spot",
        strategy_context="base_data_collect",
    )

    assert [item["asset_id"] for item in asset_calls["master"]] == ["crypto_spot:BTCUSDT"]
    assert asset_calls["master"][0]["exchange"] == "Binance"
    assert asset_calls["master"][0]["currency"] == "USDT"
    assert asset_calls["ensure"] == []
    assert [item["asset_id"] for item in universe_calls["members"][0][1]] == ["crypto_spot:BTCUSDT"]


def test_crypto_market_collection_error_preserves_existing_universe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Binance 刷新失败时只归档错误，不应把已有币池覆盖成 error/0。"""

    asset_calls: list[dict[str, Any]] = []
    universe_calls: dict[str, list[Any]] = {"universes": [], "members": []}
    archived: list[str] = []

    class FakeAssetRepository:
        def __init__(self, _session: Any) -> None:
            pass

        def upsert_asset_master(self, **kwargs: Any) -> None:
            asset_calls.append(kwargs)

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

    class FakeProvider:
        def fetch_assets(self, *, limit: int | None = None) -> AssetListResult:
            return AssetListResult(
                provider_name="ccxt_binance",
                status="error",
                collected_at=datetime(2026, 6, 4, 9, 30, tzinfo=UTC),
                error_message="binance 418",
                payload={"rate_limited": True},
                assets=[],
            )

    monkeypatch.setattr("finance_agent.data.collectors.AssetRepository", FakeAssetRepository)
    monkeypatch.setattr("finance_agent.data.collectors.UniverseRepository", FakeUniverseRepository)
    monkeypatch.setattr(
        "finance_agent.data.collectors.RawRecordRepository", FakeRawRecordRepository
    )
    monkeypatch.setattr(
        "finance_agent.data.collectors.archive_provider_result",
        lambda *args, **kwargs: archived.append(kwargs["market"]) or "raw:error",
    )

    collector = CryptoDataCollector(
        object(),
        spot_provider=FakeProvider(),
        future_provider=FakeProvider(),
    )

    result = collector.collect_markets(
        market_type="future",
        universe_id="universe:base:crypto:future:binance",
        universe_name="Binance future",
        strategy_context="base_data_collect",
    )

    assert result.result.status == "error"
    assert archived == ["crypto_future"]
    assert asset_calls == []
    assert universe_calls == {"universes": [], "members": []}


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
    monkeypatch.setattr(
        "finance_agent.data.collectors.RawRecordRepository", FakeRawRecordRepository
    )
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
    assert {item["payload"]["universe_source"] for item in asset_calls["mappings"]} == {
        "akshare:index_stock_cons_csindex:000300"
    }
    assert asset_calls["profiles"] == []
    assert universe_calls["universes"][0]["source"] == "akshare:index_stock_cons_csindex:000300"
    assert len(universe_calls["members"][0][1]) == 2
