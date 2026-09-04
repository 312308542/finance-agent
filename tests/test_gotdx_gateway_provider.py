from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from finance_agent.data.providers.gotdx_gateway import (
    GotdxGatewayError,
    GotdxGatewayProvider,
    parse_gateway_quotes,
    split_quote_symbols,
)

NOW = datetime(2026, 7, 20, 9, 35, tzinfo=UTC)


def _quote_payload(*, quality_status: str = "available") -> dict[str, Any]:
    return {
        "source": "gotdx:tdx_main",
        "received_at": "2026-07-20T17:35:00+08:00",
        "quotes": [
            {
                "symbol": "600519.SH",
                "market": "SH",
                "code": "600519",
                "last_price": 1500.25,
                "prev_close": 1490.0,
                "open": 1495.0,
                "high": 1503.0,
                "low": 1490.0,
                "volume": 12345,
                "amount": 1000000.5,
                "server_timestamp": "2026-07-20T17:34:59.500+08:00",
                "received_at": "2026-07-20T17:35:00+08:00",
                "freshness_ms": 500,
                "quality_status": quality_status,
                "provider_latency_ms": 30,
                "bid_levels": [{"price": 1500.2, "volume": 100}],
                "ask_levels": [{"price": 1500.3, "volume": 80}],
            }
        ],
    }


def _quote_payload_for_symbols(symbols: list[str]) -> dict[str, Any]:
    """构造与请求标的一一对应的网关响应。"""

    payload = _quote_payload()
    template = payload["quotes"][0]
    payload["quotes"] = []
    for symbol in reversed(symbols):
        code, market = symbol.split(".", 1)
        payload["quotes"].append(
            {
                **template,
                "symbol": symbol,
                "market": market,
                "code": code,
            }
        )
    return payload


def test_split_quote_symbols_caps_each_gateway_request_at_fifty() -> None:
    symbols = [f"{index:06d}.SZ" for index in range(121)]

    batches = split_quote_symbols(symbols)

    assert [len(batch) for batch in batches] == [50, 50, 21]
    assert batches[0][0] == "000000.SZ"
    assert batches[-1][-1] == "000120.SZ"


def test_split_quote_symbols_deduplicates_without_reordering() -> None:
    assert split_quote_symbols(["600000.SH", "000001.SZ", "600000.SH"]) == (
        ("600000.SH", "000001.SZ"),
    )


def test_provider_batches_requests_and_restores_caller_order() -> None:
    class _Response:
        status_code = 200

        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = payload

        def json(self) -> dict[str, Any]:
            return self.payload

    calls: list[list[str]] = []

    def post(_url: str, *, json: dict[str, Any], timeout: float) -> _Response:
        assert timeout == 3.0
        requested = list(json["symbols"])
        calls.append(requested)
        return _Response(_quote_payload_for_symbols(requested))

    symbols = [f"{index:06d}.SZ" for index in range(120, -1, -1)]
    provider = GotdxGatewayProvider(request_post=post)

    quotes = provider.fetch_quotes(symbols)

    assert [len(batch) for batch in calls] == [50, 50, 21]
    assert [quote.symbol for quote in quotes] == [item.removesuffix(".SZ") for item in symbols]


def test_provider_failure_identifies_the_failed_batch_range() -> None:
    class _Response:
        def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
            self.status_code = status_code
            self.payload = payload

        def json(self) -> dict[str, Any]:
            return self.payload

    call_count = 0

    def post(_url: str, *, json: dict[str, Any], timeout: float) -> _Response:
        nonlocal call_count
        assert timeout == 3.0
        call_count += 1
        if call_count == 2:
            return _Response(502, {"error": "bad gateway"})
        requested = list(json["symbols"])
        return _Response(200, _quote_payload_for_symbols(requested))

    provider = GotdxGatewayProvider(request_post=post)

    with pytest.raises(GotdxGatewayError, match="批次 51-100.*HTTP 502"):
        provider.fetch_quotes([f"{index:06d}.SZ" for index in range(121)])


