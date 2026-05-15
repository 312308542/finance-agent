"""基础数据层健康检查。

该脚本不触发第三方接口请求，只读取 PostgreSQL + TimescaleDB 和 Redis：

- `raw_records` 最近 Provider 调用状态。
- 标准表当前数据量。
- Redis 中的 Provider 熔断状态。

用于判断基础数据层是否具备进入因子计算和推荐链路的条件。
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from finance_agent.cache import create_cache_client
from finance_agent.data.collection_runtime import CollectionRuntime
from finance_agent.storage.db import create_session_factory, session_scope

JsonDict = dict[str, Any]

DEFAULT_PROVIDER_KEYS = [
    "stock_zh_a_spot",
    "stock_zh_a_hist",
    "stock_board_industry_cons_em",
    "stock_board_concept_cons_em",
    "stock_individual_fund_flow_rank",
    "stock_news_em",
    "stock_financial_analysis_indicator_em",
    "stock_value_em",
    "stock_yjbb_em",
    "stock_zh_a_stop_em",
    "stock_hot_rank_em",
    "stock_zt_pool_em",
    "stock_lhb_detail_em",
    "stock_dzjy_mrmx",
    "stock_margin_sse",
    "stock_margin_szse",
    "ccxt_binance_load_markets",
    "ccxt_binance_fetch_ohlcv",
    "binance_derivative_snapshot",
]


def main() -> None:
    """执行基础数据层健康检查。"""

    args = parse_args()
    session_factory = create_session_factory()
    cache, locks, cache_status = create_cache_client(backend=args.cache_backend)
    runtime = CollectionRuntime(cache=cache, locks=locks)

    with session_scope(session_factory) as session:
        provider_rows = load_provider_status(session, limit=args.limit)
        table_counts = load_table_counts(session)
        universe_counts = load_universe_counts(session)

    provider_keys = sorted(set(DEFAULT_PROVIDER_KEYS) | {row["endpoint"] for row in provider_rows})
    summary = {
        "checked_at": datetime.now(tz=UTC).isoformat(),
        "cache": cache_status.__dict__,
        "table_counts": table_counts,
        "universe_counts": universe_counts,
        "provider_summary": summarize_providers(provider_rows),
        "providers": provider_rows,
        "provider_circuits": runtime.list_provider_states(provider_keys),
        "gaps": infer_gaps(table_counts, provider_rows),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="检查基础数据层最近采集和缓存状态")
    parser.add_argument("--limit", type=int, default=80, help="读取最近多少条 raw_records")
    parser.add_argument(
        "--cache-backend",
        choices=["auto", "redis", "null"],
        default="auto",
        help="缓存后端；auto 会在 Redis 不可用时降级为空缓存",
    )
    return parser.parse_args()


def load_provider_status(session: Any, *, limit: int) -> list[JsonDict]:
    """读取最近 Provider 调用状态。"""

    rows = session.execute(
        text(
            """
            select endpoint,
                   provider,
                   status,
                   response_payload->>'actual_source' as actual_source,
                   response_payload->>'source_coverage' as source_coverage,
                   error_message,
                   raw_record_id,
                   collected_at
            from raw_records
            order by collected_at desc
            limit :limit
            """
        ),
        {"limit": limit},
    ).mappings()
    latest: dict[str, JsonDict] = {}
    for row in rows:
        endpoint = str(row["endpoint"])
        if endpoint in latest:
            continue
        latest[endpoint] = {
            "endpoint": endpoint,
            "provider": row["provider"],
            "status": row["status"],
            "actual_source": row["actual_source"],
            "source_coverage": row["source_coverage"],
            "error_message": row["error_message"],
            "raw_record_id": row["raw_record_id"],
            "collected_at": row["collected_at"].isoformat()
            if row["collected_at"] is not None
            else None,
        }
    return list(latest.values())


def load_table_counts(session: Any) -> JsonDict:
    """读取基础数据关键表数据量。"""

    rows = session.execute(
        text(
            """
            select 'raw_records' as table_name, count(1) as count from raw_records
            union all select 'assets', count(1) from assets
            union all select 'asset_universes', count(1) from asset_universes
            union all select 'asset_universe_members', count(1) from asset_universe_members
            union all select 'market_bars', count(1) from market_bars
            union all select 'capital_flow_snapshots', count(1) from capital_flow_snapshots
            union all select 'fundamental_snapshots', count(1) from fundamental_snapshots
            union all select 'event_records', count(1) from event_records
            union all select 'evidence', count(1) from evidence
            union all select 'risk_findings', count(1) from risk_findings
            union all select 'crypto_derivative_snapshots', count(1)
            from crypto_derivative_snapshots
            """
        )
    ).mappings()
    return {row["table_name"]: int(row["count"]) for row in rows}


def load_universe_counts(session: Any) -> list[JsonDict]:
    """读取候选池成员数量。"""

    rows = session.execute(
        text(
            """
            select universe_id, count(1) as member_count
            from asset_universe_members
            group by universe_id
            order by universe_id
            """
        )
    ).mappings()
    return [
        {"universe_id": row["universe_id"], "member_count": int(row["member_count"])}
        for row in rows
    ]


def summarize_providers(provider_rows: list[JsonDict]) -> JsonDict:
    """汇总 Provider 最新状态。"""

    return {
        "total": len(provider_rows),
        "available": sum(1 for row in provider_rows if row["status"] == "available"),
        "error": sum(1 for row in provider_rows if row["status"] == "error"),
        "unavailable": sum(1 for row in provider_rows if row["status"] == "unavailable"),
        "partial": sum(1 for row in provider_rows if row["status"] == "partial"),
    }


def infer_gaps(table_counts: JsonDict, provider_rows: list[JsonDict]) -> list[str]:
    """根据表计数和最近 Provider 状态推断基础数据缺口。"""

    gaps: list[str] = []
    required_tables = [
        "assets",
        "asset_universe_members",
        "market_bars",
        "capital_flow_snapshots",
        "fundamental_snapshots",
        "event_records",
        "evidence",
        "risk_findings",
        "crypto_derivative_snapshots",
    ]
    for table_name in required_tables:
        if int(table_counts.get(table_name) or 0) <= 0:
            gaps.append(f"{table_name} 暂无数据")
    latest_by_endpoint = {row["endpoint"]: row for row in provider_rows}
    stop_status = latest_by_endpoint.get("stock_zh_a_stop_em", {}).get("status")
    if stop_status == "error":
        gaps.append("A 股停复牌/退市接口最近失败，需要替代源或继续观察 fallback")
    hot_rank = latest_by_endpoint.get("stock_hot_rank_em", {})
    if hot_rank.get("source_coverage") == "rank_only":
        gaps.append("A 股人气榜当前只有排名种子，实时价格需要从行情层补齐")
    return gaps


if __name__ == "__main__":
    main()
