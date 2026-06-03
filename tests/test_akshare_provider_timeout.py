import pandas as pd

from finance_agent.data.providers import eastmoney_curl
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


def test_eastmoney_curl_cffi_kline_uses_browser_cookie(monkeypatch) -> None:
    """东方财富直连 K 线 fallback 应复用浏览器 cookie 会话态。"""

    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
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
        "finance_agent.data.providers.akshare_provider.curl_requests.get",
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
