"""真实数据源到数据库的冒烟验证脚本。

默认拉取少量数据，验证：
- AKShare A 股日线归一化。
- ccxt Binance K 线归一化。
- Provider 数据写入 Repository。

脚本不需要密钥，只做公开行情读取。
公网数据源偶发失败时，脚本会输出结构化错误；只要某个 Provider
返回 `error` 而不是抛出未处理异常，就说明失败路径是可控的。
"""

from __future__ import annotations

from datetime import UTC, datetime

from finance_agent.data.providers import AkshareProvider, CcxtBinanceProvider
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.repositories import (
    AssetRepository,
    MarketDataRepository,
    UniverseRepository,
)


def main() -> None:
    """执行一次真实数据源冒烟验证。"""

    akshare = AkshareProvider()
    binance = CcxtBinanceProvider()
    session_factory = create_session_factory()
    as_of = datetime.now(tz=UTC)

    ashare_bars = akshare.fetch_ohlcv(
        symbol="000001",
        timeframe="1d",
        start="20260501",
        end="20260514",
        limit=3,
    )
    crypto_bars = binance.fetch_ohlcv(symbol="BTCUSDT", timeframe="1h", limit=3)

    with session_scope(session_factory) as session:
        assets = AssetRepository(session)
        universes = UniverseRepository(session)
        market_data = MarketDataRepository(session)

        assets.upsert_asset(
            asset_id="ashare:000001",
            symbol="000001",
            name="平安银行",
            market="ashare",
            asset_type="stock",
            exchange="SZSE",
            currency="CNY",
            payload={"source": "provider_smoke"},
        )
        assets.upsert_asset(
            asset_id="crypto_spot:BTCUSDT",
            symbol="BTCUSDT",
            name="Bitcoin / USDT",
            market="crypto_spot",
            asset_type="crypto",
            exchange="Binance",
            currency="USDT",
            base_asset="BTC",
            quote_asset="USDT",
            payload={"source": "provider_smoke"},
        )
        universes.upsert_universe(
            universe_id="universe:provider_smoke:ashare",
            name="真实数据源 A 股冒烟候选池",
            source="provider_smoke",
            market="ashare",
            strategy_context="provider_smoke",
            as_of=as_of,
            total_before_filter=1,
            total_after_filter=1,
        )
        universes.replace_members(
            universe_id="universe:provider_smoke:ashare",
            members=[
                {
                    "member_id": "universe_member:universe:provider_smoke:ashare:ashare:000001",
                    "asset_id": "ashare:000001",
                    "symbol": "000001",
                    "market": "ashare",
                    "as_of": as_of,
                },
            ],
        )
        universes.upsert_universe(
            universe_id="universe:provider_smoke:crypto_spot",
            name="真实数据源数字货币冒烟候选池",
            source="provider_smoke",
            market="crypto_spot",
            strategy_context="provider_smoke",
            as_of=as_of,
            total_before_filter=1,
            total_after_filter=1,
        )
        universes.replace_members(
            universe_id="universe:provider_smoke:crypto_spot",
            members=[
                {
                    "member_id": (
                        "universe_member:universe:provider_smoke:crypto_spot:"
                        "crypto_spot:BTCUSDT"
                    ),
                    "asset_id": "crypto_spot:BTCUSDT",
                    "symbol": "BTCUSDT",
                    "market": "crypto_spot",
                    "as_of": as_of,
                },
            ],
        )

        for bar in ashare_bars.bars + crypto_bars.bars:
            market_data.upsert_bar(
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
                raw_record_id=bar.raw_record_id,
                status=bar.status,
            )

    print(
        {
            "akshare_status": ashare_bars.status,
            "akshare_bar_count": len(ashare_bars.bars),
            "akshare_error": ashare_bars.error_message,
            "binance_status": crypto_bars.status,
            "binance_bar_count": len(crypto_bars.bars),
            "binance_error": crypto_bars.error_message,
        }
    )


if __name__ == "__main__":
    main()
