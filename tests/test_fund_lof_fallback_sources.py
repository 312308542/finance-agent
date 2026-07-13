from __future__ import annotations

from argparse import Namespace
from datetime import UTC, date, datetime
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


def test_fund_bar_close_final_symbol_resolution_uses_latest_gap_filter(monkeypatch) -> None:
    """基金收盘最终日 K 应按最近交易日缺口筛选，避免手工执行时全量重跑 ETF/LOF。"""

    captured: dict = {}

    def fake_batch_symbols(session, **kwargs):
        captured.update(kwargs)
        return ["160716"]

    monkeypatch.setattr(collect_base_data, "batch_fund_bar_symbols", fake_batch_symbols)
    monkeypatch.setattr(
        collect_base_data,
        "latest_ashare_trading_datetime",
        lambda session, end_at: datetime(2026, 6, 12, tzinfo=UTC),
        raising=False,
    )

    symbols = collect_base_data.resolve_fund_bar_collection_symbols(
        object(),
        Namespace(
            sync_task_type="market_bars_close_final",
            symbol_source="market_assets",
            source_limit=None,
            fund_symbol="160716",
            fund_timeframe="1d",
            only_failed_or_stale=True,
            ashare_start="20251214",
            ashare_end="20260614",
        ),
        asset_type="lof",
    )

    assert symbols == ["160716"]
    assert captured["only_failed_or_stale"] is True
    assert captured["required_start_at"] == datetime(2025, 12, 14, tzinfo=UTC)
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


def test_fund_bar_collection_trusts_verified_leading_gap() -> None:
    """已由全量请求验证过的基金成立前空窗，不应继续触发历史补采。"""

    asset = SimpleNamespace(asset_id="fund:lof:160716")
    watermark = SimpleNamespace(
        status="available",
        next_retry_at=None,
        payload={"requested_start": "20160612", "requested_end": "20260612"},
    )

    requires_collection = collect_base_data._asset_requires_fund_bar_collection(
        asset,
        coverage={
            "fund:lof:160716": (
                84,
                datetime(2026, 2, 3, tzinfo=UTC),
                datetime(2026, 6, 12, tzinfo=UTC),
            )
        },
        watermark=watermark,
        now=datetime(2026, 6, 12, 3, 30, tzinfo=UTC),
        stale_before=None,
        required_start_at=datetime(2016, 6, 12, tzinfo=UTC),
        required_end_at=datetime(2026, 6, 12, tzinfo=UTC),
        year_coverage={
            2026: (84, datetime(2026, 2, 3, tzinfo=UTC), datetime(2026, 6, 12, tzinfo=UTC)),
        },
    )

    assert requires_collection is False


def test_fund_bar_collection_detects_middle_year_gap() -> None:
    """基金 10 年日 K 覆盖判断应识别中间年份缺口，不能只看首尾日期。"""

    asset = SimpleNamespace(asset_id="fund:lof:160716")
    watermark = SimpleNamespace(status="available", next_retry_at=None)

    requires_collection = collect_base_data._asset_requires_fund_bar_collection(
        asset,
        coverage={
            "fund:lof:160716": (
                1200,
                datetime(2016, 6, 12, tzinfo=UTC),
                datetime(2026, 6, 12, tzinfo=UTC),
            )
        },
        watermark=watermark,
        now=datetime(2026, 6, 12, 3, 30, tzinfo=UTC),
        stale_before=None,
        required_start_at=datetime(2016, 6, 12, tzinfo=UTC),
        required_end_at=datetime(2026, 6, 12, tzinfo=UTC),
        year_coverage={
            2016: (120, datetime(2016, 6, 12, tzinfo=UTC), datetime(2016, 12, 30, tzinfo=UTC)),
            2018: (240, datetime(2018, 1, 2, tzinfo=UTC), datetime(2018, 12, 28, tzinfo=UTC)),
            2026: (110, datetime(2026, 1, 2, tzinfo=UTC), datetime(2026, 6, 12, tzinfo=UTC)),
        },
    )

    assert requires_collection is True


def test_fund_bar_collection_trusts_verified_trailing_gap() -> None:
    """完整请求已成功时，应信任源端最新日之后的尾部空窗。"""

    asset = SimpleNamespace(asset_id="fund:etf:510300")
    watermark = SimpleNamespace(
        status="available",
        next_retry_at=None,
        payload={"requested_start": "20160715", "requested_end": "20260713"},
    )
    year_coverage = {
        year: (
            1,
            datetime(year, 1, 2, tzinfo=UTC),
            datetime(year, 12, 30, tzinfo=UTC),
        )
        for year in range(2016, 2027)
    }

    requires_collection = collect_base_data._asset_requires_fund_bar_collection(
        asset,
        coverage={
            asset.asset_id: (
                2428,
                datetime(2016, 7, 15, tzinfo=UTC),
                datetime(2026, 7, 10, tzinfo=UTC),
            )
        },
        watermark=watermark,
        now=datetime(2026, 7, 13, 13, 0, tzinfo=UTC),
        stale_before=None,
        required_start_at=datetime(2016, 7, 15, tzinfo=UTC),
        required_end_at=datetime(2026, 7, 13, tzinfo=UTC),
        year_coverage=year_coverage,
    )

    assert requires_collection is False


