from __future__ import annotations

from argparse import Namespace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import requests

from scripts.data import collect_base_data
from finance_agent.data.collection_runtime import CollectionTaskResult
from finance_agent.data.providers.akshare_fund_provider import AkshareFundProvider
from finance_agent.data.providers.ashare_kline_sources import fetch_tencent_kline_direct


def test_tencent_direct_parses_lof_jsonp_sample(monkeypatch) -> None:
    """腾讯直连源应能解析带交易所前缀的 LOF JSONP K 线样例。"""

    calls: list[dict] = []

    class FakeResponse:
        text = (
            'kline_dayqfq2026={"code":0,"msg":"","data":{"sz160716":'
            '{"qfqday":[["2026-06-05","1.20","1.23","1.24","1.19","12345"]]}}}'
        )

        def raise_for_status(self) -> None:
            return None

    def fake_get(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return FakeResponse()

    monkeypatch.setattr(
        "finance_agent.data.providers.ashare_kline_sources.requests.get",
        fake_get,
    )

    frame = fetch_tencent_kline_direct(
        symbol="sz160716",
        start="20260601",
        end="20260606",
        adjust="qfq",
        timeout=7.0,
    )

    assert len(frame) == 1
    assert frame.attrs["source"] == "tencent:direct:kline"
    assert calls[0]["timeout"] == 7.0
    assert "sz160716,day,2026-06-01,2026-06-06,640,qfq" in calls[0]["params"]["param"]
    assert frame.iloc[0]["close"] == 1.23


def test_tencent_direct_retries_transient_connection_reset(monkeypatch) -> None:
    """腾讯直连单个窗口遇到连接重置时应轻量重试，避免整段 10 年任务失败。"""

    calls: list[int] = []
    sleeps: list[float] = []

    class FakeResponse:
        text = (
            'kline_dayqfq2026={"code":0,"msg":"","data":{"sz160716":'
            '{"qfqday":[["2026-06-05","1.20","1.23","1.24","1.19","12345"]]}}}'
        )

        def raise_for_status(self) -> None:
            return None

    def fake_get(url, **kwargs):
        calls.append(len(calls) + 1)
        if len(calls) < 3:
            raise requests.ConnectionError("connection reset")
        return FakeResponse()

    monkeypatch.setattr(
        "finance_agent.data.providers.ashare_kline_sources.requests.get",
        fake_get,
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.ashare_kline_sources.time.sleep",
        lambda seconds: sleeps.append(seconds),
    )

    frame = fetch_tencent_kline_direct(
        symbol="sz160716",
        start="20260601",
        end="20260606",
        adjust="qfq",
        timeout=7.0,
    )

    assert calls == [1, 2, 3]
    assert sleeps == [0.5, 0.5]
    assert len(frame) == 1
    assert frame.iloc[0]["close"] == 1.23


def test_fund_lof_hist_falls_back_to_tencent_direct_when_eastmoney_fails(monkeypatch) -> None:
    """LOF 历史日 K 主源失败时应自动回退腾讯直连源，并记录降级尝试。"""

    calls: list[str] = []

    def fake_hist_em(**kwargs):
        calls.append("em")
        raise RuntimeError("eastmoney timeout")

    def fake_tencent(**kwargs):
        calls.append("tencent")
        assert kwargs["symbol"] == "sz160716"
        assert kwargs["start"] == "20260601"
        assert kwargs["end"] == "20260606"
        return pd.DataFrame(
            [
                {
                    "date": "2026-06-05",
                    "open": 1.20,
                    "high": 1.24,
                    "low": 1.19,
                    "close": 1.23,
                    "amount": 7654321,
                }
            ]
        )

    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_fund_provider.ak.fund_lof_hist_em",
        fake_hist_em,
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_fund_provider.fetch_tencent_kline_direct",
        fake_tencent,
    )

    result = AkshareFundProvider().fetch_lof_ohlcv(
        symbol="160716",
        start_date="20260601",
        end_date="20260606",
        limit=10,
    )

    assert result.status == "available"
    assert calls == ["em", "tencent"]
    assert result.payload["endpoint"] == "tencent:direct:kline"
    assert result.payload["fallback_used"] is True
    assert result.payload["provider_chain"]["source"] == "tencent:direct:kline"
    assert [attempt["status"] for attempt in result.payload["provider_chain"]["attempts"]] == ["error", "ok"]
    assert result.bars[0].asset_id == "fund:lof:160716"
    assert result.bars[0].source == "tencent:direct:kline"
    assert result.bars[0].close == Decimal("1.23")


def test_fund_bar_symbol_resolution_uses_source_limit(monkeypatch) -> None:
    """基金日 K 分批放量应使用 source_limit 限制本轮标的数量。"""

    captured: dict = {}

    def fake_batch_symbols(session, **kwargs):
        captured.update(kwargs)
        return ["160716", "161005"]

    monkeypatch.setattr(collect_base_data, "batch_fund_bar_symbols", fake_batch_symbols)

    symbols = collect_base_data.resolve_fund_bar_collection_symbols(
        object(),
        Namespace(
            symbol_source="market_assets",
            source_limit=10,
            fund_symbol="160716",
            fund_timeframe="1d",
            only_failed_or_stale=False,
            ashare_start=None,
        ),
        asset_type="lof",
    )

    assert captured["limit"] == 10
    assert symbols == ["160716", "161005"]


