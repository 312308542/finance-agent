from __future__ import annotations

from decimal import Decimal

import pandas as pd
import requests

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
