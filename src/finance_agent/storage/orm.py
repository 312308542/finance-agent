"""M0 数据库 ORM 模型。

ORM 模型用于给 Alembic 和仓储层提供结构化入口。复杂领域协议仍然以
`payload` 保存全文，领域模型不要直接依赖 ORM 类。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Index,
    Integer,
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
    """资产身份主表，统一承载 A 股和数字货币资产的稳定标识。"""

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


class AssetProfileORM(Base):
    """资产慢变资料附表，保存名称、行业等可被不同数据源补全的信息。

    指数、行业、概念、热榜、涨停池等候选池成员关系由 asset_universe_members 承载，
    不写入本表，避免同一资产因多个来源成员关系产生大量重复画像行。
    """

    __tablename__ = "asset_profiles"
    __table_args__ = (
        Index("idx_asset_profiles_asset_source", "asset_id", "source"),
        Index("idx_asset_profiles_market_symbol", "market", "symbol"),
        Index("idx_asset_profiles_sector", "sector"),
    )

    asset_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    exchange: Mapped[str | None] = mapped_column(String(64))
    sector: Mapped[str | None] = mapped_column(String(128))
    industry: Mapped[str | None] = mapped_column(String(128))
    concept: Mapped[str | None] = mapped_column(String(128))
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


class AssetProviderMappingORM(Base):
    """资产在不同 Provider 中的代码映射。"""

    __tablename__ = "asset_provider_mappings"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_symbol",
            "market",
            name="uq_asset_provider_mappings_provider_symbol_market",
        ),
        Index("idx_asset_provider_mappings_asset", "asset_id"),
        Index("idx_asset_provider_mappings_provider", "provider", "source"),
    )

    mapping_id: Mapped[str] = mapped_column(String(192), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_symbol: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_exchange: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(128), nullable=False)
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


class AssetStatusSnapshotORM(Base):
    """资产交易状态快照，承载停复牌、可交易、退市等动态状态。"""

    __tablename__ = "asset_status_snapshots"
    __table_args__ = (
        Index("idx_asset_status_asset_asof", "asset_id", "as_of"),
        Index("idx_asset_status_market_symbol_asof", "market", "symbol", "as_of"),
        Index("idx_asset_status_status", "trading_status"),
    )

    asset_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    source: Mapped[str] = mapped_column(String(128), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    tradable: Mapped[bool] = mapped_column(server_default=text("true"), nullable=False)
    trading_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class RealtimeQuoteSnapshotORM(Base):
    """实时行情快照附表，保存最新价、涨跌、成交量额等高频变化字段。"""

    __tablename__ = "realtime_quote_snapshots"
    __table_args__ = (
        Index("idx_realtime_quotes_asset_asof", "asset_id", "as_of"),
        Index("idx_realtime_quotes_market_symbol_asof", "market", "symbol", "as_of"),
        Index("idx_realtime_quotes_source", "source"),
        Index("idx_realtime_quotes_snapshot", "data_snapshot_id"),
        Index("idx_realtime_quotes_quality_asof", "quality_status", "as_of"),
    )

    asset_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    source: Mapped[str] = mapped_column(String(128), primary_key=True)
    data_snapshot_id: Mapped[str | None] = mapped_column(String(255))
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    freshness_ms: Mapped[int | None] = mapped_column(Integer)
    last_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    prev_close: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    open: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    high: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    low: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    volume: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    turnover_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    change_amount: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    change_percent: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    bid_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    ask_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    status: Mapped[str] = mapped_column(
        String(32), server_default=text("'available'"), nullable=False
    )
    quality_status: Mapped[str] = mapped_column(
        String(32), server_default=text("'available'"), nullable=False
    )
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class IntradayQuoteLatestORM(Base):
    """盘中临时行情，只保留每个资产和来源的最新值。"""

    __tablename__ = "intraday_quote_latest"
    __table_args__ = (
        Index("idx_intraday_quote_latest_market", "market", "updated_at"),
        Index("idx_intraday_quote_latest_snapshot", "data_snapshot_id"),
        Index("idx_intraday_quote_latest_quality", "quality_status", "updated_at"),
    )

    asset_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source: Mapped[str] = mapped_column(String(128), primary_key=True)
    data_snapshot_id: Mapped[str | None] = mapped_column(String(255))
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    freshness_ms: Mapped[int | None] = mapped_column(Integer)
    last_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    prev_close: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    open: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    high: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    low: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    volume: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    turnover_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    change_amount: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    change_percent: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    bid_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    ask_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    status: Mapped[str] = mapped_column(
        String(32), server_default=text("'available'"), nullable=False
    )
    quality_status: Mapped[str] = mapped_column(
        String(32), server_default=text("'available'"), nullable=False
    )
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
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
        Index(
            "uq_raw_records_exact_dedup",
            "provider",
            "endpoint",
            "request_hash",
            "content_hash",
            "status",
            unique=True,
        ),
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


class DataSnapshotORM(Base):
    """跨链路不可变数据快照元数据和可重放输入。"""

    __tablename__ = "data_snapshots"
    __table_args__ = (
        CheckConstraint(
            "quality_status IN ('available', 'partial', 'stale', 'conflict', 'unavailable', "
            "'invalid_server_time', 'after_hours_snapshot', 'clock_skew')",
            name="ck_data_snapshots_quality_status",
        ),
        Index("idx_data_snapshots_type_asof", "snapshot_type", "as_of"),
        Index("idx_data_snapshots_provider_captured", "provider", "captured_at"),
        Index("idx_data_snapshots_quality", "quality_status"),
        Index("idx_data_snapshots_content_hash", "content_hash"),
    )

    data_snapshot_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    snapshot_type: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_version: Mapped[str | None] = mapped_column(String(128))
    quality_status: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    raw_record_ids: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    snapshot_metadata: Mapped[JsonDict] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class DataSyncWatermarkORM(Base):
    """数据采集水位表，记录每个资产在不同数据域的成功水位和失败重试状态。"""

    __tablename__ = "data_sync_watermarks"
    __table_args__ = (
        PrimaryKeyConstraint(
            "asset_id",
            "data_domain",
            "timeframe",
            "provider",
            name="pk_data_sync_watermarks",
        ),
        Index("idx_data_sync_watermarks_market_domain", "market", "data_domain"),
        Index("idx_data_sync_watermarks_next_retry", "next_retry_at"),
        Index("idx_data_sync_watermarks_status", "status"),
        Index("idx_data_sync_watermarks_watermark", "data_domain", "timeframe", "watermark_at"),
    )

    asset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    data_domain: Mapped[str] = mapped_column(String(64), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), server_default=text("''"), nullable=False)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), server_default=text("'pending'"), nullable=False)
    watermark_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fail_count: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    last_error_message: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


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


class MarketBarIntradayORM(Base):
    """分钟/小时级盘中 K 线表，和长期日 K 使用独立生命周期策略。"""

    __tablename__ = "market_bars_intraday"
    __table_args__ = (
        Index(
            "idx_market_bars_intraday_asset_tf_time",
            "asset_id",
            "timeframe",
            "timestamp",
        ),
        Index("idx_market_bars_intraday_market_symbol", "market", "symbol"),
        Index("idx_market_bars_intraday_timestamp", "timestamp"),
        Index(
            "idx_market_bars_intraday_closed",
            "asset_id",
            "timeframe",
            "is_closed",
            "timestamp",
        ),
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


class FundNavSnapshotORM(Base):
    """开放式基金净值快照表。"""

    __tablename__ = "fund_nav_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "asset_id",
            "nav_date",
            name="uq_fund_nav_snapshots_source_asset_nav_date",
        ),
        Index("idx_fund_nav_snapshots_asset_nav_date", "asset_id", "nav_date"),
        Index("idx_fund_nav_snapshots_symbol_nav_date", "symbol", "nav_date"),
        Index("idx_fund_nav_snapshots_status", "status"),
        Index("idx_fund_nav_snapshots_source", "source"),
    )

    snapshot_id: Mapped[str] = mapped_column(String(192), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    nav_date: Mapped[date] = mapped_column(Date, nullable=False)
    unit_nav: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    accumulated_nav: Mapped[Decimal | None] = mapped_column(Numeric(30, 10))
    daily_return: Mapped[Decimal | None] = mapped_column(Numeric(18, 10))
    purchase_status: Mapped[str | None] = mapped_column(String(64))
    redeem_status: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(64), nullable=False)
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
        Index("idx_fundamental_asset_source_asof", "asset_id", "source", "as_of"),
        Index("idx_fundamental_asset_source_period", "asset_id", "source", "report_period"),
        Index(
            "uq_fundamental_source_asset_asof_valuation",
            "source",
            "asset_id",
            "as_of",
            unique=True,
            postgresql_where=text("report_period IS NULL"),
        ),
        Index(
            "uq_fundamental_source_asset_period_report",
            "source",
            "asset_id",
            "report_period",
            unique=True,
            postgresql_where=text("report_period IS NOT NULL"),
        ),
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
        Index(
            "idx_asset_scores_screening_strategy_rank",
            "screening_id",
            "strategy_id",
            "rank",
        ),
        Index(
            "idx_asset_scores_asset_strategy_horizon_asof",
            "asset_id",
            "strategy_id",
            "horizon",
            "as_of",
        ),
        Index("idx_asset_scores_market_score", "market", "total_score"),
        Index("idx_asset_scores_status", "status"),
    )

    score_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    universe_id: Mapped[str] = mapped_column(String(128), nullable=False)
    screening_id: Mapped[str] = mapped_column(String(160), nullable=False)
    factor_frame_id: Mapped[str] = mapped_column(String(160), nullable=False)
    horizon: Mapped[str] = mapped_column(String(32), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(128), nullable=False)
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


class ScoringStrategyORM(Base):
    """评分策略配置表，保存不同推荐策略的因子组权重。"""

    __tablename__ = "scoring_strategies"
    __table_args__ = (
        Index("idx_scoring_strategies_market_status", "market", "status"),
        Index("idx_scoring_strategies_status", "status"),
    )

    strategy_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    group_weights: Mapped[JsonDict] = mapped_column(JSONB, nullable=False)
    missing_penalty: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), server_default=text("'draft'"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class BacktestResultORM(Base):
    """轻量回测结果表，保存策略历史验证摘要和可复现数据版本。"""

    __tablename__ = "backtest_results"
    __table_args__ = (
        Index("idx_backtest_results_strategy_created", "strategy_id", "created_at"),
        Index("idx_backtest_results_universe_created", "universe_id", "created_at"),
        Index("idx_backtest_results_market_status", "market", "status"),
        Index("idx_backtest_results_window", "start_at", "end_at"),
    )

    backtest_id: Mapped[str] = mapped_column(String(192), primary_key=True)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(128), nullable=False)
    universe_id: Mapped[str] = mapped_column(String(128), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rebalance_frequency: Mapped[str] = mapped_column(String(32), nullable=False)
    metrics: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    data_versions: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class StrategyObservationRunORM(Base):
    """每日多策略前向观察批次头。"""

    __tablename__ = "strategy_observation_runs"
    __table_args__ = (
        UniqueConstraint(
            "trade_date",
            "universe_id",
            name="uq_strategy_observation_run_day_universe",
        ),
        Index("idx_strategy_observation_runs_date", "trade_date"),
        Index("idx_strategy_observation_runs_status", "status"),
    )

    observation_id: Mapped[str] = mapped_column(String(192), primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    universe_id: Mapped[str] = mapped_column(String(128), nullable=False)
    screening_id: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    data_versions: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
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


class StrategyObservationPositionORM(Base):
    """单个观察批次中某策略的 Top N 仓位。"""

    __tablename__ = "strategy_observation_positions"
    __table_args__ = (
        UniqueConstraint(
            "observation_id",
            "strategy_id",
            "asset_id",
            name="uq_strategy_position_asset",
        ),
        Index("idx_strategy_positions_observation", "observation_id"),
        Index("idx_strategy_positions_strategy_signal", "strategy_id", "signal_date"),
        Index("idx_strategy_positions_asset", "asset_id"),
    )

    position_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    observation_id: Mapped[str] = mapped_column(String(192), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(128), nullable=False)
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    score_id: Mapped[str] = mapped_column(String(255), nullable=False)
    signal_date: Mapped[date] = mapped_column(Date, nullable=False)
    entry_date: Mapped[date | None] = mapped_column(Date)
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    benchmark_entry_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class StrategyObservationOutcomeORM(Base):
    """观察仓位在 5/10/20 个交易日到期后的收益标签。"""

    __tablename__ = "strategy_observation_outcomes"
    __table_args__ = (
        UniqueConstraint(
            "position_id",
            "horizon_days",
            name="uq_strategy_outcome_horizon",
        ),
        CheckConstraint(
            "horizon_days in (5, 10, 20)",
            name="ck_strategy_outcome_horizon",
        ),
        Index("idx_strategy_outcomes_due_status", "due_trade_date", "status"),
        Index("idx_strategy_outcomes_position", "position_id"),
    )

    outcome_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    position_id: Mapped[str] = mapped_column(String(255), nullable=False)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    due_trade_date: Mapped[date | None] = mapped_column(Date)
    exit_date: Mapped[date | None] = mapped_column(Date)
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    gross_return: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    net_return: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    benchmark_return: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    excess_return: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class StrategyTrialStateORM(Base):
    """策略历史验证、试运行、关闭和晋级状态。"""

    __tablename__ = "strategy_trial_states"
    __table_args__ = (
        CheckConstraint(
            "state in ('research','historical_passed','trial','validated','disabled')",
            name="ck_strategy_trial_state",
        ),
        Index("idx_strategy_trial_states_state", "state"),
    )

    strategy_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    strategy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    historical_evidence_id: Mapped[str | None] = mapped_column(String(192))
    forward_metrics: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    consecutive_failure_count: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), nullable=False
    )
    disabled_reason: Mapped[str | None] = mapped_column(Text)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
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
    score_id: Mapped[str | None] = mapped_column(String(255))
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


class AssistantTriggerEventORM(Base):
    """私人金融助手触发事件表，记录 Agent 被唤醒前的输入事件。"""

    __tablename__ = "assistant_trigger_events"
    __table_args__ = (
        UniqueConstraint("dedup_key", "triggered_at", name="uq_trigger_events_dedup_time"),
        Index("idx_trigger_events_owner_status", "owner_id", "status"),
        Index("idx_trigger_events_type_status", "trigger_type", "status"),
        Index("idx_trigger_events_asset_time", "asset_id", "triggered_at"),
        Index("idx_trigger_events_dedup_time", "dedup_key", "triggered_at"),
        Index("idx_trigger_events_agent_runtime", "agent_runtime", "status"),
        Index("idx_trigger_events_requested_workflow", "requested_workflow_type", "status"),
    )

    trigger_event_id: Mapped[str] = mapped_column(String(192), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_ref: Mapped[str | None] = mapped_column(String(192))
    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    agent_runtime: Mapped[str] = mapped_column(
        String(64),
        server_default=text("'hermes_agent'"),
        nullable=False,
    )
    agent_task_id: Mapped[str | None] = mapped_column(String(160))
    requested_workflow_type: Mapped[str] = mapped_column(String(64), nullable=False)
    portfolio_id: Mapped[str | None] = mapped_column(String(128))
    watchlist_id: Mapped[str | None] = mapped_column(String(128))
    recommendation_run_id: Mapped[str | None] = mapped_column(String(160))
    asset_id: Mapped[str | None] = mapped_column(String(128))
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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


class DecisionGateORM(Base):
    """决策闸门结果，记录每次建议是否允许进入动作层。"""

    __tablename__ = "decision_gates"
    __table_args__ = (
        CheckConstraint(
            "status IN ('approved', 'rejected', 'pending_review', 'data_unavailable', 'expired')",
            name="ck_decision_gates_status",
        ),
        Index("idx_decision_gates_snapshot", "data_snapshot_id"),
        Index("idx_decision_gates_status_evaluated", "status", "evaluated_at"),
        Index("idx_decision_gates_type_evaluated", "decision_type", "evaluated_at"),
    )

    decision_gate_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    decision_type: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    data_snapshot_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    reasons: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    evidence_ids: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[str | None] = mapped_column(String(128))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class SchedulerTaskRunORM(Base):
    """持久化任务运行记录，支持租约、幂等、重试和进程恢复。"""

    __tablename__ = "scheduler_task_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('scheduled', 'blocked', 'pending', 'running', "
            "'completed', 'failed', 'cancelled')",
            name="ck_scheduler_task_runs_status",
        ),
        UniqueConstraint("idempotency_key", name="uq_scheduler_task_runs_idempotency"),
        Index("idx_scheduler_task_runs_due", "status", "next_retry_at", "created_at"),
        Index(
            "idx_scheduler_task_runs_scheduled",
            "status",
            "scheduled_for",
            "priority",
        ),
        Index("idx_scheduler_task_runs_lease", "status", "lease_expires_at"),
        Index("idx_scheduler_task_runs_pool", "status", "resource_pool"),
        Index("idx_scheduler_task_runs_mutex", "status", "mutex_key"),
        Index("idx_scheduler_task_runs_job_created", "job_name", "created_at"),
    )

    task_id: Mapped[str] = mapped_column(String(192), primary_key=True)
    job_name: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), server_default=text("'pending'"), nullable=False
    )
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    schedule_type: Mapped[str] = mapped_column(
        String(32), server_default=text("'manual'"), nullable=False
    )
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    priority: Mapped[int] = mapped_column(Integer, server_default=text("100"), nullable=False)
    resource_pool: Mapped[str] = mapped_column(
        String(64), server_default=text("'default'"), nullable=False
    )
    mutex_key: Mapped[str | None] = mapped_column(String(160))
    dependency_generation: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    required_data_domains: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    blocked_reason: Mapped[str | None] = mapped_column(String(64))
    blocked_detail: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    config_digest: Mapped[str | None] = mapped_column(String(64))
    coalesced_count: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), nullable=False
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, server_default=text("3"), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_token: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class OutboxEventORM(Base):
    """业务事件 Outbox，数据库提交后由独立发布器投递到 Redis Streams。"""

    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_outbox_events_idempotency"),
        CheckConstraint("attempts >= 0", name="ck_outbox_events_attempts"),
        Index("idx_outbox_events_pending", "published_at", "available_at", "created_at"),
        Index("idx_outbox_events_lease", "publish_lease_expires_at"),
        Index("idx_outbox_events_type_created", "event_type", "created_at"),
    )

    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_stream_id: Mapped[str | None] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    publish_lease_owner: Mapped[str | None] = mapped_column(String(128))
    publish_lease_token: Mapped[str | None] = mapped_column(String(128))
    publish_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class OrderDraftORM(Base):
    """用户确认建议后的订单草案，只作为文档性操作清单。"""

    __tablename__ = "order_drafts"
    __table_args__ = (
        Index("idx_order_drafts_owner_status", "owner_id", "status"),
        Index("idx_order_drafts_decision_status", "decision_log_id", "status"),
        Index("idx_order_drafts_asset_created", "asset_id", "created_at"),
    )

    order_draft_id: Mapped[str] = mapped_column(String(192), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    portfolio_id: Mapped[str] = mapped_column(String(128), nullable=False)
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    decision_log_id: Mapped[str] = mapped_column(String(160), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    suggested_price_range: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    suggested_position_ratio: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    constraints: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), server_default=text("'drafted'"), nullable=False
    )
    disclaimer: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class ExecutionRecordORM(Base):
    """用户在外部完成交易后的手工执行登记。"""

    __tablename__ = "execution_records"
    __table_args__ = (
        Index("idx_execution_records_owner_created", "owner_id", "created_at"),
        Index("idx_execution_records_asset_executed", "asset_id", "executed_at"),
        Index("idx_execution_records_draft", "order_draft_id"),
        Index("idx_execution_records_decision", "decision_log_id"),
    )

    execution_id: Mapped[str] = mapped_column(String(192), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    portfolio_id: Mapped[str] = mapped_column(String(128), nullable=False)
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    order_draft_id: Mapped[str | None] = mapped_column(String(192))
    decision_log_id: Mapped[str | None] = mapped_column(String(160))
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    executed_price: Mapped[Decimal] = mapped_column(Numeric(36, 10), nullable=False)
    executed_quantity: Mapped[Decimal] = mapped_column(Numeric(36, 10), nullable=False)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fee: Mapped[Decimal | None] = mapped_column(Numeric(36, 10))
    note: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(
        String(64), server_default=text("'user_reported'"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class UserInvestmentProfileORM(Base):
    """用户投资画像表，保存可审计、可演化的风险偏好和风格偏好。"""

    __tablename__ = "user_investment_profiles"
    __table_args__ = (
        Index("idx_user_investment_profiles_owner_status", "owner_id", "status"),
        Index("idx_user_investment_profiles_updated", "updated_at"),
    )

    profile_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    risk_appetite: Mapped[str] = mapped_column(String(32), nullable=False)
    horizon: Mapped[str] = mapped_column(String(32), nullable=False)
    capital_scale: Mapped[str] = mapped_column(String(64), nullable=False)
    style_tendency: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    timing_posture: Mapped[str] = mapped_column(String(32), nullable=False)
    dimension_confidence: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    source: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), server_default=text("'active'"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
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


class AssistantChatSessionORM(Base):
    """CLI 聊天会话表，只保存对话流水，不等同于 Finance Memory。"""

    __tablename__ = "assistant_chat_sessions"
    __table_args__ = (
        Index("idx_chat_sessions_owner_status", "owner_id", "status"),
        Index("idx_chat_sessions_owner_updated", "owner_id", "updated_at"),
        Index("idx_chat_sessions_last_message", "owner_id", "last_message_at"),
    )

    chat_session_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_count: Mapped[int] = mapped_column(server_default=text("0"), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    payload: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )


class AssistantChatMessageORM(Base):
    """CLI 聊天消息表，按会话顺序保存用户和 Agent 消息。"""

    __tablename__ = "assistant_chat_messages"
    __table_args__ = (
        UniqueConstraint(
            "chat_session_id",
            "sequence_no",
            name="uq_chat_messages_session_seq",
        ),
        Index("idx_chat_messages_session_seq", "chat_session_id", "sequence_no"),
        Index("idx_chat_messages_owner_created", "owner_id", "created_at"),
        Index("idx_chat_messages_intent", "intent"),
    )

    chat_message_id: Mapped[str] = mapped_column(String(192), primary_key=True)
    chat_session_id: Mapped[str] = mapped_column(String(160), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence_no: Mapped[int] = mapped_column(nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str | None] = mapped_column(String(64))
    data: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
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


class ModelProviderORM(Base):
    """模型供应商配置表，保存供应商的基础连接信息。"""

    __tablename__ = "model_providers"
    __table_args__ = (
        UniqueConstraint("provider_key", name="uq_model_providers_provider_key"),
        Index("idx_model_providers_vendor", "provider_vendor"),
        Index("idx_model_providers_enabled", "is_enabled"),
        Index("idx_model_providers_default", "is_default"),
    )

    provider_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_vendor: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[str | None] = mapped_column(Text)
    api_key: Mapped[str | None] = mapped_column(Text)
    timeout_seconds: Mapped[int] = mapped_column(
        Integer, server_default=text("30"), nullable=False
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true"), nullable=False
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
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


class ModelInstanceORM(Base):
    """模型实例配置表，绑定具体供应商、模型名和路由角色。"""

    __tablename__ = "model_instances"
    __table_args__ = (
        UniqueConstraint("model_key", name="uq_model_instances_model_key"),
        Index("idx_model_instances_provider_enabled", "provider_key", "is_enabled"),
        Index("idx_model_instances_role_enabled", "role", "is_enabled"),
        Index("idx_model_instances_model_type", "model_type"),
        Index("idx_model_instances_default", "is_default"),
    )

    model_instance_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    provider_key: Mapped[str] = mapped_column(String(64), nullable=False)
    model_key: Mapped[str] = mapped_column(String(128), nullable=False)
    model_type: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str | None] = mapped_column(String(64))
    route_priority: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), nullable=False
    )
    timeout_seconds: Mapped[int] = mapped_column(
        Integer, server_default=text("30"), nullable=False
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true"), nullable=False
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
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


class ModelRoutingRuleORM(Base):
    """模型路由规则表，允许在线切换常规分析和高风险复核模型。"""

    __tablename__ = "model_routing_rules"
    __table_args__ = (
        UniqueConstraint(
            "workflow_type",
            "task",
            "role",
            "decision_type",
            name="uq_model_routing_rules_scope",
        ),
        Index("idx_model_routing_rules_workflow", "workflow_type", "task"),
        Index("idx_model_routing_rules_role_enabled", "role", "is_enabled"),
    )

    rule_id: Mapped[str] = mapped_column(String(192), primary_key=True)
    workflow_type: Mapped[str] = mapped_column(String(64), nullable=False)
    task: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    model_key: Mapped[str] = mapped_column(String(128), nullable=False)
    decision_type: Mapped[str] = mapped_column(
        String(64), server_default=text("''"), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true"), nullable=False
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


class RetrievalProfileORM(Base):
    """检索配置表，保存 embedding、rerank 和 retrieval 参数。"""

    __tablename__ = "retrieval_profiles"
    __table_args__ = (
        UniqueConstraint("profile_key", name="uq_retrieval_profiles_profile_key"),
        Index("idx_retrieval_profiles_scope_default", "usage_scope", "is_default"),
        Index("idx_retrieval_profiles_enabled", "is_enabled"),
    )

    profile_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    profile_key: Mapped[str] = mapped_column(String(128), nullable=False)
    profile_name: Mapped[str] = mapped_column(String(255), nullable=False)
    usage_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    search_method: Mapped[str] = mapped_column(String(32), nullable=False)
    embedding_model_key: Mapped[str | None] = mapped_column(String(128))
    rerank_model_key: Mapped[str | None] = mapped_column(String(128))
    top_k: Mapped[int] = mapped_column(Integer, server_default=text("4"), nullable=False)
    score_threshold: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    reranking_enable: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
    )
    reranking_mode: Mapped[str | None] = mapped_column(String(32))
    weights: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true"), nullable=False
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
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


class DataRecoveryRunORM(Base):
    """停跑恢复补跑批次表：保存冻结范围、计划哈希、状态机与门控事实。"""

    __tablename__ = "data_recovery_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'approved', 'running', 'paused', 'verifying', "
            "'attention_required', 'completed', 'completed_with_exceptions', 'cancelled')",
            name="ck_data_recovery_runs_status",
        ),
        CheckConstraint(
            "gate_status IN ('recovering', 'degraded', 'open')",
            name="ck_data_recovery_runs_gate_status",
        ),
        Index("idx_data_recovery_runs_market_created", "market", "created_at"),
        Index("idx_data_recovery_runs_status", "status"),
        Index("idx_data_recovery_runs_plan_hash", "plan_hash"),
    )

    run_id: Mapped[str] = mapped_column(String(192), primary_key=True)
    market: Mapped[str] = mapped_column(String(32), nullable=False)
    universe_id: Mapped[str | None] = mapped_column(String(160))
    universe_snapshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    universe_snapshot_hash: Mapped[str | None] = mapped_column(String(128))
    gap_start_date: Mapped[date | None] = mapped_column(Date)
    cutoff_date: Mapped[date] = mapped_column(Date, nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), server_default=text("'draft'"), nullable=False
    )
    gate_status: Mapped[str] = mapped_column(
        String(32), server_default=text("'degraded'"), nullable=False
    )
    requested_by: Mapped[str | None] = mapped_column(String(128))
    approved_by: Mapped[str | None] = mapped_column(String(128))
    summary: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    quality_result: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class DataRecoveryStepORM(Base):
    """补跑批次逻辑阶段表：一个步骤可对应多个持久化调度任务分区。"""

    __tablename__ = "data_recovery_steps"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'skipped', 'cancelled')",
            name="ck_data_recovery_steps_status",
        ),
        UniqueConstraint(
            "run_id", "phase", "data_domain", name="uq_data_recovery_steps_scope"
        ),
        Index("idx_data_recovery_steps_run_status", "run_id", "status"),
    )

    step_id: Mapped[str] = mapped_column(String(224), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(192), nullable=False)
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    data_domain: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), server_default=text("'pending'"), nullable=False
    )
    depends_on: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), nullable=False
    )
    target_count: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    completed_count: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), nullable=False
    )
    retryable_count: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), nullable=False
    )
    exception_count: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), nullable=False
    )
    task_params: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    attempt_round: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class DataRecoveryTargetORM(Base):
    """补跑缺口目标区间表：区间压缩保存缺口，不逐资产逐日期展开。"""

    __tablename__ = "data_recovery_targets"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'exception', 'excluded')",
            name="ck_data_recovery_targets_status",
        ),
        # 市场级目标 asset_id 允许为空，唯一性用 coalesce 归一后判定。
        Index(
            "uq_data_recovery_targets_scope",
            "run_id",
            "data_domain",
            text("coalesce(asset_id, '')"),
            "gap_start_at",
            "gap_end_at",
            "granularity",
            unique=True,
        ),
        Index("idx_data_recovery_targets_run_status", "run_id", "status"),
        Index("idx_data_recovery_targets_step_status", "step_id", "status"),
        Index("idx_data_recovery_targets_retry", "run_id", "next_retry_at"),
    )

    target_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(192), nullable=False)
    step_id: Mapped[str] = mapped_column(String(224), nullable=False)
    data_domain: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_id: Mapped[str | None] = mapped_column(String(128))
    gap_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    gap_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    granularity: Mapped[str] = mapped_column(
        String(32), server_default=text("'1d'"), nullable=False
    )
    expected_count: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), server_default=text("'pending'"), nullable=False
    )
    exception_code: Mapped[str | None] = mapped_column(String(32))
    exception_evidence: Mapped[JsonDict] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
