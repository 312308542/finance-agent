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

from finance_agent.application.data_production_service import DataBackfillPlanner
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
    "tool_trade_date_hist_sina",
    "crypto_calendar_24x7",
    "ccxt_binance_load_markets",
    "ccxt_binance_fetch_ohlcv",
    "binance_derivative_snapshot",
]

FRESHNESS_THRESHOLDS_HOURS = {
    "market_bars": 12,
    "indicator_frames": 24,
    "factor_frames": 24,
    "asset_scores": 24,
    "signal_snapshots": 24,
    "market_calendars": 24,
    "capital_flow_snapshots": 24,
    "fundamental_snapshots": 72,
    "event_records": 48,
    "risk_findings": 48,
    "crypto_derivative_snapshots": 12,
    "screening_results": 24,
}


def main() -> None:
    """执行基础数据层健康检查。"""

    args = parse_args()
    checked_at = datetime.now(tz=UTC)
    session_factory = create_session_factory()
    cache, locks, cache_status = create_cache_client(backend=args.cache_backend)
    runtime = CollectionRuntime(cache=cache, locks=locks)

    with session_scope(session_factory) as session:
        provider_rows = load_provider_status(session, limit=args.limit)
        table_counts = load_table_counts(session)
        freshness_rows = load_table_freshness(session)
        universe_counts = load_universe_counts(session)

    provider_keys = sorted(set(DEFAULT_PROVIDER_KEYS) | {row["endpoint"] for row in provider_rows})
    gaps = infer_gaps(table_counts, provider_rows, freshness_rows)
    recommendation_readiness = build_recommendation_readiness(
        checked_at=checked_at,
        table_counts=table_counts,
        freshness_rows=freshness_rows,
        universe_counts=universe_counts,
        gaps=gaps,
    )
    gaps = infer_gaps(
        table_counts,
        provider_rows,
        freshness_rows,
        recommendation_readiness=recommendation_readiness if args.readiness else None,
    )
    summary = {
        "checked_at": checked_at.isoformat(),
        "cache": cache_status.__dict__,
        "table_counts": table_counts,
        "freshness": freshness_rows,
        "universe_counts": universe_counts,
        "provider_summary": summarize_providers(provider_rows),
        "providers": provider_rows,
        "provider_circuits": runtime.list_provider_states(provider_keys),
        "gaps": gaps,
        "refresh_hints": build_refresh_hints(table_counts, freshness_rows, provider_rows),
    }
    if args.readiness:
        summary["recommendation_readiness"] = recommendation_readiness
    summary["backfill_jobs"] = [
        job.to_scheduler_job()
        for job in DataBackfillPlanner().build_backfill_jobs(
            health_summary=summary,
            now=checked_at,
        )
    ]
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
    parser.add_argument(
        "--readiness",
        action="store_true",
        help="输出推荐链路就绪度报告，用于判断是否允许真实推荐运行",
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
            union all select 'market_calendars', count(1) from market_calendars
            union all select 'market_bars', count(1) from market_bars
            union all select 'indicator_frames', count(1) from indicator_frames
            union all select 'factor_frames', count(1) from factor_frames
            union all select 'asset_scores', count(1) from asset_scores
            union all select 'signal_snapshots', count(1) from signal_snapshots
            union all select 'screening_results', count(1) from screening_results
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


def load_table_freshness(session: Any) -> list[JsonDict]:
    """读取关键表最新更新时间，用于判断数据是否过期。"""

    rows = session.execute(
        text(
            """
            select table_name, max(as_of) as latest_as_of
            from (
                select 'market_bars' as table_name, timestamp as as_of from market_bars
                union all select 'indicator_frames', as_of from indicator_frames
                union all select 'factor_frames', as_of from factor_frames
                union all select 'asset_scores', as_of from asset_scores
                union all select 'signal_snapshots', as_of from signal_snapshots
                union all
                select 'market_calendars', trade_date::timestamptz from market_calendars
                union all select 'capital_flow_snapshots', as_of from capital_flow_snapshots
                union all select 'fundamental_snapshots', as_of from fundamental_snapshots
                union all
                select 'event_records', coalesce(published_at, collected_at) from event_records
                union all select 'risk_findings', as_of from risk_findings
                union all
                select 'crypto_derivative_snapshots', as_of from crypto_derivative_snapshots
                union all select 'screening_results', as_of from screening_results
            ) as freshness
            group by table_name
            order by table_name
            """
        )
    ).mappings()
    freshness: list[JsonDict] = []
    now = datetime.now(tz=UTC)
    for row in rows:
        latest_as_of = row["latest_as_of"]
        age_hours = None
        if latest_as_of is not None:
            age_hours = (now - latest_as_of.astimezone(UTC)).total_seconds() / 3600
        freshness.append(
            {
                "table_name": row["table_name"],
                "latest_as_of": latest_as_of.isoformat() if latest_as_of is not None else None,
                "age_hours": round(age_hours, 2) if age_hours is not None else None,
                "threshold_hours": FRESHNESS_THRESHOLDS_HOURS.get(row["table_name"]),
            }
        )
    return freshness


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


def infer_gaps(
    table_counts: JsonDict,
    provider_rows: list[JsonDict],
    freshness_rows: list[JsonDict] | None = None,
    recommendation_readiness: JsonDict | None = None,
) -> list[str]:
    """根据表计数和最近 Provider 状态推断基础数据缺口。"""

    gaps: list[str] = []
    required_tables = [
        "assets",
        "asset_universe_members",
        "market_calendars",
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
    freshness_rows = freshness_rows or []
    for row in freshness_rows:
        table_name = str(row.get("table_name") or "")
        age_hours = row.get("age_hours")
        threshold_hours = row.get("threshold_hours")
        if (
            isinstance(age_hours, (int, float))
            and isinstance(threshold_hours, int)
            and age_hours > threshold_hours
        ):
            gaps.append(f"{table_name} 最近数据已过期，建议补采")
    if recommendation_readiness and recommendation_readiness.get("status") != "ready":
        reasons = recommendation_readiness.get("reasons") or []
        if reasons:
            gaps.append(f"推荐就绪度未通过：{', '.join(str(reason) for reason in reasons)}")
    return gaps


def build_recommendation_readiness(
    *,
    checked_at: datetime,
    table_counts: JsonDict,
    freshness_rows: list[JsonDict],
    universe_counts: list[JsonDict],
    gaps: list[str],
) -> JsonDict:
    """构建推荐运行前的数据就绪度报告。

    该报告只基于已入库数据的计数和 freshness，不触发外部采集。它用于把
    “数据是否足够新、足够完整”变成推荐链路可消费的结构化闸门。
    """

    freshness_by_table = {str(row.get("table_name")): row for row in freshness_rows}
    mainboard_members = max(
        (
            int(row.get("member_count") or 0)
            for row in universe_counts
            if "mainboard" in str(row.get("universe_id") or "")
            or "ashare" in str(row.get("universe_id") or "")
        ),
        default=0,
    )
    dimensions = {
        "market_bars": readiness_dimension(
            table_counts=table_counts,
            freshness_by_table=freshness_by_table,
            table_name="market_bars",
            min_count=50,
            required=True,
        ),
        "asset_scores": readiness_dimension(
            table_counts=table_counts,
            freshness_by_table=freshness_by_table,
            table_name="asset_scores",
            min_count=50,
            required=True,
        ),
        "factor_frames": readiness_dimension(
            table_counts=table_counts,
            freshness_by_table=freshness_by_table,
            table_name="factor_frames",
            min_count=50,
            required=True,
        ),
        "capital_flow_snapshots": readiness_dimension(
            table_counts=table_counts,
            freshness_by_table=freshness_by_table,
            table_name="capital_flow_snapshots",
            min_count=1,
            required=False,
        ),
        "fundamental_snapshots": readiness_dimension(
            table_counts=table_counts,
            freshness_by_table=freshness_by_table,
            table_name="fundamental_snapshots",
            min_count=1,
            required=False,
        ),
        "event_records": readiness_dimension(
            table_counts=table_counts,
            freshness_by_table=freshness_by_table,
            table_name="event_records",
            min_count=1,
            required=False,
        ),
        "screening_results": readiness_dimension(
            table_counts=table_counts,
            freshness_by_table=freshness_by_table,
            table_name="screening_results",
            min_count=1,
            required=False,
        ),
    }
    reasons: list[str] = []
    warnings: list[str] = []
    for name, dimension in dimensions.items():
        issue_key = dimension.get("issue_key")
        if not issue_key:
            continue
        if dimension["required"]:
            reasons.append(str(issue_key))
        else:
            warnings.append(str(issue_key))
    if mainboard_members <= 0:
        warnings.append("mainboard_universe_empty")
    status = "ready" if not reasons else "blocked"
    return {
        "schema_version": "recommendation_readiness_v1",
        "checked_at": checked_at.isoformat(),
        "status": status,
        "executable": status == "ready",
        "reasons": reasons,
        "warnings": warnings,
        "dimensions": dimensions,
        "coverage": {
            "mainboard_universe_members": mainboard_members,
            "known_gaps": list(gaps),
        },
    }


def readiness_dimension(
    *,
    table_counts: JsonDict,
    freshness_by_table: dict[str, JsonDict],
    table_name: str,
    min_count: int,
    required: bool,
) -> JsonDict:
    """构建单个数据维度的就绪度。"""

    count = int(table_counts.get(table_name) or 0)
    freshness = freshness_by_table.get(table_name) or {}
    age_hours = freshness.get("age_hours")
    threshold_hours = freshness.get("threshold_hours")
    issue_key = None
    status = "ready"
    if count < min_count:
        status = "missing"
        issue_key = f"{table_name}_empty"
    elif required and not freshness:
        status = "unknown"
        issue_key = f"{table_name}_freshness_unknown"
    elif (
        isinstance(age_hours, (int, float))
        and isinstance(threshold_hours, int)
        and age_hours > threshold_hours
    ):
        status = "stale"
        issue_key = f"{table_name}_stale"
    return {
        "status": status,
        "required": required,
        "count": count,
        "min_count": min_count,
        "latest_as_of": freshness.get("latest_as_of"),
        "age_hours": age_hours,
        "threshold_hours": threshold_hours,
        "issue_key": issue_key,
    }


def build_refresh_hints(
    table_counts: JsonDict,
    freshness_rows: list[JsonDict],
    provider_rows: list[JsonDict],
) -> list[JsonDict]:
    """给调度器和健康面板生成补采建议。"""

    latest_by_endpoint = {row["endpoint"]: row for row in provider_rows}
    hints: list[JsonDict] = []
    for row in freshness_rows:
        table_name = str(row.get("table_name") or "")
        age_hours = row.get("age_hours")
        threshold_hours = row.get("threshold_hours")
        if not isinstance(age_hours, (int, float)) or not isinstance(threshold_hours, int):
            continue
        if age_hours <= threshold_hours:
            continue
        hints.append(
            {
                "table_name": table_name,
                "age_hours": age_hours,
                "threshold_hours": threshold_hours,
                "action": "refresh",
                "reason": f"{table_name} 超过 freshness 阈值，需要补采",
            }
        )
    if int(table_counts.get("capital_flow_snapshots") or 0) <= 0:
        hints.append(
            {
                "table_name": "capital_flow_snapshots",
                "action": "refresh",
                "reason": "资金流表为空，建议优先补采 A 股资金流分组",
            }
        )
    if latest_by_endpoint.get("stock_zh_a_stop_em", {}).get("status") == "error":
        hints.append(
            {
                "table_name": "event_records",
                "action": "fallback",
                "reason": "A 股停复牌源失败，建议补替代源或降级为健康检查提示",
            }
        )
    return hints


if __name__ == "__main__":
    main()
