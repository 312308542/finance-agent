"""gotdx 行情网关的只读 Python 适配器。

网关只负责重点 A 股快照，不替代收盘日 K、基本面或推荐计算。适配器保留
服务端时间、接收时间、延迟和质量状态，并把同一批响应绑定到一个
`data_snapshot_id`，过期或冲突数据由决策闸门阻止进入可执行动作。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import requests
from sqlalchemy.orm import Session

from finance_agent.storage.repositories import AssetRepository, DataSnapshotRepository
from finance_agent.storage.snapshot_contracts import DataSnapshot, build_data_snapshot, normalize_datetime

JsonDict = dict[str, Any]
RequestPost = Callable[..., Any]
GOTDX_SOURCE = "gotdx:tdx_main"
GOTDX_GATEWAY_HARD_LIMIT = 100
DEFAULT_GOTDX_BATCH_SIZE = 50


class GotdxGatewayError(RuntimeError):
    """网关响应无法作为可靠事实使用。"""


@dataclass(frozen=True)
class GotdxQuote:
    """标准化的单标的网关快照。"""

    asset_id: str
    symbol: str
    market: str
    as_of: datetime
    received_at: datetime
    server_timestamp: datetime
    quality_status: str
    status: str
    last_price: Decimal
    prev_close: Decimal | None
    open_price: Decimal | None
    high: Decimal | None
    low: Decimal | None
    volume: Decimal | None
    amount: Decimal | None
    turnover_rate: Decimal | None
    change_amount: Decimal | None
    change_percent: Decimal | None
    bid_price: Decimal | None
    ask_price: Decimal | None
    payload: JsonDict


@dataclass(frozen=True)
class GotdxSnapshotResult:
    """一次网关响应对应的不可变快照和标准写入行。"""

    snapshot: DataSnapshot
    rows: tuple[JsonDict, ...]


@dataclass(frozen=True)
class GotdxPersistenceResult:
    """快照入库结果。"""

    snapshot: DataSnapshot
    rows_written: int


def split_quote_symbols(
    symbols: list[str] | tuple[str, ...],
    *,
    batch_size: int = DEFAULT_GOTDX_BATCH_SIZE,
) -> tuple[tuple[str, ...], ...]:
    """保持输入顺序去重，并按网关安全上限切分行情代码。"""

    if batch_size < 1 or batch_size > GOTDX_GATEWAY_HARD_LIMIT:
        raise ValueError("GoTDX 行情批次必须在 1 到 100 之间")
    normalized = tuple(
        dict.fromkeys(str(item).strip() for item in symbols if str(item).strip())
    )
    return tuple(
        normalized[offset : offset + batch_size]
        for offset in range(0, len(normalized), batch_size)
    )


def parse_gateway_quotes(payload: JsonDict) -> tuple[GotdxQuote, ...]:
    """解析并校验网关 `/quotes` 响应。"""

    raw_quotes = payload.get("quotes")
    if not isinstance(raw_quotes, list) or not raw_quotes:
        raise GotdxGatewayError("网关 quotes 为空")
    top_received_at = _parse_timestamp(payload.get("received_at"), field_name="received_at")
    quotes: list[GotdxQuote] = []
    seen_codes: set[str] = set()
    for index, raw in enumerate(raw_quotes):
        if not isinstance(raw, dict):
            raise GotdxGatewayError(f"quotes[{index}] 不是对象")
        symbol, market, code = _normalize_symbol(raw.get("symbol"), raw.get("market"), raw.get("code"))
        if code in seen_codes:
            raise GotdxGatewayError(f"网关返回重复证券: {code}")
        seen_codes.add(code)
        server_timestamp = _parse_timestamp(
            raw.get("server_timestamp"), field_name=f"quotes[{index}].server_timestamp"
        )
        received_at = _parse_timestamp(
            raw.get("received_at") or payload.get("received_at"),
            field_name=f"quotes[{index}].received_at",
        )
        last_price = _required_decimal(raw.get("last_price"), field_name=f"quotes[{index}].last_price")
        quality_status = str(raw.get("quality_status") or "available").strip()
        payload_copy = dict(raw)
        payload_copy["source"] = str(payload.get("source") or GOTDX_SOURCE)
        payload_copy["received_at"] = received_at.isoformat()
        payload_copy["server_timestamp"] = server_timestamp.isoformat()
        quotes.append(
            GotdxQuote(
                asset_id=f"ashare:{code}",
                symbol=code,
                market=market,
                as_of=server_timestamp,
                received_at=max(received_at, top_received_at),
                server_timestamp=server_timestamp,
                quality_status=quality_status,
                status=quality_status,
                last_price=last_price,
                prev_close=_optional_decimal(raw.get("prev_close")),
                open_price=_optional_decimal(raw.get("open")),
                high=_optional_decimal(raw.get("high")),
                low=_optional_decimal(raw.get("low")),
                volume=_optional_decimal(raw.get("volume")),
                amount=_optional_decimal(raw.get("amount")),
                turnover_rate=_optional_decimal(raw.get("turnover_rate")),
                change_amount=_optional_decimal(raw.get("change_amount")),
                change_percent=_optional_decimal(raw.get("change_percent")),
                bid_price=_first_level_price(raw.get("bid_levels")),
                ask_price=_first_level_price(raw.get("ask_levels")),
                payload=payload_copy,
            )
        )
    return tuple(quotes)


class GotdxGatewayProvider:
    """通过本地 gotdx 网关获取重点标的快照。"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = 3.0,
        request_post: RequestPost | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout 必须大于 0")
        self.base_url = (
            base_url
            or os.getenv("FINANCE_AGENT_GOTDX_GATEWAY_URL")
            or "http://127.0.0.1:8790"
        ).rstrip("/")
        self.timeout = float(timeout)
        self.request_post = request_post or requests.post

    def fetch_quotes(self, symbols: list[str] | tuple[str, ...]) -> tuple[GotdxQuote, ...]:
        """调用网关并解析重点标的快照。"""

        batches = split_quote_symbols(symbols)
        if not batches:
            raise GotdxGatewayError("symbols 不能为空")
        merged_quotes: list[GotdxQuote] = []
        for batch_index, batch in enumerate(batches):
            start = batch_index * DEFAULT_GOTDX_BATCH_SIZE + 1
            end = start + len(batch) - 1
            try:
                response = self.request_post(
                    f"{self.base_url}/quotes",
                    json={"symbols": list(batch)},
                    timeout=self.timeout,
                )
                status_code = int(getattr(response, "status_code", 0) or 0)
                if status_code < 200 or status_code >= 300:
                    raise GotdxGatewayError(f"网关 HTTP {status_code}")
                try:
                    response_payload = response.json()
                except Exception as exc:  # noqa: BLE001 - Provider 边界需要统一错误类型
                    raise GotdxGatewayError(f"网关响应不是 JSON: {exc}") from exc
                if not isinstance(response_payload, dict):
                    raise GotdxGatewayError("网关响应必须是 JSON 对象")
                parsed = parse_gateway_quotes(response_payload)
                requested_identities = tuple(_requested_quote_identity(requested) for requested in batch)
                quotes_by_identity = {
                    _quote_identity(
                        quote.payload.get("symbol"), quote.payload.get("market"), quote.payload.get("code")
                    ): quote
                    for quote in parsed
                }
                if len(parsed) != len(batch) or set(quotes_by_identity) != set(requested_identities):
                    raise GotdxGatewayError(
                        f"网关返回证券身份不匹配或不完整: 请求 {len(batch)} 只，收到 {len(parsed)} 只"
                    )
                ordered = tuple(quotes_by_identity[identity] for identity in requested_identities)
                merged_quotes.extend(ordered)
            except (requests.RequestException, GotdxGatewayError) as exc:
                raise GotdxGatewayError(f"批次 {start}-{end} 失败: {exc}") from exc
        return tuple(merged_quotes)

    def collect_snapshot_rows(
        self,
        symbols: list[str] | tuple[str, ...],
        *,
        now: datetime | None = None,
    ) -> GotdxSnapshotResult:
        """获取快照并构造一批绑定同一数据版本的入库行。"""

        quotes = self.fetch_quotes(symbols)
        captured_at = normalize_datetime(now or datetime.now(UTC), field_name="now")
        as_of = max(quote.as_of for quote in quotes)
        captured_at = max(captured_at, as_of)
        quality_status = _aggregate_quality(quotes)
        snapshot = build_data_snapshot(
            snapshot_type="ashare_realtime_quotes",
            market="ashare",
            as_of=as_of,
            captured_at=captured_at,
            provider=GOTDX_SOURCE,
            provider_version="gateway-v1",
            quality_status=quality_status,
            payload={"quotes": [quote.payload for quote in quotes]},
            metadata={
                "source": GOTDX_SOURCE,
                "quote_count": len(quotes),
                "received_at": captured_at.isoformat(),
            },
        )
        rows = tuple(
            {
                "asset_id": quote.asset_id,
                "symbol": quote.symbol,
                "market": quote.market,
                "as_of": quote.as_of,
                "source": GOTDX_SOURCE,
                "data_snapshot_id": snapshot.data_snapshot_id,
                "captured_at": quote.received_at,
                "freshness_ms": max(
                    0,
                    int((quote.received_at - quote.server_timestamp).total_seconds() * 1000),
                ),
                "last_price": quote.last_price,
                "prev_close": quote.prev_close,
                "open": quote.open_price,
                "high": quote.high,
                "low": quote.low,
                "volume": quote.volume,
                "amount": quote.amount,
                "turnover_rate": quote.turnover_rate,
                "change_amount": quote.change_amount,
                "change_percent": quote.change_percent,
                "bid_price": quote.bid_price,
                "ask_price": quote.ask_price,
                "status": quote.status,
                "payload": quote.payload,
            }
            for quote in quotes
        )
        return GotdxSnapshotResult(snapshot=snapshot, rows=rows)

    def collect_and_persist(
        self,
        session: Session,
        symbols: list[str] | tuple[str, ...],
        *,
        now: datetime | None = None,
    ) -> GotdxPersistenceResult:
        """把网关快照、资产身份和行情行在调用方事务中持久化。"""

        result = self.collect_snapshot_rows(symbols, now=now)
        DataSnapshotRepository(session).insert_snapshot(result.snapshot)
        AssetRepository(session).ensure_assets(
            [
                {
                    "asset_id": row["asset_id"],
                    "symbol": row["symbol"],
                    "name": row["symbol"],
                    "market": row["market"],
                    "asset_type": "stock",
                    "exchange": {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}.get(
                        str(row["payload"].get("market") or ""),
                    ),
                    "currency": "CNY",
                    "payload": {"source": GOTDX_SOURCE},
                }
                for row in result.rows
            ]
        )
        rows_written = AssetRepository(session).upsert_intraday_quote_latest(result.rows)
        return GotdxPersistenceResult(snapshot=result.snapshot, rows_written=rows_written)


