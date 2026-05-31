import pandas as pd

from finance_agent.data.providers.akshare_provider import AkshareProvider


def test_akshare_hist_requests_use_finite_timeout(monkeypatch) -> None:
    """AKShare A 股 K 线请求必须传有限超时，避免上游挂住拖死调度器。"""

    eastmoney_calls: list[dict] = []
    tencent_calls: list[dict] = []

    def fake_stock_zh_a_hist(**kwargs):
        eastmoney_calls.append(kwargs)
        return pd.DataFrame()

    def fake_stock_zh_a_hist_tx(**kwargs):
        tencent_calls.append(kwargs)
        return pd.DataFrame()

    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_provider.ak.stock_zh_a_hist",
        fake_stock_zh_a_hist,
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_provider.ak.stock_zh_a_hist_tx",
        fake_stock_zh_a_hist_tx,
    )

    provider = AkshareProvider(request_timeout_seconds=7.0)

    provider._fetch_ohlcv_eastmoney(
        symbol="000552",
        timeframe="1d",
        start="20260428",
        end="20260528",
        adjust="qfq",
    )
    provider._fetch_ohlcv_tencent(
        symbol="000552",
        start="20260428",
        end="20260528",
        adjust="qfq",
    )

    assert eastmoney_calls[0]["timeout"] == 7.0
    assert tencent_calls[0]["timeout"] == 7.0


def test_fetch_ohlcv_prefers_tencent_before_eastmoney(monkeypatch) -> None:
    """A 股 K 线优先使用腾讯源，避免东方财富接口不可用时阻塞采集。"""

    calls: list[str] = []

    def fake_tencent(**kwargs):
        calls.append("tencent")
        return pd.DataFrame(
            [
                {
                    "date": "2026-05-28",
                    "open": "10.0",
                    "close": "10.5",
                    "high": "10.8",
                    "low": "9.9",
                    "amount": "1000",
                }
            ]
        )

    def fake_eastmoney(**kwargs):
        calls.append("eastmoney")
        raise AssertionError("腾讯源成功时不应调用东方财富源")

    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_provider.ak.stock_zh_a_hist_tx",
        fake_tencent,
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_provider.ak.stock_zh_a_hist",
        fake_eastmoney,
    )

    provider = AkshareProvider(request_timeout_seconds=7.0)
    result = provider.fetch_ohlcv(
        symbol="000001",
        timeframe="1d",
        start="20260528",
        end="20260528",
        adjust="qfq",
    )

    assert result.status == "available"
    assert calls == ["tencent"]
    assert result.payload["primary_source"] == "akshare:stock_zh_a_hist_tx"
    assert result.payload["actual_source"] == "akshare:stock_zh_a_hist_tx"
    assert result.payload["fallback_used"] is False


def test_fetch_ohlcv_falls_back_to_eastmoney_when_tencent_fails(monkeypatch) -> None:
    """腾讯源失败时再回退东方财富，保留备用采集能力。"""

    calls: list[str] = []

    def fake_tencent(**kwargs):
        calls.append("tencent")
        raise RuntimeError("tencent unavailable")

    def fake_eastmoney(**kwargs):
        calls.append("eastmoney")
        return pd.DataFrame(
            [
                {
                    "日期": "2026-05-28",
                    "开盘": "10.0",
                    "收盘": "10.5",
                    "最高": "10.8",
                    "最低": "9.9",
                    "成交量": "1000",
                    "成交额": "10000",
                }
            ]
        )

    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_provider.ak.stock_zh_a_hist_tx",
        fake_tencent,
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_provider.ak.stock_zh_a_hist",
        fake_eastmoney,
    )

    provider = AkshareProvider(request_timeout_seconds=7.0)
    result = provider.fetch_ohlcv(
        symbol="000001",
        timeframe="1d",
        start="20260528",
        end="20260528",
        adjust="qfq",
    )

    assert result.status == "available"
    assert calls == ["tencent", "eastmoney"]
    assert result.payload["primary_source"] == "akshare:stock_zh_a_hist_tx"
    assert result.payload["actual_source"] == "akshare:stock_zh_a_hist"
    assert result.payload["fallback_used"] is True
    assert result.payload["fallback_trace"][0]["source"] == "akshare:stock_zh_a_hist_tx"