def test_fund_bar_full_history_symbol_resolution_enables_gap_filter(monkeypatch) -> None:
    """基金 10 年日 K 初始化应默认按缺口和失败水位筛选，支持断点续跑。"""

    captured: dict = {}

    def fake_batch_symbols(session, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(collect_base_data, "batch_fund_bar_symbols", fake_batch_symbols)

    symbols = collect_base_data.resolve_fund_bar_collection_symbols(
        object(),
        Namespace(
            sync_task_type="market_bars_full_history_backfill",
            symbol_source="market_assets",
            source_limit=10,
            fund_symbol="160716",
            fund_timeframe="1d",
            only_failed_or_stale=False,
            ashare_start="20160612",
            ashare_end="20260612",
        ),
        asset_type="lof",
    )

    assert symbols == []
    assert captured["only_failed_or_stale"] is True
    assert captured["required_start_at"] == datetime(2016, 6, 12, tzinfo=UTC)
    assert captured["required_end_at"] == datetime(2026, 6, 12, tzinfo=UTC)


def test_fund_bar_collection_range_check_uses_earliest_and_latest_coverage() -> None:
    """基金 10 年日 K 覆盖判断应同时检查起止日期，不能只看最新日期。"""

    asset = SimpleNamespace(asset_id="fund:lof:160716")
    watermark = SimpleNamespace(status="available", next_retry_at=None)

    requires_collection = collect_base_data._asset_requires_fund_bar_collection(
        asset,
        coverage={
            "fund:lof:160716": (
                200,
                datetime(2024, 1, 1, tzinfo=UTC),
                datetime(2026, 6, 12, tzinfo=UTC),
            )
        },
        watermark=watermark,
        now=datetime(2026, 6, 12, 3, 30, tzinfo=UTC),
        stale_before=None,
        required_start_at=datetime(2016, 6, 12, tzinfo=UTC),
        required_end_at=datetime(2026, 6, 12, tzinfo=UTC),
    )

    assert requires_collection is True


def test_open_fund_nav_symbol_resolution_uses_source_limit(monkeypatch) -> None:
    """开放式基金净值分批放量应使用 source_limit 限制本轮标的数量。"""

    captured: dict = {}

    def fake_batch_symbols(session, **kwargs):
        captured.update(kwargs)
        return ["000001", "000002"]

    monkeypatch.setattr(collect_base_data, "batch_open_fund_nav_symbols", fake_batch_symbols)

    symbols = collect_base_data.resolve_fund_nav_collection_symbols(
        object(),
        Namespace(
            symbol_source="market_assets",
            source_limit=20,
            fund_symbol="000001",
            only_failed_or_stale=False,
            ashare_start=None,
        ),
    )

    assert captured["limit"] == 20
    assert symbols == ["000001", "000002"]


def test_open_fund_nav_full_history_symbol_resolution_enables_gap_filter(monkeypatch) -> None:
    """开放式基金 10 年净值初始化应默认按缺口和失败水位筛选。"""

    captured: dict = {}

    def fake_batch_symbols(session, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(collect_base_data, "batch_open_fund_nav_symbols", fake_batch_symbols)

    symbols = collect_base_data.resolve_fund_nav_collection_symbols(
        object(),
        Namespace(
            sync_task_type="fund_nav_full_history_backfill",
            symbol_source="market_assets",
            source_limit=20,
            fund_symbol="000001",
            only_failed_or_stale=False,
            ashare_start="20160612",
        ),
    )

    assert symbols == []
    assert captured["only_failed_or_stale"] is True
    assert captured["stale_before"] == datetime(2016, 6, 12, tzinfo=UTC)


def test_fund_success_watermark_uses_summary_latest_at_when_coverage_is_not_visible(monkeypatch) -> None:
    """基金成功水位应优先使用采集摘要日期，避免独立事务读不到刚写入的事实数据。"""

    recorded: dict = {}

    class FakeWatermarkRepository:
        def __init__(self, session) -> None:
            self.session = session

        def record_success(self, **kwargs) -> None:
            recorded.update(kwargs)

        def record_failure(self, **kwargs) -> None:  # pragma: no cover - 本测试只覆盖成功路径
            raise AssertionError("不应写失败水位")

    monkeypatch.setattr(
        collect_base_data,
        collect_base_data.DataSyncWatermarkRepository.__name__,
        FakeWatermarkRepository,
    )
    monkeypatch.setattr(collect_base_data, "_fetch_fund_bar_coverage", lambda *args, **kwargs: {})

    result = CollectionTaskResult(
        task="fund_lof_ohlcv",
        status="available",
        raw_record_id="raw:lof",
        item_count=2428,
        error_message=None,
        payload={"latest_at": "2026-06-12T00:00:00+00:00"},
    )

    collect_base_data._record_fund_symbol_watermark(
        object(),
        symbol="160716",
        asset_type="lof",
        data_domain=collect_base_data.FUND_MARKET_BAR_DATA_DOMAIN,
        provider="akshare:fund_lof_hist_em",
        timeframe="1d",
        result=result,
        occurred_at=datetime(2026, 6, 12, 3, 30, tzinfo=UTC),
    )

    assert recorded["asset_id"] == "fund:lof:160716"
    assert recorded["watermark_at"] == datetime(2026, 6, 12, tzinfo=UTC)
    assert recorded["payload"]["item_count"] == 2428