def test_fund_bar_collection_keeps_verified_middle_year_gap() -> None:
    """完整请求水位只能信任边界空窗，中间整年缺口仍必须补采。"""

    asset = SimpleNamespace(asset_id="fund:etf:510300")
    watermark = SimpleNamespace(
        status="available",
        next_retry_at=None,
        payload={"requested_start": "20160715", "requested_end": "20260713"},
    )
    year_coverage = {
        year: (
            1,
            datetime(year, 1, 2, tzinfo=UTC),
            datetime(year, 12, 30, tzinfo=UTC),
        )
        for year in range(2016, 2027)
        if year != 2020
    }

    requires_collection = collect_base_data._asset_requires_fund_bar_collection(
        asset,
        coverage={
            asset.asset_id: (
                2200,
                datetime(2016, 7, 15, tzinfo=UTC),
                datetime(2026, 7, 10, tzinfo=UTC),
            )
        },
        watermark=watermark,
        now=datetime(2026, 7, 13, 13, 0, tzinfo=UTC),
        stale_before=None,
        required_start_at=datetime(2016, 7, 15, tzinfo=UTC),
        required_end_at=datetime(2026, 7, 13, tzinfo=UTC),
        year_coverage=year_coverage,
    )

    assert requires_collection is True


def test_fund_coverage_queries_split_large_asset_sets() -> None:
    """基金覆盖查询应分块，避免大候选池耗尽 PostgreSQL 动态共享内存。"""

    executed_batch_sizes: list[int] = []

    class RecordingSession:
        def execute(self, statement):
            list_params = [
                value
                for value in statement.compile().params.values()
                if isinstance(value, (list, tuple))
            ]
            executed_batch_sizes.append(len(list_params[0]))
            return []

    session = RecordingSession()
    asset_ids = [f"fund:etf:{index:06d}" for index in range(451)]
    start_at = datetime(2016, 6, 12, tzinfo=UTC)
    end_at = datetime(2026, 6, 12, tzinfo=UTC)

    collect_base_data._fetch_fund_bar_coverage(session, asset_ids, timeframe="1d")
    collect_base_data._fetch_fund_bar_year_coverage(
        session,
        asset_ids,
        timeframe="1d",
        start_at=start_at,
        end_at=end_at,
    )
    collect_base_data._fetch_fund_nav_coverage(session, asset_ids)
    collect_base_data._fetch_fund_nav_year_coverage(
        session,
        asset_ids,
        start_at=start_at,
        end_at=end_at,
    )

    assert executed_batch_sizes == [200, 200, 51] * 4


def test_open_fund_nav_collection_trusts_verified_leading_gap() -> None:
    """已由全量请求验证过的开放式基金成立前空窗，不应继续触发净值补采。"""

    asset = SimpleNamespace(asset_id="fund:open:000001")
    watermark = SimpleNamespace(
        status="available",
        next_retry_at=None,
        payload={"requested_start": "20160612", "requested_end": "20260612"},
    )

    requires_collection = collect_base_data._asset_requires_open_nav_collection(
        asset,
        coverage={
            "fund:open:000001": (
                84,
                date(2026, 2, 3),
                date(2026, 6, 12),
            )
        },
        watermark=watermark,
        now=datetime(2026, 6, 12, 3, 30, tzinfo=UTC),
        stale_before=None,
        required_start_at=datetime(2016, 6, 12, tzinfo=UTC),
        required_end_at=datetime(2026, 6, 12, tzinfo=UTC),
        year_coverage={
            2026: (84, date(2026, 2, 3), date(2026, 6, 12)),
        },
    )

    assert requires_collection is False


