import threading
import time
from pathlib import Path

import pandas as pd

from finance_agent.data.providers.akshare_fund_provider import AkshareFundProvider
from finance_agent.data.providers import eastmoney_curl
from finance_agent.data.providers.akshare_provider import AkshareProvider
from finance_agent.data.providers.ashare_kline_sources import (
    fetch_tencent_kline_direct,
)


def test_akshare_hist_requests_use_finite_timeout(monkeypatch) -> None:
    """AKShare A 股 K 线请求必须传有限超时，避免上游挂住拖死调度器。"""

    eastmoney_calls: list[dict] = []
    tencent_calls: list[dict] = []

    def fake_stock_zh_a_hist(**kwargs):
        eastmoney_calls.append(kwargs)
        return pd.DataFrame()

    class FakeTencentResponse:
        @property
        def text(self) -> str:
            return (
                'kline_dayqfq2026={"code":0,"msg":"","data":{"sz000552":'
                '{"qfqday":[["2026-05-28","10.0","10.5","10.8","9.9","1000"]]}}}'
            )

        def raise_for_status(self) -> None:
            return None

    def fake_tencent_get(url, **kwargs):
        tencent_calls.append(kwargs)
        return FakeTencentResponse()

    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_provider.ak.stock_zh_a_hist",
        fake_stock_zh_a_hist,
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.ashare_kline_sources.requests.get",
        fake_tencent_get,
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


def test_fund_etf_hist_falls_back_to_sina_when_eastmoney_fails(monkeypatch) -> None:
    """ETF 历史日 K 主源失败时应自动回退新浪备源。"""

    calls: list[str] = []

    def fake_hist_em(**kwargs):
        calls.append("em")
        raise RuntimeError("eastmoney unavailable")

    def fake_hist_sina(**kwargs):
        calls.append("sina")
        return pd.DataFrame(
            [
                {
                    "date": "2026-06-05",
                    "open": 3.80,
                    "high": 3.95,
                    "low": 3.78,
                    "close": 3.91,
                    "volume": 123456,
                    "amount": 4567890,
                }
            ]
        )

    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_fund_provider.ak.fund_etf_hist_em",
        fake_hist_em,
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_fund_provider.ak.fund_etf_hist_sina",
        fake_hist_sina,
    )

    result = AkshareFundProvider().fetch_etf_ohlcv(
        symbol="510300",
        start_date="20260601",
        end_date="20260606",
        limit=10,
    )

    assert result.status == "available"
    assert calls == ["em", "sina"]
    assert result.payload["endpoint"] == "fund_etf_hist_sina"
    assert result.payload["fallback_used"] is True
    assert result.bars[0].source == "akshare:fund_etf_hist_sina"


def test_fetch_ohlcv_prefers_eastmoney_direct_after_benchmark(monkeypatch) -> None:
    """A 股 K 线优先使用东方财富 Direct，便于复用 Cookie 和精确日期范围。"""

    calls: list[str] = []

    def fake_tencent(**kwargs):
        calls.append("tencent")
        raise AssertionError("东方财富 Direct 成功时不应调用腾讯源")

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
        "finance_agent.data.providers.akshare_provider.fetch_tencent_kline_direct",
        fake_tencent,
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_provider.fetch_eastmoney_kline_direct",
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
    assert calls == ["eastmoney"]
    assert result.payload["primary_source"] == "eastmoney:direct:kline"
    assert result.payload["actual_source"] == "eastmoney:direct:kline"
    assert result.payload["fallback_used"] is False


def test_fetch_ohlcv_falls_back_to_tencent_when_eastmoney_fails(monkeypatch) -> None:
    """东方财富 Direct 失败时再回退腾讯 Direct，保留无 Cookie 备用采集能力。"""

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
        raise RuntimeError("eastmoney unavailable")

    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_provider.fetch_tencent_kline_direct",
        fake_tencent,
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_provider.fetch_eastmoney_kline_direct",
        fake_eastmoney,
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
    assert calls == ["eastmoney", "tencent"]
    assert result.payload["primary_source"] == "eastmoney:direct:kline"
    assert result.payload["actual_source"] == "tencent:direct:kline"
    assert result.payload["fallback_used"] is True
    assert result.payload["fallback_trace"][0]["source"] == "eastmoney:direct:kline"


