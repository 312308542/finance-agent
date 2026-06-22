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


def test_full_history_backfill_plans_only_missing_year_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """10 年历史日 K 补齐应只请求最新缺口和中间缺失年份。"""

    collect_base_data = import_collection_module()
    start_at = datetime(2024, 1, 1, tzinfo=UTC)
    end_at = datetime(2026, 6, 12, tzinfo=UTC)

    monkeypatch.setattr(
        collect_base_data,
        "_fetch_ashare_bar_year_coverage",
        lambda session, asset_ids, timeframe, start_at, end_at: {
            "ashare:000001": {
                2024: (242, datetime(2024, 1, 2, tzinfo=UTC), datetime(2024, 12, 31, tzinfo=UTC)),
                2025: (242, datetime(2025, 1, 2, tzinfo=UTC), datetime(2025, 12, 31, tzinfo=UTC)),
            },
            "ashare:000002": {
                2024: (242, datetime(2024, 1, 2, tzinfo=UTC), datetime(2024, 12, 31, tzinfo=UTC)),
                2026: (3, datetime(2026, 1, 2, tzinfo=UTC), datetime(2026, 1, 6, tzinfo=UTC)),
            },
            "ashare:000003": {},
            "ashare:000004": {
                2024: (242, datetime(2024, 1, 2, tzinfo=UTC), datetime(2024, 12, 31, tzinfo=UTC)),
                2025: (242, datetime(2025, 1, 2, tzinfo=UTC), datetime(2025, 12, 31, tzinfo=UTC)),
                2026: (100, datetime(2026, 1, 2, tzinfo=UTC), end_at),
            },
        },
        raising=False,
    )

    windows = collect_base_data.plan_ashare_market_bar_backfill_windows(
        object(),
        ["000001", "000002", "000003", "000004"],
        timeframe="1d",
        required_start_at=start_at,
        required_end_at=end_at,
    )

    assert windows == {
        "000001": [("20260101", "20260612")],
        "000002": [("20250101", "20251231"), ("20260101", "20260612")],
        "000003": [("20240101", "20260612")],
        "000004": [],
    }


