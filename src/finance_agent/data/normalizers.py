"""第三方数据源归一化函数。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd

from finance_agent.data.models import AssetData, CryptoDerivativeSnapshotData, MarketBarData


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


def normalize_binance_derivative_snapshot(
    *,
    symbol: str,
    source: str,
    premium_index: dict[str, Any] | None,
    open_interest: dict[str, Any] | None,
    long_short_ratio: dict[str, Any] | None,
    collected_at: datetime,
) -> CryptoDerivativeSnapshotData:
    """归一化 Binance U 本位合约衍生品快照。"""

    compact_symbol = symbol.replace("/", "").upper()
    premium_index = premium_index or {}
    open_interest = open_interest or {}
    long_short_ratio = long_short_ratio or {}

    premium_time = _datetime_from_milliseconds(premium_index.get("time"))
    oi_time = _datetime_from_milliseconds(open_interest.get("time"))
    ratio_time = _datetime_from_milliseconds(long_short_ratio.get("timestamp"))
    as_of = max(
        [value for value in [premium_time, oi_time, ratio_time, collected_at] if value is not None]
    )

    funding_rate = _nullable_decimal(premium_index.get("lastFundingRate"))
    mark_price = _nullable_decimal(premium_index.get("markPrice"))
    index_price = _nullable_decimal(premium_index.get("indexPrice"))
    oi_amount = _nullable_decimal(open_interest.get("openInterest"))
    open_interest_value = None
    if oi_amount is not None and mark_price is not None:
        open_interest_value = oi_amount * mark_price

    basis_rate = None
    if mark_price is not None and index_price not in {None, Decimal("0")}:
        basis_rate = (mark_price - index_price) / index_price

    snapshot_id = (
        f"crypto_derivative:{compact_symbol}:{source}:{as_of.strftime('%Y%m%dT%H%M%SZ')}"
    )
    return CryptoDerivativeSnapshotData(
        snapshot_id=snapshot_id,
        asset_id=f"crypto_future:{compact_symbol}",
        symbol=compact_symbol,
        market="crypto_future",
        source=source,
        as_of=as_of,
        funding_rate=funding_rate,
        next_funding_time=_datetime_from_milliseconds(premium_index.get("nextFundingTime")),
        open_interest=oi_amount,
        open_interest_value=open_interest_value,
        long_short_ratio=_nullable_decimal(long_short_ratio.get("longShortRatio")),
        basis_rate=basis_rate,
        liquidation_risk_score=None,
        status="available",
        payload={
            "schema_version": "1.0",
            "premium_index": premium_index,
            "open_interest": open_interest,
            "long_short_ratio": long_short_ratio,
        },
    )


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


def _nullable_decimal(value: Any) -> Decimal | None:
    """把可缺失的第三方数值转成 Decimal。"""

    if value is None or pd.isna(value):
        return None
    normalized = str(value).replace(",", "").strip()
    if not normalized:
        return None
    return Decimal(normalized)


def _datetime_from_milliseconds(value: Any) -> datetime | None:
    """把毫秒时间戳转换为 UTC datetime。"""

    if value is None or pd.isna(value):
        return None
    timestamp_ms = int(value)
    if timestamp_ms <= 0:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
