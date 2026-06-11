from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from finance_agent.data.collectors import (
    AshareP0Collector,
    AshareP1Collector,
    AshareP2Collector,
    CryptoDataCollector,
)
from finance_agent.data.models import (
    AssetData,
    AssetListResult,
    CapitalFlowSnapshotData,
    CapitalFlowSnapshotsResult,
    CryptoDerivativeSnapshotData,
    CryptoDerivativeSnapshotResult,
    FundamentalSnapshotData,
    FundamentalSnapshotsResult,
    MarketBarData,
    MarketBarsResult,
)


class _FakeRawRecords:
    def insert_raw_record(self, **_kwargs: Any) -> Any:
        return type("RawRecord", (), {"raw_record_id": "raw:test"})()


def test_ashare_asset_collection_uses_asset_batch_methods(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, list[list[dict[str, Any]]]] = {
        "masters": [],
        "profiles": [],
        "mappings": [],
        "statuses": [],
        "quotes": [],
    }

    class FakeAssetRepository:
        def __init__(self, _session: Any) -> None:
            pass

        def upsert_asset_masters(self, rows: list[dict[str, Any]]) -> int:
            calls["masters"].append(rows)
            return len(rows)

        def upsert_asset_profiles(self, rows: list[dict[str, Any]]) -> int:
            calls["profiles"].append(rows)
            return len(rows)

        def upsert_asset_provider_mappings(self, rows: list[dict[str, Any]]) -> int:
            calls["mappings"].append(rows)
            return len(rows)

        def upsert_asset_status_snapshots(self, rows: list[dict[str, Any]]) -> int:
            calls["statuses"].append(rows)
            return len(rows)

        def upsert_realtime_quote_snapshots(self, rows: list[dict[str, Any]]) -> int:
            calls["quotes"].append(rows)
            return len(rows)

        def upsert_asset_master(self, **_kwargs: Any) -> None:
            raise AssertionError("不应在资产池刷新中逐条写主表")

    class FakeUniverseRepository:
        def __init__(self, _session: Any) -> None:
            pass

        def upsert_universe(self, **_kwargs: Any) -> None:
            pass

        def replace_members(self, **_kwargs: Any) -> None:
            pass

    class FakeProvider:
        def fetch_assets(self, *, limit: int | None = None) -> AssetListResult:
            return AssetListResult(
                provider_name="akshare",
                status="available",
                collected_at=datetime(2026, 6, 8, tzinfo=UTC),
                assets=[
                    AssetData(
                        asset_id="ashare:000001",
                        symbol="000001",
                        name="平安银行",
                        market="ashare",
                        asset_type="stock",
                    ),
                    AssetData(
                        asset_id="ashare:600519",
                        symbol="600519",
                        name="贵州茅台",
                        market="ashare",
                        asset_type="stock",
                    ),
                ],
            )

    monkeypatch.setattr("finance_agent.data.collectors.AssetRepository", FakeAssetRepository)
    monkeypatch.setattr("finance_agent.data.collectors.UniverseRepository", FakeUniverseRepository)
    monkeypatch.setattr("finance_agent.data.collectors.RawRecordRepository", lambda _session: _FakeRawRecords())

    AshareP0Collector(object(), provider=FakeProvider()).collect_assets(
        universe_id="universe:test",
        universe_name="测试资产池",
        strategy_context="test",
    )

    assert [len(items[0]) for items in calls.values()] == [2, 2, 2, 2, 2]


