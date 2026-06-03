from datetime import UTC, datetime

from finance_agent.data.normalizers import (
    normalize_binance_derivative_snapshot,
    normalize_crypto_markets,
    normalize_crypto_ohlcv,
)


def test_normalize_crypto_markets_compacts_future_settlement_suffix() -> None:
    """合约 markets 的 BTC/USDT:USDT 应统一落库为紧凑交易对 BTCUSDT。"""

    assets = normalize_crypto_markets(
        {
            "BTC/USDT:USDT": {
                "symbol": "BTC/USDT:USDT",
                "base": "BTC",
                "quote": "USDT",
                "active": True,
            }
        },
        market_type="future",
    )

    assert len(assets) == 1
    assert assets[0].asset_id == "crypto_future:BTCUSDT"
    assert assets[0].symbol == "BTCUSDT"


def test_normalize_crypto_markets_preserves_future_delivery_suffix() -> None:
    """交割合约应保留交割日期后缀，避免和永续合约合并。"""

    assets = normalize_crypto_markets(
        {
            "BTC/USDT:USDT-260626": {
                "symbol": "BTC/USDT:USDT-260626",
                "base": "BTC",
                "quote": "USDT",
                "active": True,
            }
        },
        market_type="future",
    )

    assert len(assets) == 1
    assert assets[0].asset_id == "crypto_future:BTCUSDT-260626"
    assert assets[0].symbol == "BTCUSDT-260626"


def test_normalize_crypto_ohlcv_compacts_future_settlement_suffix() -> None:
    """合约 K 线也应写入相同的紧凑 asset_id，避免和 assets 主表脱节。"""

    bars = normalize_crypto_ohlcv(
        [[1780512000000, 1, 2, 0.5, 1.5, 100]],
        symbol="BTC/USDT:USDT",
        timeframe="1h",
        source="ccxt_binance",
        market_type="future",
    )

    assert len(bars) == 1
    assert bars[0].asset_id == "crypto_future:BTCUSDT"
    assert bars[0].symbol == "BTCUSDT"


def test_normalize_binance_derivative_snapshot_compacts_settlement_suffix() -> None:
    """衍生品快照应和合约资产池使用同一套紧凑交易对 ID。"""

    snapshot = normalize_binance_derivative_snapshot(
        symbol="BTC/USDT:USDT",
        source="binance_native",
        premium_index={"time": int(datetime(2026, 6, 4, tzinfo=UTC).timestamp() * 1000)},
        open_interest={},
        long_short_ratio={},
        collected_at=datetime(2026, 6, 4, tzinfo=UTC),
    )

    assert snapshot.asset_id == "crypto_future:BTCUSDT"
    assert snapshot.symbol == "BTCUSDT"