def test_open_fund_nav_collection_trusts_verified_trailing_gap() -> None:
    """完整净值请求已成功时，应信任源端最新净值日之后的尾部空窗。"""

    asset = SimpleNamespace(asset_id="fund:open:000001")
    watermark = SimpleNamespace(
        status="available",
        next_retry_at=None,
        payload={"requested_start": "20160715", "requested_end": "20260713"},
    )
    year_coverage = {
        year: (1, date(year, 1, 2), date(year, 12, 30)) for year in range(2016, 2027)
    }

    requires_collection = collect_base_data._asset_requires_open_nav_collection(
        asset,
        coverage={asset.asset_id: (2400, date(2016, 7, 15), date(2026, 7, 10))},
        watermark=watermark,
        now=datetime(2026, 7, 13, 13, 0, tzinfo=UTC),
        stale_before=None,
        required_start_at=datetime(2016, 7, 15, tzinfo=UTC),
        required_end_at=datetime(2026, 7, 13, tzinfo=UTC),
        year_coverage=year_coverage,
    )

    assert requires_collection is False


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
    assert captured["required_start_at"] == datetime(2016, 6, 12, tzinfo=UTC)


def test_open_fund_nav_full_history_symbol_resolution_passes_required_range(monkeypatch) -> None:
    """开放式基金净值初始化应传入完整起止范围，避免只按最新净值误判完成。"""

    captured: dict = {}

    def fake_batch_symbols(session, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(collect_base_data, "batch_open_fund_nav_symbols", fake_batch_symbols)

    collect_base_data.resolve_fund_nav_collection_symbols(
        object(),
        Namespace(
            sync_task_type="fund_nav_full_history_backfill",
            symbol_source="market_assets",
            source_limit=20,
            fund_symbol="000001",
            only_failed_or_stale=False,
            ashare_start="20160612",
            ashare_end="20260612",
        ),
    )

    assert captured["required_start_at"] == datetime(2016, 6, 12, tzinfo=UTC)
    assert captured["required_end_at"] == datetime(2026, 6, 12, tzinfo=UTC)


def test_open_fund_nav_collection_checks_earliest_latest_and_middle_years() -> None:
    """开放式基金净值覆盖判断应同时检查起止日期和中间年份缺口。"""

    asset = SimpleNamespace(asset_id="fund:open:000001")
    watermark = SimpleNamespace(status="available", next_retry_at=None)

    requires_collection = collect_base_data._asset_requires_open_nav_collection(
        asset,
        coverage={
            "fund:open:000001": (
                1200,
                date(2016, 6, 12),
                date(2026, 6, 12),
            )
        },
        watermark=watermark,
        now=datetime(2026, 6, 12, 3, 30, tzinfo=UTC),
        stale_before=None,
        required_start_at=datetime(2016, 6, 12, tzinfo=UTC),
        required_end_at=datetime(2026, 6, 12, tzinfo=UTC),
        year_coverage={
            2016: (160, date(2016, 6, 12), date(2016, 12, 30)),
            2018: (240, date(2018, 1, 2), date(2018, 12, 28)),
            2026: (110, date(2026, 1, 2), date(2026, 6, 12)),
        },
    )

    assert requires_collection is True


def test_open_fund_nav_daily_symbol_resolution_enables_gap_filter(monkeypatch) -> None:
    """开放式基金每日净值维护应只筛选最近窗口缺失或失败冷却已结束的基金。"""

    captured: dict = {}

    def fake_batch_symbols(session, **kwargs):
        captured.update(kwargs)
        return ["000001"]

    monkeypatch.setattr(collect_base_data, "batch_open_fund_nav_symbols", fake_batch_symbols)

    symbols = collect_base_data.resolve_fund_nav_collection_symbols(
        object(),
        Namespace(
            sync_task_type="fund_nav_daily",
            symbol_source="market_assets",
            source_limit=None,
            fund_symbol="000001",
            only_failed_or_stale=True,
            ashare_start="20260515",
        ),
    )

    assert symbols == ["000001"]
    assert captured["only_failed_or_stale"] is True
    assert captured["stale_before"] == datetime(2026, 5, 15, tzinfo=UTC)


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


def test_fund_success_watermark_persists_full_history_request_range(monkeypatch) -> None:
    """基金全历史成功水位应保存请求窗口，允许信任成立前空窗。"""

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
    result = CollectionTaskResult(
        task="fund_etf_ohlcv",
        status="available",
        raw_record_id="raw:etf",
        item_count=2428,
        error_message=None,
        payload={"latest_at": "2026-06-12T00:00:00+00:00"},
    )

    collect_base_data._record_fund_symbol_watermark(
        object(),
        symbol="510300",
        asset_type="etf",
        data_domain=collect_base_data.FUND_MARKET_BAR_DATA_DOMAIN,
        provider="akshare:fund_etf_hist_em",
        timeframe="1d",
        result=result,
        requested_start="20160715",
        requested_end="20260713",
        sync_task_type="market_bars_full_history_backfill",
    )

    assert recorded["payload"] == {
        "item_count": 2428,
        "latest_count": 2428,
        "requested_start": "20160715",
        "requested_end": "20260713",
        "sync_task_type": "market_bars_full_history_backfill",
    }