def test_fetch_ohlcv_uses_independent_source_gate_keys(monkeypatch) -> None:
    """K 线直连源应按真实上游拆分限流 key，避免东财和腾讯共用一个源状态。"""

    gate_calls: list[str] = []

    def fake_tencent(**kwargs):
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
        raise RuntimeError("eastmoney unavailable")

    def source_gate(source_key, loader):
        gate_calls.append(source_key)
        return loader()

    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_provider.fetch_tencent_kline_direct",
        fake_tencent,
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_provider.fetch_eastmoney_kline_direct",
        fake_eastmoney,
    )

    result = AkshareProvider(request_timeout_seconds=7.0).fetch_ohlcv(
        symbol="000001",
        timeframe="1d",
        start="20260528",
        end="20260528",
        adjust="qfq",
        source_gate=source_gate,
    )

    assert result.status == "available"
    assert gate_calls == ["eastmoney_kline", "tencent_kline"]
    assert result.payload["source_attempts"][0]["rate_key"] == "eastmoney_kline"
    assert result.payload["source_attempts"][1]["rate_key"] == "tencent_kline"


def test_fetch_ohlcv_falls_back_to_tencent_when_eastmoney_returns_empty(monkeypatch) -> None:
    """东方财富 Direct 返回空 K 线时也应继续尝试腾讯，避免空结果截断采集链。"""

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
        return pd.DataFrame()

    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_provider.fetch_tencent_kline_direct",
        fake_tencent,
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_provider.fetch_eastmoney_kline_direct",
        fake_eastmoney,
    )

    provider = AkshareProvider(request_timeout_seconds=7.0)
    result = provider.fetch_ohlcv(
        symbol="001235",
        timeframe="1d",
        start="20260528",
        end="20260528",
        adjust="qfq",
    )

    assert result.status == "available"
    assert calls == ["eastmoney", "tencent"]
    assert result.payload["source_attempts"][0]["source"] == "eastmoney:direct:kline"
    assert result.payload["source_attempts"][0]["status"] == "empty"
    assert result.payload["actual_source"] == "tencent:direct:kline"


def test_fetch_ohlcv_reports_unavailable_when_direct_sources_are_empty(monkeypatch) -> None:
    """两个直连源都返回空 K 线时，应标记为无数据，不能被最后的包装源网络异常误报失败。"""

    def fake_empty(**kwargs):
        return pd.DataFrame()

    def fake_akshare_error(**kwargs):
        raise RuntimeError("akshare wrapper disconnected")

    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_provider.fetch_eastmoney_kline_direct",
        fake_empty,
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_provider.fetch_tencent_kline_direct",
        fake_empty,
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_provider.ak.stock_zh_a_hist",
        fake_akshare_error,
    )

    provider = AkshareProvider(request_timeout_seconds=7.0)
    result = provider.fetch_ohlcv(
        symbol="001399",
        timeframe="1d",
        start="20260528",
        end="20260528",
        adjust="qfq",
    )

    assert result.status == "unavailable"
    assert result.error_message is None
    assert [item["status"] for item in result.payload["source_attempts"]] == [
        "empty",
        "empty",
        "error",
    ]


