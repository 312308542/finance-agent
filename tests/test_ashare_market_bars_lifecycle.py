from __future__ import annotations

import logging
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest

from finance_agent.data.collectors import ArchivedProviderResult, AshareP0Collector
from finance_agent.data.models import MarketBarData, MarketBarsResult, ProviderResult
from finance_agent.data.normalizers import normalize_ashare_hist_tx
from finance_agent.scheduler.base_data_scheduler import import_collection_module


@pytest.mark.parametrize(
    ("task_type", "is_closed", "status"),
    [
        ("market_bars_midday_partial", False, "partial"),
        ("market_bars_close_final", True, "available"),
        ("market_bars_revision", True, "available"),
    ],
)
def test_ashare_market_bar_lifecycle_tasks_expand_market_assets(
    monkeypatch: pytest.MonkeyPatch,
    task_type: str,
    is_closed: bool,
    status: str,
) -> None:
    """A 股日 K 生命周期任务都应按资产池展开，并传递闭合状态。"""

    collect_base_data = import_collection_module()
    calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append(
                {
                    "task": task,
                    "provider_key": provider_key,
                    "parameters": parameters,
                    "force": force,
                }
            )
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data,
        "batch_ashare_symbols",
        lambda session, limit, fallback_symbol, **kwargs: ["000001", "600519"],
    )
    monkeypatch.setattr(
        collect_base_data,
        "should_refresh_asset_universe_before_incremental",
        lambda session, market: False,
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p0"],
        sync_task_type=task_type,
        symbol_source="market_assets",
        is_closed=is_closed,
        status=status,
        limit=2,
        batch_size=2,
    )

    collect_base_data.run_ashare_p0(object(), args, RecordingRuntime())

    ohlcv_calls = [item for item in calls if item["task"] == "ashare_p0_ohlcv"]
    assert [item["parameters"]["symbol"] for item in ohlcv_calls] == ["000001", "600519"]
    assert {item["parameters"]["is_closed"] for item in ohlcv_calls} == {is_closed}
    assert {item["parameters"]["status"] for item in ohlcv_calls} == {status}


def test_ashare_market_bar_task_passes_split_source_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股 K 线任务入口应把真实源拆分 gate 传给 collector。"""

    collect_base_data = import_collection_module()
    captured_source_gates: list[Any] = []

    class ExecutingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            if task == "ashare_p0_ohlcv":
                collect()
            return Namespace(
                task=task,
                status="available",
                raw_record_id=None,
                item_count=1,
                error_message=None,
                payload={},
            )

    def fake_collect_ohlcv(self, **kwargs):
        captured_source_gates.append(kwargs.get("source_gate"))
        return Namespace(status="available", payload={})

    monkeypatch.setattr(
        collect_base_data,
        "batch_ashare_symbols",
        lambda *args, **kwargs: ["000001"],
    )
    monkeypatch.setattr(
        collect_base_data,
        "should_refresh_asset_universe_before_incremental",
        lambda session, market: False,
    )
    monkeypatch.setattr(
        collect_base_data,
        "record_ashare_market_bar_watermark",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        collect_base_data.AshareP0Collector,
        "collect_ohlcv",
        fake_collect_ohlcv,
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p0"],
        sync_task_type="market_bars_close_final",
        symbol_source="market_assets",
        limit=1,
        batch_size=1,
    )

    collect_base_data.run_ashare_p0(object(), args, ExecutingRuntime())

    assert captured_source_gates == [collect_base_data.ashare_kline_source_gate]


def test_ashare_ohlcv_writes_canonical_source_to_standard_bars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股标准 K 线表应使用稳定 source，真实 provider source 保留在归档和映射中。"""

    saved_batches: list[dict[str, Any]] = []
    mapping_sources: list[str] = []
    archived_results: list[MarketBarsResult] = []

    class FakeAssetRepository:
        def __init__(self, _session: Any) -> None:
            pass

        def ensure_asset(self, **_kwargs: Any) -> None:
            return None

        def upsert_asset_provider_mapping(self, **kwargs: Any) -> None:
            mapping_sources.append(kwargs["source"])

    class FakeUniverseRepository:
        def __init__(self, _session: Any) -> None:
            pass

    class FakeMarketDataRepository:
        def __init__(self, _session: Any) -> None:
            pass

        def upsert_bar(self, **kwargs: Any) -> None:
            raise AssertionError(f"不应逐条写入 K 线: {kwargs}")

        def upsert_bars(self, bars: list[dict[str, Any]], *, chunk_size: int = 500) -> int:
            saved_batches.append({"bars": bars, "chunk_size": chunk_size})
            return len(bars)

    class FakeRawRecordRepository:
        def __init__(self, _session: Any) -> None:
            pass

    class FakeProvider:
        def fetch_ohlcv(self, **_kwargs: Any) -> MarketBarsResult:
            collected_at = datetime(2026, 6, 5, 15, 10, tzinfo=UTC)
            return MarketBarsResult(
                provider_name="akshare",
                status="available",
                collected_at=collected_at,
                payload={
                    "primary_source": "eastmoney:direct:kline",
                    "actual_source": "tencent:direct:kline",
                },
                bars=[
                    MarketBarData(
                        asset_id="ashare:000001",
                        symbol="000001",
                        market="ashare",
                        timeframe="1d",
                        timestamp=collected_at,
                        open_price=Decimal("10.00"),
                        high=Decimal("10.50"),
                        low=Decimal("9.90"),
                        close=Decimal("10.20"),
                        volume=Decimal("1000"),
                        amount=Decimal("10200"),
                        source="tencent:direct:kline",
                        adjustment="qfq",
                    ),
                    MarketBarData(
                        asset_id="ashare:000001",
                        symbol="000001",
                        market="ashare",
                        timeframe="1d",
                        timestamp=collected_at + timedelta(days=1),
                        open_price=Decimal("10.20"),
                        high=Decimal("10.80"),
                        low=Decimal("10.10"),
                        close=Decimal("10.60"),
                        volume=Decimal("1100"),
                        amount=Decimal("11660"),
                        source="tencent:direct:kline",
                        adjustment="qfq",
                    ),
                ],
            )

    def fake_archive(_raw_records: Any, result: MarketBarsResult, **_kwargs: Any) -> str:
        archived_results.append(result)
        return "raw:ashare:kline"

    monkeypatch.setattr("finance_agent.data.collectors.AssetRepository", FakeAssetRepository)
    monkeypatch.setattr("finance_agent.data.collectors.UniverseRepository", FakeUniverseRepository)
    monkeypatch.setattr(
        "finance_agent.data.collectors.MarketDataRepository",
        FakeMarketDataRepository,
    )
    monkeypatch.setattr(
        "finance_agent.data.collectors.RawRecordRepository",
        FakeRawRecordRepository,
    )
    monkeypatch.setattr("finance_agent.data.collectors.archive_provider_result", fake_archive)

    collector = AshareP0Collector(object(), provider=FakeProvider())

    collector.collect_ohlcv(symbol="000001")

    assert archived_results[0].bars[0].source == "tencent:direct:kline"
    assert mapping_sources == ["tencent:direct:kline"]
    assert len(saved_batches) == 1
    assert saved_batches[0]["chunk_size"] == 500
    assert [bar["source"] for bar in saved_batches[0]["bars"]] == [
        "canonical:ashare:kline",
        "canonical:ashare:kline",
    ]


