"""数据 Provider 返回的轻量数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
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
class FundNavSnapshotData:
    """Provider 返回的开放式基金净值快照。"""

    snapshot_id: str
    asset_id: str
    symbol: str
    market: str
    source: str
    nav_date: date
    unit_nav: Decimal | None = None
    accumulated_nav: Decimal | None = None
    daily_return: Decimal | None = None
    purchase_status: str | None = None
    redeem_status: str | None = None
    status: str = "available"
    payload: JsonDict = field(default_factory=dict)


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
class UniverseSeedData:
    """Provider 返回的候选池种子成员。"""

    seed_id: str
    source_name: str
    source_type: str
    symbol: str
    name: str
    market: str
    asset_id: str
    rank_hint: int | None = None
    as_of: datetime | None = None
    payload: JsonDict = field(default_factory=dict)


@dataclass(frozen=True)
class CapitalFlowSnapshotData:
    """Provider 返回的 A 股资金流快照。"""

    snapshot_id: str
    asset_id: str
    symbol: str
    market: str
    window: str
    source: str
    as_of: datetime
    main_net_inflow: Decimal | None = None
    northbound_net_inflow: Decimal | None = None
    turnover_rate: Decimal | None = None
    amount: Decimal | None = None
    status: str = "available"
    payload: JsonDict = field(default_factory=dict)


@dataclass(frozen=True)
class FundamentalSnapshotData:
    """Provider 返回的 A 股财务和估值快照。"""

    snapshot_id: str
    asset_id: str
    symbol: str
    source: str
    status: str
    as_of: datetime
    report_period: str | None = None
    pe_ttm: Decimal | None = None
    pb: Decimal | None = None
    roe: Decimal | None = None
    revenue_growth_yoy: Decimal | None = None
    net_profit_growth_yoy: Decimal | None = None
    debt_to_asset: Decimal | None = None
    operating_cashflow: Decimal | None = None
    missing_fields: list[str] = field(default_factory=list)
    payload: JsonDict = field(default_factory=dict)


@dataclass(frozen=True)
class EventRecordData:
    """Provider 返回的事件记录。"""

    event_id: str
    market: str
    event_type: str
    title: str
    source: str
    collected_at: datetime
    asset_id: str | None = None
    symbol: str | None = None
    summary: str | None = None
    sentiment: str = "unknown"
    importance: str = "medium"
    url: str | None = None
    published_at: datetime | None = None
    payload: JsonDict = field(default_factory=dict)


@dataclass(frozen=True)
class EvidenceData:
    """Provider 返回的推荐证据索引。"""

    evidence_id: str
    evidence_type: str
    source: str
    title: str
    reliability: str
    collected_at: datetime
    asset_id: str | None = None
    summary: str | None = None
    data_ref: str | None = None
    url: str | None = None
    as_of: datetime | None = None
    payload: JsonDict = field(default_factory=dict)


@dataclass(frozen=True)
class RiskFindingData:
    """Provider 返回的风险发现。"""

    risk_id: str
    scope: str
    risk_type: str
    severity: str
    title: str
    as_of: datetime
    asset_id: str | None = None
    score: Decimal | None = None
    description: str | None = None
    evidence_ids: list[str] = field(default_factory=list)
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
class FundNavSnapshotsResult(ProviderResult):
    """开放式基金净值结果。"""

    snapshots: list[FundNavSnapshotData] = field(default_factory=list)


@dataclass(frozen=True)
class CryptoDerivativeSnapshotResult(ProviderResult):
    """数字货币衍生品快照结果。"""

    snapshot: CryptoDerivativeSnapshotData | None = None


@dataclass(frozen=True)
class UniverseSeedsResult(ProviderResult):
    """候选池种子结果。"""

    seeds: list[UniverseSeedData] = field(default_factory=list)


@dataclass(frozen=True)
class CapitalFlowSnapshotsResult(ProviderResult):
    """资金流快照结果。"""

    snapshots: list[CapitalFlowSnapshotData] = field(default_factory=list)


@dataclass(frozen=True)
class FundamentalSnapshotsResult(ProviderResult):
    """财务估值快照结果。"""

    snapshots: list[FundamentalSnapshotData] = field(default_factory=list)


@dataclass(frozen=True)
class EventRecordsResult(ProviderResult):
    """事件记录结果。"""

    events: list[EventRecordData] = field(default_factory=list)
    evidence: list[EvidenceData] = field(default_factory=list)


@dataclass(frozen=True)
class RiskFindingsResult(ProviderResult):
    """风险发现结果。"""

    risks: list[RiskFindingData] = field(default_factory=list)
    evidence: list[EvidenceData] = field(default_factory=list)
    events: list[EventRecordData] = field(default_factory=list)


@dataclass(frozen=True)
class SentimentSignalsResult(ProviderResult):
    """情绪种子和风险信号结果。"""

    seeds: list[UniverseSeedData] = field(default_factory=list)
    events: list[EventRecordData] = field(default_factory=list)
    risks: list[RiskFindingData] = field(default_factory=list)
    evidence: list[EvidenceData] = field(default_factory=list)