def test_eastmoney_curl_cffi_kline_uses_browser_cookie(monkeypatch) -> None:
    """东方财富直连 K 线 fallback 应复用浏览器 cookie 会话态。"""

    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "rc": 0,
                "data": {
                    "klines": [
                        "2026-05-28,10.0,10.5,10.8,9.9,1000,10000,1.0,2.0,0.2,3.0"
                    ]
                }
            }

    def fake_get(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeResponse()

    monkeypatch.setenv("FINANCE_AGENT_EASTMONEY_COOKIE", "qgqp_b_id=test-cookie")
    monkeypatch.setattr(
        "finance_agent.data.providers.ashare_kline_sources.curl_requests.get",
        fake_get,
    )

    provider = AkshareProvider(request_timeout_seconds=7.0)
    df = provider._fetch_ohlcv_eastmoney_curl_cffi(
        symbol="000001",
        timeframe="1d",
        start="20260501",
        end="20260528",
        adjust="qfq",
    )

    assert len(df.index) == 1
    assert captured["kwargs"]["timeout"] == 7.0
    assert captured["kwargs"]["impersonate"] == "chrome120"
    assert captured["kwargs"]["headers"]["Cookie"] == "qgqp_b_id=test-cookie"


def test_eastmoney_common_curl_get_json_uses_browser_cookie(monkeypatch) -> None:
    """通用东方财富 curl fallback 应在配置 cookie 时追加 Cookie 头。"""

    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": {"diff": []}}

    def fake_get(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeResponse()

    monkeypatch.setenv("FINANCE_AGENT_EASTMONEY_COOKIE", "qgqp_b_id=test-cookie")
    monkeypatch.setattr(eastmoney_curl.curl_requests, "get", fake_get)

    eastmoney_curl._curl_get_json("https://push2.eastmoney.com/api/qt/clist/get", params={})

    assert captured["kwargs"]["headers"]["Cookie"] == "qgqp_b_id=test-cookie"


def test_eastmoney_headers_generates_anonymous_cookie_when_env_missing(monkeypatch) -> None:
    """未配置浏览器 cookie 时，应生成进程内匿名东方财富 cookie。"""

    monkeypatch.delenv("FINANCE_AGENT_EASTMONEY_COOKIE", raising=False)
    if hasattr(eastmoney_curl, "_SYNTHETIC_EASTMONEY_COOKIE"):
        monkeypatch.setattr(eastmoney_curl, "_SYNTHETIC_EASTMONEY_COOKIE", None)

    headers = eastmoney_curl.eastmoney_headers()

    assert "Cookie" in headers
    for cookie_name in [
        "st_nvi",
        "qgqp_b_id",
        "nid18",
        "nid18_create_time",
        "gviem",
        "gviem_create_time",
        "st_pvi",
        "st_sp",
        "st_inirUrl",
    ]:
        assert f"{cookie_name}=" in headers["Cookie"]


def test_eastmoney_headers_prefers_configured_cookie(monkeypatch) -> None:
    """显式配置浏览器 cookie 时，不覆盖用户提供的 cookie。"""

    monkeypatch.setenv("FINANCE_AGENT_EASTMONEY_COOKIE", "qgqp_b_id=configured-cookie")

    headers = eastmoney_curl.eastmoney_headers()

    assert headers["Cookie"] == "qgqp_b_id=configured-cookie"


def test_eastmoney_headers_reads_cookie_file_when_env_missing(monkeypatch, tmp_path) -> None:
    """未配置环境变量时，应从本地 secret 文件读取东方财富 cookie。"""

    cookie_file = tmp_path / "eastmoney_cookie.json"
    cookie_file.write_text(
        '{"cookie": "qgqp_b_id=file-cookie; st_pvi=123", "updated_at": "2026-06-05T00:00:00+00:00"}',
        encoding="utf-8",
    )
    monkeypatch.delenv("FINANCE_AGENT_EASTMONEY_COOKIE", raising=False)
    monkeypatch.setenv("FINANCE_AGENT_EASTMONEY_COOKIE_FILE", str(cookie_file))

    headers = eastmoney_curl.eastmoney_headers()

    assert headers["Cookie"] == "qgqp_b_id=file-cookie; st_pvi=123"


def test_eastmoney_headers_auto_refreshes_cookie_file(monkeypatch, tmp_path) -> None:
    """自动刷新开启时，缺失的本地 Cookie 文件应由刷新函数补齐。"""

    cookie_file = tmp_path / "eastmoney_cookie.json"

    def fake_refresh(*, output_path, headed=False, timeout_ms=45_000):
        output_path.write_text('{"cookie": "qgqp_b_id=auto-cookie"}', encoding="utf-8")
        return {"cookie_count": 1}

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("FINANCE_AGENT_EASTMONEY_COOKIE", raising=False)
    monkeypatch.setenv("FINANCE_AGENT_EASTMONEY_COOKIE_AUTO_REFRESH", "1")
    monkeypatch.setenv("FINANCE_AGENT_EASTMONEY_COOKIE_FILE", str(cookie_file))
    monkeypatch.setattr(eastmoney_curl, "refresh_eastmoney_cookie_file", fake_refresh)

    headers = eastmoney_curl.eastmoney_headers()

    assert headers["Cookie"] == "qgqp_b_id=auto-cookie"


def test_eastmoney_force_refresh_uses_singleflight_lock(monkeypatch, tmp_path) -> None:
    """并发强制刷新 Cookie 时只允许一个线程真正打开浏览器刷新。"""

    cookie_file = tmp_path / "eastmoney_cookie.json"
    refresh_calls: list[Path] = []
    refresh_lock = threading.Lock()

    def fake_refresh(*, output_path, headed=False, timeout_ms=45_000):
        time.sleep(0.05)
        with refresh_lock:
            refresh_calls.append(output_path)
        output_path.write_text('{"cookie": "qgqp_b_id=singleflight-cookie"}', encoding="utf-8")
        return {"cookie_count": 1}

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("FINANCE_AGENT_EASTMONEY_COOKIE", raising=False)
    monkeypatch.setenv("FINANCE_AGENT_EASTMONEY_COOKIE_AUTO_REFRESH", "1")
    monkeypatch.setenv("FINANCE_AGENT_EASTMONEY_COOKIE_FILE", str(cookie_file))
    monkeypatch.setattr(eastmoney_curl, "refresh_eastmoney_cookie_file", fake_refresh)

    start_event = threading.Event()
    results: list[dict] = []

    def refresh_worker() -> None:
        start_event.wait(timeout=1)
        results.append(eastmoney_curl.ensure_eastmoney_cookie(force=True))

    threads = [threading.Thread(target=refresh_worker) for _ in range(5)]
    for thread in threads:
        thread.start()
    start_event.set()
    for thread in threads:
        thread.join(timeout=2)

    assert len(results) == 5
    assert len(refresh_calls) == 1
    assert eastmoney_curl.eastmoney_headers()["Cookie"] == "qgqp_b_id=singleflight-cookie"


def test_eastmoney_kline_cookie_probe_opens_cooldown_when_cookie_invalid(monkeypatch, tmp_path) -> None:
    """K 线 Cookie 刷新后必须通过 push2his 探测；失败时进入冷却并快速跳过。"""

    cookie_file = tmp_path / "eastmoney_cookie.json"
    refresh_calls: list[Path] = []
    probe_calls: list[dict] = []

    def fake_refresh(*, output_path, headed=False, timeout_ms=45_000):
        refresh_calls.append(output_path)
        output_path.write_text('{"cookie": "qgqp_b_id=bad-cookie"}', encoding="utf-8")
        return {"cookie_count": 1}

    def fake_get(url, **kwargs):
        probe_calls.append({"url": url, **kwargs})
        raise RuntimeError("push2his disconnected")

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("FINANCE_AGENT_EASTMONEY_COOKIE", raising=False)
    monkeypatch.setenv("FINANCE_AGENT_EASTMONEY_COOKIE_AUTO_REFRESH", "1")
    monkeypatch.setenv("FINANCE_AGENT_EASTMONEY_COOKIE_FILE", str(cookie_file))
    monkeypatch.setenv("FINANCE_AGENT_EASTMONEY_KLINE_COOKIE_COOLDOWN_SECONDS", "60")
    monkeypatch.setattr(eastmoney_curl, "refresh_eastmoney_cookie_file", fake_refresh)
    monkeypatch.setattr(eastmoney_curl.curl_requests, "get", fake_get)
    eastmoney_curl.reset_eastmoney_kline_cookie_health_for_tests()

    try:
        try:
            eastmoney_curl.ensure_eastmoney_kline_cookie(force=True)
        except RuntimeError as exc:
            assert "Cookie 校验失败" in str(exc)
        else:  # pragma: no cover - 测试红灯保护
            raise AssertionError("invalid kline cookie should fail")

        try:
            eastmoney_curl.ensure_eastmoney_kline_cookie()
        except RuntimeError as exc:
            assert "冷却中" in str(exc)
        else:  # pragma: no cover - 测试红灯保护
            raise AssertionError("cooling kline source should fail fast")

        assert len(refresh_calls) == 1
        assert len(probe_calls) == 1
    finally:
        eastmoney_curl.reset_eastmoney_kline_cookie_health_for_tests()


def test_eastmoney_kline_cookie_probe_recovers_after_cooldown(monkeypatch, tmp_path) -> None:
    """冷却到期后下一次探测成功，应自动恢复东方财富 K 线源。"""

    cookie_file = tmp_path / "eastmoney_cookie.json"
    cookie_file.write_text('{"cookie": "qgqp_b_id=good-cookie"}', encoding="utf-8")
    now = [1_000.0]

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"rc": 0, "data": {"klines": ["2026-06-12,1,1,1,1,1,1"]}}

    monkeypatch.delenv("FINANCE_AGENT_EASTMONEY_COOKIE", raising=False)
    monkeypatch.setenv("FINANCE_AGENT_EASTMONEY_COOKIE_FILE", str(cookie_file))
    monkeypatch.setenv("FINANCE_AGENT_EASTMONEY_KLINE_COOKIE_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("FINANCE_AGENT_EASTMONEY_KLINE_COOKIE_PROBE_TTL_SECONDS", "300")
    monkeypatch.setattr(eastmoney_curl.time, "time", lambda: now[0])
    monkeypatch.setattr(eastmoney_curl.curl_requests, "get", lambda *args, **kwargs: FakeResponse())
    eastmoney_curl.reset_eastmoney_kline_cookie_health_for_tests()

    try:
        eastmoney_curl.mark_eastmoney_kline_cookie_unavailable("previous failure")
        try:
            eastmoney_curl.ensure_eastmoney_kline_cookie()
        except RuntimeError as exc:
            assert "冷却中" in str(exc)
        else:  # pragma: no cover - 测试红灯保护
            raise AssertionError("source should still be cooling before cooldown expires")

        now[0] += 31
        status = eastmoney_curl.ensure_eastmoney_kline_cookie()

        assert status["available"] is True
        assert status["source"] == "file"
        assert status["probe_ok"] is True
        assert eastmoney_curl.eastmoney_kline_cookie_health_status()["state"] == "healthy"
    finally:
        eastmoney_curl.reset_eastmoney_kline_cookie_health_for_tests()


def test_tencent_direct_kline_uses_external_two_year_windows(monkeypatch) -> None:
    """腾讯 Direct 应由适配层控制切片，避免调用 AKShare 内部重叠循环。"""

    captured_params: list[dict] = []

    class FakeResponse:
        status_code = 200

        @property
        def text(self) -> str:
            return (
                'kline_dayqfq2024={"code":0,"msg":"","data":{"sh603507":'
                '{"qfqday":[["2024-01-02","10.0","10.5","10.8","9.9","1000"]]}}}'
            )

        def raise_for_status(self) -> None:
            return None

    def fake_get(url, **kwargs):
        captured_params.append(kwargs["params"])
        return FakeResponse()

    monkeypatch.setattr(
        "finance_agent.data.providers.ashare_kline_sources.requests.get",
        fake_get,
    )

    df = fetch_tencent_kline_direct(
        symbol="603507",
        start="20240101",
        end="20251231",
        adjust="qfq",
        timeout=7.0,
    )

    assert len(df.index) == 1
    assert captured_params == [
        {
            "_var": "kline_dayqfq2024",
            "param": "sh603507,day,2024-01-01,2025-12-31,640,qfq",
            "r": "0.8205512681390605",
        }
    ]


def test_fetch_ohlcv_uses_direct_tencent_adapter_after_eastmoney_failure(monkeypatch) -> None:
    """Provider 对外保持 MarketBarsResult，东财失败后可使用腾讯 Direct 适配层。"""

    def fake_eastmoney(**kwargs):
        raise RuntimeError("eastmoney unavailable")

    def fake_tencent(**kwargs):
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

    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_provider.fetch_eastmoney_kline_direct",
        fake_eastmoney,
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_provider.fetch_tencent_kline_direct",
        fake_tencent,
    )

    result = AkshareProvider(request_timeout_seconds=7.0).fetch_ohlcv(
        symbol="603507",
        timeframe="1d",
        start="20260528",
        end="20260528",
        adjust="qfq",
    )

    assert result.status == "available"
    assert result.payload["primary_source"] == "eastmoney:direct:kline"
    assert result.payload["actual_source"] == "tencent:direct:kline"
    assert result.payload["source_attempts"][0]["source"] == "eastmoney:direct:kline"
    assert result.payload["source_attempts"][1]["source"] == "tencent:direct:kline"
    assert result.bars[0].source == "tencent:direct:kline"


def test_fetch_assets_eastmoney_uses_curl_cffi_when_akshare_disconnects(monkeypatch) -> None:
    """AKShare 东方财富入口断开时，应直连 clist/get 补齐全 A 资产列表。"""

    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "data": {
                    "total": 2,
                    "diff": [
                        {"f12": "000001", "f14": "平安银行", "f2": 10.66, "f3": -0.93},
                        {"f12": "600519", "f14": "贵州茅台", "f2": 1410.0, "f3": 0.12},
                    ],
                }
            }

    def fake_get(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeResponse()

    monkeypatch.setenv("FINANCE_AGENT_EASTMONEY_COOKIE", "qgqp_b_id=test-cookie")
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_provider.ak.stock_zh_a_spot_em",
        lambda: (_ for _ in ()).throw(RuntimeError("peer closed connection")),
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_provider.curl_requests.get",
        fake_get,
    )

    result = AkshareProvider(request_timeout_seconds=7.0).fetch_assets()

    assert result.status == "available"
    assert result.payload["actual_source"] == "eastmoney:curl_cffi:stock_zh_a_spot_em"
    assert [(asset.symbol, asset.name) for asset in result.assets] == [
        ("000001", "平安银行"),
        ("600519", "贵州茅台"),
    ]
    assert captured["kwargs"]["timeout"] == 7.0
    assert captured["kwargs"]["headers"]["Cookie"] == "qgqp_b_id=test-cookie"


