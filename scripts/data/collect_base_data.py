"""基础数据层统一采集命令。

该命令用于按分组刷新推荐系统基础数据。它只调用 Provider 和 Collector，
不做因子计算、评分、Agent 分析或交易执行。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from finance_agent.data.collectors import (
    ArchivedProviderResult,
    AshareP0Collector,
    AshareP1Collector,
    AshareP2Collector,
    AshareRiskSentimentCollector,
    CryptoDataCollector,
)
from finance_agent.storage.db import create_session_factory, session_scope

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class CollectionTaskResult:
    """单个采集任务的摘要结果。"""

    task: str
    status: str
    raw_record_id: str
    item_count: int
    error_message: str | None
    payload: JsonDict


def main() -> None:
    """解析命令行参数并执行基础数据采集。"""

    args = parse_args()
    session_factory = create_session_factory()
    started_at = datetime.now(tz=UTC)

    with session_scope(session_factory) as session:
        results: list[CollectionTaskResult] = []
        selected_groups = set(args.group)
        if "all" in selected_groups:
            selected_groups = {"ashare-p0", "ashare-p1", "ashare-p2", "ashare-risk", "crypto"}

        if "ashare-p0" in selected_groups:
            results.extend(run_ashare_p0(session, args))
        if "ashare-p1" in selected_groups:
            results.extend(run_ashare_p1(session, args))
        if "ashare-p2" in selected_groups:
            results.extend(run_ashare_p2(session, args))
        if "ashare-risk" in selected_groups:
            results.extend(run_ashare_risk(session, args))
        if "crypto" in selected_groups:
            results.extend(run_crypto(session, args))

    summary = {
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(tz=UTC).isoformat(),
        "groups": args.group,
        "total_tasks": len(results),
        "available": sum(1 for item in results if item.status == "available"),
        "error": sum(1 for item in results if item.status == "error"),
        "unavailable": sum(1 for item in results if item.status == "unavailable"),
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


def run_ashare_p0(session: Any, args: argparse.Namespace) -> list[CollectionTaskResult]:
    """执行 A 股 P0 资产和行情采集。"""

    collector = AshareP0Collector(session)
    archives = [
        (
            "ashare_p0_assets",
            collector.collect_assets(
                universe_id="universe:base:ashare:p0:all_a_sample",
                universe_name="基础数据采集 A 股样例池",
                strategy_context="base_data_collect",
                limit=args.limit,
            ),
        ),
        (
            "ashare_p0_ohlcv",
            collector.collect_ohlcv(
                symbol=args.ashare_symbol,
                timeframe=args.ashare_timeframe,
                start=args.ashare_start,
                end=args.ashare_end,
                limit=args.limit,
                adjust=args.ashare_adjust,
            ),
        ),
    ]
    return [summarize_archive(name, archive) for name, archive in archives]


def run_ashare_p1(session: Any, args: argparse.Namespace) -> list[CollectionTaskResult]:
    """执行 A 股 P1 行业、概念、资金流和新闻采集。"""

    collector = AshareP1Collector(session)
    archives = [
        (
            "ashare_p1_industry_members",
            collector.collect_industry_members(
                industry_name=args.industry,
                universe_id=f"universe:base:ashare:p1:industry:{args.industry}",
                universe_name=f"基础数据采集行业种子池-{args.industry}",
                strategy_context="base_data_collect",
                limit=args.limit,
            ),
        ),
        (
            "ashare_p1_concept_members",
            collector.collect_concept_members(
                concept_name=args.concept,
                universe_id=f"universe:base:ashare:p1:concept:{args.concept}",
                universe_name=f"基础数据采集概念种子池-{args.concept}",
                strategy_context="base_data_collect",
                limit=args.limit,
            ),
        ),
        (
            "ashare_p1_flow_rank",
            collector.collect_flow_rank(indicator=args.flow_window, limit=args.limit),
        ),
        (
            "ashare_p1_stock_news",
            collector.collect_stock_news(
                symbol=args.ashare_symbol,
                asset_name=args.ashare_name,
                limit=min(args.limit, 3),
            ),
        ),
    ]
    return [summarize_archive(name, archive) for name, archive in archives]


def run_ashare_p2(session: Any, args: argparse.Namespace) -> list[CollectionTaskResult]:
    """执行 A 股 P2 财务、估值和业绩采集。"""

    collector = AshareP2Collector(session)
    archives = [
        (
            "ashare_p2_financial_indicators",
            collector.collect_financial_indicators(
                symbol=args.ashare_symbol,
                asset_name=args.ashare_name,
                limit=args.limit,
            ),
        ),
        (
            "ashare_p2_valuation",
            collector.collect_valuation(
                symbol=args.ashare_symbol,
                asset_name=args.ashare_name,
                limit=args.limit,
            ),
        ),
        (
            "ashare_p2_performance_report",
            collector.collect_performance_report(
                date=args.report_date,
                report_type="业绩报表",
                limit=args.limit,
            ),
        ),
    ]
    return [summarize_archive(name, archive) for name, archive in archives]


def run_ashare_risk(session: Any, args: argparse.Namespace) -> list[CollectionTaskResult]:
    """执行 A 股风险和短线情绪采集。"""

    collector = AshareRiskSentimentCollector(session)
    archives = [
        (
            "ashare_risk_stop_list",
            collector.collect_stop_list(limit=args.limit),
        ),
        (
            "ashare_sentiment_hot_rank",
            collector.collect_hot_rank(
                universe_id="universe:base:ashare:p2:sentiment:hot_rank",
                universe_name="基础数据采集 A 股人气榜种子池",
                strategy_context="base_data_collect",
                limit=args.limit,
            ),
        ),
        (
            "ashare_sentiment_zt_pool",
            collector.collect_zt_pool(
                date=args.risk_end,
                universe_id=f"universe:base:ashare:p2:sentiment:zt_pool:{args.risk_end}",
                universe_name=f"基础数据采集 A 股涨停池-{args.risk_end}",
                strategy_context="base_data_collect",
                limit=args.limit,
            ),
        ),
        (
            "ashare_risk_lhb_detail",
            collector.collect_lhb_detail(
                start_date=args.risk_start,
                end_date=args.risk_end,
                limit=args.limit,
            ),
        ),
        (
            "ashare_risk_block_trades",
            collector.collect_block_trades(
                symbol=args.risk_block_symbol,
                start_date=args.risk_start,
                end_date=args.risk_end,
                limit=args.limit,
            ),
        ),
        (
            "ashare_risk_margin_sse",
            collector.collect_margin_sse(
                start_date=args.risk_start,
                end_date=args.risk_end,
                limit=args.limit,
            ),
        ),
        (
            "ashare_risk_margin_szse",
            collector.collect_margin_szse(date=args.risk_end, limit=args.limit),
        ),
    ]
    return [summarize_archive(name, archive) for name, archive in archives]


def run_crypto(session: Any, args: argparse.Namespace) -> list[CollectionTaskResult]:
    """执行数字货币资产、K 线和衍生品快照采集。"""

    collector = CryptoDataCollector(session)
    archives = [
        (
            "crypto_markets",
            collector.collect_markets(
                market_type=args.crypto_market_type,
                universe_id=f"universe:base:crypto:{args.crypto_market_type}:binance",
                universe_name=f"基础数据采集 Binance {args.crypto_market_type} 候选池",
                strategy_context="base_data_collect",
                limit=args.limit,
            ),
        ),
        (
            "crypto_ohlcv",
            collector.collect_ohlcv(
                symbol=args.crypto_symbol,
                timeframe=args.crypto_timeframe,
                market_type=args.crypto_market_type,
                limit=args.limit,
            ),
        ),
        (
            "crypto_derivative_snapshot",
            collector.collect_derivative_snapshot(symbol=args.crypto_symbol),
        ),
    ]
    return [summarize_archive(name, archive) for name, archive in archives]


def summarize_archive(task: str, archive: ArchivedProviderResult) -> CollectionTaskResult:
    """把带归档编号的 Provider 结果压缩成命令输出摘要。"""

    result = archive.result
    return CollectionTaskResult(
        task=task,
        status=result.status,
        raw_record_id=archive.raw_record_id,
        item_count=infer_item_count(result),
        error_message=result.error_message,
        payload={
            "actual_source": result.payload.get("actual_source"),
            "fallback_used": result.payload.get("fallback_used"),
            "source_coverage": result.payload.get("source_coverage"),
        },
    )


def infer_item_count(result: Any) -> int:
    """根据 ProviderResult 子类型推断采集条数。"""

    for attr_name in ("assets", "bars", "seeds", "snapshots", "risks", "events", "evidence"):
        value = getattr(result, attr_name, None)
        if value is not None:
            return len(value)
    if getattr(result, "snapshot", None) is not None:
        return 1
    return 0


if __name__ == "__main__":
    main()
