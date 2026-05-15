"""数字货币基础数据冒烟验证脚本。

该脚本只读取公开行情，不需要密钥，不包含账户或下单逻辑。
验证内容：
- ccxt Binance 交易对列表进入 `assets` 和币种候选池。
- ccxt Binance K 线进入 `market_bars`。
- Binance 原生 U 本位合约衍生品快照进入 `crypto_derivative_snapshots`。
- 每次 Provider 调用都进入 `raw_records`，成功和失败都可审计。
"""

from __future__ import annotations

from finance_agent.data.collectors import CryptoDataCollector
from finance_agent.storage.db import create_session_factory, session_scope


def main() -> None:
    """拉取 Binance 公开数据并写入数据库。"""

    session_factory = create_session_factory()
    with session_scope(session_factory) as session:
        collector = CryptoDataCollector(session)
        markets_archive = collector.collect_markets(
            market_type="spot",
            universe_id="universe:crypto_smoke:binance_spot",
            universe_name="Binance 现货冒烟候选池",
            strategy_context="crypto_smoke",
            limit=5,
        )
        ohlcv_archive = collector.collect_ohlcv(
            symbol="BTCUSDT",
            timeframe="1h",
            market_type="spot",
            limit=3,
        )
        derivative_archive = collector.collect_derivative_snapshot(symbol="BTCUSDT")

    markets = markets_archive.result
    ohlcv = ohlcv_archive.result
    derivative = derivative_archive.result
    derivative_snapshot = getattr(derivative, "snapshot", None)

    print(
        {
            "markets_status": markets.status,
            "markets_count": len(getattr(markets, "assets", [])),
            "markets_error": markets.error_message,
            "ohlcv_status": ohlcv.status,
            "ohlcv_count": len(getattr(ohlcv, "bars", [])),
            "ohlcv_error": ohlcv.error_message,
            "derivative_status": derivative.status,
            "derivative_error": derivative.error_message,
            "derivative_as_of": derivative_snapshot.as_of.isoformat()
            if derivative_snapshot
            else None,
            "raw_record_count": 3,
            "raw_record_ids": [
                markets_archive.raw_record_id,
                ohlcv_archive.raw_record_id,
                derivative_archive.raw_record_id,
            ],
        }
    )


if __name__ == "__main__":
    main()