def test_fetch_assets_eastmoney_curl_cffi_tries_next_host(monkeypatch) -> None:
    """东方财富列表子域名超时时，应尝试下一个 push2 子域名。"""

    called_urls: list[str] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "data": {
                    "total": 1,
                    "diff": [{"f12": "000001", "f14": "平安银行", "f2": 10.66, "f3": -0.93}],
                }
            }

    def fake_get(url, **kwargs):
        called_urls.append(url)
        if len(called_urls) == 1:
            raise TimeoutError("first host timeout")
        return FakeResponse()

    monkeypatch.delenv("FINANCE_AGENT_EASTMONEY_COOKIE", raising=False)
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_provider.curl_requests.get",
        fake_get,
    )

    df = AkshareProvider(request_timeout_seconds=7.0)._fetch_assets_eastmoney_curl_cffi()

    assert len(df.index) == 1
    assert len(called_urls) == 2


def test_fetch_assets_falls_back_to_full_code_name_before_tencent(monkeypatch) -> None:
    """东方财富实时入口失败时，优先使用全 A 代码名册补齐资产池。"""

    calls: list[str] = []

    def fake_eastmoney():
        calls.append("eastmoney")
        raise RuntimeError("eastmoney unavailable")

    def fake_code_name():
        calls.append("code_name")
        return pd.DataFrame(
            [
                {"code": "000001", "name": "平安银行"},
                {"code": "600519", "name": "贵州茅台"},
            ]
        )

    def fake_tencent():
        calls.append("tencent")
        raise AssertionError("全 A 名册成功时不应调用腾讯 rank 兜底")

    monkeypatch.setattr(AkshareProvider, "_fetch_assets_eastmoney", lambda self: fake_eastmoney())
    monkeypatch.setattr(AkshareProvider, "_fetch_assets_code_name", lambda self: fake_code_name())
    monkeypatch.setattr(AkshareProvider, "_fetch_assets_tencent", lambda self: fake_tencent())

    result = AkshareProvider().fetch_assets()

    assert result.status == "available"
    assert calls == ["eastmoney", "code_name"]
    assert result.payload["actual_source"] == "akshare:stock_info_a_code_name"
    assert result.payload["fallback_used"] is True
    assert [(asset.symbol, asset.name) for asset in result.assets] == [
        ("000001", "平安银行"),
        ("600519", "贵州茅台"),
    ]