def test_ashare_flow_rank_uses_batch_fact_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, list[list[dict[str, Any]]]] = {"assets": [], "flows": []}

    class FakeAssetRepository:
        def __init__(self, _session: Any) -> None:
            pass

        def ensure_assets(self, rows: list[dict[str, Any]]) -> int:
            calls["assets"].append(rows)
            return len(rows)

        def ensure_asset(self, **_kwargs: Any) -> None:
            raise AssertionError("不应在资金流采集中逐条确保资产")

    class FakeCapitalFlowRepository:
        def __init__(self, _session: Any) -> None:
            pass

        def upsert_capital_flow_snapshots(self, rows: list[dict[str, Any]]) -> int:
            calls["flows"].append(rows)
            return len(rows)

        def upsert_capital_flow_snapshot(self, **_kwargs: Any) -> None:
            raise AssertionError("不应在资金流采集中逐条写快照")

    class FakeFlowProvider:
        def fetch_flow_rank(
            self,
            *,
            indicator: str = "今日",
            limit: int | None = None,
        ) -> CapitalFlowSnapshotsResult:
            return CapitalFlowSnapshotsResult(
                provider_name="akshare",
                status="available",
                collected_at=datetime(2026, 6, 8, tzinfo=UTC),
                snapshots=[
                    CapitalFlowSnapshotData(
                        snapshot_id="flow:000001",
                        asset_id="ashare:000001",
                        symbol="000001",
                        market="ashare",
                        window="today",
                        source="akshare:flow",
                        as_of=datetime(2026, 6, 8, tzinfo=UTC),
                        amount=Decimal("100"),
                    )
                ],
            )

    monkeypatch.setattr("finance_agent.data.collectors.AssetRepository", FakeAssetRepository)
    monkeypatch.setattr(
        "finance_agent.data.collectors.CapitalFlowRepository", FakeCapitalFlowRepository
    )
    monkeypatch.setattr("finance_agent.data.collectors.RawRecordRepository", lambda _session: _FakeRawRecords())

    AshareP1Collector(object(), flow_provider=FakeFlowProvider()).collect_flow_rank()

    assert len(calls["assets"][0]) == 1
    assert len(calls["flows"][0]) == 1


def test_ashare_fundamentals_use_batch_fact_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, list[list[dict[str, Any]]]] = {"assets": [], "fundamentals": []}

    class FakeAssetRepository:
        def __init__(self, _session: Any) -> None:
            pass

        def ensure_assets(self, rows: list[dict[str, Any]]) -> int:
            calls["assets"].append(rows)
            return len(rows)

        def ensure_asset(self, **_kwargs: Any) -> None:
            raise AssertionError("不应在财务采集中逐条确保资产")

    class FakeFundamentalRepository:
        def __init__(self, _session: Any) -> None:
            pass

        def upsert_fundamental_snapshots(self, rows: list[dict[str, Any]]) -> int:
            calls["fundamentals"].append(rows)
            return len(rows)

        def upsert_fundamental_snapshot(self, **_kwargs: Any) -> None:
            raise AssertionError("不应在财务采集中逐条写快照")

    result = FundamentalSnapshotsResult(
        provider_name="akshare",
        status="available",
        collected_at=datetime(2026, 6, 8, tzinfo=UTC),
        snapshots=[
            FundamentalSnapshotData(
                snapshot_id="fundamental:000001",
                asset_id="ashare:000001",
                symbol="000001",
                source="akshare:fundamental",
                status="available",
                as_of=datetime(2026, 6, 8, tzinfo=UTC),
            )
        ],
    )

    monkeypatch.setattr("finance_agent.data.collectors.AssetRepository", FakeAssetRepository)
    monkeypatch.setattr(
        "finance_agent.data.collectors.FundamentalDataRepository",
        FakeFundamentalRepository,
    )
    monkeypatch.setattr("finance_agent.data.collectors.RawRecordRepository", lambda _session: _FakeRawRecords())

    collector = AshareP2Collector(object())
    collector._persist_fundamental_snapshots(result, raw_record_id="raw:test")

    assert len(calls["assets"][0]) == 1
    assert len(calls["fundamentals"][0]) == 1


