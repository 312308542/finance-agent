"""审计和治理 `market_bars` 行情 K 线表。

默认模式只读取并输出脏数据报告；带 `--apply-safe-fixes` 时只执行低风险修复：
- 把 `status='partial' AND is_closed=true` 的闭合历史 K 线修正为 `available`。
- 不删除重复 source 数据，不自动处理非正复权价格，避免误删供应商特有历史。
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from finance_agent.storage.db import create_session_factory, session_scope


JsonDict = dict[str, Any]


def _json_default(value: Any) -> Any:
    """把数据库返回值转换成可 JSON 序列化的结构。"""

    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def _mappings(session: Session, sql: str, params: JsonDict | None = None) -> list[JsonDict]:
    """执行只读 SQL 并返回字典行。"""

    return [dict(row) for row in session.execute(text(sql), params or {}).mappings()]


def _scalar(session: Session, sql: str, params: JsonDict | None = None) -> Any:
    """执行只读 SQL 并返回第一列。"""

    return session.execute(text(sql), params or {}).scalar_one()


def _safe_probe(session: Session, sql: str, params: JsonDict | None = None) -> JsonDict:
    """探测可选 TimescaleDB 视图，兼容本地测试库或未安装扩展的环境。"""

    try:
        with session.begin_nested():
            rows = _mappings(session, sql, params)
        return {"status": "ok", "rows": rows}
    except SQLAlchemyError as exc:
        return {"status": "unavailable", "error": str(exc)}


def audit_market_bars(session: Session, *, sample_limit: int = 10) -> JsonDict:
    """汇总 `market_bars` 的规模、重复来源和脏数据分布。"""

    duplicate_group_count = _scalar(
        session,
        """
        SELECT count(*)
        FROM (
            SELECT asset_id, timeframe, timestamp, adjustment
            FROM market_bars
            GROUP BY asset_id, timeframe, timestamp, adjustment
            HAVING count(*) > 1
        ) AS duplicated
        """,
    )
    partial_closed_count = _scalar(
        session,
        """
        SELECT count(*)
        FROM market_bars
        WHERE status = 'partial'
          AND is_closed IS true
        """,
    )
    structural_bad_count = _scalar(
        session,
        """
        SELECT count(*)
        FROM market_bars
        WHERE high < low
           OR high < open
           OR high < close
           OR low > open
           OR low > close
           OR volume < 0
           OR amount < 0
        """,
    )
    non_positive_ohlc_count = _scalar(
        session,
        """
        SELECT count(*)
        FROM market_bars
        WHERE open <= 0
           OR high <= 0
           OR low <= 0
           OR close <= 0
        """,
    )
    usable_non_positive_ohlc_count = _scalar(
        session,
        """
        SELECT count(*)
        FROM market_bars
        WHERE status IN ('available', 'revised')
          AND (
              open <= 0
           OR high <= 0
           OR low <= 0
           OR close <= 0
          )
        """,
    )

    return {
        "total_rows": _scalar(session, "SELECT count(*) FROM market_bars"),
        "by_market_timeframe": _mappings(
            session,
            """
            SELECT market, timeframe, count(*) AS row_count, min(timestamp) AS min_at, max(timestamp) AS max_at
            FROM market_bars
            GROUP BY market, timeframe
            ORDER BY row_count DESC
            """,
        ),
        "by_source_adjustment": _mappings(
            session,
            """
            SELECT source, adjustment, count(*) AS row_count
            FROM market_bars
            GROUP BY source, adjustment
            ORDER BY row_count DESC
            """,
        ),
        "duplicates": {
            "canonical_group_count": duplicate_group_count,
            "source_sets": _mappings(
                session,
                """
                WITH grouped AS (
                    SELECT
                        asset_id,
                        timeframe,
                        timestamp,
                        adjustment,
                        string_agg(source, ',' ORDER BY source) AS source_set
                    FROM market_bars
                    GROUP BY asset_id, timeframe, timestamp, adjustment
                    HAVING count(*) > 1
                )
                SELECT source_set, count(*) AS group_count
                FROM grouped
                GROUP BY source_set
                ORDER BY group_count DESC
                LIMIT :limit
                """,
                {"limit": sample_limit},
            ),
            "samples": _mappings(
                session,
                """
                SELECT asset_id, symbol, timeframe, timestamp, adjustment, count(*) AS source_count,
                       string_agg(source, ',' ORDER BY source) AS sources
                FROM market_bars
                GROUP BY asset_id, symbol, timeframe, timestamp, adjustment
                HAVING count(*) > 1
                ORDER BY timestamp DESC, asset_id
                LIMIT :limit
                """,
                {"limit": sample_limit},
            ),
        },
        "dirty_data": {
            "partial_closed_count": partial_closed_count,
            "partial_closed_by_source": _mappings(
                session,
                """
                SELECT source, adjustment, count(*) AS row_count, min(timestamp) AS min_at, max(timestamp) AS max_at
                FROM market_bars
                WHERE status = 'partial'
                  AND is_closed IS true
                GROUP BY source, adjustment
                ORDER BY row_count DESC
                LIMIT :limit
                """,
                {"limit": sample_limit},
            ),
            "structural_bad_ohlcv_count": structural_bad_count,
            "structural_bad_samples": _mappings(
                session,
                """
                SELECT asset_id, symbol, timestamp, source, adjustment, open, high, low, close, volume, amount
                FROM market_bars
                WHERE high < low
                   OR high < open
                   OR high < close
                   OR low > open
                   OR low > close
                   OR volume < 0
                   OR amount < 0
                ORDER BY timestamp DESC, asset_id
                LIMIT :limit
                """,
                {"limit": sample_limit},
            ),
            "non_positive_ohlc_count": non_positive_ohlc_count,
            "usable_non_positive_ohlc_count": usable_non_positive_ohlc_count,
            "non_positive_ohlc_samples": _mappings(
                session,
                """
                SELECT asset_id, symbol, timestamp, source, adjustment, open, high, low, close
                FROM market_bars
                WHERE open <= 0
                   OR high <= 0
                   OR low <= 0
                   OR close <= 0
                ORDER BY timestamp DESC, asset_id
                LIMIT :limit
                """,
                {"limit": sample_limit},
            ),
            "invalid_ashare_symbol_count": _scalar(
                session,
                """
                SELECT count(*)
                FROM market_bars
                WHERE market = 'ashare'
                  AND symbol !~ '^[0-9]{6}$'
                """,
            ),
        },
        "timescale": {
            "hypertables": _safe_probe(
                session,
                """
                SELECT hypertable_name, num_chunks, compression_enabled
                FROM timescaledb_information.hypertables
                WHERE hypertable_name = 'market_bars'
                """,
            ),
            "jobs": _safe_probe(
                session,
                """
                SELECT job_id, proc_name, hypertable_name, schedule_interval, config
                FROM timescaledb_information.jobs
                WHERE hypertable_name = 'market_bars'
                ORDER BY job_id
                """,
            ),
        },
    }


def apply_safe_fixes(session: Session) -> JsonDict:
    """执行低风险修复，不做删除动作。"""

    partial_result = session.execute(
        text(
            """
            UPDATE market_bars
            SET status = 'available'
            WHERE market = 'ashare'
              AND timeframe = '1d'
              AND status = 'partial'
              AND is_closed IS true
            """
        )
    )
    non_positive_result = session.execute(
        text(
            """
            UPDATE market_bars
            SET status = 'error'
            WHERE status <> 'error'
              AND (
                  open <= 0
               OR high <= 0
               OR low <= 0
               OR close <= 0
               OR high < low
               OR high < open
               OR high < close
               OR low > open
               OR low > close
               OR volume < 0
               OR amount < 0
              )
            """
        )
    )
    return {
        "partial_closed_rows_set_available": partial_result.rowcount,
        "unusable_ohlcv_rows_set_error": non_positive_result.rowcount,
        "duplicate_rows_deleted": 0,
        "note": "重复来源不在 safe-fixes 中删除；非正或结构异常 OHLCV 仅标记 error。",
    }


def apply_compression_policy(session: Session) -> JsonDict:
    """为旧分区启用 TimescaleDB 自动压缩策略；未安装 TimescaleDB 时安全跳过。"""

    try:
        with session.begin_nested():
            session.execute(
                text(
                    """
                    DO $$
                    BEGIN
                        IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                            PERFORM add_compression_policy(
                                'market_bars',
                                INTERVAL '90 days',
                                if_not_exists => TRUE
                            );
                        END IF;
                    EXCEPTION
                        WHEN undefined_function OR duplicate_object THEN
                            NULL;
                    END $$;
                    """
                )
            )
    except SQLAlchemyError as exc:
        return {"status": "skipped", "error": str(exc)}
    return {"status": "ok", "older_than": "90 days"}


def _allow_large_timescale_maintenance(session: Session) -> None:
    """当前事务内放开 Timescale 压缩块 DML 解压行数限制。"""

    session.execute(
        text("SET LOCAL timescaledb.max_tuples_decompressed_per_dml_transaction = 0")
    )


def canonicalize_ashare_kline_source(session: Session) -> JsonDict:
    """把 A 股日 K 标准表迁移到稳定 source，并按同键保留一条事实记录。"""

    _allow_large_timescale_maintenance(session)
    delete_result = session.execute(
        text(
            """
            WITH ranked AS (
                SELECT
                    asset_id,
                    timeframe,
                    timestamp,
                    source,
                    adjustment,
                    row_number() OVER (
                        PARTITION BY asset_id, timeframe, timestamp, adjustment
                        ORDER BY
                            CASE status
                                WHEN 'available' THEN 0
                                WHEN 'revised' THEN 1
                                WHEN 'partial' THEN 2
                                WHEN 'error' THEN 4
                                ELSE 3
                            END,
                            CASE source
                                WHEN 'canonical:ashare:kline' THEN 0
                                WHEN 'tencent:direct:kline' THEN 1
                                WHEN 'eastmoney:direct:kline' THEN 2
                                WHEN 'eastmoney:curl_cffi:kline' THEN 3
                                WHEN 'akshare:stock_zh_a_hist_tx' THEN 4
                                WHEN 'akshare:stock_zh_a_hist' THEN 5
                                ELSE 6
                            END,
                            raw_record_id DESC NULLS LAST
                    ) AS rn
                FROM market_bars
                WHERE market = 'ashare'
                  AND timeframe = '1d'
            ),
            deleted AS (
                DELETE FROM market_bars AS bar
                USING ranked
                WHERE bar.asset_id = ranked.asset_id
                  AND bar.timeframe = ranked.timeframe
                  AND bar.timestamp = ranked.timestamp
                  AND bar.source = ranked.source
                  AND bar.adjustment = ranked.adjustment
                  AND ranked.rn > 1
                RETURNING 1
            )
            SELECT count(*) AS duplicate_rows_deleted
            FROM deleted
            """
        )
    ).mappings().one()
    update_result = session.execute(
        text(
            """
            UPDATE market_bars
            SET source = 'canonical:ashare:kline'
            WHERE market = 'ashare'
              AND timeframe = '1d'
              AND source <> 'canonical:ashare:kline'
            """
        )
    )
    return {
        "duplicate_rows_deleted": delete_result["duplicate_rows_deleted"],
        "rows_set_canonical": update_result.rowcount,
    }


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="审计和治理 market_bars 行情 K 线表")
    parser.add_argument("--sample-limit", type=int, default=10, help="报告样例行数")
    parser.add_argument(
        "--apply-safe-fixes",
        action="store_true",
        help="执行低风险修复：修正闭合 K 线的 partial 状态，不删除重复数据",
    )
    parser.add_argument(
        "--apply-compression-policy",
        action="store_true",
        help="为 TimescaleDB market_bars 增加 90 天自动压缩策略",
    )
    parser.add_argument(
        "--apply-canonical-ashare-source",
        action="store_true",
        help="把 A 股日 K 迁移到 canonical source，并删除同键重复 source 行",
    )
    return parser.parse_args()


def main() -> None:
    """执行审计，可选执行安全治理动作。"""

    args = parse_args()
    session_factory = create_session_factory()
    with session_scope(session_factory) as session:
        before = audit_market_bars(session, sample_limit=args.sample_limit)
        actions: JsonDict = {}
        if args.apply_safe_fixes:
            actions["safe_fixes"] = apply_safe_fixes(session)
        if args.apply_canonical_ashare_source:
            actions["canonical_ashare_source"] = canonicalize_ashare_kline_source(session)
        if args.apply_compression_policy:
            actions["compression_policy"] = apply_compression_policy(session)
        after = audit_market_bars(session, sample_limit=args.sample_limit) if actions else None

    print(
        json.dumps(
            {
                "before": before,
                "actions": actions,
                "after": after,
            },
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        )
    )


if __name__ == "__main__":
    main()