def test_fetch_assets_falls_back_to_tencent_when_code_name_fails(monkeypatch) -> None:
    """全 A 代码名册也失败时，保留腾讯 rank 作为最后兜底。"""

    calls: list[str] = []

    def fake_eastmoney():
        calls.append("eastmoney")
        raise RuntimeError("eastmoney unavailable")

    def fake_code_name():
        calls.append("code_name")
        raise RuntimeError("code name unavailable")

    def fake_tencent():
        calls.append("tencent")
        return pd.DataFrame([{"code": "sh600519", "name": "贵州茅台"}])

    monkeypatch.setattr(AkshareProvider, "_fetch_assets_eastmoney", lambda self: fake_eastmoney())
    monkeypatch.setattr(AkshareProvider, "_fetch_assets_code_name", lambda self: fake_code_name())
    monkeypatch.setattr(AkshareProvider, "_fetch_assets_tencent", lambda self: fake_tencent())

    result = AkshareProvider().fetch_assets()

    assert result.status == "available"
    assert calls == ["eastmoney", "code_name", "tencent"]
    assert result.payload["actual_source"] == "akshare:stock_zh_a_spot_tx"
    assert result.payload["fallback_used"] is True
    assert result.assets[0].asset_id == "ashare:600519"


def test_fetch_assets_code_name_keeps_partial_exchange_results(monkeypatch) -> None:
    """分交易所名册部分失败时，已成功的沪深结果仍可用于资产池。"""

    provider = AkshareProvider()

    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_provider.ak.stock_info_sz_name_code",
        lambda symbol: pd.DataFrame([{"A股代码": "000001", "A股简称": "平安银行"}]),
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_provider.ak.stock_info_sh_name_code",
        lambda symbol: pd.DataFrame([{"证券代码": "600519", "证券简称": "贵州茅台"}]),
    )
    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_provider.ak.stock_info_bj_name_code",
        lambda: (_ for _ in ()).throw(RuntimeError("bse timeout")),
    )

    df = provider._fetch_assets_code_name()

    assert sorted(df["code"].tolist()) == ["000001", "600519"]
    assert df.attrs["source_errors"] == [
        {"source": "akshare:stock_info_bj_name_code", "error_message": "bse timeout"}
    ]