def _normalize_symbol(raw_symbol: Any, raw_market: Any, raw_code: Any) -> tuple[str, str, str]:
    code, _exchange = _quote_identity(raw_symbol, raw_market, raw_code)
    return code, "ashare", code


def _quote_identity(raw_symbol: Any, raw_market: Any, raw_code: Any) -> tuple[str, str]:
    """核对所有显式身份字段，禁止用 symbol 覆盖冲突的 code 或 market。"""

    value = str(raw_symbol or "").strip().upper()
    code = str(raw_code or "").strip()
    market_token = str(raw_market if raw_market is not None else "").strip().upper()
    market_map = {
        "SH": "SH", "SSE": "SH", "1": "SH",
        "SZ": "SZ", "SZSE": "SZ", "0": "SZ",
        "BJ": "BJ", "BSE": "BJ", "2": "BJ",
    }
    if market_token and market_token not in market_map:
        raise GotdxGatewayError(f"证券身份的市场无效: {raw_market!r}")
    if "." in value:
        symbol_code, symbol_market = value.split(".", 1)
        if symbol_market not in market_map:
            raise GotdxGatewayError(f"证券身份的市场无效: {raw_symbol!r}")
        if code and code != symbol_code:
            raise GotdxGatewayError(f"证券身份不一致: symbol={raw_symbol!r} code={raw_code!r}")
        if market_token and market_map[market_token] != market_map[symbol_market]:
            raise GotdxGatewayError(f"证券身份不一致: symbol={raw_symbol!r} market={raw_market!r}")
        code, market_token = symbol_code, symbol_market
    else:
        if value and code and value != code:
            raise GotdxGatewayError(f"证券身份不一致: symbol={raw_symbol!r} code={raw_code!r}")
        code = code or value
    if market_token not in market_map:
        raise GotdxGatewayError(f"证券身份缺少市场: symbol={raw_symbol!r} market={raw_market!r}")
    if len(code) != 6 or not code.isascii() or not code.isdigit():
        raise GotdxGatewayError(f"A 股代码无效: {code}")
    return code, market_map[market_token]


