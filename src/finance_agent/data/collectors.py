"""数据采集编排服务。

Collector 负责把 Provider 的结构化结果落到标准表，并把每次调用归档到
`raw_records`。它不负责因子计算、评分或推荐决策。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from finance_agent.data.models import (
    AssetListResult,
    CapitalFlowSnapshotsResult,
    CryptoDerivativeSnapshotResult,
    EventRecordsResult,
    FundamentalSnapshotsResult,
    MarketBarsResult,
    ProviderResult,
    RiskFindingsResult,
    SentimentSignalsResult,
    UniverseSeedsResult,
)
from finance_agent.data.providers import (
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
)
from finance_agent.storage.repositories import (
    AssetRepository,
    CapitalFlowRepository,
    DerivativeDataRepository,
    EventRepository,
    FundamentalDataRepository,
    MarketDataRepository,
    RawRecordRepository,
    RiskRepository,
    UniverseRepository,
)

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class ArchivedProviderResult:
    """带 raw_records 归档编号的 Provider 调用结果。"""

    result: ProviderResult
    raw_record_id: str


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
        if result.status != "available":
            return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

        for asset in result.assets:
            self.assets.upsert_asset(
                asset_id=asset.asset_id,
                symbol=asset.symbol,
                name=asset.name,
                market=asset.market,
                asset_type=asset.asset_type,
                exchange=asset.exchange,
                currency=asset.currency,
                sector=asset.sector,
                base_asset=asset.base_asset,
                quote_asset=asset.quote_asset,
                tradable=asset.tradable,
                status=asset.status,
                payload=asset.payload | {"raw_record_id": raw_record_id},
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

        for bar in result.bars:
            self._upsert_crypto_asset_stub(
                asset_id=bar.asset_id,
                symbol=bar.symbol,
                market=bar.market,
                source=bar.source,
            )
            self.market_data.upsert_bar(
                asset_id=bar.asset_id,
                symbol=bar.symbol,
                market=bar.market,
                timeframe=bar.timeframe,
                timestamp=bar.timestamp,
                end_timestamp=bar.end_timestamp,
                open_price=bar.open_price,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                amount=bar.amount,
                source=bar.source,
                adjustment=bar.adjustment,
                is_closed=bar.is_closed,
                raw_record_id=raw_record_id,
                status=bar.status,
            )
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
        self._upsert_crypto_asset_stub(
            asset_id=snapshot.asset_id,
            symbol=snapshot.symbol,
            market=snapshot.market,
            source=snapshot.source,
        )
        self.derivatives.upsert_crypto_derivative_snapshot(
            snapshot_id=snapshot.snapshot_id,
            asset_id=snapshot.asset_id,
            symbol=snapshot.symbol,
            market=snapshot.market,
            source=snapshot.source,
            as_of=snapshot.as_of,
            funding_rate=snapshot.funding_rate,
            next_funding_time=snapshot.next_funding_time,
            open_interest=snapshot.open_interest,
            open_interest_value=snapshot.open_interest_value,
            long_short_ratio=snapshot.long_short_ratio,
            basis_rate=snapshot.basis_rate,
            liquidation_risk_score=snapshot.liquidation_risk_score,
            status=snapshot.status,
            payload=snapshot.payload | {"raw_record_id": raw_record_id},
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
        base_asset, quote_asset = _split_crypto_symbol(symbol)
        self.assets.upsert_asset(
            asset_id=asset_id,
            symbol=symbol,
            name=f"{base_asset} / {quote_asset}" if quote_asset else symbol,
            market=market,
            asset_type="crypto",
            exchange="Binance",
            currency=quote_asset,
            base_asset=base_asset,
            quote_asset=quote_asset,
            payload={"source": source},
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
            total_after_filter=len(result.assets),
            status=result.status,
            payload={
                "provider_payload": result.payload,
                "raw_record_id": raw_record_id,
                "error": result.error_message,
            },
        )
        if result.status != "available":
            return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

        for asset in result.assets:
            self.assets.upsert_asset(
                asset_id=asset.asset_id,
                symbol=asset.symbol,
                name=asset.name,
                market=asset.market,
                asset_type=asset.asset_type,
                exchange=asset.exchange,
                currency=asset.currency,
                sector=asset.sector,
                base_asset=asset.base_asset,
                quote_asset=asset.quote_asset,
                tradable=asset.tradable,
                status=asset.status,
                payload=asset.payload | {"raw_record_id": raw_record_id},
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
        timeframe: str = "1d",
        start: str | None = None,
        end: str | None = None,
        limit: int | None = None,
        adjust: str = "qfq",
    ) -> ArchivedProviderResult:
        """采集 A 股 K 线，并写入 `market_bars`。"""

        result = self.provider.fetch_ohlcv(
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            limit=limit,
            adjust=adjust,
        )
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint="stock_zh_a_hist",
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
        if result.status != "available":
            return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

        for bar in result.bars:
            self._ensure_asset_stub(asset_id=bar.asset_id, symbol=bar.symbol, source=bar.source)
            self.market_data.upsert_bar(
                asset_id=bar.asset_id,
                symbol=bar.symbol,
                market=bar.market,
                timeframe=bar.timeframe,
                timestamp=bar.timestamp,
                end_timestamp=bar.end_timestamp,
                open_price=bar.open_price,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                amount=bar.amount,
                source=bar.source,
                adjustment=bar.adjustment,
                is_closed=bar.is_closed,
                raw_record_id=raw_record_id,
                status=bar.status,
            )
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def _ensure_asset_stub(self, *, asset_id: str, symbol: str, source: str) -> None:
        """仅在资产不存在时写入占位资产，避免覆盖已有名称和交易所信息。"""

        try:
            self.assets.get_asset(asset_id)
            return
        except NoResultFound:
            pass
        self.assets.upsert_asset(
            asset_id=asset_id,
            symbol=symbol,
            name=symbol,
            market="ashare",
            asset_type="stock",
            currency="CNY",
            payload={"source": source},
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
    ) -> None:
        self.assets = AssetRepository(session)
        self.universes = UniverseRepository(session)
        self.capital_flows = CapitalFlowRepository(session)
        self.events = EventRepository(session)
        self.raw_records = RawRecordRepository(session)
        self.sector_provider = sector_provider or AshareSectorProvider()
        self.flow_provider = flow_provider or AshareCapitalFlowProvider()
        self.event_provider = event_provider or AshareEventProvider()

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

        for seed in result.seeds:
            self.assets.upsert_asset(
                asset_id=seed.asset_id,
                symbol=seed.symbol,
                name=seed.name,
                market=seed.market,
                asset_type="stock",
                payload=seed.payload,
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

        for seed in result.seeds:
            self.assets.upsert_asset(
                asset_id=seed.asset_id,
                symbol=seed.symbol,
                name=seed.name,
                market=seed.market,
                asset_type="stock",
                payload=seed.payload,
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

        for seed in result.seeds:
            self.assets.upsert_asset(
                asset_id=seed.asset_id,
                symbol=seed.symbol,
                name=seed.name,
                market=seed.market,
                asset_type="stock",
                payload=seed.payload,
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

        for snapshot in result.snapshots:
            self.assets.upsert_asset(
                asset_id=snapshot.asset_id,
                symbol=snapshot.symbol,
                name=snapshot.symbol,
                market=snapshot.market,
                asset_type="stock",
                payload={"source": snapshot.source},
            )
            self.capital_flows.upsert_capital_flow_snapshot(
                snapshot_id=snapshot.snapshot_id,
                asset_id=snapshot.asset_id,
                symbol=snapshot.symbol,
                market=snapshot.market,
                main_net_inflow=snapshot.main_net_inflow,
                northbound_net_inflow=snapshot.northbound_net_inflow,
                turnover_rate=snapshot.turnover_rate,
                amount=snapshot.amount,
                window=snapshot.window,
                source=snapshot.source,
                status=snapshot.status,
                as_of=snapshot.as_of,
                payload=snapshot.payload | {"raw_record_id": raw_record_id},
            )
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

    def collect_stock_news(
        self,
        *,
        symbol: str,
        asset_name: str | None = None,
        limit: int | None = None,
    ) -> ArchivedProviderResult:
        """采集个股新闻，并写入事件和证据表。"""

        result = self.event_provider.fetch_stock_news(symbol=symbol, limit=limit)
        raw_record_id = archive_provider_result(
            self.raw_records,
            result,
            endpoint="stock_news_em",
            request_params={"symbol": symbol, "limit": limit},
            symbol=symbol,
        )
        if result.status != "available":
            return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

        self.assets.upsert_asset(
            asset_id=f"ashare:{symbol}",
            symbol=symbol,
            name=asset_name or symbol,
            market="ashare",
            asset_type="stock",
            payload={"source": "akshare:stock_news_em"},
        )
        for event in result.events:
            self.events.upsert_event(
                event_id=event.event_id,
                asset_id=event.asset_id,
                symbol=event.symbol,
                market=event.market,
                event_type=event.event_type,
                title=event.title,
                summary=event.summary,
                sentiment=event.sentiment,
                importance=event.importance,
                source=event.source,
                url=event.url,
                published_at=event.published_at,
                collected_at=event.collected_at,
                payload=event.payload | {"raw_record_id": raw_record_id},
            )
        for item in result.evidence:
            self.events.upsert_evidence(
                evidence_id=item.evidence_id,
                evidence_type=item.evidence_type,
                asset_id=item.asset_id,
                source=item.source,
                title=item.title,
                summary=item.summary,
                data_ref=item.data_ref,
                url=item.url,
                reliability=item.reliability,
                as_of=item.as_of,
                collected_at=item.collected_at,
                payload=item.payload | {"raw_record_id": raw_record_id},
            )
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)

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

        for event in result.events:
            self.events.upsert_event(
                event_id=event.event_id,
                asset_id=event.asset_id,
                symbol=event.symbol,
                market=event.market,
                event_type=event.event_type,
                title=event.title,
                summary=event.summary,
                sentiment=event.sentiment,
                importance=event.importance,
                source=event.source,
                url=event.url,
                published_at=event.published_at,
                collected_at=event.collected_at,
                payload=event.payload | {"raw_record_id": raw_record_id},
            )
        for item in result.evidence:
            self.events.upsert_evidence(
                evidence_id=item.evidence_id,
                evidence_type=item.evidence_type,
                asset_id=item.asset_id,
                source=item.source,
                title=item.title,
                summary=item.summary,
                data_ref=item.data_ref,
                url=item.url,
                reliability=item.reliability,
                as_of=item.as_of,
                collected_at=item.collected_at,
                payload=item.payload | {"raw_record_id": raw_record_id},
            )
        return ArchivedProviderResult(result=result, raw_record_id=raw_record_id)


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
        for snapshot in result.snapshots:
            self.assets.upsert_asset(
                asset_id=snapshot.asset_id,
                symbol=snapshot.symbol,
                name=asset_name or snapshot.symbol,
                market="ashare",
                asset_type="stock",
                payload={"source": snapshot.source},
            )
            self.fundamentals.upsert_fundamental_snapshot(
                snapshot_id=snapshot.snapshot_id,
                asset_id=snapshot.asset_id,
                symbol=snapshot.symbol,
                report_period=snapshot.report_period,
                pe_ttm=snapshot.pe_ttm,
                pb=snapshot.pb,
                roe=snapshot.roe,
                revenue_growth_yoy=snapshot.revenue_growth_yoy,
                net_profit_growth_yoy=snapshot.net_profit_growth_yoy,
                debt_to_asset=snapshot.debt_to_asset,
                operating_cashflow=snapshot.operating_cashflow,
                source=snapshot.source,
                status=snapshot.status,
                missing_fields=snapshot.missing_fields,
                as_of=snapshot.as_of,
                payload=snapshot.payload | {"raw_record_id": raw_record_id},
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

        for seed in result.seeds:
            self.assets.upsert_asset(
                asset_id=seed.asset_id,
                symbol=seed.symbol,
                name=seed.name,
                market=seed.market,
                asset_type="stock",
                payload=seed.payload | {"raw_record_id": raw_record_id},
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

    def _persist_events(
        self,
        events: list[Any],
        *,
        raw_record_id: str,
    ) -> None:
        """写入事件记录。"""

        for event in events:
            self.events.upsert_event(
                event_id=event.event_id,
                asset_id=event.asset_id,
                symbol=event.symbol,
                market=event.market,
                event_type=event.event_type,
                title=event.title,
                summary=event.summary,
                sentiment=event.sentiment,
                importance=event.importance,
                source=event.source,
                url=event.url,
                published_at=event.published_at,
                collected_at=event.collected_at,
                payload=event.payload | {"raw_record_id": raw_record_id},
            )

    def _persist_evidence(
        self,
        evidence: list[Any],
        *,
        raw_record_id: str,
    ) -> None:
        """写入证据索引。"""

        for item in evidence:
            self.events.upsert_evidence(
                evidence_id=item.evidence_id,
                evidence_type=item.evidence_type,
                asset_id=item.asset_id,
                source=item.source,
                title=item.title,
                summary=item.summary,
                data_ref=item.data_ref,
                url=item.url,
                reliability=item.reliability,
                as_of=item.as_of,
                collected_at=item.collected_at,
                payload=item.payload | {"raw_record_id": raw_record_id},
            )

    def _persist_risks(
        self,
        risks: list[Any],
        *,
        raw_record_id: str,
    ) -> None:
        """写入风险发现。"""

        for risk in risks:
            self.risks.upsert_risk_finding(
                risk_id=risk.risk_id,
                asset_id=risk.asset_id,
                scope=risk.scope,
                risk_type=risk.risk_type,
                severity=risk.severity,
                score=risk.score,
                title=risk.title,
                description=risk.description,
                as_of=risk.as_of,
                evidence_ids=risk.evidence_ids,
                payload=risk.payload | {"raw_record_id": raw_record_id},
            )
