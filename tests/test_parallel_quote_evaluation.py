"""gotdx 与 AKShare 并行评估及临时行情清理测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from threading import Barrier
from typing import Any

import pytest

from finance_agent.data.models import AssetData, AssetListResult
from finance_agent.data.providers.parallel_quotes import (
    ParallelQuoteEvaluator,
    clear_intraday_quote_cache,
)
from finance_agent.scheduler.base_data_scheduler import import_collection_module


def test_parallel_evaluator_runs_both_sources_and_keeps_provenance() -> None:
    barrier = Barrier(2)
    calls: list[str] = []
    now = datetime(2026, 7, 20, 9, 40, tzinfo=UTC)

    def fetch_gotdx(_: tuple[str, ...]) -> list[dict[str, Any]]:
        calls.append("gotdx")
        barrier.wait(timeout=1)
        return [{"asset_id": "ashare:600519", "last_price": "1500", "as_of": now}]

    def fetch_akshare(_: tuple[str, ...]) -> list[dict[str, Any]]:
        calls.append("akshare")
        barrier.wait(timeout=1)
        return [{"asset_id": "ashare:600519", "last_price": "1499", "as_of": now}]

    result = ParallelQuoteEvaluator(fetch_gotdx, fetch_akshare).evaluate(
        symbols=("600519.SH",),
        data_snapshot_id="snapshot:quotes:1",
    )

    assert sorted(calls) == ["akshare", "gotdx"]
    assert {row["source"] for row in result.rows} == {"gotdx:tdx_main", "akshare:stock_zh_a_spot"}
    assert {row["data_snapshot_id"] for row in result.rows} == {"snapshot:quotes:1"}
    assert result.metrics["source_count"] == 2
    assert result.metrics["price_delta"]["ashare:600519"] == Decimal("1")
    assert result.metrics["conflicts"] == {}


def test_parallel_evaluator_marks_cross_source_price_conflict() -> None:
    """跨源价格偏差超过阈值时，必须显式标记冲突而不是静默选择一个来源。"""

    now = datetime(2026, 7, 20, 9, 40, tzinfo=UTC)

    result = ParallelQuoteEvaluator(
        lambda _symbols: [{"asset_id": "ashare:600519", "last_price": "1500", "as_of": now}],
        lambda _symbols: [{"asset_id": "ashare:600519", "last_price": "1470", "as_of": now}],
    ).evaluate(symbols=("600519.SH",), data_snapshot_id="snapshot:quotes:conflict")

    assert result.metrics["conflicts"]["ashare:600519"]["relative_delta"] == Decimal("0.020408")
    assert {row["quality_status"] for row in result.rows} == {"conflict"}
    assert all(row["payload"]["cross_source_quality"] == "conflict" for row in result.rows)


def test_clear_intraday_quote_cache_removes_only_temporary_rows() -> None:
    cache = {
        ("ashare:600519", "gotdx:tdx_main"): {"last_price": Decimal("1500")},
        ("ashare:000001", "akshare:stock_zh_a_spot"): {"last_price": Decimal("12")},
    }

    removed = clear_intraday_quote_cache(cache)

    assert removed == 2
    assert cache == {}


def test_realtime_collection_task_wires_both_providers_and_latest_storage(monkeypatch) -> None:
    module = import_collection_module()
    calls: list[str] = []

    class _Gotdx:
        def __init__(self, **_: Any) -> None:
            pass

        def fetch_quotes(self, symbols: tuple[str, ...]) -> list[Any]:
            calls.append(f"gotdx:{symbols[0]}")
            return [
                type(
                    "Quote",
                    (),
                    {
                        "asset_id": "ashare:600519",
                        "symbol": "600519",
                        "market": "ashare",
                        "as_of": datetime(2026, 7, 20, 9, 40, tzinfo=UTC),
                        "received_at": datetime(2026, 7, 20, 9, 40, 1, tzinfo=UTC),
                        "server_timestamp": datetime(2026, 7, 20, 9, 40, tzinfo=UTC),
                        "last_price": Decimal("1500"),
                        "prev_close": Decimal("1490"),
                        "open_price": Decimal("1495"),
                        "high": Decimal("1502"),
                        "low": Decimal("1490"),
                        "volume": Decimal("100"),
                        "amount": Decimal("1000"),
                        "turnover_rate": None,
                        "change_amount": Decimal("10"),
                        "change_percent": Decimal("0.67"),
                        "bid_price": Decimal("1499.9"),
                        "ask_price": Decimal("1500.1"),
                        "status": "available",
                        "quality_status": "available",
                        "payload": {},
                    },
                )()
            ]

    class _Akshare:
        def fetch_assets(self, *, limit: int | None = None) -> AssetListResult:
            calls.append("akshare")
            return AssetListResult(
                provider_name="akshare",
                status="available",
                collected_at=datetime(2026, 7, 20, 9, 40, tzinfo=UTC),
                assets=[
                    AssetData(
                        asset_id="ashare:600519",
                        symbol="600519",
                        name="贵州茅台",
                        market="ashare",
                        asset_type="stock",
                        payload={"最新价": "1499"},
                    )
                ],
            )

    class _Repo:
        rows: list[Any] = []

        def upsert_intraday_quote_latest(self, rows: Any) -> int:
            self.rows = list(rows)
            calls.append(f"persist:{len(rows)}")
            return len(rows)

    class _Snapshots:
        inserted: list[Any] = []

        def insert_snapshot(self, snapshot: Any) -> Any:
            self.inserted.append(snapshot)
            return snapshot

    class _Runtime:
        def run_task(self, *, collect: Any, **_: Any) -> Any:
            return collect()

    monkeypatch.setattr(module, "resolve_realtime_quote_symbols", lambda *_args, **_kwargs: ["600519.SH"])
    monkeypatch.setattr(module, "GotdxGatewayProvider", _Gotdx)
    monkeypatch.setattr(module, "AkshareProvider", _Akshare)
    repository = _Repo()
    snapshots = _Snapshots()
    monkeypatch.setattr(module, "AssetRepository", lambda _session: repository)
    monkeypatch.setattr(module, "DataSnapshotRepository", lambda _session: snapshots)

    result = module.build_ashare_parallel_realtime_task(object(), type("Args", (), {})(), _Runtime())

    assert result.result.status == "available"
    assert calls[:2] == ["gotdx:600519.SH", "akshare"] or calls[:2] == ["akshare", "gotdx:600519.SH"]
    assert "persist:2" in calls
    assert len(snapshots.inserted) == 1
    assert {row["data_snapshot_id"] for row in repository.rows} == {
        snapshots.inserted[0].data_snapshot_id
    }


def test_akshare_quote_fetcher_propagates_provider_failure(monkeypatch) -> None:
    """AKShare 返回错误状态时必须进入双源错误隔离，不能被当成空成功。"""

    module = import_collection_module()

    class _Akshare:
        def fetch_assets(self, *, limit: int | None = None) -> AssetListResult:
            del limit
            return AssetListResult(
                provider_name="akshare",
                status="error",
                collected_at=datetime(2026, 7, 20, 9, 40, tzinfo=UTC),
                error_message="timeout",
            )

    with pytest.raises(RuntimeError, match="AKShare 行情 Provider 不可用"):
        module._fetch_akshare_quote_rows(_Akshare(), ("600519.SH",))