def _requested_quote_identity(value: str) -> tuple[str, str]:
    """与网关请求格式兼容，同时保留交易所参与响应集合校验。"""

    value = value.strip().upper()
    if "." in value:
        return _quote_identity(value, None, None)
    if value[:2] in {"SH", "SZ", "BJ"}:
        return _quote_identity(value[2:], value[:2], None)
    if value.startswith("6"):
        market = "SH"
    elif value.startswith(("0", "3")):
        market = "SZ"
    elif value.startswith(("4", "8", "92")):
        market = "BJ"
    else:
        raise GotdxGatewayError(f"A 股请求身份无效: {value}")
    return _quote_identity(value, market, None)


def _parse_timestamp(value: Any, *, field_name: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise GotdxGatewayError(f"{field_name} 不能为空")
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise GotdxGatewayError(f"{field_name} 不是 ISO 时间: {text}") from exc
    return normalize_datetime(parsed, field_name=field_name)


def _required_decimal(value: Any, *, field_name: str) -> Decimal:
    result = _optional_decimal(value)
    if result is None:
        raise GotdxGatewayError(f"{field_name} 不能为空")
    return result


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or str(value).strip() in {"", "-", "--", "None", "nan"}:
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("%", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _first_level_price(value: Any) -> Decimal | None:
    if not isinstance(value, list) or not value or not isinstance(value[0], dict):
        return None
    return _optional_decimal(value[0].get("price"))


def _aggregate_quality(quotes: tuple[GotdxQuote, ...]) -> str:
    statuses = {quote.quality_status for quote in quotes}
    if statuses == {"available"}:
        return "available"
    if len(statuses) == 1:
        return next(iter(statuses))
    return "partial"
