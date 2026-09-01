"""数据采集编排服务。

Collector 负责把 Provider 的结构化结果落到标准表，并把每次调用归档到
`raw_records`。它不负责因子计算、评分或推荐决策。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.data.freshness import (
    ASHARE_SPOT_VALUATION_SOURCE,
    ashare_daily_snapshot_at,
)
from finance_agent.data.models import (
    AssetData,
    AssetListResult,
    CapitalFlowSnapshotsResult,
    CryptoDerivativeSnapshotResult,
    EventRecordData,
    EventRecordsResult,
    FundamentalSnapshotsResult,
    FundNavSnapshotsResult,
    MarketBarData,
    MarketBarsResult,
    ProviderResult,
    RiskFindingsResult,
    SentimentSignalsResult,
    UniverseSeedData,
    UniverseSeedsResult,
)
from finance_agent.data.normalizers import stable_id
from finance_agent.data.providers import (
    AkshareFundProvider,
    AkshareProvider,
    AshareCapitalFlowProvider,
    AshareEventProvider,
    AshareFundamentalProvider,
    AshareRiskProvider,
    AshareSectorProvider,
    AshareSentimentProvider,
    AshareValuationProvider,
    BinanceNativeProvider,
    CcxtBinanceProvider,
    EastmoneyArticleFetcher,
)
from finance_agent.storage.repositories import (
    AssetRepository,
    CapitalFlowRepository,
    DerivativeDataRepository,
    EventRepository,
    FundamentalDataRepository,
    FundNavRepository,
    MarketDataRepository,
    RawRecordRepository,
    RiskRepository,
    UniverseRepository,
)

JsonDict = dict[str, Any]
logger = logging.getLogger(__name__)
UNIVERSE_SEED_MAPPING_SOURCE = "akshare:universe_seed"
CANONICAL_ASHARE_KLINE_SOURCE = "canonical:ashare:kline"
EXCHANGE_TRADED_FUND_TYPES = {"etf", "lof"}


@dataclass(frozen=True)
class ArchivedProviderResult:
    """带 raw_records 归档编号的 Provider 调用结果。"""

    result: ProviderResult
    raw_record_id: str


def _deduplicate_fund_assets_by_symbol(assets: Sequence[AssetData]) -> list[AssetData]:
    """基金资产按代码去重，同代码优先保留 ETF/LOF 交易所交易形态。"""

    priority = {"etf": 3, "lof": 2, "open_fund": 1}
    ordered_symbols: list[str] = []
    selected: dict[str, AssetData] = {}
    for asset in assets:
        symbol = str(asset.symbol or "").strip()
        if not symbol:
            continue
        if symbol not in selected:
            ordered_symbols.append(symbol)
            selected[symbol] = asset
            continue
        current = selected[symbol]
        if priority.get(asset.asset_type, 0) > priority.get(current.asset_type, 0):
            selected[symbol] = asset
    return [selected[symbol] for symbol in ordered_symbols if symbol in selected]


def _existing_fund_asset_data(asset: Any) -> AssetData:
    """把数据库已有基金身份转换为子源失败时可沿用的候选池成员。"""

    return AssetData(
        asset_id=str(asset.asset_id),
        symbol=str(asset.symbol),
        name=str(asset.name),
        market=str(asset.market),
        asset_type=str(asset.asset_type),
        exchange=asset.exchange,
        currency=asset.currency,
        sector=asset.sector,
        base_asset=asset.base_asset,
        quote_asset=asset.quote_asset,
        tradable=bool(asset.tradable),
        status=str(asset.status),
        payload=dict(asset.payload or {}),
    )


def archive_provider_result(
    raw_records: RawRecordRepository,
    result: ProviderResult,
    *,
    endpoint: str,
    request_params: JsonDict,
    symbol: str | None = None,
    market: str | None = "ashare",
) -> str:
    """把 Provider 返回结果归档到 raw_records。"""

    payload = result.payload | {
        "status": result.status,
        "error_message": result.error_message,
    }
    if isinstance(result, AssetListResult):
        payload["assets"] = [asset.__dict__ for asset in result.assets]
    elif isinstance(result, MarketBarsResult):
        payload["bars"] = [bar.__dict__ for bar in result.bars]
    elif isinstance(result, FundNavSnapshotsResult):
        payload["snapshots"] = [snapshot.__dict__ for snapshot in result.snapshots]
    elif isinstance(result, CryptoDerivativeSnapshotResult):
        payload["snapshot"] = result.snapshot.__dict__ if result.snapshot else None
    elif isinstance(result, UniverseSeedsResult):
        payload["seeds"] = [seed.__dict__ for seed in result.seeds]
    elif isinstance(result, CapitalFlowSnapshotsResult):
        payload["snapshots"] = [snapshot.__dict__ for snapshot in result.snapshots]
    elif isinstance(result, FundamentalSnapshotsResult):
        payload["snapshots"] = [snapshot.__dict__ for snapshot in result.snapshots]
    elif isinstance(result, EventRecordsResult):
        payload["events"] = [event.__dict__ for event in result.events]
        payload["evidence"] = [item.__dict__ for item in result.evidence]
    elif isinstance(result, RiskFindingsResult):
        payload["risks"] = [risk.__dict__ for risk in result.risks]
        payload["evidence"] = [item.__dict__ for item in result.evidence]
        payload["events"] = [event.__dict__ for event in result.events]
    elif isinstance(result, SentimentSignalsResult):
        payload["seeds"] = [seed.__dict__ for seed in result.seeds]
        payload["events"] = [event.__dict__ for event in result.events]
        payload["risks"] = [risk.__dict__ for risk in result.risks]
        payload["evidence"] = [item.__dict__ for item in result.evidence]

    record = raw_records.insert_raw_record(
        provider=result.provider_name,
        endpoint=endpoint,
        symbol=symbol,
        market=market,
        request_params=request_params,
        response_payload=payload,
        status=result.status,
        error_message=result.error_message,
        as_of=result.collected_at,
        collected_at=result.collected_at,
    )
    return record.raw_record_id


def _is_complete_sector_member_snapshot(
    result: UniverseSeedsResult,
    *,
    limit: int | None,
) -> bool:
    """只有未限量的完整板块快照才允许排除旧成员。"""

    if result.status != "available" or limit is not None:
        return False
    coverage = str(result.payload.get("source_coverage") or "").strip().lower()
    return coverage not in {"first_page", "limited", "partial"}


def _nullable_decimal(value: Any) -> Decimal | None:
    """把 Provider payload 中的可选数值安全转成 Decimal。"""

    if value is None:
        return None
    text = str(value).replace(",", "").replace("%", "").strip()
    if not text or text in {"-", "--", "nan", "None"}:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _first_decimal(payload: JsonDict, names: tuple[str, ...]) -> Decimal | None:
    """按多个可能字段名读取第一个可用数值。"""

    raw = payload.get("raw")
    if isinstance(raw, dict):
        for name in names:
            value = _nullable_decimal(raw.get(name))
            if value is not None:
                return value
    for name in names:
        value = _nullable_decimal(payload.get(name))
        if value is not None:
            return value
    return None


def _provider_from_source(source: str) -> str:
    """从 `akshare:xxx`、`ccxt:xxx` 这类 source 中提取 provider 名。"""

    return source.split(":", 1)[0] if ":" in source else source


def _endpoint_from_source(source: str) -> str:
    """从 source 中提取适合写入 raw_records.endpoint 的接口名。"""

    return source.split(":", 1)[1] if ":" in source else source


def _standard_market_bar_source(bar: MarketBarData) -> str:
    """为标准行情表选择稳定 source，避免 provider 降级导致同一根 K 线重复入库。"""

    if bar.market == "ashare" and bar.timeframe == "1d":
        return CANONICAL_ASHARE_KLINE_SOURCE
    return bar.source


def _market_bar_values(
    bar: MarketBarData,
    *,
    raw_record_id: str,
    source: str | None = None,
) -> JsonDict:
    """把采集层 K 线模型转换为标准行情表批量入库行。"""

    return {
        "asset_id": bar.asset_id,
        "symbol": bar.symbol,
        "market": bar.market,
        "timeframe": bar.timeframe,
        "timestamp": bar.timestamp,
        "end_timestamp": bar.end_timestamp,
        "open": bar.open_price,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "amount": bar.amount,
        "source": source or bar.source,
        "adjustment": bar.adjustment,
        "is_closed": bar.is_closed,
        "raw_record_id": raw_record_id,
        "status": bar.status,
    }


def _persist_rows(
    repository: Any,
    batch_method_name: str,
    single_method_name: str,
    rows: Sequence[JsonDict],
) -> None:
    """优先使用仓储批量接口；测试替身或旧实现没有批量接口时回退到单条写入。"""

    if not rows:
        return
    batch_method = getattr(repository, batch_method_name, None)
    if callable(batch_method):
        batch_method(list(rows))
        return
    single_method = getattr(repository, single_method_name)
    for row in rows:
        single_method(**row)


def _persist_asset_identity_rows(
    repository: Any,
    assets: Sequence[AssetData],
    *,
    as_of: datetime,
    raw_record_id: str,
    source: str,
    include_realtime_quote: bool = False,
    realtime_quote_mode: str = "history",
) -> None:
    """批量写入资产主数据和附表。"""

    master_rows: list[JsonDict] = []
    profile_rows: list[JsonDict] = []
    mapping_rows: list[JsonDict] = []
    status_rows: list[JsonDict] = []
    quote_rows: list[JsonDict] = []
    for asset in assets:
        payload = asset.payload | {"raw_record_id": raw_record_id}
        master_rows.append(
            {
                "asset_id": asset.asset_id,
                "symbol": asset.symbol,
                "name": asset.name,
                "market": asset.market,
                "asset_type": asset.asset_type,
                "exchange": asset.exchange,
                "currency": asset.currency,
                "sector": asset.sector,
                "base_asset": asset.base_asset,
                "quote_asset": asset.quote_asset,
                "tradable": asset.tradable,
                "status": asset.status,
                "payload": payload,
            }
        )
        profile_rows.append(
            {
                "asset_id": asset.asset_id,
                "symbol": asset.symbol,
                "name": asset.name,
                "market": asset.market,
                "exchange": asset.exchange,
                "sector": asset.sector,
                "source": source,
                "as_of": as_of,
                "payload": payload,
            }
        )
        mapping_rows.append(
            {
                "asset_id": asset.asset_id,
                "symbol": asset.symbol,
                "market": asset.market,
                "provider": _provider_from_source(source),
                "provider_symbol": str(asset.payload.get("source_symbol") or asset.symbol),
                "provider_exchange": asset.exchange,
                "source": source,
                "status": asset.status,
                "payload": payload,
            }
        )
        status_rows.append(
            {
                "asset_id": asset.asset_id,
                "symbol": asset.symbol,
                "market": asset.market,
                "source": source,
                "as_of": as_of,
                "tradable": asset.tradable,
                "trading_status": asset.status,
                "payload": payload,
            }
        )
        if include_realtime_quote:
            quote_rows.append(
                {
                    "asset_id": asset.asset_id,
                    "symbol": asset.symbol,
                    "market": asset.market,
                    "source": source,
                    "as_of": as_of,
                    "last_price": _first_decimal(payload, ("最新价", "最新", "单位净值")),
                    "prev_close": _first_decimal(payload, ("昨收", "昨收价")),
                    "open_price": _first_decimal(payload, ("今开", "开盘")),
                    "high": _first_decimal(payload, ("最高",)),
                    "low": _first_decimal(payload, ("最低",)),
                    "volume": _first_decimal(payload, ("成交量",)),
                    "amount": _first_decimal(payload, ("成交额",)),
                    "turnover_rate": _first_decimal(payload, ("换手率",)),
                    "change_amount": _first_decimal(payload, ("涨跌额",)),
                    "change_percent": _first_decimal(payload, ("涨跌幅", "日增长率")),
                    "status": asset.status,
                    "payload": payload,
                }
            )

    _persist_rows(repository, "upsert_asset_masters", "upsert_asset_master", master_rows)
    _persist_rows(repository, "upsert_asset_profiles", "upsert_asset_profile", profile_rows)
    _persist_rows(
        repository,
        "upsert_asset_provider_mappings",
        "upsert_asset_provider_mapping",
        mapping_rows,
    )
    _persist_rows(
        repository,
        "upsert_asset_status_snapshots",
        "upsert_asset_status_snapshot",
        status_rows,
    )
    if realtime_quote_mode == "latest" and callable(
        getattr(repository, "upsert_intraday_quote_latest", None)
    ):
        _persist_rows(
            repository,
            "upsert_intraday_quote_latest",
            "upsert_intraday_quote_latest",
            quote_rows,
        )
    else:
        _persist_rows(
            repository,
            "upsert_realtime_quote_snapshots",
            "upsert_realtime_quote_snapshot",
            quote_rows,
        )


def build_ashare_spot_valuation_rows(
    assets: Sequence[AssetData],
    *,
    collected_at: datetime,
    raw_record_id: str,
) -> list[JsonDict]:
    """从全市场实时行情原始行生成日级 PE/PB 估值快照。"""

    rows: list[JsonDict] = []
    for asset in assets:
        pe_ttm = _first_decimal(
            asset.payload,
            ("市盈率-动态", "市盈率动态", "PE(TTM)", "滚动市盈率"),
        )
        pb = _first_decimal(asset.payload, ("市净率", "PB"))
        missing_fields = [
            field_name
            for field_name, value in (("pe_ttm", pe_ttm), ("pb", pb))
            if value is None
        ]
        if len(missing_fields) == 2:
            continue
        raw = asset.payload.get("raw")
        source_observed_at = collected_at
        if isinstance(raw, dict):
            try:
                source_timestamp = int(raw.get("更新时间戳") or 0)
            except (TypeError, ValueError):
                source_timestamp = 0
            if source_timestamp > 0:
                source_observed_at = datetime.fromtimestamp(source_timestamp, tz=UTC)
        snapshot_at = ashare_daily_snapshot_at(source_observed_at)
        rows.append(
            {
                "snapshot_id": stable_id(
                    "fundamental",
                    ASHARE_SPOT_VALUATION_SOURCE,
                    asset.symbol,
                    snapshot_at,
                ),
                "asset_id": asset.asset_id,
                "symbol": asset.symbol,
                "report_period": None,
                "pe_ttm": pe_ttm,
                "pb": pb,
                "source": ASHARE_SPOT_VALUATION_SOURCE,
                "status": "partial" if missing_fields else "available",
                "missing_fields": missing_fields,
                "as_of": snapshot_at,
                "payload": {
                    "raw": raw if isinstance(raw, dict) else {},
                    "raw_record_id": raw_record_id,
                    "valuation_kind": "spot_snapshot",
                },
            }
        )
    return rows


def _persist_seed_identity_rows(
    repository: Any,
    seeds: Sequence[UniverseSeedData],
    *,
    raw_record_id: str,
    source: str,
) -> None:
    """批量写入候选池种子对应的资产占位和 Provider 映射。"""

    asset_rows: list[JsonDict] = []
    mapping_rows: list[JsonDict] = []
    for seed in seeds:
        payload = seed.payload | {
            "raw_record_id": raw_record_id,
            "universe_source": source,
        }
        asset_rows.append(
            {
                "asset_id": seed.asset_id,
                "symbol": seed.symbol,
                "name": seed.name,
                "market": seed.market,
                "asset_type": "stock",
                "currency": "CNY",
                "payload": payload,
            }
        )
        mapping_rows.append(
            {
                "asset_id": seed.asset_id,
                "symbol": seed.symbol,
                "market": seed.market,
                "provider": _provider_from_source(source),
                "provider_symbol": seed.symbol,
                "source": UNIVERSE_SEED_MAPPING_SOURCE,
                "payload": payload,
            }
        )
    _persist_rows(repository, "ensure_assets", "ensure_asset", asset_rows)
    _persist_rows(
        repository,
        "upsert_asset_provider_mappings",
        "upsert_asset_provider_mapping",
        mapping_rows,
    )


def _persist_asset_stub_rows(repository: Any, stubs: Sequence[JsonDict]) -> None:
    """批量写入 K 线等明细数据依赖的资产占位身份。"""

    if not stubs:
        return
    asset_rows: list[JsonDict] = []
    mapping_rows: list[JsonDict] = []
    seen_assets: set[str] = set()
    seen_mappings: set[tuple[str, str]] = set()
    for stub in stubs:
        asset_id = str(stub["asset_id"])
        symbol = str(stub["symbol"])
        market = str(stub["market"])
        source = str(stub["source"])
        if asset_id not in seen_assets:
            payload = dict(stub.get("payload") or {})
            payload.setdefault("source", source)
            asset_rows.append(
                {
                    "asset_id": asset_id,
                    "symbol": symbol,
                    "name": stub.get("name") or symbol,
                    "market": market,
                    "asset_type": stub.get("asset_type"),
                    "exchange": stub.get("exchange"),
                    "currency": stub.get("currency"),
                    "base_asset": stub.get("base_asset"),
                    "quote_asset": stub.get("quote_asset"),
                    "payload": payload,
                }
            )
            seen_assets.add(asset_id)
        mapping_key = (asset_id, source)
        if mapping_key not in seen_mappings:
            mapping_rows.append(
                {
                    "asset_id": asset_id,
                    "symbol": symbol,
                    "market": market,
                    "provider": _provider_from_source(source),
                    "provider_symbol": stub.get("provider_symbol") or symbol,
                    "provider_exchange": stub.get("provider_exchange") or stub.get("exchange"),
                    "source": source,
                    "payload": stub.get("payload") or {"source": source},
                }
            )
            seen_mappings.add(mapping_key)
    _persist_rows(repository, "ensure_assets", "ensure_asset", asset_rows)
    _persist_rows(
        repository,
        "upsert_asset_provider_mappings",
        "upsert_asset_provider_mapping",
        mapping_rows,
    )


def _crypto_asset_stub_row(
    *,
    asset_id: str,
    symbol: str,
    market: str,
    source: str,
) -> JsonDict:
    """生成数字货币资产占位行。"""

    base_asset, quote_asset = _split_crypto_symbol(symbol)
    return {
        "asset_id": asset_id,
        "symbol": symbol,
        "name": f"{base_asset} / {quote_asset}" if quote_asset else symbol,
        "market": market,
        "asset_type": "crypto",
        "exchange": "Binance",
        "currency": quote_asset,
        "base_asset": base_asset,
        "quote_asset": quote_asset,
        "provider_exchange": "Binance",
        "source": source,
        "payload": {"source": source},
    }


def _ashare_asset_stub_row(*, asset_id: str, symbol: str, source: str) -> JsonDict:
    """生成 A 股资产占位行，仅在权威资产池尚未补齐时兜底使用。"""

    return {
        "asset_id": asset_id,
        "symbol": symbol,
        "name": symbol,
        "market": "ashare",
        "asset_type": "stock",
        "currency": "CNY",
        "source": source,
        "payload": {"source": source},
    }


def _fund_asset_stub_row(*, asset_id: str, symbol: str, source: str) -> JsonDict:
    """生成基金 K 线资产占位行。"""

    return {
        "asset_id": asset_id,
        "symbol": symbol,
        "name": symbol,
        "market": "fund",
        "asset_type": "etf" if ":etf:" in asset_id else "lof",
        "currency": "CNY",
        "source": source,
        "payload": {"source": source},
    }


def _event_rows(events: Sequence[Any], *, raw_record_id: str) -> list[JsonDict]:
    return [
        {
            "event_id": event.event_id,
            "asset_id": event.asset_id,
            "symbol": event.symbol,
            "market": event.market,
            "event_type": event.event_type,
            "title": event.title,
            "summary": event.summary,
            "sentiment": event.sentiment,
            "importance": event.importance,
            "source": event.source,
            "url": event.url,
            "published_at": event.published_at,
            "collected_at": event.collected_at,
            "payload": event.payload | {"raw_record_id": raw_record_id},
        }
        for event in events
    ]


def _evidence_rows(evidence: Sequence[Any], *, raw_record_id: str) -> list[JsonDict]:
    return [
        {
            "evidence_id": item.evidence_id,
            "evidence_type": item.evidence_type,
            "asset_id": item.asset_id,
            "source": item.source,
            "title": item.title,
            "summary": item.summary,
            "data_ref": item.data_ref,
            "url": item.url,
            "reliability": item.reliability,
            "as_of": item.as_of,
            "collected_at": item.collected_at,
            "payload": item.payload | {"raw_record_id": raw_record_id},
        }
        for item in evidence
    ]


def _risk_rows(risks: Sequence[Any], *, raw_record_id: str) -> list[JsonDict]:
    return [
        {
            "risk_id": risk.risk_id,
            "asset_id": risk.asset_id,
            "scope": risk.scope,
            "risk_type": risk.risk_type,
            "severity": risk.severity,
            "score": risk.score,
            "title": risk.title,
            "description": risk.description,
            "as_of": risk.as_of,
            "evidence_ids": risk.evidence_ids,
            "payload": risk.payload | {"raw_record_id": raw_record_id},
        }
        for risk in risks
    ]


class CryptoDataCollector:
    """数字货币基础数据采集编排器。

    这里仅采集公开行情和衍生品公开快照，不包含账户、下单或交易执行能力。
    """

    def __init__(
        self,
        session: Session,
        *,
        spot_provider: CcxtBinanceProvider | None = None,
        future_provider: CcxtBinanceProvider | None = None,
        derivative_provider: BinanceNativeProvider | None = None,
    ) -> None:
        self.assets = AssetRepository(session)
        self.universes = UniverseRepository(session)
        self.market_data = MarketDataRepository(session)
        self.derivatives = DerivativeDataRepository(session)
        self.raw_records = RawRecordRepository(session)
        self.spot_provider = spot_provider or CcxtBinanceProvider(default_type="spot")
        self.future_provider = future_provider or CcxtBinanceProvider(default_type="future")
        self.derivative_provider = derivative_provider or BinanceNativeProvider()

    def collect_markets(
        self,
        *,
        market_type: str = "spot",
        universe_id: str,
        universe_name: str,
        strategy_context: str,
        limit: int | None = None,
    ) -> ArchivedProviderResult:
        """采集 Binance 交易对列表，并写入币种候选池。"""

        provider = self._ccxt_provider(market_type)
        result = provider.fetch_assets(limit=limit)
        market = self._crypto_market_name(market_type)
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint="ccxt_binance_load_markets",
            request_params={"market_type": market_type, "limit": limit},
            market=market,
        )

        if result.status != "available":
            logger.warning(
                "Binance 资产池刷新失败，保留上一版 universe 元信息 market=%s status=%s error=%s",
                market,
                result.status,
                result.error_message,
            )
            return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

        self.universes.upsert_universe(
            universe_id=universe_id,
            name=universe_name,
            source=f"ccxt:binance:{market_type}:load_markets",
            market=market,
            strategy_context=strategy_context,
            as_of=result.collected_at,
            total_before_filter=len(result.assets),
            total_after_filter=len(result.assets),
            status=result.status,
            payload={
                "provider_payload": result.payload,
                "raw_record_id": raw_record_id,
                "error": result.error_message,
            },
        )

        _persist_asset_identity_rows(
            self.assets,
            result.assets,
            as_of=result.collected_at,
            raw_record_id=raw_record_id,
            source=f"ccxt:binance:{market_type}:load_markets",
        )
        self.universes.replace_members(
            universe_id=universe_id,
            members=[
                {
                    "member_id": f"universe_member:{universe_id}:{asset.symbol}",
                    "asset_id": asset.asset_id,
                    "symbol": asset.symbol,
                    "market": asset.market,
                    "as_of": result.collected_at,
                    "rank_hint": index,
                    "payload": asset.payload | {"raw_record_id": raw_record_id},
                }
                for index, asset in enumerate(result.assets, start=1)
            ],
        )
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def collect_ohlcv(
        self,
        *,
        symbol: str,
        timeframe: str,
        market_type: str = "spot",
        start: str | None = None,
        end: str | None = None,
        limit: int | None = None,
    ) -> ArchivedProviderResult:
        """采集数字货币 K 线，并写入 `market_bars`。"""

        provider = self._ccxt_provider(market_type)
        result = provider.fetch_ohlcv(
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            limit=limit,
        )
        market = self._crypto_market_name(market_type)
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint="ccxt_binance_fetch_ohlcv",
            request_params={
                "symbol": symbol,
                "timeframe": timeframe,
                "market_type": market_type,
                "start": start,
                "end": end,
                "limit": limit,
            },
            symbol=symbol.replace("/", "").upper(),
            market=market,
        )
        if result.status != "available":
            return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

        asset_stubs: set[tuple[str, str, str, str]] = set()
        asset_stub_rows: list[JsonDict] = []
        bar_rows: list[JsonDict] = []
        for bar in result.bars:
            stub_key = (bar.asset_id, bar.symbol, bar.market, bar.source)
            if stub_key not in asset_stubs:
                asset_stub_rows.append(
                    _crypto_asset_stub_row(
                        asset_id=bar.asset_id,
                        symbol=bar.symbol,
                        market=bar.market,
                        source=bar.source,
                    )
                )
                asset_stubs.add(stub_key)
            bar_rows.append(_market_bar_values(bar, raw_record_id=raw_record_id))
        _persist_asset_stub_rows(self.assets, asset_stub_rows)
        self.market_data.upsert_bars(bar_rows, chunk_size=500)
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def collect_derivative_snapshot(self, *, symbol: str) -> ArchivedProviderResult:
        """采集 Binance U 本位合约衍生品公开快照。"""

        compact_symbol = symbol.replace("/", "").upper()
        result = self.derivative_provider.fetch_derivative_snapshot(symbol=compact_symbol)
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint="binance_derivative_snapshot",
            request_params={"symbol": compact_symbol},
            symbol=compact_symbol,
            market="crypto_future",
        )
        if result.status != "available" or result.snapshot is None:
            return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

        snapshot = result.snapshot
        _persist_asset_stub_rows(
            self.assets,
            [
                _crypto_asset_stub_row(
                    asset_id=snapshot.asset_id,
                    symbol=snapshot.symbol,
                    market=snapshot.market,
                    source=snapshot.source,
                )
            ],
        )
        _persist_rows(
            self.derivatives,
            "upsert_crypto_derivative_snapshots",
            "upsert_crypto_derivative_snapshot",
            [
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "asset_id": snapshot.asset_id,
                    "symbol": snapshot.symbol,
                    "market": snapshot.market,
                    "source": snapshot.source,
                    "as_of": snapshot.as_of,
                    "funding_rate": snapshot.funding_rate,
                    "next_funding_time": snapshot.next_funding_time,
                    "open_interest": snapshot.open_interest,
                    "open_interest_value": snapshot.open_interest_value,
                    "long_short_ratio": snapshot.long_short_ratio,
                    "basis_rate": snapshot.basis_rate,
                    "liquidation_risk_score": snapshot.liquidation_risk_score,
                    "status": snapshot.status,
                    "payload": snapshot.payload | {"raw_record_id": raw_record_id},
                }
            ],
        )
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def _ccxt_provider(self, market_type: str) -> CcxtBinanceProvider:
        if market_type in {"future", "swap"}:
            return self.future_provider
        return self.spot_provider

    @staticmethod
    def _crypto_market_name(market_type: str) -> str:
        return "crypto_future" if market_type in {"future", "swap"} else "crypto_spot"

    def _upsert_crypto_asset_stub(
        self,
        *,
        asset_id: str,
        symbol: str,
        market: str,
        source: str,
    ) -> None:
        _persist_asset_stub_rows(
            self.assets,
            [
                _crypto_asset_stub_row(
                    asset_id=asset_id,
                    symbol=symbol,
                    market=market,
                    source=source,
                )
            ],
        )

    def _persist_asset_identity_and_details(
        self,
        asset: AssetData,
        *,
        as_of: datetime,
        raw_record_id: str,
        source: str,
    ) -> None:
        _persist_asset_identity_rows(
            self.assets,
            [asset],
            as_of=as_of,
            raw_record_id=raw_record_id,
            source=source,
        )


def _split_crypto_symbol(symbol: str) -> tuple[str, str | None]:
    """把 BTCUSDT 或 BTC/USDT 拆成基础币和计价币。"""

    normalized = symbol.replace("/", "").upper()
    common_quotes = ["USDT", "USDC", "BUSD", "BTC", "ETH", "USD"]
    for quote in common_quotes:
        if normalized.endswith(quote) and len(normalized) > len(quote):
            return normalized[: -len(quote)], quote
    return normalized, None


class AshareP0Collector:
    """A 股 P0 资产和行情采集编排器。"""

    def __init__(
        self,
        session: Session,
        *,
        provider: AkshareProvider | None = None,
    ) -> None:
        self.assets = AssetRepository(session)
        self.universes = UniverseRepository(session)
        self.market_data = MarketDataRepository(session)
        self.fundamentals = FundamentalDataRepository(session)
        self.raw_records = RawRecordRepository(session)
        self.provider = provider or AkshareProvider()

    def collect_assets(
        self,
        *,
        universe_id: str,
        universe_name: str,
        strategy_context: str,
        limit: int | None = None,
    ) -> ArchivedProviderResult:
        """采集 A 股资产列表，并写入全 A 候选池种子。"""

        result = self.provider.fetch_assets(limit=limit)
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint="stock_zh_a_spot",
            request_params={"limit": limit},
            market="ashare",
        )
        self.universes.upsert_universe(
            universe_id=universe_id,
            name=universe_name,
            source="akshare:stock_zh_a_spot",
            market="ashare",
            strategy_context=strategy_context,
            as_of=result.collected_at,
            total_before_filter=len(result.assets),
            total_after_filter=sum(1 for asset in result.assets if asset.tradable),
            status=result.status,
            payload={
                "provider_payload": result.payload,
                "raw_record_id": raw_record_id,
                "error": result.error_message,
            },
        )
        if result.status != "available":
            return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

        _persist_asset_identity_rows(
            self.assets,
            result.assets,
            as_of=result.collected_at,
            raw_record_id=raw_record_id,
            source="akshare:stock_zh_a_spot",
            include_realtime_quote=True,
            realtime_quote_mode="latest",
        )
        _persist_rows(
            self.fundamentals,
            "upsert_fundamental_snapshots",
            "upsert_fundamental_snapshot",
            build_ashare_spot_valuation_rows(
                result.assets,
                collected_at=result.collected_at,
                raw_record_id=raw_record_id,
            ),
        )
        self.universes.replace_members(
            universe_id=universe_id,
            members=[
                {
                    "member_id": f"universe_member:{universe_id}:{asset.symbol}",
                    "asset_id": asset.asset_id,
                    "symbol": asset.symbol,
                    "market": asset.market,
                    "as_of": result.collected_at,
                    "included": asset.tradable,
                    "removed_reason": None if asset.tradable else "untradable_realtime_quote",
                    "rank_hint": index,
                    "payload": asset.payload | {"raw_record_id": raw_record_id},
                }
                for index, asset in enumerate(result.assets, start=1)
            ],
        )
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def collect_ohlcv(
        self,
        *,
        symbol: str,
        timeframe: str = "1d",
        start: str | None = None,
        end: str | None = None,
        limit: int | None = None,
        adjust: str = "qfq",
        is_closed: bool = True,
        status: str = "available",
        source_gate: Callable[[str, Callable[[], Any]], Any] | None = None,
    ) -> ArchivedProviderResult:
        """采集 A 股 K 线，并写入 `market_bars`。"""

        total_started = time.perf_counter()
        logger.info(
            "A 股 K 线采集开始 symbol=%s timeframe=%s start=%s end=%s limit=%s adjust=%s "
            "is_closed=%s status=%s",
            symbol,
            timeframe,
            start,
            end,
            limit,
            adjust,
            is_closed,
            status,
        )
        provider_started = time.perf_counter()
        result = self.provider.fetch_ohlcv(
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            limit=limit,
            adjust=adjust,
            is_closed=is_closed,
            status=status,
            source_gate=source_gate,
        )
        provider_elapsed = round(time.perf_counter() - provider_started, 3)
        logger.info(
            "A 股 K 线 Provider 请求完成 symbol=%s status=%s bars=%s source=%s "
            "provider_elapsed_seconds=%.3f source_attempts=%s",
            symbol,
            result.status,
            len(result.bars),
            result.payload.get("actual_source"),
            provider_elapsed,
            result.payload.get("source_attempts"),
        )
        result.payload.setdefault("timing", {})["provider_elapsed_seconds"] = provider_elapsed

        archive_started = time.perf_counter()
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint=_endpoint_from_source(
                str(result.payload.get("primary_source") or "stock_zh_a_hist_tx")
            ),
            request_params={
                "symbol": symbol,
                "timeframe": timeframe,
                "start": start,
                "end": end,
                "limit": limit,
                "adjust": adjust,
            },
            symbol=symbol,
            market="ashare",
        )
        archive_elapsed = round(time.perf_counter() - archive_started, 3)
        result.payload.setdefault("timing", {})["raw_archive_elapsed_seconds"] = archive_elapsed
        logger.info(
            "A 股 K 线 raw_records 归档完成 symbol=%s raw_record_id=%s "
            "archive_elapsed_seconds=%.3f",
            symbol,
            raw_record_id,
            archive_elapsed,
        )
        if result.status != "available":
            total_elapsed = round(time.perf_counter() - total_started, 3)
            result.payload.setdefault("timing", {}).update(
                {
                    "market_bars_persist_elapsed_seconds": 0.0,
                    "persisted_bar_count": 0,
                    "total_elapsed_seconds": total_elapsed,
                }
            )
            logger.info(
                "A 股 K 线采集落库完成 symbol=%s status=%s bars=%s persisted_bars=0 "
                "total_elapsed_seconds=%.3f error=%s",
                symbol,
                result.status,
                len(result.bars),
                total_elapsed,
                result.error_message,
            )
            return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

        persist_started = time.perf_counter()
        asset_stubs: set[tuple[str, str, str]] = set()
        asset_stub_rows: list[JsonDict] = []
        bar_rows: list[JsonDict] = []
        for bar in result.bars:
            stub_key = (bar.asset_id, bar.symbol, bar.source)
            if stub_key not in asset_stubs:
                asset_stub_rows.append(
                    _ashare_asset_stub_row(
                        asset_id=bar.asset_id,
                        symbol=bar.symbol,
                        source=bar.source,
                    )
                )
                asset_stubs.add(stub_key)
            bar_rows.append(
                _market_bar_values(
                    bar,
                    raw_record_id=raw_record_id,
                    source=_standard_market_bar_source(bar),
                )
            )
        _persist_asset_stub_rows(self.assets, asset_stub_rows)
        persisted_count = self.market_data.upsert_bars(bar_rows, chunk_size=500)
        persist_elapsed = round(time.perf_counter() - persist_started, 3)
        total_elapsed = round(time.perf_counter() - total_started, 3)
        result.payload.setdefault("timing", {}).update(
            {
                "market_bars_persist_elapsed_seconds": persist_elapsed,
                "persisted_bar_count": persisted_count,
                "total_elapsed_seconds": total_elapsed,
            }
        )
        logger.info(
            "A 股 K 线标准表入库完成 symbol=%s rows=%s persist_elapsed_seconds=%.3f "
            "rows_per_second=%.2f",
            symbol,
            persisted_count,
            persist_elapsed,
            persisted_count / persist_elapsed if persist_elapsed > 0 else float(persisted_count),
        )
        logger.info(
            "A 股 K 线采集落库完成 symbol=%s status=%s bars=%s persisted_bars=%s "
            "source=%s provider_elapsed_seconds=%.3f archive_elapsed_seconds=%.3f "
            "persist_elapsed_seconds=%.3f total_elapsed_seconds=%.3f",
            symbol,
            result.status,
            len(result.bars),
            persisted_count,
            result.payload.get("actual_source"),
            provider_elapsed,
            archive_elapsed,
            persist_elapsed,
            total_elapsed,
        )
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def _ensure_asset_stub(self, *, asset_id: str, symbol: str, source: str) -> None:
        """仅在资产不存在时写入占位资产，避免覆盖已有名称和交易所信息。"""

        _persist_asset_stub_rows(
            self.assets,
            [_ashare_asset_stub_row(asset_id=asset_id, symbol=symbol, source=source)],
        )

    def _persist_asset_identity_and_details(
        self,
        asset: AssetData,
        *,
        as_of: datetime,
        raw_record_id: str,
        source: str,
    ) -> None:
        _persist_asset_identity_rows(
            self.assets,
            [asset],
            as_of=as_of,
            raw_record_id=raw_record_id,
            source=source,
            include_realtime_quote=True,
        )


class FundDataCollector:
    """基金资产池、场内基金日 K 和开放式基金净值采集编排器。"""

    def __init__(
        self,
        session: Session,
        *,
        provider: AkshareFundProvider | None = None,
    ) -> None:
        self.assets = AssetRepository(session)
        self.universes = UniverseRepository(session)
        self.market_data = MarketDataRepository(session)
        self.fund_nav = FundNavRepository(session)
        self.raw_records = RawRecordRepository(session)
        self.provider = provider or AkshareFundProvider()

    def collect_universe(
        self,
        *,
        universe_id: str,
        universe_name: str,
        strategy_context: str,
    ) -> list[ArchivedProviderResult]:
        """顺序刷新 ETF、LOF、开放式基金资产池，并回写统一候选池。"""

        existing_assets = self.assets.find_by_market("fund", only_tradable=False)
        results = [
            (
                "akshare:fund_etf_spot_em",
                "etf",
                self.provider.fetch_etf_assets(),
            ),
            (
                "akshare:fund_lof_spot_em",
                "lof",
                self.provider.fetch_lof_assets(),
            ),
            (
                "akshare:fund_open_fund_daily_em",
                "open_fund",
                self.provider.fetch_open_fund_assets(),
            ),
        ]
        archived_results: list[ArchivedProviderResult] = []
        all_assets: list[AssetData] = []
        exchange_traded_symbols = {
            str(asset.symbol or "").strip()
            for asset in existing_assets
            if (
                asset.asset_type in EXCHANGE_TRADED_FUND_TYPES
                and str(asset.symbol or "").strip()
            )
        }
        latest_as_of: datetime | None = None
        for source, asset_type, result in results:
            raw_record_id = archive_provider_result(
                self.raw_records,
                result,
                endpoint=_endpoint_from_source(source),
                request_params={},
                market="fund",
            )
            archived_results.append(ArchivedProviderResult(result=result, raw_record_id=raw_record_id))
            latest_as_of = max(
                [value for value in [latest_as_of, result.collected_at] if value is not None]
            )
            if result.status != "available":
                all_assets.extend(
                    _existing_fund_asset_data(asset)
                    for asset in existing_assets
                    if asset.asset_type == asset_type
                )
                continue
            assets_to_persist = list(result.assets)
            result_asset_types = {asset.asset_type for asset in assets_to_persist}
            current_exchange_traded_symbols = {
                asset.symbol
                for asset in assets_to_persist
                if asset.asset_type in EXCHANGE_TRADED_FUND_TYPES
            }
            if current_exchange_traded_symbols:
                self.assets.delete_fund_open_placeholders_without_nav(current_exchange_traded_symbols)
                exchange_traded_symbols.update(current_exchange_traded_symbols)
            if "open_fund" in result_asset_types and exchange_traded_symbols:
                assets_to_persist = [
                    asset for asset in assets_to_persist if asset.symbol not in exchange_traded_symbols
                ]
            all_assets.extend(assets_to_persist)
            _persist_asset_identity_rows(
                self.assets,
                assets_to_persist,
                as_of=result.collected_at,
                raw_record_id=raw_record_id,
                source=source,
            )

        if latest_as_of is None:
            latest_as_of = datetime.now(tz=UTC)
        all_assets = _deduplicate_fund_assets_by_symbol(all_assets)
        self.universes.upsert_universe(
            universe_id=universe_id,
            name=universe_name,
            source="akshare:fund_universe",
            market="fund",
            strategy_context=strategy_context,
            as_of=latest_as_of,
            total_before_filter=len(all_assets),
            total_after_filter=len(all_assets),
            status="available" if all_assets else "unavailable",
            payload={
                "asset_count": len(all_assets),
                "source_count": len(results),
                "sources": [source for source, _, _ in results],
            },
        )
        if all_assets:
            self.universes.replace_members(
                universe_id=universe_id,
                members=[
                    {
                        "member_id": f"universe_member:{universe_id}:{asset.asset_id}",
                        "asset_id": asset.asset_id,
                        "symbol": asset.symbol,
                        "market": asset.market,
                        "as_of": latest_as_of,
                        "rank_hint": index,
                        "payload": asset.payload,
                    }
                    for index, asset in enumerate(all_assets, start=1)
                ],
            )
            self.universes.prune_missing_members(
                universe_id=universe_id,
                current_asset_ids=[asset.asset_id for asset in all_assets],
                as_of=latest_as_of,
                removed_reason="not_in_latest_fund_universe",
            )
        return archived_results

    def collect_etf_ohlcv(
        self,
        *,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
        limit: int | None = None,
        is_closed: bool = True,
        status: str = "available",
    ) -> ArchivedProviderResult:
        """采集 ETF 历史日 K。"""

        result = self.provider.fetch_etf_ohlcv(
            symbol=symbol,
            start_date=start,
            end_date=end,
            limit=limit,
            is_closed=is_closed,
            status=status,
        )
        return self._persist_fund_bars(result=result, symbol=symbol, endpoint="fund_etf_hist_em")

    def collect_lof_ohlcv(
        self,
        *,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
        limit: int | None = None,
        is_closed: bool = True,
        status: str = "available",
    ) -> ArchivedProviderResult:
        """采集 LOF 历史日 K。"""

        result = self.provider.fetch_lof_ohlcv(
            symbol=symbol,
            start_date=start,
            end_date=end,
            limit=limit,
            is_closed=is_closed,
            status=status,
        )
        return self._persist_fund_bars(result=result, symbol=symbol, endpoint="fund_lof_hist_em")

    def collect_open_fund_nav(
        self,
        *,
        symbol: str,
        limit: int | None = None,
    ) -> ArchivedProviderResult:
        """采集开放式基金净值历史。"""

        result = self.provider.fetch_open_fund_nav(symbol=symbol, limit=limit)
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint="fund_open_fund_info_em",
            request_params={"symbol": symbol, "limit": limit},
            symbol=symbol,
            market="fund",
        )
        if result.status != "available":
            return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

        _persist_rows(
            self.assets,
            "ensure_assets",
            "ensure_asset",
            [
                {
                    "asset_id": f"fund:open:{symbol}",
                    "symbol": symbol,
                    "name": symbol,
                    "market": "fund",
                    "asset_type": "open_fund",
                    "currency": "CNY",
                    "payload": {"source": "akshare:fund_open_fund_info_em"},
                }
            ],
        )
        _persist_rows(
            self.assets,
            "upsert_asset_provider_mappings",
            "upsert_asset_provider_mapping",
            [
                {
                    "asset_id": f"fund:open:{symbol}",
                    "symbol": symbol,
                    "market": "fund",
                    "provider": "akshare",
                    "provider_symbol": symbol,
                    "source": "akshare:fund_open_fund_info_em",
                }
            ],
        )
        _persist_rows(
            self.fund_nav,
            "upsert_snapshots",
            "upsert_snapshot",
            [
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "asset_id": snapshot.asset_id,
                    "symbol": snapshot.symbol,
                    "market": snapshot.market,
                    "nav_date": snapshot.nav_date,
                    "source": snapshot.source,
                    "unit_nav": snapshot.unit_nav,
                    "accumulated_nav": snapshot.accumulated_nav,
                    "daily_return": snapshot.daily_return,
                    "purchase_status": snapshot.purchase_status,
                    "redeem_status": snapshot.redeem_status,
                    "status": snapshot.status,
                    "payload": snapshot.payload | {"raw_record_id": raw_record_id},
                }
                for snapshot in result.snapshots
            ],
        )
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def _persist_fund_bars(
        self,
        *,
        result: MarketBarsResult,
        symbol: str,
        endpoint: str,
    ) -> ArchivedProviderResult:
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint=endpoint,
            request_params={"symbol": symbol},
            symbol=symbol,
            market="fund",
        )
        if result.status != "available":
            return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)
        asset_stubs: set[tuple[str, str, str]] = set()
        asset_stub_rows: list[JsonDict] = []
        bar_rows: list[JsonDict] = []
        for bar in result.bars:
            stub_key = (bar.asset_id, bar.symbol, bar.source)
            if stub_key not in asset_stubs:
                asset_stub_rows.append(
                    _fund_asset_stub_row(
                        asset_id=bar.asset_id,
                        symbol=bar.symbol,
                        source=bar.source,
                    )
                )
                asset_stubs.add(stub_key)
            bar_rows.append(_market_bar_values(bar, raw_record_id=raw_record_id))
        _persist_asset_stub_rows(self.assets, asset_stub_rows)
        self.market_data.upsert_bars(bar_rows, chunk_size=500)
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def _ensure_asset_stub(self, *, asset_id: str, symbol: str, source: str) -> None:
        """仅在基金资产不存在时写入占位身份。"""

        _persist_asset_stub_rows(
            self.assets,
            [_fund_asset_stub_row(asset_id=asset_id, symbol=symbol, source=source)],
        )

    def _persist_asset_identity_and_details(
        self,
        asset: AssetData,
        *,
        as_of: datetime,
        raw_record_id: str,
        source: str,
    ) -> None:
        _persist_asset_identity_rows(
            self.assets,
            [asset],
            as_of=as_of,
            raw_record_id=raw_record_id,
            source=source,
            include_realtime_quote=True,
        )


class AshareP1Collector:
    """A 股 P1 数据采集编排器。"""

    def __init__(
        self,
        session: Session,
        *,
        sector_provider: AshareSectorProvider | None = None,
        flow_provider: AshareCapitalFlowProvider | None = None,
        event_provider: AshareEventProvider | None = None,
        article_fetcher: Any | None = None,
    ) -> None:
        self.assets = AssetRepository(session)
        self.universes = UniverseRepository(session)
        self.capital_flows = CapitalFlowRepository(session)
        self.events = EventRepository(session)
        self.raw_records = RawRecordRepository(session)
        self.sector_provider = sector_provider or AshareSectorProvider()
        self.flow_provider = flow_provider or AshareCapitalFlowProvider()
        self.event_provider = event_provider or AshareEventProvider()
        self.article_fetcher = (
            article_fetcher if article_fetcher is not None else EastmoneyArticleFetcher()
        )

    def collect_industry_members(
        self,
        *,
        industry_name: str,
        universe_id: str,
        universe_name: str,
        strategy_context: str,
        limit: int | None = None,
    ) -> ArchivedProviderResult:
        """采集行业成分种子，并写入候选池定义和成员。"""

        result = self.sector_provider.fetch_industry_members(
            industry_name=industry_name,
            limit=limit,
        )
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint="stock_board_industry_cons_em",
            request_params={"symbol": industry_name, "limit": limit},
        )

        self.universes.upsert_universe(
            universe_id=universe_id,
            name=universe_name,
            source="akshare:stock_board_industry_cons_em",
            market="ashare",
            strategy_context=strategy_context,
            as_of=result.collected_at,
            total_before_filter=len(result.seeds),
            total_after_filter=len(result.seeds),
            status=result.status,
            payload={
                "provider_payload": result.payload,
                "raw_record_id": raw_record_id,
                "error": result.error_message,
            },
        )
        if result.status != "available":
            return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

        _persist_seed_identity_rows(
            self.assets,
            result.seeds,
            raw_record_id=raw_record_id,
            source="akshare:stock_board_industry_cons_em",
        )
        self.universes.replace_members(
            universe_id=universe_id,
            members=[
                {
                    "member_id": f"universe_member:{universe_id}:{seed.symbol}",
                    "asset_id": seed.asset_id,
                    "symbol": seed.symbol,
                    "market": seed.market,
                    "as_of": seed.as_of or result.collected_at,
                    "rank_hint": seed.rank_hint,
                    "payload": seed.payload | {"raw_record_id": raw_record_id},
                }
                for seed in result.seeds
            ],
        )
        if _is_complete_sector_member_snapshot(result, limit=limit):
            self.universes.prune_missing_members(
                universe_id=universe_id,
                current_asset_ids=[seed.asset_id for seed in result.seeds],
                as_of=result.collected_at,
                removed_reason="not_in_latest_sector_snapshot",
            )
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def collect_index_members(
        self,
        *,
        index_code: str,
        index_name: str,
        universe_id: str,
        universe_name: str,
        strategy_context: str,
        limit: int | None = None,
    ) -> ArchivedProviderResult:
        """采集指数成分种子，并写入候选池定义和成员。"""

        result = self.sector_provider.fetch_index_members(
            index_code=index_code,
            index_name=index_name,
            limit=limit,
        )
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint="index_stock_cons_csindex",
            request_params={"index_code": index_code, "index_name": index_name, "limit": limit},
        )

        self.universes.upsert_universe(
            universe_id=universe_id,
            name=universe_name,
            source=f"akshare:index_stock_cons_csindex:{index_code}",
            market="ashare",
            strategy_context=strategy_context,
            as_of=result.collected_at,
            total_before_filter=len(result.seeds),
            total_after_filter=len(result.seeds),
            status=result.status,
            payload={
                "provider_payload": result.payload,
                "raw_record_id": raw_record_id,
                "error": result.error_message,
            },
        )
        if result.status != "available":
            return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

        _persist_seed_identity_rows(
            self.assets,
            result.seeds,
            raw_record_id=raw_record_id,
            source=f"akshare:index_stock_cons_csindex:{index_code}",
        )
        self.universes.replace_members(
            universe_id=universe_id,
            members=[
                {
                    "member_id": f"universe_member:{universe_id}:{seed.symbol}",
                    "asset_id": seed.asset_id,
                    "symbol": seed.symbol,
                    "market": seed.market,
                    "as_of": seed.as_of or result.collected_at,
                    "rank_hint": seed.rank_hint,
                    "payload": seed.payload | {"raw_record_id": raw_record_id},
                }
                for seed in result.seeds
            ],
        )
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def collect_concept_members(
        self,
        *,
        concept_name: str,
        universe_id: str,
        universe_name: str,
        strategy_context: str,
        limit: int | None = None,
    ) -> ArchivedProviderResult:
        """采集概念成分种子，并写入候选池定义和成员。"""

        result = self.sector_provider.fetch_concept_members(
            concept_name=concept_name,
            limit=limit,
        )
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint="stock_board_concept_cons_em",
            request_params={"symbol": concept_name, "limit": limit},
        )

        self.universes.upsert_universe(
            universe_id=universe_id,
            name=universe_name,
            source="akshare:stock_board_concept_cons_em",
            market="ashare",
            strategy_context=strategy_context,
            as_of=result.collected_at,
            total_before_filter=len(result.seeds),
            total_after_filter=len(result.seeds),
            status=result.status,
            payload={
                "provider_payload": result.payload,
                "raw_record_id": raw_record_id,
                "error": result.error_message,
            },
        )
        if result.status != "available":
            return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

        _persist_seed_identity_rows(
            self.assets,
            result.seeds,
            raw_record_id=raw_record_id,
            source="akshare:stock_board_concept_cons_em",
        )
        self.universes.replace_members(
            universe_id=universe_id,
            members=[
                {
                    "member_id": f"universe_member:{universe_id}:{seed.symbol}",
                    "asset_id": seed.asset_id,
                    "symbol": seed.symbol,
                    "market": seed.market,
                    "as_of": seed.as_of or result.collected_at,
                    "rank_hint": seed.rank_hint,
                    "payload": seed.payload | {"raw_record_id": raw_record_id},
                }
                for seed in result.seeds
            ],
        )
        if _is_complete_sector_member_snapshot(result, limit=limit):
            self.universes.prune_missing_members(
                universe_id=universe_id,
                current_asset_ids=[seed.asset_id for seed in result.seeds],
                as_of=result.collected_at,
                removed_reason="not_in_latest_sector_snapshot",
            )
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def collect_flow_rank(
        self,
        *,
        indicator: str = "今日",
        limit: int | None = None,
    ) -> ArchivedProviderResult:
        """采集个股资金流排名，并写入资金流快照。"""

        result = self.flow_provider.fetch_flow_rank(indicator=indicator, limit=limit)
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint="stock_individual_fund_flow_rank",
            request_params={"indicator": indicator, "limit": limit},
        )
        if result.status != "available":
            return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

        _persist_rows(
            self.assets,
            "ensure_assets",
            "ensure_asset",
            [
                {
                    "asset_id": snapshot.asset_id,
                    "symbol": snapshot.symbol,
                    "name": snapshot.symbol,
                    "market": snapshot.market,
                    "asset_type": "stock",
                    "payload": {"source": snapshot.source},
                }
                for snapshot in result.snapshots
            ],
        )
        _persist_rows(
            self.capital_flows,
            "upsert_capital_flow_snapshots",
            "upsert_capital_flow_snapshot",
            [
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "asset_id": snapshot.asset_id,
                    "symbol": snapshot.symbol,
                    "market": snapshot.market,
                    "main_net_inflow": snapshot.main_net_inflow,
                    "northbound_net_inflow": snapshot.northbound_net_inflow,
                    "turnover_rate": snapshot.turnover_rate,
                    "amount": snapshot.amount,
                    "window": snapshot.window,
                    "source": snapshot.source,
                    "status": snapshot.status,
                    "as_of": snapshot.as_of,
                    "payload": snapshot.payload | {"raw_record_id": raw_record_id},
                }
                for snapshot in result.snapshots
            ],
        )
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def collect_individual_flow(
        self,
        *,
        symbol: str,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int | None = None,
    ) -> ArchivedProviderResult:
        """采集单只 A 股历史资金流，并写入资金流快照。"""

        result = self.flow_provider.fetch_individual_flow(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint="stock_individual_fund_flow",
            request_params={
                "symbol": symbol,
                "start_date": start_date.isoformat() if start_date else None,
                "end_date": end_date.isoformat() if end_date else None,
                "limit": limit,
            },
        )
        if result.status != "available":
            return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

        _persist_rows(
            self.assets,
            "ensure_assets",
            "ensure_asset",
            [
                {
                    "asset_id": snapshot.asset_id,
                    "symbol": snapshot.symbol,
                    "name": snapshot.symbol,
                    "market": snapshot.market,
                    "asset_type": "stock",
                    "payload": {"source": snapshot.source},
                }
                for snapshot in result.snapshots
            ],
        )
        _persist_rows(
            self.capital_flows,
            "upsert_capital_flow_snapshots",
            "upsert_capital_flow_snapshot",
            [
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "asset_id": snapshot.asset_id,
                    "symbol": snapshot.symbol,
                    "market": snapshot.market,
                    "main_net_inflow": snapshot.main_net_inflow,
                    "northbound_net_inflow": snapshot.northbound_net_inflow,
                    "turnover_rate": snapshot.turnover_rate,
                    "amount": snapshot.amount,
                    "window": snapshot.window,
                    "source": snapshot.source,
                    "status": snapshot.status,
                    "as_of": snapshot.as_of,
                    "payload": snapshot.payload | {"raw_record_id": raw_record_id},
                }
                for snapshot in result.snapshots
            ],
        )
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def collect_northbound_flow(
        self,
        *,
        symbol: str = "北向资金",
        limit: int | None = None,
    ) -> ArchivedProviderResult:
        """采集北向资金市场级或个股数据，并写入资金流快照。"""

        result = self.flow_provider.fetch_northbound_flow(symbol=symbol, limit=limit)
        endpoint = (
            "stock_hsgt_hist_em"
            if str(symbol or "").strip() in {"北向资金", "沪股通", "深股通"}
            else "stock_hsgt_individual_em"
        )
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint=endpoint,
            request_params={"symbol": symbol, "limit": limit},
            symbol=None if endpoint == "stock_hsgt_hist_em" else symbol,
        )
        if result.status != "available":
            return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

        _persist_rows(
            self.capital_flows,
            "upsert_capital_flow_snapshots",
            "upsert_capital_flow_snapshot",
            [
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "asset_id": snapshot.asset_id,
                    "symbol": snapshot.symbol,
                    "market": snapshot.market,
                    "main_net_inflow": snapshot.main_net_inflow,
                    "northbound_net_inflow": snapshot.northbound_net_inflow,
                    "turnover_rate": snapshot.turnover_rate,
                    "amount": snapshot.amount,
                    "window": snapshot.window,
                    "source": snapshot.source,
                    "status": snapshot.status,
                    "as_of": snapshot.as_of,
                    "payload": snapshot.payload | {"raw_record_id": raw_record_id},
                }
                for snapshot in result.snapshots
            ],
        )
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def collect_stock_news(
        self,
        *,
        symbol: str,
        asset_name: str | None = None,
        limit: int | None = None,
        enrich_articles: bool = True,
    ) -> ArchivedProviderResult:
        """采集个股新闻，并写入事件和证据表。"""

        result = self.event_provider.fetch_stock_news(
            symbol=symbol,
            asset_name=str(asset_name or "").strip(),
            limit=limit,
        )
        if enrich_articles and result.status == "available":
            result = self._enrich_stock_news_articles(result)
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint="stock_news_em",
            request_params={
                "symbol": symbol,
                "limit": limit,
                "enrich_articles": enrich_articles,
            },
            symbol=symbol,
        )
        if result.status != "available":
            return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

        _persist_rows(
            self.assets,
            "ensure_assets",
            "ensure_asset",
            [
                {
                    "asset_id": f"ashare:{symbol}",
                    "symbol": symbol,
                    "name": asset_name or symbol,
                    "market": "ashare",
                    "asset_type": "stock",
                    "payload": {"source": "akshare:stock_news_em"},
                }
            ],
        )
        _persist_rows(
            self.events,
            "upsert_events",
            "upsert_event",
            _event_rows(result.events, raw_record_id=raw_record_id),
        )
        _persist_rows(
            self.events,
            "upsert_evidence_items",
            "upsert_evidence",
            _evidence_rows(result.evidence, raw_record_id=raw_record_id),
        )
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def enrich_existing_stock_news_article(
        self,
        *,
        event_id: str,
        url: str,
        asset_id: str | None = None,
        symbol: str | None = None,
        title: str | None = None,
        source_excerpt: str | None = None,
    ) -> ArchivedProviderResult:
        """补抓已入库新闻正文，并回填事件和证据 payload。"""

        article_payload = self._fetch_article_payload(url)
        if source_excerpt:
            article_payload = article_payload | {"source_excerpt": source_excerpt}
        status = str(article_payload.get("status") or "error")
        now = datetime.now(tz=UTC)
        result = EventRecordsResult(
            provider_name="eastmoney_article",
            status="available" if status == "available" else "error",
            collected_at=now,
            events=[
                EventRecordData(
                    event_id=event_id,
                    asset_id=asset_id,
                    symbol=symbol,
                    market="ashare",
                    event_type="news_article",
                    title=title or event_id,
                    source="eastmoney:article_page",
                    collected_at=now,
                    summary=source_excerpt,
                    url=url,
                    payload={"article": article_payload},
                )
            ],
            payload={
                "endpoint": "stock_news_article",
                "event_id": event_id,
                "url": url,
                "article_fetch": {
                    "enabled": True,
                    "attempted": 1,
                    "available": 1 if status == "available" else 0,
                    "failed": 1 if status == "error" else 0,
                    "unavailable": 1 if status == "unavailable" else 0,
                },
            },
            error_message=str(article_payload.get("error_message") or "")
            if status == "error"
            else None,
        )
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint="stock_news_article",
            request_params={"event_id": event_id, "url": url},
            symbol=symbol,
        )
        article_update = {
            "event_id": event_id,
            "article_payload": article_payload | {"raw_record_id": raw_record_id},
        }
        _persist_rows(
            self.events,
            "update_event_article_payloads",
            "update_event_article_payload",
            [article_update],
        )
        _persist_rows(
            self.events,
            "update_evidence_article_payloads_by_events",
            "update_evidence_article_payloads_by_event",
            [article_update],
        )
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def _enrich_stock_news_articles(self, result: EventRecordsResult) -> EventRecordsResult:
        """按新闻链接补抓原文，并把抓取状态写入事件和证据 payload。"""

        if getattr(self, "article_fetcher", None) is None:
            return result

        article_cache: dict[str, JsonDict] = {}
        articles_by_event_id: dict[str, JsonDict] = {}
        enriched_events = []

        for event in result.events:
            article_payload: JsonDict | None = None
            if event.url:
                if event.url not in article_cache:
                    article_cache[event.url] = self._fetch_article_payload(event.url)
                base_payload = article_cache[event.url]
                article_payload = base_payload | {"source_excerpt": event.summary}
                articles_by_event_id[event.event_id] = article_payload

            enriched_events.append(
                replace(
                    event,
                    payload=(
                        event.payload | {"article": article_payload}
                        if article_payload
                        else event.payload
                    ),
                )
            )

        enriched_evidence = []
        for item in result.evidence:
            article_payload = articles_by_event_id.get(str(item.data_ref or ""))
            enriched_evidence.append(
                replace(
                    item,
                    payload=(
                        item.payload | {"article": article_payload}
                        if article_payload
                        else item.payload
                    ),
                )
            )

        article_statuses = [payload.get("status") for payload in article_cache.values()]
        attempted = len(article_cache)
        available = sum(1 for status in article_statuses if status == "available")
        failed = sum(1 for status in article_statuses if status == "error")

        return replace(
            result,
            events=enriched_events,
            evidence=enriched_evidence,
            payload=result.payload
            | {
                "article_fetch": {
                    "enabled": True,
                    "attempted": attempted,
                    "available": available,
                    "failed": failed,
                    "unavailable": attempted - available - failed,
                }
            },
        )

    def _fetch_article_payload(self, url: str) -> JsonDict:
        """调用正文抓取器，并把异常也转换成可追踪 payload。"""

        try:
            result = self.article_fetcher.fetch(url)
        except Exception as exc:
            return {
                "url": url,
                "status": "error",
                "source": "eastmoney:article_page",
                "fetched_at": datetime.now(tz=UTC).isoformat(),
                "title": None,
                "full_text": None,
                "text_length": 0,
                "html_length": None,
                "truncated": False,
                "error_message": str(exc),
            }
        payload = result.to_payload()
        if not isinstance(payload, dict):
            return {
                "url": url,
                "status": "error",
                "source": "eastmoney:article_page",
                "fetched_at": datetime.now(tz=UTC).isoformat(),
                "title": None,
                "full_text": None,
                "text_length": 0,
                "html_length": None,
                "truncated": False,
                "error_message": "article_fetcher 返回了非字典 payload",
            }
        return payload

    def collect_notice_reports(
        self,
        *,
        symbol: str = "全部",
        date: str,
        limit: int | None = None,
    ) -> ArchivedProviderResult:
        """采集公告披露事件，并写入事件和证据表。"""

        result = self.event_provider.fetch_notice_reports(
            symbol=symbol,
            date=date,
            limit=limit,
        )
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint="stock_notice_report",
            request_params={"symbol": symbol, "date": date, "limit": limit},
            symbol=symbol if symbol != "全部" else None,
        )
        if result.status != "available":
            return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

        _persist_rows(
            self.events,
            "upsert_events",
            "upsert_event",
            _event_rows(result.events, raw_record_id=raw_record_id),
        )
        _persist_rows(
            self.events,
            "upsert_evidence_items",
            "upsert_evidence",
            _evidence_rows(result.evidence, raw_record_id=raw_record_id),
        )
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def _persist_seed_identity(
        self,
        seed: UniverseSeedData,
        *,
        raw_record_id: str,
        source: str,
    ) -> None:
        """把候选池种子的身份信息写入主表和 Provider 映射。

        候选池成员关系应由 asset_universe_members 表承载；这里不再写 asset_profiles，
        避免同一股票因属于多个指数、行业、概念或榜单而在资产画像表中膨胀。
        """

        _persist_seed_identity_rows(
            self.assets,
            [seed],
            raw_record_id=raw_record_id,
            source=source,
        )


class AshareP2Collector:
    """A 股 P2 财务和估值采集编排器。"""

    def __init__(
        self,
        session: Session,
        *,
        fundamental_provider: AshareFundamentalProvider | None = None,
        valuation_provider: AshareValuationProvider | None = None,
    ) -> None:
        self.assets = AssetRepository(session)
        self.fundamentals = FundamentalDataRepository(session)
        self.raw_records = RawRecordRepository(session)
        self.fundamental_provider = fundamental_provider or AshareFundamentalProvider()
        self.valuation_provider = valuation_provider or AshareValuationProvider()

    def collect_financial_indicators(
        self,
        *,
        symbol: str,
        asset_name: str | None = None,
        limit: int | None = None,
    ) -> ArchivedProviderResult:
        """采集个股主要财务指标并写入财务快照。"""

        result = self.fundamental_provider.fetch_financial_indicators(
            symbol=symbol,
            limit=limit,
        )
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint="stock_financial_analysis_indicator_em",
            request_params={"symbol": symbol, "indicator": "按报告期", "limit": limit},
            symbol=symbol,
        )
        self._persist_fundamental_snapshots(
            result,
            raw_record_id=raw_record_id,
            asset_name=asset_name,
        )
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def collect_valuation(
        self,
        *,
        symbol: str,
        asset_name: str | None = None,
        limit: int | None = None,
    ) -> ArchivedProviderResult:
        """采集个股估值序列并写入财务估值快照。"""

        result = self.valuation_provider.fetch_valuation(symbol=symbol, limit=limit)
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint="stock_value_em",
            request_params={"symbol": symbol, "limit": limit},
            symbol=symbol,
        )
        self._persist_fundamental_snapshots(
            result,
            raw_record_id=raw_record_id,
            asset_name=asset_name,
        )
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def collect_performance_report(
        self,
        *,
        date: str,
        report_type: str = "业绩报表",
        limit: int | None = None,
    ) -> ArchivedProviderResult:
        """采集业绩报表、快报或预告并写入财务快照。"""

        result = self.fundamental_provider.fetch_performance_report(
            date=date,
            report_type=report_type,
            limit=limit,
        )
        endpoint = str(result.payload.get("endpoint") or "stock_yjbb_em")
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint=endpoint,
            request_params={"date": date, "report_type": report_type, "limit": limit},
        )
        self._persist_fundamental_snapshots(result, raw_record_id=raw_record_id)
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def collect_dividend_yield(
        self,
        *,
        universe: str = "上证A股",
        limit: int | None = None,
    ) -> ArchivedProviderResult:
        """采集股息率数据并写入财务估值快照。"""

        result = self.valuation_provider.fetch_dividend_yield(universe=universe, limit=limit)
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint="stock_a_gxl_lg",
            request_params={"symbol": universe, "limit": limit},
        )
        self._persist_fundamental_snapshots(result, raw_record_id=raw_record_id)
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def collect_financial_profile(
        self,
        *,
        symbol: str,
        asset_name: str | None = None,
        report_date: str | None = None,
        limit: int | None = None,
    ) -> list[ArchivedProviderResult]:
        """一次采集单标的财务指标、估值、业绩和全市场股息率。"""

        results = [
            self.collect_financial_indicators(symbol=symbol, asset_name=asset_name, limit=limit),
            self.collect_valuation(symbol=symbol, asset_name=asset_name, limit=limit),
        ]
        if report_date:
            results.append(
                self.collect_performance_report(
                    date=report_date,
                    report_type="业绩报表",
                    limit=limit,
                )
            )
        results.append(self.collect_dividend_yield(limit=limit))
        return results

    def _persist_fundamental_snapshots(
        self,
        result: FundamentalSnapshotsResult,
        *,
        raw_record_id: str,
        asset_name: str | None = None,
    ) -> None:
        """把财务估值快照写入标准表。"""

        if result.status != "available":
            return
        _persist_rows(
            self.assets,
            "ensure_assets",
            "ensure_asset",
            [
                {
                    "asset_id": snapshot.asset_id,
                    "symbol": snapshot.symbol,
                    "name": asset_name or snapshot.symbol,
                    "market": "ashare",
                    "asset_type": "stock",
                    "payload": {"source": snapshot.source},
                }
                for snapshot in result.snapshots
            ],
        )
        _persist_rows(
            self.fundamentals,
            "upsert_fundamental_snapshots",
            "upsert_fundamental_snapshot",
            [
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "asset_id": snapshot.asset_id,
                    "symbol": snapshot.symbol,
                    "report_period": snapshot.report_period,
                    "pe_ttm": snapshot.pe_ttm,
                    "pb": snapshot.pb,
                    "roe": snapshot.roe,
                    "revenue_growth_yoy": snapshot.revenue_growth_yoy,
                    "net_profit_growth_yoy": snapshot.net_profit_growth_yoy,
                    "debt_to_asset": snapshot.debt_to_asset,
                    "operating_cashflow": snapshot.operating_cashflow,
                    "source": snapshot.source,
                    "status": snapshot.status,
                    "missing_fields": snapshot.missing_fields,
                    "as_of": snapshot.as_of,
                    "payload": snapshot.payload | {"raw_record_id": raw_record_id},
                }
                for snapshot in result.snapshots
            ],
        )


class AshareRiskSentimentCollector:
    """A 股风险和短线情绪采集编排器。"""

    def __init__(
        self,
        session: Session,
        *,
        risk_provider: AshareRiskProvider | None = None,
        sentiment_provider: AshareSentimentProvider | None = None,
    ) -> None:
        self.assets = AssetRepository(session)
        self.universes = UniverseRepository(session)
        self.events = EventRepository(session)
        self.risks = RiskRepository(session)
        self.raw_records = RawRecordRepository(session)
        self.risk_provider = risk_provider or AshareRiskProvider()
        self.sentiment_provider = sentiment_provider or AshareSentimentProvider()

    def collect_stop_list(self, *, limit: int | None = None) -> ArchivedProviderResult:
        """采集停牌列表，失败也会写入原始响应归档。"""

        result = self.risk_provider.fetch_stop_list(limit=limit)
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint="stock_zh_a_stop_em",
            request_params={"limit": limit},
        )
        self._persist_risk_result(result, raw_record_id=raw_record_id)
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def collect_hot_rank(
        self,
        *,
        universe_id: str,
        universe_name: str,
        strategy_context: str,
        limit: int | None = None,
    ) -> ArchivedProviderResult:
        """采集人气榜，并写入热度候选种子和事件。"""

        result = self.sentiment_provider.fetch_hot_rank(limit=limit)
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint="stock_hot_rank_em",
            request_params={"limit": limit},
        )
        self._persist_sentiment_result(
            result,
            raw_record_id=raw_record_id,
            universe_id=universe_id,
            universe_name=universe_name,
            universe_source="akshare:stock_hot_rank_em",
            strategy_context=strategy_context,
        )
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def collect_zt_pool(
        self,
        *,
        date: str,
        universe_id: str,
        universe_name: str,
        strategy_context: str,
        limit: int | None = None,
    ) -> ArchivedProviderResult:
        """采集涨停池，并写入强势候选种子、事件和过热风险。"""

        result = self.sentiment_provider.fetch_zt_pool(date=date, limit=limit)
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint="stock_zt_pool_em",
            request_params={"date": date, "limit": limit},
        )
        self._persist_sentiment_result(
            result,
            raw_record_id=raw_record_id,
            universe_id=universe_id,
            universe_name=universe_name,
            universe_source="akshare:stock_zt_pool_em",
            strategy_context=strategy_context,
        )
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def collect_lhb_detail(
        self,
        *,
        start_date: str,
        end_date: str,
        limit: int | None = None,
    ) -> ArchivedProviderResult:
        """采集龙虎榜明细，并写入风险发现和证据。"""

        result = self.risk_provider.fetch_lhb_detail(
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint="stock_lhb_detail_em",
            request_params={"start_date": start_date, "end_date": end_date, "limit": limit},
        )
        self._persist_risk_result(result, raw_record_id=raw_record_id)
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def collect_block_trades(
        self,
        *,
        start_date: str,
        end_date: str,
        symbol: str = "A股",
        limit: int | None = None,
    ) -> ArchivedProviderResult:
        """采集大宗交易明细，并写入风险发现和证据。"""

        result = self.risk_provider.fetch_block_trades(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint="stock_dzjy_mrmx",
            request_params={
                "symbol": symbol,
                "start_date": start_date,
                "end_date": end_date,
                "limit": limit,
            },
        )
        self._persist_risk_result(result, raw_record_id=raw_record_id)
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def collect_margin_sse(
        self,
        *,
        start_date: str,
        end_date: str,
        limit: int | None = None,
    ) -> ArchivedProviderResult:
        """采集上交所两融汇总，并写入市场级风险发现。"""

        result = self.risk_provider.fetch_margin_sse(
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint="stock_margin_sse",
            request_params={"start_date": start_date, "end_date": end_date, "limit": limit},
        )
        self._persist_risk_result(result, raw_record_id=raw_record_id)
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def collect_margin_szse(self, *, date: str, limit: int | None = None) -> ArchivedProviderResult:
        """采集深交所两融汇总，并写入市场级风险发现。"""

        result = self.risk_provider.fetch_margin_szse(date=date, limit=limit)
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint="stock_margin_szse",
            request_params={"date": date, "limit": limit},
        )
        self._persist_risk_result(result, raw_record_id=raw_record_id)
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def collect_restricted_release(
        self,
        *,
        start_date: str,
        end_date: str,
        limit: int | None = None,
        risk_window_days: int = 30,
        risk_ratio_threshold: Decimal = Decimal("0.05"),
    ) -> ArchivedProviderResult:
        """采集限售解禁详情，并写入事件和临近解禁风险。"""

        result = self.risk_provider.fetch_restricted_release(
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            risk_window_days=risk_window_days,
            risk_ratio_threshold=risk_ratio_threshold,
        )
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint="stock_restricted_release_detail_em",
            request_params={
                "start_date": start_date,
                "end_date": end_date,
                "limit": limit,
                "risk_window_days": risk_window_days,
                "risk_ratio_threshold": str(risk_ratio_threshold),
            },
        )
        self._persist_risk_result(result, raw_record_id=raw_record_id)
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def collect_pledge_ratio(
        self,
        *,
        date: str | None = None,
        limit: int | None = None,
        risk_ratio_threshold: Decimal = Decimal("0.30"),
    ) -> ArchivedProviderResult:
        """采集上市公司股权质押比例，并写入风险发现。"""

        result = self.risk_provider.fetch_pledge_ratio(
            date=date,
            limit=limit,
            risk_ratio_threshold=risk_ratio_threshold,
        )
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint="stock_gpzy_pledge_ratio_em",
            request_params={
                "date": date,
                "limit": limit,
                "risk_ratio_threshold": str(risk_ratio_threshold),
            },
        )
        self._persist_risk_result(result, raw_record_id=raw_record_id)
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def _persist_sentiment_result(
        self,
        result: SentimentSignalsResult,
        *,
        raw_record_id: str,
        universe_id: str,
        universe_name: str,
        universe_source: str,
        strategy_context: str,
    ) -> None:
        """把情绪种子、事件和风险发现写入标准表。"""

        self.universes.upsert_universe(
            universe_id=universe_id,
            name=universe_name,
            source=universe_source,
            market="ashare",
            strategy_context=strategy_context,
            as_of=result.collected_at,
            total_before_filter=len(result.seeds),
            total_after_filter=len(result.seeds),
            status=result.status,
            payload={
                "provider_payload": result.payload,
                "raw_record_id": raw_record_id,
                "error": result.error_message,
            },
        )
        if result.status != "available":
            return

        _persist_seed_identity_rows(
            self.assets,
            result.seeds,
            raw_record_id=raw_record_id,
            source=universe_source,
        )
        self.universes.replace_members(
            universe_id=universe_id,
            members=[
                {
                    "member_id": f"universe_member:{universe_id}:{seed.symbol}",
                    "asset_id": seed.asset_id,
                    "symbol": seed.symbol,
                    "market": seed.market,
                    "as_of": seed.as_of or result.collected_at,
                    "rank_hint": seed.rank_hint,
                    "payload": seed.payload | {"raw_record_id": raw_record_id},
                }
                for seed in result.seeds
            ],
        )
        self._persist_events(result.events, raw_record_id=raw_record_id)
        self._persist_evidence(result.evidence, raw_record_id=raw_record_id)
        self._persist_risks(result.risks, raw_record_id=raw_record_id)

    def _persist_risk_result(
        self,
        result: RiskFindingsResult,
        *,
        raw_record_id: str,
    ) -> None:
        """把风险结果写入风险、证据和事件表。"""

        if result.status != "available":
            return
        self._persist_events(result.events, raw_record_id=raw_record_id)
        self._persist_evidence(result.evidence, raw_record_id=raw_record_id)
        self._persist_risks(result.risks, raw_record_id=raw_record_id)

    def _persist_seed_identity(
        self,
        seed: UniverseSeedData,
        *,
        raw_record_id: str,
        source: str,
    ) -> None:
        """把情绪候选池种子的身份信息写入主表和 Provider 映射。"""

        _persist_seed_identity_rows(
            self.assets,
            [seed],
            raw_record_id=raw_record_id,
            source=source,
        )

    def _persist_events(
        self,
        events: list[Any],
        *,
        raw_record_id: str,
    ) -> None:
        """写入事件记录。"""

        _persist_rows(
            self.events,
            "upsert_events",
            "upsert_event",
            _event_rows(events, raw_record_id=raw_record_id),
        )

    def _persist_evidence(
        self,
        evidence: list[Any],
        *,
        raw_record_id: str,
    ) -> None:
        """写入证据索引。"""

        _persist_rows(
            self.events,
            "upsert_evidence_items",
            "upsert_evidence",
            _evidence_rows(evidence, raw_record_id=raw_record_id),
        )

    def _persist_risks(
        self,
        risks: list[Any],
        *,
        raw_record_id: str,
    ) -> None:
        """写入风险发现。"""

        _persist_rows(
            self.risks,
            "upsert_risk_findings",
            "upsert_risk_finding",
            _risk_rows(risks, raw_record_id=raw_record_id),
        )
