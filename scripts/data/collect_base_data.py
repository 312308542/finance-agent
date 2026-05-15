"""基础数据层统一采集命令。

该命令用于按分组刷新推荐系统基础数据。它只调用 Provider 和 Collector，
不做因子计算、评分、Agent 分析或交易执行。
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from typing import Any

from finance_agent.cache import create_cache_client
from finance_agent.data.collection_runtime import (
    CollectionRuntime,
    CollectionTaskResult,
    ProviderCircuitPolicy,
)
from finance_agent.data.collectors import (
    AshareP0Collector,
    AshareP1Collector,
    AshareP2Collector,
    AshareRiskSentimentCollector,
    CryptoDataCollector,
)
from finance_agent.storage.db import create_session_factory, session_scope

JsonDict = dict[str, Any]


def main() -> None:
    """解析命令行参数并执行基础数据采集。"""

    args = parse_args()
    session_factory = create_session_factory()
    started_at = datetime.now(tz=UTC)
    cache, locks, cache_status = create_cache_client(backend=args.cache_backend)
    runtime = CollectionRuntime(
        cache=cache,
        locks=locks,
        lock_ttl_seconds=args.lock_ttl_seconds,
        circuit_policy=ProviderCircuitPolicy(
            failure_threshold=args.circuit_failure_threshold,
            cooldown_seconds=args.circuit_cooldown_seconds,
        ),
    )

    with session_scope(session_factory) as session:
        results: list[CollectionTaskResult] = []
        selected_groups = set(args.group)
        if "all" in selected_groups:
            selected_groups = {"ashare-p0", "ashare-p1", "ashare-p2", "ashare-risk", "crypto"}

        if "ashare-p0" in selected_groups:
            results.extend(run_ashare_p0(session, args, runtime))
        if "ashare-p1" in selected_groups:
            results.extend(run_ashare_p1(session, args, runtime))
        if "ashare-p2" in selected_groups:
            results.extend(run_ashare_p2(session, args, runtime))
        if "ashare-risk" in selected_groups:
            results.extend(run_ashare_risk(session, args, runtime))
        if "crypto" in selected_groups:
            results.extend(run_crypto(session, args, runtime))

    summary = {
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(tz=UTC).isoformat(),
        "groups": args.group,
        "cache": cache_status.__dict__,
        "total_tasks": len(results),
        "available": sum(1 for item in results if item.status == "available"),
        "error": sum(1 for item in results if item.status == "error"),
        "unavailable": sum(1 for item in results if item.status == "unavailable"),
        "skipped": sum(1 for item in results if item.status == "skipped"),
        "locked": sum(1 for item in results if item.status == "locked"),
        "results": [item.__dict__ for item in results],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


def parse_args() -> argparse.Namespace:
    """解析采集命令参数。"""

    parser = argparse.ArgumentParser(description="按分组采集基础数据并写入标准表")
    parser.add_argument(
        "--group",
        action="append",
        choices=["all", "ashare-p0", "ashare-p1", "ashare-p2", "ashare-risk", "crypto"],
        default=None,
        help="采集分组；可重复传入。默认只跑 ashare-p1",
    )
    parser.add_argument("--limit", type=int, default=5, help="每类列表型任务采集条数")
    parser.add_argument("--ashare-symbol", default="000001", help="A 股样例代码")
    parser.add_argument("--ashare-name", default="平安银行", help="A 股样例名称")
    parser.add_argument("--ashare-start", default="20260501", help="A 股 K 线开始日期")
    parser.add_argument("--ashare-end", default="20260514", help="A 股 K 线结束日期")
    parser.add_argument("--ashare-timeframe", default="1d", help="A 股 K 线周期")
    parser.add_argument("--ashare-adjust", default="qfq", help="A 股复权类型")
    parser.add_argument("--industry", default="银行", help="A 股行业种子名称")
    parser.add_argument("--concept", default="融资融券", help="A 股概念种子名称")
    parser.add_argument("--flow-window", default="5日", help="A 股资金流周期")
    parser.add_argument("--report-date", default="20250331", help="业绩报表日期")
    parser.add_argument("--risk-start", default="20260501", help="A 股风险数据开始日期")
    parser.add_argument("--risk-end", default="20260514", help="A 股风险数据结束日期")
    parser.add_argument("--risk-block-symbol", default="A股", help="大宗交易市场范围")
    parser.add_argument(
        "--cache-backend",
        choices=["auto", "redis", "null"],
        default="auto",
        help="缓存和任务锁后端；生产调度建议使用 redis",
    )
    parser.add_argument("--lock-ttl-seconds", type=int, default=600, help="采集任务锁 TTL")
    parser.add_argument(
        "--circuit-failure-threshold",
        type=int,
        default=3,
        help="Provider 连续失败多少次后熔断",
    )
    parser.add_argument(
        "--circuit-cooldown-seconds",
        type=int,
        default=900,
        help="Provider 熔断冷却时间",
    )
    parser.add_argument(
        "--force-provider",
        action="store_true",
        help="忽略 Provider 熔断状态，强制执行采集",
    )
    parser.add_argument("--crypto-symbol", default="BTCUSDT", help="数字货币交易对")
    parser.add_argument("--crypto-timeframe", default="1h", help="数字货币 K 线周期")
    parser.add_argument(
        "--crypto-market-type",
        default="spot",
        choices=["spot", "future", "swap"],
        help="ccxt Binance 市场类型",
    )
    args = parser.parse_args()
    if args.group is None:
        args.group = ["ashare-p1"]
    return args


def run_ashare_p0(
    session: Any,
    args: argparse.Namespace,
    runtime: CollectionRuntime,
) -> list[CollectionTaskResult]:
    """执行 A 股 P0 资产和行情采集。"""

    collector = AshareP0Collector(session)
    return [
        runtime.run_task(
            task="ashare_p0_assets",
            provider_key="stock_zh_a_spot",
            parameters={"limit": args.limit},
            force=args.force_provider,
            collect=lambda: collector.collect_assets(
                universe_id="universe:base:ashare:p0:all_a_sample",
                universe_name="基础数据采集 A 股样例池",
                strategy_context="base_data_collect",
                limit=args.limit,
            ),
        ),
        runtime.run_task(
            task="ashare_p0_ohlcv",
            provider_key="stock_zh_a_hist",
            parameters={
                "symbol": args.ashare_symbol,
                "timeframe": args.ashare_timeframe,
                "start": args.ashare_start,
                "end": args.ashare_end,
                "adjust": args.ashare_adjust,
                "limit": args.limit,
            },
            force=args.force_provider,
            collect=lambda: collector.collect_ohlcv(
                symbol=args.ashare_symbol,
                timeframe=args.ashare_timeframe,
                start=args.ashare_start,
                end=args.ashare_end,
                limit=args.limit,
                adjust=args.ashare_adjust,
            ),
        ),
    ]


def run_ashare_p1(
    session: Any,
    args: argparse.Namespace,
    runtime: CollectionRuntime,
) -> list[CollectionTaskResult]:
    """执行 A 股 P1 行业、概念、资金流和新闻采集。"""

    collector = AshareP1Collector(session)
    return [
        runtime.run_task(
            task="ashare_p1_industry_members",
            provider_key="stock_board_industry_cons_em",
            parameters={"industry": args.industry, "limit": args.limit},
            force=args.force_provider,
            collect=lambda: collector.collect_industry_members(
                industry_name=args.industry,
                universe_id=f"universe:base:ashare:p1:industry:{args.industry}",
                universe_name=f"基础数据采集行业种子池-{args.industry}",
                strategy_context="base_data_collect",
                limit=args.limit,
            ),
        ),
        runtime.run_task(
            task="ashare_p1_concept_members",
            provider_key="stock_board_concept_cons_em",
            parameters={"concept": args.concept, "limit": args.limit},
            force=args.force_provider,
            collect=lambda: collector.collect_concept_members(
                concept_name=args.concept,
                universe_id=f"universe:base:ashare:p1:concept:{args.concept}",
                universe_name=f"基础数据采集概念种子池-{args.concept}",
                strategy_context="base_data_collect",
                limit=args.limit,
            ),
        ),
        runtime.run_task(
            task="ashare_p1_flow_rank",
            provider_key="stock_individual_fund_flow_rank",
            parameters={"indicator": args.flow_window, "limit": args.limit},
            force=args.force_provider,
            collect=lambda: collector.collect_flow_rank(
                indicator=args.flow_window,
                limit=args.limit,
            ),
        ),
        runtime.run_task(
            task="ashare_p1_stock_news",
            provider_key="stock_news_em",
            parameters={"symbol": args.ashare_symbol, "limit": min(args.limit, 3)},
            force=args.force_provider,
            collect=lambda: collector.collect_stock_news(
                symbol=args.ashare_symbol,
                asset_name=args.ashare_name,
                limit=min(args.limit, 3),
            ),
        ),
    ]


def run_ashare_p2(
    session: Any,
    args: argparse.Namespace,
    runtime: CollectionRuntime,
) -> list[CollectionTaskResult]:
    """执行 A 股 P2 财务、估值和业绩采集。"""

    collector = AshareP2Collector(session)
    return [
        runtime.run_task(
            task="ashare_p2_financial_indicators",
            provider_key="stock_financial_analysis_indicator_em",
            parameters={"symbol": args.ashare_symbol, "limit": args.limit},
            force=args.force_provider,
            collect=lambda: collector.collect_financial_indicators(
                symbol=args.ashare_symbol,
                asset_name=args.ashare_name,
                limit=args.limit,
            ),
        ),
        runtime.run_task(
            task="ashare_p2_valuation",
            provider_key="stock_value_em",
            parameters={"symbol": args.ashare_symbol, "limit": args.limit},
            force=args.force_provider,
            collect=lambda: collector.collect_valuation(
                symbol=args.ashare_symbol,
                asset_name=args.ashare_name,
                limit=args.limit,
            ),
        ),
        runtime.run_task(
            task="ashare_p2_performance_report",
            provider_key="stock_yjbb_em",
            parameters={"date": args.report_date, "report_type": "业绩报表", "limit": args.limit},
            force=args.force_provider,
            collect=lambda: collector.collect_performance_report(
                date=args.report_date,
                report_type="业绩报表",
                limit=args.limit,
            ),
        ),
    ]


def run_ashare_risk(
    session: Any,
    args: argparse.Namespace,
    runtime: CollectionRuntime,
) -> list[CollectionTaskResult]:
    """执行 A 股风险和短线情绪采集。"""

    collector = AshareRiskSentimentCollector(session)
    return [
        runtime.run_task(
            task="ashare_risk_stop_list",
            provider_key="stock_zh_a_stop_em",
            parameters={"limit": args.limit},
            force=args.force_provider,
            collect=lambda: collector.collect_stop_list(limit=args.limit),
        ),
        runtime.run_task(
            task="ashare_sentiment_hot_rank",
            provider_key="stock_hot_rank_em",
            parameters={"limit": args.limit},
            force=args.force_provider,
            collect=lambda: collector.collect_hot_rank(
                universe_id="universe:base:ashare:p2:sentiment:hot_rank",
                universe_name="基础数据采集 A 股人气榜种子池",
                strategy_context="base_data_collect",
                limit=args.limit,
            ),
        ),
        runtime.run_task(
            task="ashare_sentiment_zt_pool",
            provider_key="stock_zt_pool_em",
            parameters={"date": args.risk_end, "limit": args.limit},
            force=args.force_provider,
            collect=lambda: collector.collect_zt_pool(
                date=args.risk_end,
                universe_id=f"universe:base:ashare:p2:sentiment:zt_pool:{args.risk_end}",
                universe_name=f"基础数据采集 A 股涨停池-{args.risk_end}",
                strategy_context="base_data_collect",
                limit=args.limit,
            ),
        ),
        runtime.run_task(
            task="ashare_risk_lhb_detail",
            provider_key="stock_lhb_detail_em",
            parameters={
                "start_date": args.risk_start,
                "end_date": args.risk_end,
                "limit": args.limit,
            },
            force=args.force_provider,
            collect=lambda: collector.collect_lhb_detail(
                start_date=args.risk_start,
                end_date=args.risk_end,
                limit=args.limit,
            ),
        ),
        runtime.run_task(
            task="ashare_risk_block_trades",
            provider_key="stock_dzjy_mrmx",
            parameters={
                "symbol": args.risk_block_symbol,
                "start_date": args.risk_start,
                "end_date": args.risk_end,
                "limit": args.limit,
            },
            force=args.force_provider,
            collect=lambda: collector.collect_block_trades(
                symbol=args.risk_block_symbol,
                start_date=args.risk_start,
                end_date=args.risk_end,
                limit=args.limit,
            ),
        ),
        runtime.run_task(
            task="ashare_risk_margin_sse",
            provider_key="stock_margin_sse",
            parameters={
                "start_date": args.risk_start,
                "end_date": args.risk_end,
                "limit": args.limit,
            },
            force=args.force_provider,
            collect=lambda: collector.collect_margin_sse(
                start_date=args.risk_start,
                end_date=args.risk_end,
                limit=args.limit,
            ),
        ),
        runtime.run_task(
            task="ashare_risk_margin_szse",
            provider_key="stock_margin_szse",
            parameters={"date": args.risk_end, "limit": args.limit},
            force=args.force_provider,
            collect=lambda: collector.collect_margin_szse(date=args.risk_end, limit=args.limit),
        ),
    ]


def run_crypto(
    session: Any,
    args: argparse.Namespace,
    runtime: CollectionRuntime,
) -> list[CollectionTaskResult]:
    """执行数字货币资产、K 线和衍生品快照采集。"""

    collector = CryptoDataCollector(session)
    return [
        runtime.run_task(
            task="crypto_markets",
            provider_key="ccxt_binance_load_markets",
            parameters={"market_type": args.crypto_market_type, "limit": args.limit},
            force=args.force_provider,
            collect=lambda: collector.collect_markets(
                market_type=args.crypto_market_type,
                universe_id=f"universe:base:crypto:{args.crypto_market_type}:binance",
                universe_name=f"基础数据采集 Binance {args.crypto_market_type} 候选池",
                strategy_context="base_data_collect",
                limit=args.limit,
            ),
        ),
        runtime.run_task(
            task="crypto_ohlcv",
            provider_key="ccxt_binance_fetch_ohlcv",
            parameters={
                "symbol": args.crypto_symbol,
                "timeframe": args.crypto_timeframe,
                "market_type": args.crypto_market_type,
                "limit": args.limit,
            },
            force=args.force_provider,
            collect=lambda: collector.collect_ohlcv(
                symbol=args.crypto_symbol,
                timeframe=args.crypto_timeframe,
                market_type=args.crypto_market_type,
                limit=args.limit,
            ),
        ),
        runtime.run_task(
            task="crypto_derivative_snapshot",
            provider_key="binance_derivative_snapshot",
            parameters={"symbol": args.crypto_symbol},
            force=args.force_provider,
            collect=lambda: collector.collect_derivative_snapshot(symbol=args.crypto_symbol),
        ),
    ]


if __name__ == "__main__":
    main()
