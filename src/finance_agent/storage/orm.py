"""M0 数据库 ORM 模型。

ORM 模型用于给 Alembic 和仓储层提供结构化入口。复杂领域协议仍然以
`payload` 保存全文，领域模型不要直接依赖 ORM 类。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    Index,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """SQLAlchemy 声明式基类。"""


JsonDict = dict[str, Any]


class AssetORM(Base):
    """资产主数据表，统一承载 A 股和数字货币资产。"""

    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("market", "symbol", name="uq_assets_market_symbol"),
        Index("idx_assets_market_symbol", "market", "symbol"),
        Index("idx_assets_exchange", "exchange"),
        Index("idx_assets_sector", "sector"),
        Index("idx_assets_status", "status"),
    )

    asset_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange: Mapped[str | None] = mapped_column(String(64))
    currency: Mapped[str | None] = mapped_column(String(16))
    sector: Mapped[str | None] = mapped_column(String(128))
    base_asset: Mapped[str | None] = mapped_column(String(64))
    quote_asset: Mapped[str | None] = mapped_column(String(64))
    tradable: Mapped[bool] = mapped_column(server_default=text("true"), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), server_default=text("'available'"), nullable=False
    )
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class AssetUniverseORM(Base):
    """候选池定义表。"""

    __tablename__ = "asset_universes"
    __table_args__ = (
        Index("idx_universes_market_as_of", "market", "as_of"),
        Index("idx_universes_source", "source"),
        Index("idx_universes_strategy", "strategy_context"),
        Index("idx_universes_owner_visibility", "owner_id", "visibility"),
    )

    universe_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    strategy_context: Mapped[str | None] = mapped_column(String(128))
    owner_id: Mapped[str | None] = mapped_column(String(128))
    visibility: Mapped[str] = mapped_column(
        String(32), server_default=text("'system'"), nullable=False
    )
    base_universe_id: Mapped[str | None] = mapped_column(String(128))
    total_before_filter: Mapped[int | None]
    total_after_filter: Mapped[int | None]
    filters: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), server_default=text("'available'"), nullable=False
    )
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class AssetUniverseMemberORM(Base):
    """候选池成员表。"""

    __tablename__ = "asset_universe_members"
    __table_args__ = (
        UniqueConstraint("universe_id", "asset_id", name="uq_universe_members_universe_asset"),
        Index("idx_universe_members_universe", "universe_id"),
        Index("idx_universe_members_asset", "asset_id"),
        Index("idx_universe_members_market_symbol", "market", "symbol"),
    )

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    universe_id: Mapped[str] = mapped_column(String(128), nullable=False)
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    included: Mapped[bool] = mapped_column(server_default=text("true"), nullable=False)
    removed_reason: Mapped[str | None] = mapped_column(Text)
    rank_hint: Mapped[int | None]
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class RawRecordORM(Base):
    """Provider 原始响应归档表。"""

    __tablename__ = "raw_records"
    __table_args__ = (
        Index("idx_raw_records_provider_endpoint", "provider", "endpoint"),
        Index("idx_raw_records_asset_as_of", "asset_id", "as_of"),
        Index("idx_raw_records_collected_at", "collected_at"),
        Index("idx_raw_records_status", "status"),
        Index("idx_raw_records_request_hash", "provider", "endpoint", "request_hash"),
        Index("idx_raw_records_content_hash", "content_hash"),
    )

    raw_record_id: Mapped[str] = mapped_column(String(192), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(128), nullable=False)
    asset_id: Mapped[str | None] = mapped_column(String(128))
    symbol: Mapped[str | None] = mapped_column(String(64))
    market: Mapped[str | None] = mapped_column(String(32))
    request_params: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    request_hash: Mapped[str | None] = mapped_column(String(128))
    response_payload: Mapped[JsonDict] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(128))
    provider_version: Mapped[str | None] = mapped_column(String(64))
    latency_ms: Mapped[int | None]
    retry_count: Mapped[int] = mapped_column(server_default=text("0"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MarketBarORM(Base):
    """标准 OHLCV 行情表，迁移中会转换为 TimescaleDB hypertable。"""

    __tablename__ = "market_bars"
    __table_args__ = (
        Index("idx_market_bars_asset_tf_time", "asset_id", "timeframe", "timestamp"),
        Index("idx_market_bars_market_symbol", "market", "symbol"),
        Index("idx_market_bars_timestamp", "timestamp"),
        Index("idx_market_bars_closed", "asset_id", "timeframe", "is_closed", "timestamp"),
    )

    asset_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    end_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    open: Mapped[Decimal] = mapped_column(Numeric(30, 10), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(30, 10), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(30, 10), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(30, 10), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(36, 10), nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    adjustment: Mapped[str] = mapped_column(
        String(16), primary_key=True, server_default=text("''"), nullable=False
    )
    is_closed: Mapped[bool] = mapped_column(server_default=text("true"), nullable=False)
    raw_record_id: Mapped[str | None] = mapped_column(String(192))
    status: Mapped[str] = mapped_column(
        String(32), server_default=text("'available'"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class CryptoDerivativeSnapshotORM(Base):
    """数字货币衍生品快照表，迁移中会转换为 TimescaleDB hypertable。"""

    __tablename__ = "crypto_derivative_snapshots"
    __table_args__ = (
        Index("idx_crypto_derivatives_snapshot_id", "snapshot_id"),
        Index("idx_crypto_derivatives_asset_asof", "asset_id", "as_of"),
        Index("idx_crypto_derivatives_symbol_asof", "symbol", "as_of"),
        Index("idx_crypto_derivatives_status", "status"),
    )

    asset_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(192), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    funding_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    next_funding_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    open_interest: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    open_interest_value: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    long_short_ratio: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    basis_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    liquidation_risk_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    status: Mapped[str] = mapped_column(
        String(32), server_default=text("'available'"), nullable=False
    )
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class MarketCalendarORM(Base):
    """交易日历表。"""

    __tablename__ = "market_calendars"
    __table_args__ = (
        UniqueConstraint(
            "market", "exchange", "trade_date", "session_type", name="uq_market_calendars_session"
        ),
        Index("idx_market_calendars_market_date", "market", "trade_date"),
        Index("idx_market_calendars_exchange_date", "exchange", "trade_date"),
    )

    calendar_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange: Mapped[str] = mapped_column(String(64), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_trading_day: Mapped[bool] = mapped_column(nullable=False)
    open_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    session_type: Mapped[str] = mapped_column(String(32), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), server_default=text("'available'"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class EvidenceORM(Base):
    """推荐证据索引表。"""

    __tablename__ = "evidence"
    __table_args__ = (
        Index("idx_evidence_asset_asof", "asset_id", "as_of"),
        Index("idx_evidence_type", "evidence_type"),
        Index("idx_evidence_source", "source"),
    )

    evidence_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    evidence_type: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_id: Mapped[str | None] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    data_ref: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(Text)
    reliability: Mapped[str] = mapped_column(String(32), nullable=False)
    as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class FundamentalSnapshotORM(Base):
    """A 股财务和估值快照表。"""

    __tablename__ = "fundamental_snapshots"
    __table_args__ = (
        Index("idx_fundamental_asset_period", "asset_id", "report_period"),
        Index("idx_fundamental_as_of", "as_of"),
        Index("idx_fundamental_status", "status"),
        Index("idx_fundamental_source", "source"),
    )

    snapshot_id: Mapped[str] = mapped_column(String(192), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    report_period: Mapped[str | None] = mapped_column(String(32))
    pe_ttm: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    pb: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    roe: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    revenue_growth_yoy: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    net_profit_growth_yoy: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    debt_to_asset: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    operating_cashflow: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    missing_fields: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class CapitalFlowSnapshotORM(Base):
    """A 股资金流快照表。"""

    __tablename__ = "capital_flow_snapshots"
    __table_args__ = (
        Index("idx_capital_flow_asset_window_asof", "asset_id", "window", "as_of"),
        Index("idx_capital_flow_symbol_asof", "symbol", "as_of"),
        Index("idx_capital_flow_source", "source"),
    )

    snapshot_id: Mapped[str] = mapped_column(String(192), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    main_net_inflow: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    northbound_net_inflow: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    turnover_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    window: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class EventRecordORM(Base):
    """新闻、公告、监管、链上和市场事件表。"""

    __tablename__ = "event_records"
    __table_args__ = (
        Index("idx_events_asset_published", "asset_id", "published_at"),
        Index("idx_events_market_type", "market", "event_type"),
        Index("idx_events_importance", "importance"),
        Index("idx_events_source", "source"),
    )

    event_id: Mapped[str] = mapped_column(String(192), primary_key=True)
    asset_id: Mapped[str | None] = mapped_column(String(128))
    symbol: Mapped[str | None] = mapped_column(String(64))
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    sentiment: Mapped[str] = mapped_column(String(32), nullable=False)
    importance: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class IndicatorFrameORM(Base):
    """推荐链路使用的技术指标快照表。"""

    __tablename__ = "indicator_frames"
    __table_args__ = (
        UniqueConstraint(
            "asset_id",
            "timeframe",
            "horizon",
            "library",
            "input_end_at",
            name="uq_indicator_frames_input",
        ),
        Index("idx_indicator_frames_asset_tf_asof", "asset_id", "timeframe", "as_of"),
        Index("idx_indicator_frames_market_horizon", "market", "horizon"),
        Index("idx_indicator_frames_status", "status"),
    )

    indicator_frame_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    horizon: Mapped[str] = mapped_column(String(32), nullable=False)
    library: Mapped[str] = mapped_column(String(64), nullable=False)
    library_version: Mapped[str | None] = mapped_column(String(64))
    input_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    input_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    bar_count: Mapped[int] = mapped_column(nullable=False)
    rsi_14: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    macd: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    macd_signal: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    macd_hist: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    atr_14: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    bb_percent_b: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    ma_20: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    ma_60: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class FactorFrameORM(Base):
    """推荐因子结果表。"""

    __tablename__ = "factor_frames"
    __table_args__ = (
        Index("idx_factor_frames_asset_horizon_asof", "asset_id", "horizon", "as_of"),
        Index("idx_factor_frames_market_horizon", "market", "horizon"),
        Index("idx_factor_frames_status", "status"),
    )

    factor_frame_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    horizon: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    total_available_groups: Mapped[int] = mapped_column(nullable=False)
    missing_groups: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    source_ids: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    indicator_frame_id: Mapped[str | None] = mapped_column(String(160))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class ScreeningResultORM(Base):
    """候选池初筛结果表。"""

    __tablename__ = "screening_results"
    __table_args__ = (
        Index("idx_screening_universe_strategy", "universe_id", "strategy"),
        Index("idx_screening_market_asof", "market", "as_of"),
    )

    screening_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    universe_id: Mapped[str] = mapped_column(String(128), nullable=False)
    strategy: Mapped[str] = mapped_column(String(128), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    passed_count: Mapped[int] = mapped_column(nullable=False)
    removed_count: Mapped[int] = mapped_column(nullable=False)
    rules: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class ScreeningResultItemORM(Base):
    """单标的初筛明细表。"""

    __tablename__ = "screening_result_items"
    __table_args__ = (
        UniqueConstraint("screening_id", "asset_id", name="uq_screening_items_screening_asset"),
        Index("idx_screening_items_screening_passed", "screening_id", "passed"),
        Index("idx_screening_items_asset", "asset_id"),
        Index("idx_screening_items_market", "market"),
    )

    screening_item_id: Mapped[str] = mapped_column(String(192), primary_key=True)
    screening_id: Mapped[str] = mapped_column(String(160), nullable=False)
    universe_id: Mapped[str] = mapped_column(String(128), nullable=False)
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    passed: Mapped[bool] = mapped_column(nullable=False)
    removed_reason: Mapped[str | None] = mapped_column(Text)
    failed_rules: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    passed_rules: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    data_status: Mapped[str] = mapped_column(String(32), nullable=False)
    liquidity_status: Mapped[str | None] = mapped_column(String(32))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class AssetScoreORM(Base):
    """多维评分和推荐排序基础表。"""

    __tablename__ = "asset_scores"
    __table_args__ = (
        Index("idx_asset_scores_universe_rank", "universe_id", "rank"),
        Index("idx_asset_scores_asset_horizon_asof", "asset_id", "horizon", "as_of"),
        Index("idx_asset_scores_market_score", "market", "total_score"),
        Index("idx_asset_scores_status", "status"),
    )

    score_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    universe_id: Mapped[str] = mapped_column(String(128), nullable=False)
    screening_id: Mapped[str] = mapped_column(String(160), nullable=False)
    factor_frame_id: Mapped[str] = mapped_column(String(160), nullable=False)
    horizon: Mapped[str] = mapped_column(String(32), nullable=False)
    total_score: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    technical_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    fundamental_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    valuation_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    flow_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    derivatives_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    event_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    risk_penalty: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), server_default=text("0"), nullable=False
    )
    rank: Mapped[int] = mapped_column(nullable=False)
    rank_in_universe: Mapped[int | None]
    confidence: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    missing_penalty: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), server_default=text("0"), nullable=False
    )
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class SignalSnapshotORM(Base):
    """可解释信号快照表。"""

    __tablename__ = "signal_snapshots"
    __table_args__ = (
        Index("idx_signals_asset_horizon_asof", "asset_id", "horizon", "as_of"),
        Index("idx_signals_market_direction", "market", "direction"),
        Index("idx_signals_status", "status"),
    )

    signal_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    horizon: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(32), nullable=False)
    score: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class RiskFindingORM(Base):
    """风险发现和风险反驳表。"""

    __tablename__ = "risk_findings"
    __table_args__ = (
        Index("idx_risks_asset_asof", "asset_id", "as_of"),
        Index("idx_risks_type_severity", "risk_type", "severity"),
        Index("idx_risks_scope", "scope"),
    )

    risk_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    asset_id: Mapped[str | None] = mapped_column(String(128))
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    risk_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    score: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class RecommendationRunORM(Base):
    """一次推荐运行记录。"""

    __tablename__ = "recommendation_runs"
    __table_args__ = (
        Index("idx_recommendation_runs_universe", "universe_id"),
        Index("idx_recommendation_runs_strategy_market", "strategy", "market"),
        Index("idx_recommendation_runs_started_at", "started_at"),
    )

    run_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    universe_id: Mapped[str | None] = mapped_column(String(128))
    screening_id: Mapped[str | None] = mapped_column(String(160))
    strategy: Mapped[str] = mapped_column(String(128), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    horizon: Mapped[str] = mapped_column(String(32), nullable=False)
    limit: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class RecommendationRunUniverseORM(Base):
    """推荐运行和候选池关联表。"""

    __tablename__ = "recommendation_run_universes"
    __table_args__ = (
        UniqueConstraint("run_id", "universe_id", name="uq_run_universes_run_universe"),
        Index("idx_run_universes_run", "run_id"),
        Index("idx_run_universes_universe", "universe_id"),
        Index("idx_run_universes_market", "market"),
    )

    id: Mapped[str] = mapped_column(String(192), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(160), nullable=False)
    universe_id: Mapped[str] = mapped_column(String(128), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    weight: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    asset_count: Mapped[int | None]
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class AssetRecommendationORM(Base):
    """单标的推荐结果表。"""

    __tablename__ = "asset_recommendations"
    __table_args__ = (
        Index("idx_recommendations_run_rank", "run_id", "rank"),
        Index("idx_recommendations_asset_created", "asset_id", "created_at"),
        Index("idx_recommendations_market_action", "market", "action"),
        Index("idx_recommendations_score", "total_score"),
    )

    recommendation_id: Mapped[str] = mapped_column(String(192), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(160), nullable=False)
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    horizon: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    rank: Mapped[int] = mapped_column(nullable=False)
    total_score: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    conviction: Mapped[str] = mapped_column(String(32), nullable=False)
    score_id: Mapped[str | None] = mapped_column(String(160))
    factor_frame_id: Mapped[str | None] = mapped_column(String(160))
    signal_ids: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    risk_ids: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    agent_analysis_item_ids: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    evidence_ids: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    watch_conditions: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    invalid_if: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class AgentAnalysisRunORM(Base):
    """Agent 分析运行审计表。"""

    __tablename__ = "agent_analysis_runs"
    __table_args__ = (
        Index("idx_agent_runs_recommendation", "run_id"),
        Index("idx_agent_runs_agent_status", "agent_name", "status"),
        Index("idx_agent_runs_started_at", "started_at"),
    )

    agent_run_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(160), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_role: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128))
    model_version: Mapped[str | None] = mapped_column(String(128))
    input_ref: Mapped[str | None] = mapped_column(String(255))
    input_summary: Mapped[str | None] = mapped_column(Text)
    output_summary: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latency_ms: Mapped[int | None]
    error_message: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class AgentAnalysisItemORM(Base):
    """Agent 单标的分析明细表。"""

    __tablename__ = "agent_analysis_items"
    __table_args__ = (
        UniqueConstraint(
            "agent_run_id", "asset_id", "agent_name", name="uq_agent_items_run_asset_agent"
        ),
        Index("idx_agent_items_run_asset", "run_id", "asset_id"),
        Index("idx_agent_items_asset_asof", "asset_id", "as_of"),
        Index("idx_agent_items_agent_stance", "agent_name", "stance"),
    )

    agent_analysis_item_id: Mapped[str] = mapped_column(String(192), primary_key=True)
    agent_run_id: Mapped[str] = mapped_column(String(160), nullable=False)
    run_id: Mapped[str] = mapped_column(String(160), nullable=False)
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False)
    stance: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    key_points: Mapped[list[JsonDict]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    risk_ids: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    evidence_ids: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    summary: Mapped[str | None] = mapped_column(Text)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class PortfolioORM(Base):
    """用户组合定义表，支撑私人金融助手长期监控。"""

    __tablename__ = "portfolios"
    __table_args__ = (
        Index("idx_portfolios_owner", "owner_id"),
        Index("idx_portfolios_status", "status"),
    )

    portfolio_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    portfolio_type: Mapped[str] = mapped_column(String(32), nullable=False)
    base_currency: Mapped[str] = mapped_column(String(16), nullable=False)
    risk_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    total_equity: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    cash: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    market_value: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    max_position_weight: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    max_drawdown_alert: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class PositionORM(Base):
    """组合当前持仓表。"""

    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint(
            "portfolio_id",
            "asset_id",
            "side",
            name="uq_positions_portfolio_asset_side",
        ),
        Index("idx_positions_portfolio", "portfolio_id"),
        Index("idx_positions_asset", "asset_id"),
        Index("idx_positions_market", "market"),
    )

    position_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    portfolio_id: Mapped[str] = mapped_column(String(128), nullable=False)
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(36, 10), nullable=False)
    avg_cost: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    last_price: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    market_value: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    unrealized_pnl_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    portfolio_weight: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    leverage: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    liquidation_price: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class PortfolioSnapshotORM(Base):
    """组合历史快照表，用于长期追踪资产变化。"""

    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        PrimaryKeyConstraint("snapshot_id", "captured_at"),
        Index("idx_portfolio_snapshots_portfolio_time", "portfolio_id", "captured_at"),
        Index("idx_portfolio_snapshots_owner_time", "owner_id", "captured_at"),
    )

    snapshot_id: Mapped[str] = mapped_column(String(192), nullable=False)
    portfolio_id: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    total_equity: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    cash: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    market_value: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    position_count: Mapped[int | None]
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class PositionSnapshotORM(Base):
    """持仓历史快照表，用于追踪盈亏、仓位和风险暴露变化。"""

    __tablename__ = "position_snapshots"
    __table_args__ = (
        PrimaryKeyConstraint("snapshot_id", "captured_at"),
        Index("idx_position_snapshots_position_time", "position_id", "captured_at"),
        Index("idx_position_snapshots_portfolio_time", "portfolio_id", "captured_at"),
        Index("idx_position_snapshots_asset_time", "asset_id", "captured_at"),
    )

    snapshot_id: Mapped[str] = mapped_column(String(192), nullable=False)
    position_id: Mapped[str] = mapped_column(String(160), nullable=False)
    portfolio_id: Mapped[str] = mapped_column(String(128), nullable=False)
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(36, 10), nullable=False)
    avg_cost: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    last_price: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    market_value: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    unrealized_pnl_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    portfolio_weight: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    leverage: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    liquidation_price: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class WatchlistORM(Base):
    """私人观察池定义表。"""

    __tablename__ = "watchlists"
    __table_args__ = (
        Index("idx_watchlists_owner_status", "owner_id", "status"),
        Index("idx_watchlists_market", "market"),
    )

    watchlist_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    market: Mapped[str | None] = mapped_column(String(32))
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class WatchlistItemORM(Base):
    """私人观察池成员表。"""

    __tablename__ = "watchlist_items"
    __table_args__ = (
        UniqueConstraint("watchlist_id", "asset_id", name="uq_watchlist_items_watchlist_asset"),
        Index("idx_watchlist_items_watchlist_status", "watchlist_id", "status"),
        Index("idx_watchlist_items_asset", "asset_id"),
        Index("idx_watchlist_items_next_review", "next_review_at"),
    )

    watchlist_item_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    watchlist_id: Mapped[str] = mapped_column(String(128), nullable=False)
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(192))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    watch_conditions: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    trigger_conditions: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    invalid_conditions: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    risk_level: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    next_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_reason: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class AssetThesisORM(Base):
    """投资假设表。"""

    __tablename__ = "asset_theses"
    __table_args__ = (
        Index("idx_asset_theses_asset_status", "asset_id", "status"),
        Index("idx_asset_theses_source", "source_type", "source_id"),
    )

    thesis_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(192))
    thesis: Mapped[str] = mapped_column(Text, nullable=False)
    supporting_points: Mapped[list[JsonDict]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    risk_points: Mapped[list[JsonDict]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    invalid_if: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class WatchlistItemEventORM(Base):
    """观察池成员事件表，记录入池、升级、剔除和人工确认等轨迹。"""

    __tablename__ = "watchlist_item_events"
    __table_args__ = (
        Index("idx_watchlist_events_watchlist_created", "watchlist_id", "created_at"),
        Index("idx_watchlist_events_item_created", "watchlist_item_id", "created_at"),
        Index("idx_watchlist_events_asset_created", "asset_id", "created_at"),
        Index("idx_watchlist_events_owner_created", "owner_id", "created_at"),
    )

    event_id: Mapped[str] = mapped_column(String(192), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    watchlist_id: Mapped[str] = mapped_column(String(128), nullable=False)
    watchlist_item_id: Mapped[str] = mapped_column(String(160), nullable=False)
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    source_decision_id: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class MonitoringAlertORM(Base):
    """监控提醒表，只记录触发事件，不直接代表最终买卖建议。"""

    __tablename__ = "monitoring_alerts"
    __table_args__ = (
        Index("idx_alerts_owner_status", "owner_id", "status"),
        Index("idx_alerts_asset_asof", "asset_id", "as_of"),
        Index("idx_alerts_severity", "severity"),
    )

    alert_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    portfolio_id: Mapped[str | None] = mapped_column(String(128))
    asset_id: Mapped[str | None] = mapped_column(String(128))
    alert_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    triggered_by: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_condition: Mapped[str] = mapped_column(Text, nullable=False)
    current_value: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    threshold_value: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class DecisionLogORM(Base):
    """决策日志表，记录系统建议、用户动作、反馈和证据引用。"""

    __tablename__ = "decision_logs"
    __table_args__ = (
        Index("idx_decision_logs_owner_created", "owner_id", "created_at"),
        Index("idx_decision_logs_asset_created", "asset_id", "created_at"),
        Index("idx_decision_logs_type", "decision_type"),
    )

    decision_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    portfolio_id: Mapped[str | None] = mapped_column(String(128))
    asset_id: Mapped[str | None] = mapped_column(String(128))
    decision_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_recommendation_id: Mapped[str | None] = mapped_column(String(192))
    source_alert_id: Mapped[str | None] = mapped_column(String(160))
    workflow_run_id: Mapped[str | None] = mapped_column(String(160))
    suggested_action: Mapped[str] = mapped_column(String(64), nullable=False)
    user_action: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    reason_ids: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    risk_ids: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    evidence_ids: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class AssistantMemoryORM(Base):
    """Finance Memory 长期记忆表，不保存 Hermes 通用对话记忆。"""

    __tablename__ = "assistant_memories"
    __table_args__ = (
        Index("idx_memories_owner_type", "owner_id", "memory_type"),
        Index("idx_memories_scope_asset", "scope", "asset_id"),
        Index("idx_memories_status", "status"),
    )

    memory_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    memory_type: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_id: Mapped[str | None] = mapped_column(String(128))
    source_decision_id: Mapped[str | None] = mapped_column(String(160))
    source_review_task_id: Mapped[str | None] = mapped_column(String(160))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_ref: Mapped[str | None] = mapped_column(String(160))
    confidence: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class MemoryEmbeddingORM(Base):
    """Finance Memory 语义召回索引表，第一阶段用 JSONB 预留向量。"""

    __tablename__ = "memory_embeddings"
    __table_args__ = (
        Index("idx_memory_embeddings_owner_source", "owner_id", "source_type", "source_id"),
        Index("idx_memory_embeddings_memory", "memory_id"),
        Index("idx_memory_embeddings_hash", "content_hash"),
    )

    embedding_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    memory_id: Mapped[str | None] = mapped_column(String(160))
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(192), nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(JSONB)
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class FinancialMemoryEdgeORM(Base):
    """Finance Memory 轻量图谱边表。"""

    __tablename__ = "financial_memory_edges"
    __table_args__ = (
        UniqueConstraint(
            "owner_id",
            "source_type",
            "source_id",
            "relation_type",
            "target_type",
            "target_id",
            name="uq_memory_edges_owner_source_relation_target",
        ),
        Index("idx_memory_edges_source", "owner_id", "source_type", "source_id"),
        Index("idx_memory_edges_target", "owner_id", "target_type", "target_id"),
        Index("idx_memory_edges_relation", "relation_type"),
    )

    edge_id: Mapped[str] = mapped_column(String(192), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(192), nullable=False)
    relation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(192), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class ReviewTaskORM(Base):
    """复盘任务表。"""

    __tablename__ = "review_tasks"
    __table_args__ = (
        Index("idx_review_tasks_owner_due", "owner_id", "due_at"),
        Index("idx_review_tasks_status", "status"),
        Index("idx_review_tasks_source_decision", "source_decision_id"),
    )

    review_task_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    asset_id: Mapped[str | None] = mapped_column(String(128))
    source_decision_id: Mapped[str | None] = mapped_column(String(160))
    review_type: Mapped[str] = mapped_column(String(64), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    review_questions: Mapped[list[JsonDict]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    result_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class DataQualitySnapshotORM(Base):
    """资产级数据质量快照表，用于推荐报告说明数据新鲜度和缺口。"""

    __tablename__ = "data_quality_snapshots"
    __table_args__ = (
        PrimaryKeyConstraint("quality_id", "checked_at"),
        Index("idx_quality_asset_domain_checked", "asset_id", "data_domain", "checked_at"),
        Index("idx_quality_market_domain", "market", "data_domain"),
        Index("idx_quality_status", "status", "freshness_status"),
    )

    quality_id: Mapped[str] = mapped_column(String(192), nullable=False)
    asset_id: Mapped[str | None] = mapped_column(String(128))
    symbol: Mapped[str | None] = mapped_column(String(64))
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    data_domain: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    freshness_status: Mapped[str] = mapped_column(String(32), nullable=False)
    latest_data_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    missing_items: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    issue_count: Mapped[int] = mapped_column(nullable=False)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class AgentWorkflowRunORM(Base):
    """上层主 Agent 调用底层 Workflow 的运行审计表。"""

    __tablename__ = "agent_workflow_runs"
    __table_args__ = (
        Index("idx_workflow_runs_owner_started", "owner_id", "started_at"),
        Index("idx_workflow_runs_type_status", "workflow_type", "status"),
    )

    workflow_run_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    workflow_type: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_ref: Mapped[str | None] = mapped_column(String(192))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    input_ref: Mapped[str | None] = mapped_column(String(255))
    output_ref: Mapped[str | None] = mapped_column(String(255))
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class AgentWorkflowEventORM(Base):
    """Workflow 可审计事件表，供排障和前端办公室展示。"""

    __tablename__ = "agent_workflow_events"
    __table_args__ = (
        Index("idx_workflow_events_run_created", "workflow_run_id", "created_at"),
        Index("idx_workflow_events_agent", "agent_name"),
    )

    workflow_event_id: Mapped[str] = mapped_column(String(192), primary_key=True)
    workflow_run_id: Mapped[str] = mapped_column(String(160), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_name: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_ids: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
