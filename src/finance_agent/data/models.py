"""数据 Provider 返回的轻量数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class AssetData:
    """Provider 返回的资产主数据。"""

    asset_id: str
    symbol: str
    name: str
    market: str
    asset_type: str
    exchange: str | None = None
    currency: str | None = None
    sector: str | None = None
    base_asset: str | None = None
    quote_asset: str | None = None
    tradable: bool = True
    status: str = "available"
    payload: JsonDict = field(default_factory=dict)


@dataclass(frozen=True)
class MarketBarData:
    """Provider 返回的标准 K 线数据。"""

    asset_id: str
    symbol: str
    market: str
    timeframe: str
    timestamp: datetime
    open_price: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source: str
    adjustment: str = ""
    end_timestamp: datetime | None = None
    amount: Decimal | None = None
    is_closed: bool = True
    raw_record_id: str | None = None
    status: str = "available"


@dataclass(frozen=True)
class CryptoDerivativeSnapshotData:
    """Provider 返回的数字货币衍生品快照。"""

    snapshot_id: str
    asset_id: str
    symbol: str
    market: str
    source: str
    as_of: datetime
    funding_rate: Decimal | None = None
    next_funding_time: datetime | None = None
    open_interest: Decimal | None = None
    open_interest_value: Decimal | None = None
    long_short_ratio: Decimal | None = None
    basis_rate: Decimal | None = None
    liquidation_risk_score: Decimal | None = None
    status: str = "available"
    payload: JsonDict = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderResult:
    """Provider 调用结果包装。"""

    provider_name: str
    status: str
    collected_at: datetime
    payload: JsonDict = field(default_factory=dict)
    error_message: str | None = None


@dataclass(frozen=True)
class AssetListResult(ProviderResult):
    """资产列表结果。"""

    assets: list[AssetData] = field(default_factory=list)


@dataclass(frozen=True)
class MarketBarsResult(ProviderResult):
    """K 线结果。"""

    bars: list[MarketBarData] = field(default_factory=list)


@dataclass(frozen=True)
class CryptoDerivativeSnapshotResult(ProviderResult):
    """数字货币衍生品快照结果。"""

    snapshot: CryptoDerivativeSnapshotData | None = None