def test_full_history_backfill_trusts_verified_leading_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已由全量请求验证过的上市前空窗，不应继续拆成年份窗口反复采集。"""

    collect_base_data = import_collection_module()
    start_at = datetime(2016, 6, 16, tzinfo=UTC)
    end_at = datetime(2026, 6, 12, tzinfo=UTC)

    monkeypatch.setattr(
        collect_base_data,
        "_fetch_ashare_bar_year_coverage",
        lambda session, asset_ids, timeframe, start_at, end_at: {
            "ashare:001220": {
                2026: (84, datetime(2026, 2, 3, tzinfo=UTC), end_at),
            },
            "ashare:603507": {
                2026: (84, datetime(2026, 2, 3, tzinfo=UTC), end_at),
            },
        },
        raising=False,
    )

    windows = collect_base_data.plan_ashare_market_bar_backfill_windows(
        object(),
        ["001220", "603507"],
        timeframe="1d",
        required_start_at=start_at,
        required_end_at=end_at,
        trusted_leading_gap_symbols={"001220"},
    )

    assert windows["001220"] == []
    assert windows["603507"][0] == ("20160616", "20161231")


def test_merge_ashare_market_bar_window_results_deduplicates_circuit_messages() -> None:
    """同一标的多个年度窗口都被熔断跳过时，任务日志不应重复拼接同一条错误。"""

    collect_base_data = import_collection_module()
    message = "Provider 熔断中，跳过本次采集"
    results = [
        collect_base_data.CollectionTaskResult(
            task="ashare_p0_ohlcv",
            status="skipped",
            raw_record_id=None,
            item_count=0,
            error_message=message,
            payload={"provider_key": "stock_zh_a_hist_tx:001220"},
        )
        for _ in range(3)
    ]

    result = collect_base_data.merge_ashare_market_bar_window_results(
        results,
        windows=[
            ("20160101", "20161231"),
            ("20170101", "20171231"),
            ("20180101", "20181231"),
        ],
    )

    assert result.status == "skipped"
    assert result.error_message == message
    assert result.payload["error_message_counts"] == {message: 3}


def test_merge_ashare_market_bar_window_results_keeps_partial_window_failure() -> None:
    """只要有窗口真实失败，多窗口合并仍应保留失败状态。"""

    collect_base_data = import_collection_module()
    results = [
        collect_base_data.CollectionTaskResult(
            task="ashare_p0_ohlcv",
            status="available",
            raw_record_id="raw:ok",
            item_count=120,
            error_message=None,
            payload={"actual_source": "tencent_kline"},
        ),
        collect_base_data.CollectionTaskResult(
            task="ashare_p0_ohlcv",
            status="error",
            raw_record_id=None,
            item_count=0,
            error_message="curl: (56) Connection closed abruptly",
            payload={"provider_key": "stock_zh_a_hist_tx:001220"},
        ),
    ]

    result = collect_base_data.merge_ashare_market_bar_window_results(
        results,
        windows=[
            ("20240101", "20241231"),
            ("20250101", "20251231"),
        ],
    )

    assert result.status == "error"
    assert result.error_message == "curl: (56) Connection closed abruptly"


def test_merge_ashare_market_bar_window_results_treats_empty_windows_as_skipped() -> None:
    """上市前年度空窗口不应和熔断跳过一起合并成失败，避免新股历史补齐被误判。"""

    collect_base_data = import_collection_module()
    circuit_message = "Provider 熔断中，跳过本次采集"
    results = [
        collect_base_data.CollectionTaskResult(
            task="ashare_p0_ohlcv",
            status="unavailable",
            raw_record_id="raw:empty",
            item_count=0,
            error_message=None,
            payload={"actual_source": "tencent:direct:kline"},
        ),
        collect_base_data.CollectionTaskResult(
            task="ashare_p0_ohlcv",
            status="skipped",
            raw_record_id=None,
            item_count=0,
            error_message=circuit_message,
            payload={"provider_key": "stock_zh_a_hist_tx:603915"},
        ),
    ]

    result = collect_base_data.merge_ashare_market_bar_window_results(
        results,
        windows=[
            ("20160101", "20161231"),
            ("20170101", "20171231"),
        ],
    )

    assert result.status == "skipped"
    assert result.error_message == circuit_message
    assert result.payload["empty_windows"][0] == {
        "start": "20160101",
        "end": "20161231",
        "status": "unavailable",
        "item_count": 0,
        "raw_record_id": "raw:empty",
    }


def test_merge_ashare_market_bar_window_results_sums_available_windows() -> None:
    """多个年度窗口都成功时，应合并为可用结果并累计条数。"""

    collect_base_data = import_collection_module()
    results = [
        collect_base_data.CollectionTaskResult(
            task="ashare_p0_ohlcv",
            status="available",
            raw_record_id="raw:2024",
            item_count=240,
            error_message=None,
            payload={"actual_source": "tencent_kline"},
        ),
        collect_base_data.CollectionTaskResult(
            task="ashare_p0_ohlcv",
            status="available",
            raw_record_id="raw:2025",
            item_count=242,
            error_message=None,
            payload={"actual_source": "tencent_kline"},
        ),
    ]

    result = collect_base_data.merge_ashare_market_bar_window_results(
        results,
        windows=[
            ("20240101", "20241231"),
            ("20250101", "20251231"),
        ],
    )

    assert result.status == "available"
    assert result.item_count == 482
    assert result.error_message is None
    assert len(result.payload["completed_windows"]) == 2
    assert result.payload["failed_windows"] == []


def test_full_history_symbol_selection_detects_middle_year_gap_despite_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """即使水位覆盖 10 年窗口，中间整年缺失也必须重新入队补齐。"""

    collect_base_data = import_collection_module()
    start_at = datetime(2024, 1, 1, tzinfo=UTC)
    end_at = datetime(2026, 6, 12, tzinfo=UTC)

    class FakeAssetRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def find_by_market(self, market: str, *, only_tradable: bool = True) -> list[Any]:
            assert market == "ashare"
            return [Namespace(asset_id="ashare:000001", symbol="000001")]

    coverage = {
        "ashare:000001": (300, datetime(2024, 1, 2, tzinfo=UTC), end_at),
    }
    watermarks = {
        "ashare:000001": Namespace(
            status="available",
            next_retry_at=None,
            payload={"requested_start": "20240101", "requested_end": "20260612"},
        )
    }
    year_coverage = {
        "ashare:000001": {
            2024: (242, datetime(2024, 1, 2, tzinfo=UTC), datetime(2024, 12, 31, tzinfo=UTC)),
            2026: (100, datetime(2026, 1, 2, tzinfo=UTC), end_at),
        }
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
        "_fetch_ashare_bar_year_coverage",
        lambda session, asset_ids, timeframe, start_at, end_at: year_coverage,
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
        only_failed_or_stale=True,
        required_start_at=start_at,
        required_end_at=end_at,
    )

    assert symbols == ["000001"]


def test_close_final_symbol_selection_requires_latest_trading_day_despite_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """收盘最终日 K 应以真实 K 线最新交易日为准，不能只依赖请求水位跳过。"""

    collect_base_data = import_collection_module()
    request_start = datetime(2026, 1, 1, tzinfo=UTC)
    request_end = datetime(2026, 6, 12, tzinfo=UTC)

    class FakeAssetRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def find_by_market(self, market: str, *, only_tradable: bool = True) -> list[Any]:
            assert market == "ashare"
            return [
                Namespace(asset_id="ashare:000001", symbol="000001"),
                Namespace(asset_id="ashare:000002", symbol="000002"),
            ]

    monkeypatch.setattr(collect_base_data, "AssetRepository", FakeAssetRepository)
    monkeypatch.setattr(
        collect_base_data,
        "_fetch_ashare_bar_coverage",
        lambda session, asset_ids, timeframe: {
            "ashare:000001": (
                120,
                datetime(2026, 1, 2, tzinfo=UTC),
                datetime(2026, 6, 11, tzinfo=UTC),
            ),
            "ashare:000002": (
                121,
                datetime(2026, 1, 2, tzinfo=UTC),
                request_end,
            ),
        },
        raising=False,
    )
    monkeypatch.setattr(
        collect_base_data,
        "_fetch_data_sync_watermarks",
        lambda session, asset_ids, data_domain, provider, timeframe: {
            "ashare:000001": Namespace(
                status="available",
                next_retry_at=None,
                payload={
                    "requested_start": "20260101",
                    "requested_end": "20260612",
                },
            ),
            "ashare:000002": Namespace(
                status="available",
                next_retry_at=None,
                payload={
                    "requested_start": "20260101",
                    "requested_end": "20260612",
                },
            ),
        },
        raising=False,
    )

    symbols = collect_base_data.batch_ashare_symbols(
        object(),
        fallback_symbol="000001",
        only_failed_or_stale=True,
        required_start_at=request_start,
        required_end_at=request_end,
    )

    assert symbols == ["000001"]


def test_full_history_backfill_uses_planned_window_when_collecting_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """10 年初始化执行时应把单标的请求缩小到缺口年份窗口。"""

    collect_base_data = import_collection_module()
    runtime_parameters: list[dict[str, Any]] = []
    collector_calls: list[dict[str, Any]] = []

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
                runtime_parameters.append(parameters)
                collect()
            return Namespace(
                task=task,
                status="available",
                raw_record_id=None,
                item_count=1,
                error_message=None,
                payload={},
            )

    def fake_collect_ohlcv(self, **kwargs: Any) -> Any:
        collector_calls.append(kwargs)
        return Namespace(status="available", payload={})

    monkeypatch.setattr(
        collect_base_data,
        "should_refresh_asset_universe_before_incremental",
        lambda session, market: False,
    )
    monkeypatch.setattr(
        collect_base_data,
        "batch_ashare_symbols",
        lambda *args, **kwargs: ["000001"],
    )
    monkeypatch.setattr(
        collect_base_data,
        "plan_ashare_market_bar_backfill_windows",
        lambda *args, **kwargs: {"000001": [("20260101", "20260612")]},
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
        sync_task_type="market_bars_full_history_backfill",
        symbol_source="market_assets",
        ashare_start="20240101",
        ashare_end="20260612",
        batch_size=1,
    )

    collect_base_data.run_ashare_p0(object(), args, ExecutingRuntime())

    assert runtime_parameters[0]["start"] == "20260101"
    assert runtime_parameters[0]["end"] == "20260612"
    assert collector_calls[0]["start"] == "20260101"
    assert collector_calls[0]["end"] == "20260612"


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


def test_ashare_kline_source_gate_reports_eastmoney_cookie_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股东财 K 线源门禁应把 Cookie 健康状态写入任务监控。"""

    collect_base_data = import_collection_module()
    snapshots: list[dict[str, Any]] = []

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

    monkeypatch.setattr(collect_base_data, "COLLECTION_PROGRESS_RECORDER", FakeProgress())
    monkeypatch.setattr(
        collect_base_data,
        "eastmoney_kline_cookie_health_status",
        lambda: {
            "state": "cooling",
            "cooldown_remaining_seconds": 600,
            "last_error_message": "curl: (56) Connection closed abruptly",
        },
    )

    with pytest.raises(RuntimeError):
        collect_base_data.ashare_kline_source_gate(
            "eastmoney_kline",
            lambda: (_ for _ in ()).throw(RuntimeError("eastmoney unavailable")),
        )

    assert snapshots == [
        {
            "source_key": "eastmoney_kline_cookie",
            "snapshot": {
                "state": "cooling",
                "cooldown_remaining_seconds": 600,
                "last_error_message": "curl: (56) Connection closed abruptly",
                "failure_rate": 1.0,
                "effective_max_concurrency": 0,
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