def test_parse_gateway_quotes_maps_asset_and_decimal_values() -> None:
    quotes = parse_gateway_quotes(_quote_payload())

    assert len(quotes) == 1
    quote = quotes[0]
    assert quote.asset_id == "ashare:600519"
    assert quote.symbol == "600519"
    assert quote.market == "ashare"
    assert quote.as_of == datetime(2026, 7, 20, 9, 34, 59, 500000, tzinfo=UTC)
    assert quote.last_price == Decimal("1500.25")
    assert quote.payload["bid_levels"][0]["volume"] == 100


def test_parse_gateway_quotes_keeps_quality_status_for_gate() -> None:
    quote = parse_gateway_quotes(_quote_payload(quality_status="after_hours_snapshot"))[0]

    assert quote.quality_status == "after_hours_snapshot"
    assert quote.status == "after_hours_snapshot"


def test_parse_gateway_quotes_rejects_missing_server_time_or_price() -> None:
    payload = _quote_payload()
    payload["quotes"][0]["server_timestamp"] = ""
    with pytest.raises(GotdxGatewayError, match="server_timestamp"):
        parse_gateway_quotes(payload)

    payload = _quote_payload()
    payload["quotes"][0]["last_price"] = None
    with pytest.raises(GotdxGatewayError, match="last_price"):
        parse_gateway_quotes(payload)


def test_provider_creates_snapshot_and_rows_without_external_network() -> None:
    class _Response:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return _quote_payload()

    calls: list[dict[str, Any]] = []

    def post(url: str, *, json: dict[str, Any], timeout: float) -> _Response:
        calls.append({"url": url, "json": json, "timeout": timeout})
        return _Response()

    provider = GotdxGatewayProvider(base_url="http://127.0.0.1:8790", request_post=post)
    result = provider.collect_snapshot_rows(["600519.SH"], now=NOW)

    assert calls == [
        {
            "url": "http://127.0.0.1:8790/quotes",
            "json": {"symbols": ["600519.SH"]},
            "timeout": 3.0,
        }
    ]
    assert result.snapshot.data_snapshot_id.startswith("snapshot:ashare_realtime_quotes:ashare:")
    assert result.snapshot.quality_status == "available"
    assert result.rows[0]["data_snapshot_id"] == result.snapshot.data_snapshot_id
    assert result.rows[0]["source"] == "gotdx:tdx_main"


def test_provider_rejects_http_failure() -> None:
    class _Response:
        status_code = 502

        def json(self) -> dict[str, Any]:
            return {"error": "bad gateway"}

    provider = GotdxGatewayProvider(
        base_url="http://127.0.0.1:8790",
        request_post=lambda *_args, **_kwargs: _Response(),
    )

    with pytest.raises(GotdxGatewayError, match="HTTP 502"):
        provider.collect_snapshot_rows(["600519.SH"], now=NOW)


def test_provider_reads_gateway_url_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINANCE_AGENT_GOTDX_GATEWAY_URL", "http://gotdx-sidecar:8790/")

    provider = GotdxGatewayProvider()

    assert provider.base_url == "http://gotdx-sidecar:8790"


def test_provider_persists_snapshot_before_quote_rows() -> None:
    class _Response:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return _quote_payload()

    class _Result:
        rowcount = 1

    class _Session:
        def __init__(self) -> None:
            self.executed: list[Any] = []
            self.flush_count = 0

        def execute(self, statement: Any) -> _Result:
            self.executed.append(statement)
            return _Result()

        def flush(self) -> None:
            self.flush_count += 1

        def get_one(self, _model: Any, key: Any) -> Any:
            return SimpleNamespace(data_snapshot_id=key)

    from types import SimpleNamespace

    session = _Session()
    provider = GotdxGatewayProvider(
        base_url="http://127.0.0.1:8790",
        request_post=lambda *_args, **_kwargs: _Response(),
    )

    result = provider.collect_and_persist(session, ["600519.SH"], now=NOW)

    assert result.rows_written == 1
    assert result.snapshot.data_snapshot_id.startswith("snapshot:")
    assert len(session.executed) == 3
    assert session.flush_count == 3
