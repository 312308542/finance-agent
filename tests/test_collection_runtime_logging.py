from datetime import UTC, datetime
import logging

from finance_agent.cache.null_cache import NullCacheClient
from finance_agent.data.collection_runtime import (
    CollectionRuntime,
    ProviderCircuitPolicy,
    provider_state_key,
    summarize_archive,
)
from finance_agent.data.collectors import ArchivedProviderResult
from finance_agent.data.models import (
    AssetData,
    AssetListResult,
    EventRecordData,
    MarketBarData,
    MarketBarsResult,
    ProviderResult,
    RiskFindingsResult,
)


class RecordingRuntimeCache(NullCacheClient):
    """记录运行时 Provider 状态，便于验证熔断计数。"""

    def __init__(self) -> None:
        self.values = {}

    def get_json(self, key):
        return self.values.get(key)

    def set_json(self, key, value, *, ttl_seconds=None) -> None:
        self.values[key] = value


def test_collection_runtime_logs_console_progress(caplog) -> None:
    """采集任务应通过标准 logging 输出开始和写库摘要，便于控制台观察。"""

    runtime = CollectionRuntime(cache=NullCacheClient(), locks=NullCacheClient())

    def collect() -> ArchivedProviderResult:
        return ArchivedProviderResult(
            result=ProviderResult(
                provider_name="unit-provider",
                status="available",
                collected_at=datetime.now(tz=UTC),
                payload={"actual_source": "unit-source"},
            ),
            raw_record_id="raw:unit",
        )

    caplog.set_level(logging.INFO, logger="finance_agent.data.collection_runtime")

    result = runtime.run_task(
        task="unit_task",
        provider_key="unit_provider",
        parameters={"symbol": "000001"},
        collect=collect,
    )

    messages = [record.getMessage() for record in caplog.records]
    assert result.status == "available"
    assert any("采集任务开始" in message and "task=unit_task" in message for message in messages)
    assert any(
        "采集任务完成" in message
        and "task=unit_task" in message
        and "status=available" in message
        and "raw_record_id=raw:unit" in message
        for message in messages
    )


def test_collection_runtime_does_not_open_circuit_for_unavailable_empty_window() -> None:
    """K 线早期年度无数据属于空窗口，不应累计 Provider 熔断失败次数。"""

    cache = RecordingRuntimeCache()
    provider_key = "stock_zh_a_hist_tx:603915"
    runtime = CollectionRuntime(
        cache=cache,
        locks=NullCacheClient(),
        circuit_policy=ProviderCircuitPolicy(failure_threshold=1),
    )

    def collect() -> ArchivedProviderResult:
        return ArchivedProviderResult(
            result=ProviderResult(
                provider_name="unit-provider",
                status="unavailable",
                collected_at=datetime.now(tz=UTC),
                error_message=None,
                payload={"actual_source": "tencent:direct:kline"},
            ),
            raw_record_id="raw:empty-window",
        )

    result = runtime.run_task(
        task="ashare_p0_ohlcv",
        provider_key=provider_key,
        parameters={"symbol": "603915", "start": "20160101", "end": "20161231"},
        collect=collect,
    )

    assert result.status == "unavailable"
    provider_state = cache.values[provider_state_key(provider_key)]
    assert provider_state["status"] == "closed"
    assert provider_state["failure_count"] == 0


