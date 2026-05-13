"""创建 M0 推荐主链路表

Revision ID: 20260514_0001
Revises:
Create Date: 2026-05-14 00:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260514_0001"
down_revision = None
branch_labels = None
depends_on = None


def _jsonb_object() -> sa.Column:
    return sa.Column(
        "payload",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    op.create_table(
        "assets",
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("asset_type", sa.String(length=32), nullable=False),
        sa.Column("exchange", sa.String(length=64), nullable=True),
        sa.Column("currency", sa.String(length=16), nullable=True),
        sa.Column("sector", sa.String(length=128), nullable=True),
        sa.Column("base_asset", sa.String(length=64), nullable=True),
        sa.Column("quote_asset", sa.String(length=64), nullable=True),
        sa.Column("tradable", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "status", sa.String(length=32), server_default=sa.text("'available'"), nullable=False
        ),
        _jsonb_object(),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("asset_id"),
        sa.UniqueConstraint("market", "symbol", name="uq_assets_market_symbol"),
    )
    op.create_index("idx_assets_market_symbol", "assets", ["market", "symbol"])
    op.create_index("idx_assets_exchange", "assets", ["exchange"])
    op.create_index("idx_assets_sector", "assets", ["sector"])
    op.create_index("idx_assets_status", "assets", ["status"])

    op.create_table(
        "asset_universes",
        sa.Column("universe_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("strategy_context", sa.String(length=128), nullable=True),
        sa.Column("owner_id", sa.String(length=128), nullable=True),
        sa.Column(
            "visibility", sa.String(length=32), server_default=sa.text("'system'"), nullable=False
        ),
        sa.Column("base_universe_id", sa.String(length=128), nullable=True),
        sa.Column("total_before_filter", sa.Integer(), nullable=True),
        sa.Column("total_after_filter", sa.Integer(), nullable=True),
        sa.Column(
            "filters",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "status", sa.String(length=32), server_default=sa.text("'available'"), nullable=False
        ),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("universe_id"),
    )
    op.create_index("idx_universes_market_as_of", "asset_universes", ["market", "as_of"])
    op.create_index("idx_universes_source", "asset_universes", ["source"])
    op.create_index("idx_universes_strategy", "asset_universes", ["strategy_context"])
    op.create_index("idx_universes_owner_visibility", "asset_universes", ["owner_id", "visibility"])

    op.create_table(
        "asset_universe_members",
        sa.Column("id", sa.String(length=160), nullable=False),
        sa.Column("universe_id", sa.String(length=128), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("included", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("removed_reason", sa.Text(), nullable=True),
        sa.Column("rank_hint", sa.Integer(), nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("universe_id", "asset_id", name="uq_universe_members_universe_asset"),
    )
    op.create_index("idx_universe_members_universe", "asset_universe_members", ["universe_id"])
    op.create_index("idx_universe_members_asset", "asset_universe_members", ["asset_id"])
    op.create_index(
        "idx_universe_members_market_symbol", "asset_universe_members", ["market", "symbol"]
    )

    op.create_table(
        "raw_records",
        sa.Column("raw_record_id", sa.String(length=192), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("endpoint", sa.String(length=128), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=True),
        sa.Column("symbol", sa.String(length=64), nullable=True),
        sa.Column("market", sa.String(length=32), nullable=True),
        sa.Column(
            "request_params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("request_hash", sa.String(length=128), nullable=True),
        sa.Column("response_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.String(length=128), nullable=True),
        sa.Column("provider_version", sa.String(length=64), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=True),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("raw_record_id"),
    )
    op.create_index("idx_raw_records_provider_endpoint", "raw_records", ["provider", "endpoint"])
    op.create_index("idx_raw_records_asset_as_of", "raw_records", ["asset_id", "as_of"])
    op.create_index("idx_raw_records_collected_at", "raw_records", ["collected_at"])
    op.create_index("idx_raw_records_status", "raw_records", ["status"])
    op.create_index(
        "idx_raw_records_request_hash", "raw_records", ["provider", "endpoint", "request_hash"]
    )
    op.create_index("idx_raw_records_content_hash", "raw_records", ["content_hash"])

    op.create_table(
        "market_bars",
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("open", sa.Numeric(precision=30, scale=10), nullable=False),
        sa.Column("high", sa.Numeric(precision=30, scale=10), nullable=False),
        sa.Column("low", sa.Numeric(precision=30, scale=10), nullable=False),
        sa.Column("close", sa.Numeric(precision=30, scale=10), nullable=False),
        sa.Column("volume", sa.Numeric(precision=36, scale=10), nullable=False),
        sa.Column("amount", sa.Numeric(precision=36, scale=10), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("adjustment", sa.String(length=16), server_default=sa.text("''"), nullable=False),
        sa.Column("is_closed", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("raw_record_id", sa.String(length=192), nullable=True),
        sa.Column(
            "status", sa.String(length=32), server_default=sa.text("'available'"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("asset_id", "timeframe", "timestamp", "source", "adjustment"),
    )
    op.create_index(
        "idx_market_bars_asset_tf_time", "market_bars", ["asset_id", "timeframe", "timestamp"]
    )
    op.create_index("idx_market_bars_market_symbol", "market_bars", ["market", "symbol"])
    op.create_index("idx_market_bars_timestamp", "market_bars", ["timestamp"])
    op.create_index(
        "idx_market_bars_closed", "market_bars", ["asset_id", "timeframe", "is_closed", "timestamp"]
    )
    op.execute(
        """
        SELECT create_hypertable(
          'market_bars',
          'timestamp',
          if_not_exists => TRUE,
          chunk_time_interval => INTERVAL '1 month'
        )
        """
    )
    op.execute(
        """
        ALTER TABLE market_bars SET (
          timescaledb.compress,
          timescaledb.compress_segmentby = 'asset_id,timeframe',
          timescaledb.compress_orderby = 'timestamp DESC'
        )
        """
    )

    op.create_table(
        "market_calendars",
        sa.Column("calendar_id", sa.String(length=160), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("exchange", sa.String(length=64), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("is_trading_day", sa.Boolean(), nullable=False),
        sa.Column("open_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("session_type", sa.String(length=32), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column(
            "status", sa.String(length=32), server_default=sa.text("'available'"), nullable=False
        ),
        sa.Column("source", sa.String(length=64), nullable=False),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("calendar_id"),
        sa.UniqueConstraint(
            "market", "exchange", "trade_date", "session_type", name="uq_market_calendars_session"
        ),
    )
    op.create_index(
        "idx_market_calendars_market_date", "market_calendars", ["market", "trade_date"]
    )
    op.create_index(
        "idx_market_calendars_exchange_date", "market_calendars", ["exchange", "trade_date"]
    )

    op.create_table(
        "evidence",
        sa.Column("evidence_id", sa.String(length=160), nullable=False),
        sa.Column("evidence_type", sa.String(length=64), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("data_ref", sa.String(length=255), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("reliability", sa.String(length=32), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=True),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("evidence_id"),
    )
    op.create_index("idx_evidence_asset_asof", "evidence", ["asset_id", "as_of"])
    op.create_index("idx_evidence_type", "evidence", ["evidence_type"])
    op.create_index("idx_evidence_source", "evidence", ["source"])

    op.create_table(
        "indicator_frames",
        sa.Column("indicator_frame_id", sa.String(length=160), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("horizon", sa.String(length=32), nullable=False),
        sa.Column("library", sa.String(length=64), nullable=False),
        sa.Column("library_version", sa.String(length=64), nullable=True),
        sa.Column("input_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("input_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bar_count", sa.Integer(), nullable=False),
        sa.Column("rsi_14", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("macd", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("macd_signal", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("macd_hist", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("atr_14", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("bb_percent_b", sa.Numeric(precision=18, scale=8), nullable=True),
        sa.Column("ma_20", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("ma_60", sa.Numeric(precision=30, scale=10), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("indicator_frame_id"),
        sa.UniqueConstraint(
            "asset_id",
            "timeframe",
            "horizon",
            "library",
            "input_end_at",
            name="uq_indicator_frames_input",
        ),
    )
    op.create_index(
        "idx_indicator_frames_asset_tf_asof", "indicator_frames", ["asset_id", "timeframe", "as_of"]
    )
    op.create_index(
        "idx_indicator_frames_market_horizon", "indicator_frames", ["market", "horizon"]
    )
    op.create_index("idx_indicator_frames_status", "indicator_frames", ["status"])

    op.create_table(
        "factor_frames",
        sa.Column("factor_frame_id", sa.String(length=160), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("horizon", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("total_available_groups", sa.Integer(), nullable=False),
        sa.Column(
            "missing_groups",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "source_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("indicator_frame_id", sa.String(length=160), nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("factor_frame_id"),
    )
    op.create_index(
        "idx_factor_frames_asset_horizon_asof", "factor_frames", ["asset_id", "horizon", "as_of"]
    )
    op.create_index("idx_factor_frames_market_horizon", "factor_frames", ["market", "horizon"])
    op.create_index("idx_factor_frames_status", "factor_frames", ["status"])

    op.create_table(
        "screening_results",
        sa.Column("screening_id", sa.String(length=160), nullable=False),
        sa.Column("universe_id", sa.String(length=128), nullable=False),
        sa.Column("strategy", sa.String(length=128), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("passed_count", sa.Integer(), nullable=False),
        sa.Column("removed_count", sa.Integer(), nullable=False),
        sa.Column(
            "rules",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("screening_id"),
    )
    op.create_index(
        "idx_screening_universe_strategy", "screening_results", ["universe_id", "strategy"]
    )
    op.create_index("idx_screening_market_asof", "screening_results", ["market", "as_of"])

    op.create_table(
        "screening_result_items",
        sa.Column("screening_item_id", sa.String(length=192), nullable=False),
        sa.Column("screening_id", sa.String(length=160), nullable=False),
        sa.Column("universe_id", sa.String(length=128), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("removed_reason", sa.Text(), nullable=True),
        sa.Column(
            "failed_rules",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "passed_rules",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("data_status", sa.String(length=32), nullable=False),
        sa.Column("liquidity_status", sa.String(length=32), nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("screening_item_id"),
        sa.UniqueConstraint("screening_id", "asset_id", name="uq_screening_items_screening_asset"),
    )
    op.create_index(
        "idx_screening_items_screening_passed", "screening_result_items", ["screening_id", "passed"]
    )
    op.create_index("idx_screening_items_asset", "screening_result_items", ["asset_id"])
    op.create_index("idx_screening_items_market", "screening_result_items", ["market"])

    op.create_table(
        "asset_scores",
        sa.Column("score_id", sa.String(length=160), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("universe_id", sa.String(length=128), nullable=False),
        sa.Column("screening_id", sa.String(length=160), nullable=False),
        sa.Column("factor_frame_id", sa.String(length=160), nullable=False),
        sa.Column("horizon", sa.String(length=32), nullable=False),
        sa.Column("total_score", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("technical_score", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("fundamental_score", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("valuation_score", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("flow_score", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("derivatives_score", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("event_score", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column(
            "risk_penalty",
            sa.Numeric(precision=12, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("rank_in_universe", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column(
            "missing_penalty",
            sa.Numeric(precision=12, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("rule_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("score_id"),
    )
    op.create_index("idx_asset_scores_universe_rank", "asset_scores", ["universe_id", "rank"])
    op.create_index(
        "idx_asset_scores_asset_horizon_asof", "asset_scores", ["asset_id", "horizon", "as_of"]
    )
    op.create_index("idx_asset_scores_market_score", "asset_scores", ["market", "total_score"])
    op.create_index("idx_asset_scores_status", "asset_scores", ["status"])

    op.create_table(
        "signal_snapshots",
        sa.Column("signal_id", sa.String(length=160), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("horizon", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=32), nullable=False),
        sa.Column("score", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("rule_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("signal_id"),
    )
    op.create_index(
        "idx_signals_asset_horizon_asof", "signal_snapshots", ["asset_id", "horizon", "as_of"]
    )
    op.create_index("idx_signals_market_direction", "signal_snapshots", ["market", "direction"])
    op.create_index("idx_signals_status", "signal_snapshots", ["status"])

    op.create_table(
        "risk_findings",
        sa.Column("risk_id", sa.String(length=160), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=True),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("risk_type", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("score", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "evidence_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("risk_id"),
    )
    op.create_index("idx_risks_asset_asof", "risk_findings", ["asset_id", "as_of"])
    op.create_index("idx_risks_type_severity", "risk_findings", ["risk_type", "severity"])
    op.create_index("idx_risks_scope", "risk_findings", ["scope"])

    op.create_table(
        "recommendation_runs",
        sa.Column("run_id", sa.String(length=160), nullable=False),
        sa.Column("universe_id", sa.String(length=128), nullable=True),
        sa.Column("screening_id", sa.String(length=160), nullable=True),
        sa.Column("strategy", sa.String(length=128), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("horizon", sa.String(length=32), nullable=False),
        sa.Column("limit", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("idx_recommendation_runs_universe", "recommendation_runs", ["universe_id"])
    op.create_index(
        "idx_recommendation_runs_strategy_market", "recommendation_runs", ["strategy", "market"]
    )
    op.create_index("idx_recommendation_runs_started_at", "recommendation_runs", ["started_at"])

    op.create_table(
        "recommendation_run_universes",
        sa.Column("id", sa.String(length=192), nullable=False),
        sa.Column("run_id", sa.String(length=160), nullable=False),
        sa.Column("universe_id", sa.String(length=128), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("weight", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("asset_count", sa.Integer(), nullable=True),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "universe_id", name="uq_run_universes_run_universe"),
    )
    op.create_index("idx_run_universes_run", "recommendation_run_universes", ["run_id"])
    op.create_index("idx_run_universes_universe", "recommendation_run_universes", ["universe_id"])
    op.create_index("idx_run_universes_market", "recommendation_run_universes", ["market"])

    op.create_table(
        "asset_recommendations",
        sa.Column("recommendation_id", sa.String(length=192), nullable=False),
        sa.Column("run_id", sa.String(length=160), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("horizon", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("total_score", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("conviction", sa.String(length=32), nullable=False),
        sa.Column("score_id", sa.String(length=160), nullable=True),
        sa.Column("factor_frame_id", sa.String(length=160), nullable=True),
        sa.Column(
            "signal_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "risk_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "agent_analysis_item_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "evidence_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "watch_conditions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "invalid_if",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("recommendation_id"),
    )
    op.create_index("idx_recommendations_run_rank", "asset_recommendations", ["run_id", "rank"])
    op.create_index(
        "idx_recommendations_asset_created", "asset_recommendations", ["asset_id", "created_at"]
    )
    op.create_index(
        "idx_recommendations_market_action", "asset_recommendations", ["market", "action"]
    )
    op.create_index("idx_recommendations_score", "asset_recommendations", ["total_score"])

    op.create_table(
        "agent_analysis_runs",
        sa.Column("agent_run_id", sa.String(length=160), nullable=False),
        sa.Column("run_id", sa.String(length=160), nullable=False),
        sa.Column("agent_name", sa.String(length=64), nullable=False),
        sa.Column("agent_role", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("model_version", sa.String(length=128), nullable=True),
        sa.Column("input_ref", sa.String(length=255), nullable=True),
        sa.Column("input_summary", sa.Text(), nullable=True),
        sa.Column("output_summary", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("agent_run_id"),
    )
    op.create_index("idx_agent_runs_recommendation", "agent_analysis_runs", ["run_id"])
    op.create_index("idx_agent_runs_agent_status", "agent_analysis_runs", ["agent_name", "status"])
    op.create_index("idx_agent_runs_started_at", "agent_analysis_runs", ["started_at"])

    op.create_table(
        "agent_analysis_items",
        sa.Column("agent_analysis_item_id", sa.String(length=192), nullable=False),
        sa.Column("agent_run_id", sa.String(length=160), nullable=False),
        sa.Column("run_id", sa.String(length=160), nullable=False),
        sa.Column("asset_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("market", sa.String(length=32), nullable=False),
        sa.Column("agent_name", sa.String(length=64), nullable=False),
        sa.Column("stance", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column(
            "key_points",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "risk_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "evidence_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        _jsonb_object(),
        sa.PrimaryKeyConstraint("agent_analysis_item_id"),
        sa.UniqueConstraint(
            "agent_run_id", "asset_id", "agent_name", name="uq_agent_items_run_asset_agent"
        ),
    )
    op.create_index("idx_agent_items_run_asset", "agent_analysis_items", ["run_id", "asset_id"])
    op.create_index("idx_agent_items_asset_asof", "agent_analysis_items", ["asset_id", "as_of"])
    op.create_index(
        "idx_agent_items_agent_stance", "agent_analysis_items", ["agent_name", "stance"]
    )


def downgrade() -> None:
    for table_name in [
        "agent_analysis_items",
        "agent_analysis_runs",
        "asset_recommendations",
        "recommendation_run_universes",
        "recommendation_runs",
        "risk_findings",
        "signal_snapshots",
        "asset_scores",
        "screening_result_items",
        "screening_results",
        "factor_frames",
        "indicator_frames",
        "evidence",
        "market_calendars",
        "market_bars",
        "raw_records",
        "asset_universe_members",
        "asset_universes",
        "assets",
    ]:
        op.drop_table(table_name)