def test_ashare_ohlcv_logs_provider_archive_and_persist_timings(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 股 K 线单股采集应输出 Provider、归档和标准表入库耗时。"""

    class FakeAssetRepository:
        def __init__(self, _session: Any) -> None:
            pass

        def ensure_asset(self, **_kwargs: Any) -> None:
            return None

        def upsert_asset_provider_mapping(self, **_kwargs: Any) -> None:
            return None

    class FakeUniverseRepository:
        def __init__(self, _session: Any) -> None:
            pass

    class FakeMarketDataRepository:
        def __init__(self, _session: Any) -> None:
            pass

        def upsert_bar(self, **_kwargs: Any) -> None:
            raise AssertionError("不应逐条写入 K 线")

        def upsert_bars(self, bars: list[dict[str, Any]], *, chunk_size: int = 500) -> int:
            assert chunk_size == 500
            return len(bars)

    class FakeRawRecordRepository:
        def __init__(self, _session: Any) -> None:
            pass

    class FakeProvider:
        def fetch_ohlcv(self, **_kwargs: Any) -> MarketBarsResult:
            collected_at = datetime(2026, 6, 5, 15, 10, tzinfo=UTC)
            return MarketBarsResult(
                provider_name="akshare",
                status="available",
                collected_at=collected_at,
                payload={
                    "primary_source": "eastmoney:direct:kline",
                    "actual_source": "eastmoney:direct:kline",
                    "source_attempts": [
                        {
                            "source": "eastmoney:direct:kline",
                            "status": "ok",
                            "elapsed_seconds": 0.123,
                            "row_count": 1,
                        }
                    ],
                },
                bars=[
                    MarketBarData(
                        asset_id="ashare:000001",
                        symbol="000001",
                        market="ashare",
                        timeframe="1d",
                        timestamp=collected_at,
                        open_price=Decimal("10.00"),
                        high=Decimal("10.50"),
                        low=Decimal("9.90"),
                        close=Decimal("10.20"),
                        volume=Decimal("1000"),
                        amount=Decimal("10200"),
                        source="eastmoney:direct:kline",
                        adjustment="qfq",
                    )
                ],
            )

    monkeypatch.setattr("finance_agent.data.collectors.AssetRepository", FakeAssetRepository)
    monkeypatch.setattr("finance_agent.data.collectors.UniverseRepository", FakeUniverseRepository)
    monkeypatch.setattr(
        "finance_agent.data.collectors.MarketDataRepository",
        FakeMarketDataRepository,
    )
    monkeypatch.setattr(
        "finance_agent.data.collectors.RawRecordRepository",
        FakeRawRecordRepository,
    )
    monkeypatch.setattr(
        "finance_agent.data.collectors.archive_provider_result",
        lambda *_args, **_kwargs: "raw:ashare:kline",
    )
    caplog.set_level(logging.INFO, logger="finance_agent.data.collectors")

    result = AshareP0Collector(object(), provider=FakeProvider()).collect_ohlcv(symbol="000001")

    assert "A 股 K 线 Provider 请求完成 symbol=000001" in caplog.text
    assert "A 股 K 线 raw_records 归档完成 symbol=000001" in caplog.text
    assert "A 股 K 线标准表入库完成 symbol=000001" in caplog.text
    assert "A 股 K 线采集落库完成 symbol=000001" in caplog.text
    assert result.result.payload["timing"]["persisted_bar_count"] == 1
    assert "market_bars_persist_elapsed_seconds" in result.result.payload["timing"]


def test_normalize_ashare_hist_tx_defaults_to_closed_available_bars() -> None:
    """腾讯历史日 K 默认应作为收盘后的正式 K 线落库。"""

    bars = normalize_ashare_hist_tx(
        pd.DataFrame(
            [
                {
                    "date": "2026-06-03",
                    "open": "10.00",
                    "close": "10.20",
                    "high": "10.30",
                    "low": "9.90",
                    "amount": "10000",
                }
            ]
        ),
        symbol="sz000001",
        timeframe="1d",
        source="akshare:stock_zh_a_hist_tx",
        adjustment="qfq",
    )

    assert bars[0].is_closed is True
    assert bars[0].status == "available"


def test_normalize_ashare_hist_tx_can_mark_midday_partial_bars() -> None:
    """午盘临时 K 线应显式写为未闭合 partial。"""

    bars = normalize_ashare_hist_tx(
        pd.DataFrame(
            [
                {
                    "date": "2026-06-03",
                    "open": "10.00",
                    "close": "10.20",
                    "high": "10.30",
                    "low": "9.90",
                    "amount": "10000",
                }
            ]
        ),
        symbol="sz000001",
        timeframe="1d",
        source="akshare:stock_zh_a_hist_tx",
        adjustment="qfq",
        is_closed=False,
        status="partial",
    )

    assert bars[0].is_closed is False
    assert bars[0].status == "partial"


def test_revision_symbol_selection_keeps_only_missing_stale_or_retryable_assets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """凌晨修正只应选择缺口、过旧或失败重试到期的标的。"""

    collect_base_data = import_collection_module()
    now = datetime(2026, 6, 4, 2, 10, tzinfo=UTC)
    stale_before = datetime(2026, 5, 28, tzinfo=UTC)

    class FakeAssetRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def find_by_market(self, market: str, *, only_tradable: bool = True) -> list[Any]:
            assert market == "ashare"
            return [
                Namespace(asset_id="ashare:000001", symbol="000001"),
                Namespace(asset_id="ashare:000002", symbol="000002"),
                Namespace(asset_id="ashare:000003", symbol="000003"),
                Namespace(asset_id="ashare:000004", symbol="000004"),
                Namespace(asset_id="ashare:000005", symbol="000005"),
            ]

    coverage = {
        "ashare:000001": (120, datetime(2026, 6, 3, tzinfo=UTC)),
        "ashare:000003": (120, datetime(2026, 6, 3, tzinfo=UTC)),
        "ashare:000004": (120, datetime(2026, 6, 3, tzinfo=UTC)),
        "ashare:000005": (80, datetime(2026, 5, 20, tzinfo=UTC)),
    }
    watermarks = {
        "ashare:000003": Namespace(status="error", next_retry_at=now - timedelta(minutes=1)),
        "ashare:000004": Namespace(status="error", next_retry_at=now + timedelta(minutes=10)),
    }

    monkeypatch.setattr(collect_base_data, "AssetRepository", FakeAssetRepository)
    monkeypatch.setattr(
        collect_base_data,
        "_fetch_ashare_bar_coverage",
        lambda session, asset_ids, timeframe: coverage,
        raising=False,
    )
    monkeypatch.setattr(
        collect_base_data,
        "_fetch_data_sync_watermarks",
        lambda session, asset_ids, data_domain, provider, timeframe: watermarks,
        raising=False,
    )

    symbols = collect_base_data.batch_ashare_symbols(
        object(),
        fallback_symbol="000001",
        now=now,
        only_failed_or_stale=True,
        stale_before=stale_before,
    )

    assert symbols == ["000002", "000005", "000003"]


def test_rate_limited_collection_records_source_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """采集入口应把成功和失败反馈给限频器，用于动态退避。"""

    collect_base_data = import_collection_module()
    events: list[tuple[str, str]] = []

    class FakeLimiter:
        def acquire(self, source_key: str) -> Any:
            class _Context:
                def __enter__(self) -> None:
                    return None

                def __exit__(self, *_args: object) -> None:
                    return None

            return _Context()

        def record_success(self, source_key: str) -> None:
            events.append(("success", source_key))

        def record_failure(self, source_key: str, error_message: str | None = None) -> None:
            events.append(("failure", source_key))

    monkeypatch.setattr(collect_base_data, "SOURCE_RATE_LIMITER", FakeLimiter())

    collect_base_data.run_rate_limited_collection("stock_zh_a_hist_tx", lambda: "ok")
    with pytest.raises(RuntimeError):
        collect_base_data.run_rate_limited_collection(
            "stock_zh_a_hist_tx",
            lambda: (_ for _ in ()).throw(RuntimeError("curl: (56) closed")),
        )

    assert events == [
        ("success", "stock_zh_a_hist_tx"),
        ("failure", "stock_zh_a_hist_tx"),
    ]


def test_rate_limited_collection_updates_progress_source_rate_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """采集失败触发退避后，应把 source 限频快照写入进度记录器。"""

    collect_base_data = import_collection_module()
    snapshots: list[dict[str, Any]] = []

    class FakeLimiter:
        def acquire(self, source_key: str) -> Any:
            class _Context:
                def __enter__(self) -> None:
                    return None

                def __exit__(self, *_args: object) -> None:
                    return None

            return _Context()

        def record_success(self, source_key: str) -> None:
            return None

        def record_failure(self, source_key: str, error_message: str | None = None) -> None:
            return None

        def adaptive_snapshot(self, source_key: str) -> dict[str, Any]:
            return {
                "failure_count": 1,
                "disconnect_count": 1,
                "effective_max_concurrency": 1,
                "effective_min_interval_seconds": 2.0,
            }

    class FakeProgress:
        def source_rate_updated(
            self,
            *,
            source_key: str,
            snapshot: dict[str, Any],
            ttl_seconds: int | None = None,
        ) -> None:
            snapshots.append(
                {
                    "source_key": source_key,
                    "snapshot": snapshot,
                    "ttl_seconds": ttl_seconds,
                }
            )

    monkeypatch.setattr(collect_base_data, "SOURCE_RATE_LIMITER", FakeLimiter())
    monkeypatch.setattr(collect_base_data, "COLLECTION_PROGRESS_RECORDER", FakeProgress())

    with pytest.raises(RuntimeError):
        collect_base_data.run_rate_limited_collection(
            "stock_zh_a_hist_tx",
            lambda: (_ for _ in ()).throw(RuntimeError("curl: (56) closed")),
        )

    assert snapshots == [
        {
            "source_key": "stock_zh_a_hist_tx",
            "snapshot": {
                "failure_count": 1,
                "disconnect_count": 1,
                "effective_max_concurrency": 1,
                "effective_min_interval_seconds": 2.0,
            },
            "ttl_seconds": None,
        }
    ]


def test_rate_limited_collection_records_archived_provider_error_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """采集器返回 ArchivedProviderResult 时，应读取内部 ProviderResult 状态进行源级失败统计。"""

    collect_base_data = import_collection_module()
    events: list[tuple[str, str, str | None]] = []

    class FakeLimiter:
        def acquire(self, source_key: str) -> Any:
            class _Context:
                def __enter__(self) -> None:
                    return None

                def __exit__(self, *_args: object) -> None:
                    return None

            return _Context()

        def record_success(self, source_key: str) -> None:
            events.append(("success", source_key, None))

        def record_failure(self, source_key: str, error_message: str | None = None) -> None:
            events.append(("failure", source_key, error_message))

    archived_error = ArchivedProviderResult(
        result=ProviderResult(
            provider_name="akshare",
            status="error",
            collected_at=datetime.now(tz=UTC),
            error_message="curl: (56) Connection closed abruptly",
        ),
        raw_record_id="raw:error",
    )

    monkeypatch.setattr(collect_base_data, "SOURCE_RATE_LIMITER", FakeLimiter())

    result = collect_base_data.run_rate_limited_collection(
        "stock_zh_a_hist_tx",
        lambda: archived_error,
    )

    assert result is archived_error
    assert events == [
        ("failure", "stock_zh_a_hist_tx", "curl: (56) Connection closed abruptly"),
    ]
