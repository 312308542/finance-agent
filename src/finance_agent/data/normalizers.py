"""第三方数据源归一化函数。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd

from finance_agent.data.models import AssetData, MarketBarData


def to_decimal(value: Any) -> Decimal:
    """把第三方返回的数值安全转成 Decimal。"""

    if value is None or pd.isna(value):
        return Decimal("0")
    return Decimal(str(value).replace(",", ""))


def normalize_ashare_spot(df: pd.DataFrame, *, limit: int | None = None) -> list[AssetData]:
    """归一化 AKShare A 股实时列表。"""

    assets: list[AssetData] = []
    rows = df.head(limit) if limit else df
    for row in rows.to_dict("records"):
        symbol = str(row.get("代码", "")).strip()
        if not symbol:
            continue
        assets.append(
            AssetData(
                asset_id=f"ashare:{symbol}",
                symbol=symbol,
                name=str(row.get("名称", symbol)).strip(),
                market="ashare",
                asset_type="stock",
                exchange=infer_ashare_exchange(symbol),
                currency="CNY",
                tradable=True,
                payload={"raw": row},
            )
        )
    return assets


def normalize_ashare_hist(
    df: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    source: str,
    adjustment: str,
) -> list[MarketBarData]:
    """归一化 AKShare A 股历史行情。"""

    bars: list[MarketBarData] = []
    for row in df.to_dict("records"):
        timestamp = pd.Timestamp(row["日期"]).to_pydatetime().replace(tzinfo=UTC)
        bars.append(
            MarketBarData(
                asset_id=f"ashare:{symbol}",
                symbol=symbol,
                market="ashare",
                timeframe=timeframe,
                timestamp=timestamp,
                open_price=to_decimal(row.get("开盘")),
                high=to_decimal(row.get("最高")),
                low=to_decimal(row.get("最低")),
                close=to_decimal(row.get("收盘")),
                volume=to_decimal(row.get("成交量")),
                amount=to_decimal(row.get("成交额")),
                source=source,
                adjustment=adjustment,
                is_closed=True,
            )
        )
    return bars


def normalize_crypto_markets(
    markets: dict[str, dict[str, Any]],
    *,
    limit: int | None = None,
    market_type: str = "spot",
) -> list[AssetData]:
    """归一化 ccxt markets 为资产列表。"""

    assets: list[AssetData] = []
    market_name = "crypto_future" if market_type in {"future", "swap"} else "crypto_spot"
    selected = list(markets.values())
    if limit:
        selected = selected[:limit]

    for item in selected:
        symbol = str(item.get("symbol") or "").replace("/", "")
        base_asset = item.get("base")
        quote_asset = item.get("quote")
        if not symbol or not base_asset or not quote_asset:
            continue
        active = bool(item.get("active", True))
        assets.append(
            AssetData(
                asset_id=f"{market_name}:{symbol}",
                symbol=symbol,
                name=f"{base_asset} / {quote_asset}",
                market=market_name,
                asset_type="crypto",
                exchange="Binance",
                currency=str(quote_asset),
                base_asset=str(base_asset),
                quote_asset=str(quote_asset),
                tradable=active,
                status="available" if active else "stale",
                payload={"raw": item},
            )
        )
    return assets


def normalize_crypto_ohlcv(
    rows: list[list[Any]],
    *,
    symbol: str,
    timeframe: str,
    source: str,
    market_type: str = "spot",
) -> list[MarketBarData]:
    """归一化 ccxt OHLCV。"""

    market_name = "crypto_future" if market_type in {"future", "swap"} else "crypto_spot"
    compact_symbol = symbol.replace("/", "")
    bars: list[MarketBarData] = []
    duration = timeframe_to_timedelta(timeframe)
    for timestamp_ms, open_price, high, low, close, volume in rows:
        timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
        bars.append(
            MarketBarData(
                asset_id=f"{market_name}:{compact_symbol}",
                symbol=compact_symbol,
                market=market_name,
                timeframe=timeframe,
                timestamp=timestamp,
                end_timestamp=timestamp + duration if duration else None,
                open_price=to_decimal(open_price),
                high=to_decimal(high),
                low=to_decimal(low),
                close=to_decimal(close),
                volume=to_decimal(volume),
                source=source,
                adjustment="",
                is_closed=True,
            )
        )
    return bars


def infer_ashare_exchange(symbol: str) -> str:
    """根据 A 股代码推断交易所。"""

    if symbol.startswith(("6", "9")):
        return "SSE"
    if symbol.startswith(("0", "2", "3")):
        return "SZSE"
    if symbol.startswith(("4", "8")):
        return "BSE"
    return "UNKNOWN"


def timeframe_to_timedelta(timeframe: str) -> timedelta | None:
    """将常见 K 线周期转换成时间长度。"""

    unit = timeframe[-1]
    try:
        amount = int(timeframe[:-1])
    except ValueError:
        return None
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)
    if unit == "w":
        return timedelta(weeks=amount)
    return None