def test_summarize_archive_merges_multiple_provider_archives() -> None:
    """多 Provider 资产池采集应能合并多个 raw_records 摘要，避免运行期按单对象取值报错。"""

    collected_at = datetime.now(tz=UTC)
    archives = [
        ArchivedProviderResult(
            result=AssetListResult(
                provider_name="fund-etf",
                status="available",
                collected_at=collected_at,
                payload={"actual_source": "akshare:fund_etf_spot_em"},
                assets=[
                    AssetData(
                        asset_id="fund:etf:510300",
                        symbol="510300",
                        name="沪深300ETF",
                        market="fund",
                        asset_type="etf",
                    )
                ],
            ),
            raw_record_id="raw:etf",
        ),
        ArchivedProviderResult(
            result=AssetListResult(
                provider_name="fund-lof",
                status="available",
                collected_at=collected_at,
                payload={"actual_source": "akshare:fund_lof_spot_em"},
                assets=[
                    AssetData(
                        asset_id="fund:lof:160716",
                        symbol="160716",
                        name="嘉实基本面50",
                        market="fund",
                        asset_type="lof",
                    )
                ],
            ),
            raw_record_id="raw:lof",
        ),
        ArchivedProviderResult(
            result=AssetListResult(
                provider_name="fund-open",
                status="unavailable",
                collected_at=collected_at,
                payload={"actual_source": "akshare:fund_open_fund_info_em"},
                error_message="接口临时不可用",
            ),
            raw_record_id="raw:open",
        ),
    ]

    summary = summarize_archive("fund_universe", archives)

    assert summary.status == "available"
    assert summary.raw_record_id is None
    assert summary.item_count == 2
    assert summary.error_message is None
    assert summary.payload["raw_record_ids"] == ["raw:etf", "raw:lof", "raw:open"]
    assert summary.payload["actual_source"] == [
        "akshare:fund_etf_spot_em",
        "akshare:fund_lof_spot_em",
    ]
    assert summary.payload["source_results"] == [
        {
            "provider_name": "fund-etf",
            "status": "available",
            "raw_record_id": "raw:etf",
            "item_count": 1,
            "actual_source": "akshare:fund_etf_spot_em",
            "error_message": None,
        },
        {
            "provider_name": "fund-lof",
            "status": "available",
            "raw_record_id": "raw:lof",
            "item_count": 1,
            "actual_source": "akshare:fund_lof_spot_em",
            "error_message": None,
        },
        {
            "provider_name": "fund-open",
            "status": "unavailable",
            "raw_record_id": "raw:open",
            "item_count": 0,
            "actual_source": "akshare:fund_open_fund_info_em",
            "error_message": "接口临时不可用",
        },
    ]


def test_summarize_archive_accepts_lightweight_single_archive_wrapper() -> None:
    """内部日历任务的轻量归档包装应按单个 Provider 结果摘要处理。"""

    class LightweightArchive:
        def __init__(self) -> None:
            self.result = ProviderResult(
                provider_name="tool_trade_date_hist_sina",
                status="available",
                collected_at=datetime.now(tz=UTC),
                payload={"actual_source": "akshare:tool_trade_date_hist_sina"},
            )
            self.raw_record_id = None

    summary = summarize_archive("ashare_p0_calendar", LightweightArchive())

    assert summary.status == "available"
    assert summary.raw_record_id is None
    assert summary.item_count == 0
    assert summary.payload["actual_source"] == "akshare:tool_trade_date_hist_sina"


def test_summarize_archive_includes_latest_timestamp_for_market_bars() -> None:
    """K 线摘要应携带最新时间，供水位写入在跨事务场景下断点续跑。"""

    collected_at = datetime.now(tz=UTC)
    archive = ArchivedProviderResult(
        result=MarketBarsResult(
            provider_name="fund-lof",
            status="available",
            collected_at=collected_at,
            bars=[
                MarketBarData(
                    asset_id="fund:lof:160716",
                    symbol="160716",
                    market="fund",
                    timeframe="1d",
                    timestamp=datetime(2026, 6, 11, tzinfo=UTC),
                    open_price=1,
                    high=1,
                    low=1,
                    close=1,
                    volume=1,
                    source="akshare:fund_lof_hist_em",
                ),
                MarketBarData(
                    asset_id="fund:lof:160716",
                    symbol="160716",
                    market="fund",
                    timeframe="1d",
                    timestamp=datetime(2026, 6, 12, tzinfo=UTC),
                    open_price=1,
                    high=1,
                    low=1,
                    close=1,
                    volume=1,
                    source="akshare:fund_lof_hist_em",
                ),
            ],
        ),
        raw_record_id="raw:lof",
    )

    summary = summarize_archive("fund_lof_ohlcv", archive)

    assert summary.payload["earliest_at"] == "2026-06-11T00:00:00+00:00"
    assert summary.payload["latest_at"] == "2026-06-12T00:00:00+00:00"


def test_summarize_archive_counts_risk_result_events_when_no_risks() -> None:
    """风险结果只有事件时，摘要数量也应反映已落库事件数。"""

    collected_at = datetime.now(tz=UTC)
    archive = ArchivedProviderResult(
        result=RiskFindingsResult(
            provider_name="akshare",
            status="available",
            collected_at=collected_at,
            events=[
                EventRecordData(
                    event_id="event:restricted:600750",
                    market="ashare",
                    event_type="restricted_release",
                    title="华润江中(600750) 限售股解禁",
                    source="akshare:stock_restricted_release_detail_em",
                    collected_at=collected_at,
                ),
                EventRecordData(
                    event_id="event:restricted:603286",
                    market="ashare",
                    event_type="restricted_release",
                    title="日盈电子(603286) 限售股解禁",
                    source="akshare:stock_restricted_release_detail_em",
                    collected_at=collected_at,
                ),
            ],
        ),
        raw_record_id="raw:restricted",
    )

    summary = summarize_archive("ashare_risk_restricted_release", archive)

    assert summary.item_count == 2