def test_ashare_ohlcv_uses_batch_asset_stub_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, list[list[dict[str, Any]]]] = {"assets": [], "mappings": [], "bars": []}

    class FakeAssetRepository:
        def __init__(self, _session: Any) -> None:
            pass

        def ensure_assets(self, rows: list[dict[str, Any]]) -> int:
            calls["assets"].append(rows)
            return len(rows)

        def upsert_asset_provider_mappings(self, rows: list[dict[str, Any]]) -> int:
            calls["mappings"].append(rows)
            return len(rows)

        def ensure_asset(self, **_kwargs: Any) -> None:
            raise AssertionError("A 股 K 线采集不应逐条写资产占位")

        def upsert_asset_provider_mapping(self, **_kwargs: Any) -> None:
            raise AssertionError("A 股 K 线采集不应逐条写 Provider 映射")

    class FakeMarketRepository:
        def __init__(self, _session: Any) -> None:
            pass

        def upsert_bars(self, rows: list[dict[str, Any]], *, chunk_size: int = 500) -> int:
            calls["bars"].append(rows)
            return len(rows)

    class FakeProvider:
        def fetch_ohlcv(
            self,
            *,
            symbol: str,
            timeframe: str,
            start: str | None = None,
            end: str | None = None,
            limit: int | None = None,
            adjust: str = "qfq",
            is_closed: bool = True,
            status: str = "available",
            source_gate: Any | None = None,
        ) -> MarketBarsResult:
            return MarketBarsResult(
                provider_name="akshare",
                status="available",
                collected_at=datetime(2026, 6, 8, tzinfo=UTC),
                bars=[
                    MarketBarData(
                        asset_id="ashare:000001",
                        symbol="000001",
                        market="ashare",
                        timeframe="1d",
                        timestamp=datetime(2026, 6, 5, tzinfo=UTC),
                        open_price=Decimal("1"),
                        high=Decimal("1"),
                        low=Decimal("1"),
                        close=Decimal("1"),
                        volume=Decimal("10"),
                        source="akshare:stock_zh_a_hist_tx",
                    ),
                    MarketBarData(
                        asset_id="ashare:000001",
                        symbol="000001",
                        market="ashare",
                        timeframe="1d",
                        timestamp=datetime(2026, 6, 6, tzinfo=UTC),
                        open_price=Decimal("1"),
                        high=Decimal("1"),
                        low=Decimal("1"),
                        close=Decimal("1"),
                        volume=Decimal("10"),
                        source="akshare:stock_zh_a_hist_tx",
                    ),
                ],
            )

    monkeypatch.setattr("finance_agent.data.collectors.AssetRepository", FakeAssetRepository)
    monkeypatch.setattr("finance_agent.data.collectors.MarketDataRepository", FakeMarketRepository)
    monkeypatch.setattr("finance_agent.data.collectors.RawRecordRepository", lambda _session: _FakeRawRecords())

    AshareP0Collector(object(), provider=FakeProvider()).collect_ohlcv(
        symbol="000001",
        timeframe="1d",
    )

    assert len(calls["assets"][0]) == 1
    assert len(calls["mappings"][0]) == 1
    assert len(calls["bars"][0]) == 2


def test_crypto_derivative_snapshot_uses_batch_asset_stub_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, list[list[dict[str, Any]]]] = {
        "assets": [],
        "mappings": [],
        "derivatives": [],
    }

    class FakeAssetRepository:
        def __init__(self, _session: Any) -> None:
            pass

        def ensure_assets(self, rows: list[dict[str, Any]]) -> int:
            calls["assets"].append(rows)
            return len(rows)

        def upsert_asset_provider_mappings(self, rows: list[dict[str, Any]]) -> int:
            calls["mappings"].append(rows)
            return len(rows)

        def ensure_asset(self, **_kwargs: Any) -> None:
            raise AssertionError("Crypto 快照采集不应逐条写资产占位")

        def upsert_asset_provider_mapping(self, **_kwargs: Any) -> None:
            raise AssertionError("Crypto 快照采集不应逐条写 Provider 映射")

    class FakeDerivativeRepository:
        def __init__(self, _session: Any) -> None:
            pass

        def upsert_crypto_derivative_snapshots(self, rows: list[dict[str, Any]]) -> int:
            calls["derivatives"].append(rows)
            return len(rows)

    class FakeDerivativeProvider:
        def fetch_derivative_snapshot(self, *, symbol: str) -> CryptoDerivativeSnapshotResult:
            return CryptoDerivativeSnapshotResult(
                provider_name="binance",
                status="available",
                collected_at=datetime(2026, 6, 8, tzinfo=UTC),
                snapshot=CryptoDerivativeSnapshotData(
                    snapshot_id="derivative:BTCUSDT",
                    asset_id="crypto_future:BTCUSDT",
                    symbol="BTC/USDT",
                    market="crypto_future",
                    source="binance:native:futures",
                    as_of=datetime(2026, 6, 8, tzinfo=UTC),
                ),
            )

    monkeypatch.setattr("finance_agent.data.collectors.AssetRepository", FakeAssetRepository)
    monkeypatch.setattr(
        "finance_agent.data.collectors.DerivativeDataRepository",
        FakeDerivativeRepository,
    )
    monkeypatch.setattr("finance_agent.data.collectors.RawRecordRepository", lambda _session: _FakeRawRecords())

    CryptoDataCollector(object(), derivative_provider=FakeDerivativeProvider()).collect_derivative_snapshot(
        symbol="BTC/USDT",
    )

    assert len(calls["assets"][0]) == 1
    assert len(calls["mappings"][0]) == 1
    assert len(calls["derivatives"][0]) == 1
