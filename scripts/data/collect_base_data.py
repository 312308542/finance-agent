"""基础数据层统一采集命令。

该命令用于按分组刷新推荐系统基础数据。它只调用 Provider 和 Collector，
不做因子计算、评分、Agent 分析或交易执行。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from finance_agent.application.data_production_service import MarketCalendarService
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
from finance_agent.data.models import ProviderResult
from finance_agent.data.providers import AkshareProvider
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.orm import MarketBarORM
from finance_agent.storage.repositories import AssetRepository, MarketCalendarRepository

JsonDict = dict[str, Any]
logger = logging.getLogger(__name__)

ALL_GROUPS = ("ashare-p0", "ashare-p1", "ashare-p2", "ashare-risk", "crypto")
COLLECTION_ARG_DEFAULTS: JsonDict = {
    "group": ["ashare-p1"],
    "limit": 5,
    "sync_task_type": None,
    "mode": None,
    "sources": [],
    "data_packages": [],
    "batch_size": 200,
    "lookback": None,
    "symbol_source": "market_assets",
    "ashare_symbol": "000001",
    "ashare_name": "平安银行",
    "ashare_start": "20260501",
    "ashare_end": "20260514",
    "ashare_timeframe": "1d",
    "ashare_adjust": "qfq",
    "industry": "银行",
    "concept": "融资融券",
    "index_catalog_limit": 20,
    "industry_catalog_limit": 120,
    "concept_catalog_limit": 200,
    "catalog_member_limit": 0,
    "flow_window": "5日",
    "report_date": "20250331",
    "risk_start": "20260501",
    "risk_end": "20260514",
    "risk_block_symbol": "A股",
    "cache_backend": "auto",
    "lock_ttl_seconds": 600,
    "circuit_failure_threshold": 3,
    "circuit_cooldown_seconds": 900,
    "force_provider": False,
    "crypto_symbol": "BTCUSDT",
    "crypto_timeframe": "1h",
    "crypto_market_type": "spot",
}


def main() -> None:
    """解析命令行参数并执行基础数据采集。"""

    configure_logging_from_environment()
    args = parse_args()
    summary = collect_base_data(args)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


def configure_logging_from_environment() -> None:
    """为直接运行采集脚本和调度子进程配置控制台日志。"""

    root_logger = logging.getLogger()
    if root_logger.handlers:
        return

    level_name = os.getenv("FINANCE_AGENT_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [pid=%(process)d thread=%(threadName)s] "
        "%(name)s - %(message)s"
    )
    root_logger.setLevel(level)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)
    root_logger.addHandler(console_handler)

    process_log_file = os.getenv("FINANCE_AGENT_PROCESS_LOG_FILE")
    if process_log_file:
        path = Path(process_log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        root_logger.addHandler(file_handler)


def default_collection_args(**overrides: Any) -> argparse.Namespace:
    """生成采集入口默认参数，供调度器和其他编排脚本复用。"""

    unknown_keys = sorted(set(overrides) - set(COLLECTION_ARG_DEFAULTS))
    if unknown_keys:
        raise ValueError(f"不支持的基础数据采集参数: {', '.join(unknown_keys)}")

    values = {
        key: list(value) if isinstance(value, list) else value
        for key, value in COLLECTION_ARG_DEFAULTS.items()
    }
    values.update(overrides)
    if values["group"] is None:
        values["group"] = list(COLLECTION_ARG_DEFAULTS["group"])
    elif isinstance(values["group"], str):
        values["group"] = [values["group"]]
    else:
        values["group"] = list(values["group"])
    return argparse.Namespace(**values)


def collect_base_data(args: argparse.Namespace) -> JsonDict:
    """按传入参数执行基础数据采集，并返回结构化摘要。"""

    configure_logging_from_environment()
    session_factory = create_session_factory()
    started_at = datetime.now(tz=UTC)
    logger.info(
        "基础数据采集开始 groups=%s sync_task_type=%s mode=%s data_packages=%s "
        "cache_backend=%s limit=%s",
        args.group,
        getattr(args, "sync_task_type", None),
        getattr(args, "mode", None),
        getattr(args, "data_packages", []),
        args.cache_backend,
        args.limit,
    )
    cache, locks, cache_status = create_cache_client(backend=args.cache_backend)
    logger.info(
        "基础数据采集缓存就绪 backend=%s status=%s message=%s",
        args.cache_backend,
        getattr(cache_status, "status", None),
        getattr(cache_status, "message", None),
    )
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
            selected_groups = set(ALL_GROUPS)
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
        "sync_task_type": getattr(args, "sync_task_type", None),
        "mode": getattr(args, "mode", None),
        "data_packages": getattr(args, "data_packages", []),
        "cache": cache_status.__dict__,
        "total_tasks": len(results),
        "available": sum(1 for item in results if item.status == "available"),
        "error": sum(1 for item in results if item.status == "error"),
        "unavailable": sum(1 for item in results if item.status == "unavailable"),
        "skipped": sum(1 for item in results if item.status == "skipped"),
        "locked": sum(1 for item in results if item.status == "locked"),
        "results": [item.__dict__ for item in results],
    }
    logger.info(
        "基础数据采集完成 groups=%s total_tasks=%s available=%s error=%s unavailable=%s "
        "skipped=%s locked=%s",
        summary["groups"],
        summary["total_tasks"],
        summary["available"],
        summary["error"],
        summary["unavailable"],
        summary["skipped"],
        summary["locked"],
    )
    return summary


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
    parser.add_argument(
        "--limit",
        type=int,
        default=COLLECTION_ARG_DEFAULTS["limit"],
        help="每类列表型任务采集条数",
    )
    parser.add_argument(
        "--sync-task-type",
        default=COLLECTION_ARG_DEFAULTS["sync_task_type"],
        help="同步任务类型，例如 market_bars_backfill、universe_refresh、event_refresh。",
    )
    parser.add_argument(
        "--symbol-source",
        default=COLLECTION_ARG_DEFAULTS["symbol_source"],
        choices=["manual", "market_assets", "universe"],
        help="K 线等按标的采集任务的代码来源。",
    )
    parser.add_argument(
        "--ashare-symbol",
        default=COLLECTION_ARG_DEFAULTS["ashare_symbol"],
        help="A 股样例代码",
    )
    parser.add_argument(
        "--ashare-name",
        default=COLLECTION_ARG_DEFAULTS["ashare_name"],
        help="A 股样例名称",
    )
    parser.add_argument(
        "--ashare-start",
        default=COLLECTION_ARG_DEFAULTS["ashare_start"],
        help="A 股 K 线开始日期",
    )
    parser.add_argument(
        "--ashare-end",
        default=COLLECTION_ARG_DEFAULTS["ashare_end"],
        help="A 股 K 线结束日期",
    )
    parser.add_argument(
        "--ashare-timeframe",
        default=COLLECTION_ARG_DEFAULTS["ashare_timeframe"],
        help="A 股 K 线周期",
    )
    parser.add_argument(
        "--ashare-adjust",
        default=COLLECTION_ARG_DEFAULTS["ashare_adjust"],
        help="A 股复权类型",
    )
    parser.add_argument(
        "--industry",
        default=COLLECTION_ARG_DEFAULTS["industry"],
        help="A 股行业种子名称",
    )
    parser.add_argument(
        "--concept",
        default=COLLECTION_ARG_DEFAULTS["concept"],
        help="A 股概念种子名称",
    )
    parser.add_argument(
        "--index-catalog-limit",
        type=int,
        default=COLLECTION_ARG_DEFAULTS["index_catalog_limit"],
        help="Universe 刷新时自动展开的指数目录数量；0 表示不限制",
    )
    parser.add_argument(
        "--industry-catalog-limit",
        type=int,
        default=COLLECTION_ARG_DEFAULTS["industry_catalog_limit"],
        help="Universe 刷新时自动展开的行业目录数量；0 表示不限制",
    )
    parser.add_argument(
        "--concept-catalog-limit",
        type=int,
        default=COLLECTION_ARG_DEFAULTS["concept_catalog_limit"],
        help="Universe 刷新时自动展开的概念目录数量；0 表示不限制",
    )
    parser.add_argument(
        "--catalog-member-limit",
        type=int,
        default=COLLECTION_ARG_DEFAULTS["catalog_member_limit"],
        help="每个指数/行业/概念成员采集条数；0 表示不限制，调度任务 limit 只控制目录数量",
    )
    parser.add_argument(
        "--flow-window",
        default=COLLECTION_ARG_DEFAULTS["flow_window"],
        help="A 股资金流周期",
    )
    parser.add_argument(
        "--report-date",
        default=COLLECTION_ARG_DEFAULTS["report_date"],
        help="业绩报表日期",
    )
    parser.add_argument(
        "--risk-start",
        default=COLLECTION_ARG_DEFAULTS["risk_start"],
        help="A 股风险数据开始日期",
    )
    parser.add_argument(
        "--risk-end",
        default=COLLECTION_ARG_DEFAULTS["risk_end"],
        help="A 股风险数据结束日期",
    )
    parser.add_argument(
        "--risk-block-symbol",
        default=COLLECTION_ARG_DEFAULTS["risk_block_symbol"],
        help="大宗交易市场范围",
    )
    parser.add_argument(
        "--cache-backend",
        choices=["auto", "redis", "null"],
        default=COLLECTION_ARG_DEFAULTS["cache_backend"],
        help="缓存和任务锁后端；生产调度建议使用 redis",
    )
    parser.add_argument(
        "--lock-ttl-seconds",
        type=int,
        default=COLLECTION_ARG_DEFAULTS["lock_ttl_seconds"],
        help="采集任务锁 TTL",
    )
    parser.add_argument(
        "--circuit-failure-threshold",
        type=int,
        default=COLLECTION_ARG_DEFAULTS["circuit_failure_threshold"],
        help="Provider 连续失败多少次后熔断",
    )
    parser.add_argument(
        "--circuit-cooldown-seconds",
        type=int,
        default=COLLECTION_ARG_DEFAULTS["circuit_cooldown_seconds"],
        help="Provider 熔断冷却时间",
    )
    parser.add_argument(
        "--force-provider",
        action="store_true",
        help="忽略 Provider 熔断状态，强制执行采集",
    )
    parser.add_argument(
        "--crypto-symbol",
        default=COLLECTION_ARG_DEFAULTS["crypto_symbol"],
        help="数字货币交易对",
    )
    parser.add_argument(
        "--crypto-timeframe",
        default=COLLECTION_ARG_DEFAULTS["crypto_timeframe"],
        help="数字货币 K 线周期",
    )
    parser.add_argument(
        "--crypto-market-type",
        default=COLLECTION_ARG_DEFAULTS["crypto_market_type"],
        choices=["spot", "future", "swap"],
        help="ccxt Binance 市场类型",
    )
    args = parser.parse_args()
    if args.group is None:
        args.group = list(COLLECTION_ARG_DEFAULTS["group"])
    return args


def run_ashare_p0(
    session: Any,
    args: argparse.Namespace,
    runtime: CollectionRuntime,
) -> list[CollectionTaskResult]:
    """执行 A 股 P0 资产和行情采集。"""

    collector = AshareP0Collector(session)
    task_type = task_type_name(args)
    if task_type == "calendar_refresh":
        return [
            runtime.run_task(
                task="ashare_p0_calendar",
                provider_key="tool_trade_date_hist_sina",
                parameters={"start": args.ashare_start, "end": args.ashare_end},
                force=args.force_provider,
                collect=lambda: collect_ashare_calendar(
                    session,
                    start=args.ashare_start,
                    end=args.ashare_end,
                ),
            )
        ]
    if task_type == "universe_refresh":
        return [
            runtime.run_task(
                task="ashare_p0_calendar",
                provider_key="tool_trade_date_hist_sina",
                parameters={"start": args.ashare_start, "end": args.ashare_end},
                force=args.force_provider,
                collect=lambda: collect_ashare_calendar(
                    session,
                    start=args.ashare_start,
                    end=args.ashare_end,
                ),
            ),
            runtime.run_task(
                task="ashare_p0_assets",
                provider_key="stock_zh_a_spot",
                parameters={"limit": args.limit},
                force=args.force_provider,
                collect=lambda: collector.collect_assets(
                    universe_id="universe:base:ashare:p0:all_a",
                    universe_name="基础数据全 A 候选池",
                    strategy_context="base_data_collect",
                    limit=args.limit,
                ),
            )
        ]
    if task_type == "realtime_quote_refresh":
        return [
            runtime.run_task(
                task="ashare_p0_calendar",
                provider_key="tool_trade_date_hist_sina",
                parameters={"start": args.ashare_start, "end": args.ashare_end},
                force=args.force_provider,
                collect=lambda: collect_ashare_calendar(
                    session,
                    start=args.ashare_start,
                    end=args.ashare_end,
                ),
            ),
            runtime.run_task(
                task="ashare_p0_assets",
                provider_key="stock_zh_a_spot",
                parameters={"limit": args.limit},
                force=args.force_provider,
                collect=lambda: collector.collect_assets(
                    universe_id="universe:base:ashare:p0:all_a",
                    universe_name="基础数据全 A 候选池",
                    strategy_context="base_data_collect",
                    limit=args.limit,
                ),
            )
        ]
    if task_type == "market_bars_backfill":
        tasks = [
            runtime.run_task(
                task="ashare_p0_calendar",
                provider_key="tool_trade_date_hist_sina",
                parameters={"start": args.ashare_start, "end": args.ashare_end},
                force=args.force_provider,
                collect=lambda: collect_ashare_calendar(
                    session,
                    start=args.ashare_start,
                    end=args.ashare_end,
                ),
            )
        ]
        symbols = resolve_ashare_collection_symbols(session, args)
        for symbol in symbols:
            tasks.append(
                runtime.run_task(
                    task="ashare_p0_ohlcv",
                    provider_key=ashare_ohlcv_provider_key(symbol),
                    parameters={
                        "symbol": symbol,
                        "timeframe": args.ashare_timeframe,
                        "start": args.ashare_start,
                        "end": args.ashare_end,
                        "adjust": args.ashare_adjust,
                        "limit": args.limit,
                    },
                    force=args.force_provider,
                    collect=lambda symbol=symbol: collector.collect_ohlcv(
                        symbol=symbol,
                        timeframe=args.ashare_timeframe,
                        start=args.ashare_start,
                        end=args.ashare_end,
                        limit=args.limit,
                        adjust=args.ashare_adjust,
                    ),
                )
            )
        return tasks
    return [
        runtime.run_task(
            task="ashare_p0_calendar",
            provider_key="tool_trade_date_hist_sina",
            parameters={"start": args.ashare_start, "end": args.ashare_end},
            force=args.force_provider,
            collect=lambda: collect_ashare_calendar(
                session,
                start=args.ashare_start,
                end=args.ashare_end,
            ),
        ),
        runtime.run_task(
            task="ashare_p0_assets",
            provider_key="stock_zh_a_spot",
            parameters={"limit": args.limit},
            force=args.force_provider,
            collect=lambda: collector.collect_assets(
                universe_id="universe:base:ashare:p0:all_a",
                universe_name="基础数据全 A 候选池",
                strategy_context="base_data_collect",
                limit=args.limit,
            ),
        ),
        runtime.run_task(
            task="ashare_p0_ohlcv",
            provider_key=ashare_ohlcv_provider_key(args.ashare_symbol),
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
    task_type = task_type_name(args)
    if task_type == "universe_refresh":
        return build_ashare_p1_universe_tasks(collector, args, runtime)
    if task_type == "capital_flow_refresh":
        return [
            runtime.run_task(
                task="ashare_p1_flow_rank",
                provider_key="stock_individual_fund_flow_rank",
                parameters={"indicator": args.flow_window, "limit": args.limit},
                force=args.force_provider,
                collect=lambda: collector.collect_flow_rank(
                    indicator=args.flow_window,
                    limit=args.limit,
                ),
            )
        ]
    if task_type == "event_refresh":
        return [
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
            runtime.run_task(
                task="ashare_p1_notice_reports",
                provider_key="stock_notice_report",
                parameters={"symbol": "全部", "date": args.risk_end, "limit": min(args.limit, 10)},
                force=args.force_provider,
                collect=lambda: collector.collect_notice_reports(
                    symbol="全部",
                    date=args.risk_end,
                    limit=min(args.limit, 10),
                ),
            ),
        ]
    return build_ashare_p1_default_tasks(collector, args, runtime)


def build_ashare_p1_universe_tasks(
    collector: AshareP1Collector,
    args: argparse.Namespace,
    runtime: CollectionRuntime,
) -> list[CollectionTaskResult]:
    """按目录自动展开 A 股 P1 的 universe_refresh 任务。"""

    tasks: list[CollectionTaskResult] = []
    index_sources = fetch_catalog_entries(
        collector.sector_provider.fetch_index_catalog(limit=positive_limit(args.index_catalog_limit)),
        key="indexes",
        default=[{"code": "000300", "name": "沪深300"}],
    )
    industry_sources = fetch_catalog_entries(
        collector.sector_provider.fetch_industry_names(
            limit=positive_limit(args.industry_catalog_limit)
        ),
        key="names",
        default=[args.industry],
    )
    concept_sources = fetch_catalog_entries(
        collector.sector_provider.fetch_concept_names(
            limit=positive_limit(args.concept_catalog_limit)
        ),
        key="names",
        default=[args.concept],
    )
    member_limit = normalize_member_limit(args.catalog_member_limit)
    index_limit = positive_limit(args.limit)

    for item in index_sources:
        index_code = str(item.get("code") or "").strip()
        index_name = str(item.get("name") or index_code).strip()
        if not index_code:
            continue
        tasks.append(
            runtime.run_task(
                task=f"ashare_p1_index_members:{index_code}",
                provider_key="index_stock_cons_csindex",
                parameters={
                    "index_code": index_code,
                    "index_name": index_name,
                    "limit": member_limit,
                },
                force=args.force_provider,
                collect=lambda index_code=index_code, index_name=index_name: (
                    collector.collect_index_members(
                        index_code=index_code,
                        index_name=index_name,
                        universe_id=f"universe:base:ashare:p1:index:{index_code}",
                        universe_name=f"基础数据指数种子池-{index_name}",
                        strategy_context="base_data_collect",
                        limit=member_limit,
                    )
                ),
            )
        )

    for industry_name in industry_sources:
        normalized_name = str(industry_name).strip()
        if not normalized_name:
            continue
        tasks.append(
            runtime.run_task(
                task=f"ashare_p1_industry_members:{normalized_name}",
                provider_key="stock_board_industry_cons_em",
                parameters={"industry": normalized_name, "limit": member_limit},
                force=args.force_provider,
                collect=lambda industry_name=normalized_name: collector.collect_industry_members(
                    industry_name=industry_name,
                    universe_id=f"universe:base:ashare:p1:industry:{industry_name}",
                    universe_name=f"基础数据采集行业种子池-{industry_name}",
                    strategy_context="base_data_collect",
                    limit=member_limit,
                ),
            )
        )

    for concept_name in concept_sources:
        normalized_name = str(concept_name).strip()
        if not normalized_name:
            continue
        tasks.append(
            runtime.run_task(
                task=f"ashare_p1_concept_members:{normalized_name}",
                provider_key="stock_board_concept_cons_em",
                parameters={"concept": normalized_name, "limit": member_limit},
                force=args.force_provider,
                collect=lambda concept_name=normalized_name: collector.collect_concept_members(
                    concept_name=concept_name,
                    universe_id=f"universe:base:ashare:p1:concept:{concept_name}",
                    universe_name=f"基础数据采集概念种子池-{concept_name}",
                    strategy_context="base_data_collect",
                    limit=member_limit,
                ),
            )
        )

    tasks.append(
        runtime.run_task(
            task="ashare_p1_flow_rank",
            provider_key="stock_individual_fund_flow_rank",
            parameters={"indicator": args.flow_window, "limit": index_limit},
            force=args.force_provider,
            collect=lambda: collector.collect_flow_rank(
                indicator=args.flow_window,
                limit=index_limit,
            ),
        )
    )
    return tasks


def build_ashare_p1_default_tasks(
    collector: AshareP1Collector,
    args: argparse.Namespace,
    runtime: CollectionRuntime,
) -> list[CollectionTaskResult]:
    """构建 A 股 P1 的默认任务包。"""

    return [
        *build_ashare_p1_universe_tasks(collector, args, runtime),
        runtime.run_task(
            task="ashare_p1_stock_news",
            provider_key="stock_news_em",
            parameters={"symbol": args.ashare_symbol, "limit": bounded_limit(args.limit, 3)},
            force=args.force_provider,
            collect=lambda: collector.collect_stock_news(
                symbol=args.ashare_symbol,
                asset_name=args.ashare_name,
                limit=bounded_limit(args.limit, 3),
            ),
        ),
        runtime.run_task(
            task="ashare_p1_notice_reports",
            provider_key="stock_notice_report",
            parameters={
                "symbol": "全部",
                "date": args.risk_end,
                "limit": bounded_limit(args.limit, 10),
            },
            force=args.force_provider,
            collect=lambda: collector.collect_notice_reports(
                symbol="全部",
                date=args.risk_end,
                limit=bounded_limit(args.limit, 10),
            ),
        ),
    ]


def fetch_catalog_entries(
    result: Any,
    *,
    key: str,
    default: list[dict[str, str]] | list[str],
) -> list[Any]:
    """从目录 Provider 结果中抽取条目；失败时使用默认值。"""

    payload = getattr(result, "payload", {}) or {}
    entries = payload.get(key)
    if isinstance(entries, list) and entries:
        return entries
    return list(default)


def positive_limit(value: Any) -> int | None:
    """把 0 转换为不限，其他值保持正整数。"""

    if value in {None, "", 0, "0"}:
        return None
    number = int(value)
    if number <= 0:
        return None
    return number


def normalize_member_limit(value: Any) -> int | None:
    """把成员展开限制标准化。"""

    limit = positive_limit(value)
    return limit if limit is not None else None


def bounded_limit(limit: int | None, upper: int) -> int:
    """对可为空的限制值取上限。"""

    if limit is None:
        return upper
    return min(limit, upper)


def run_ashare_p2(
    session: Any,
    args: argparse.Namespace,
    runtime: CollectionRuntime,
) -> list[CollectionTaskResult]:
    """执行 A 股 P2 财务、估值和业绩采集。"""

    collector = AshareP2Collector(session)
    task_type = task_type_name(args)
    if task_type == "fundamental_refresh":
        symbols = batch_ashare_symbols(
            session,
            limit=args.limit,
            fallback_symbol=args.ashare_symbol,
        )
        tasks: list[CollectionTaskResult] = []
        for symbol in symbols:
            tasks.extend(
                [
                    runtime.run_task(
                        task="ashare_p2_financial_indicators",
                        provider_key="stock_financial_analysis_indicator_em",
                        parameters={"symbol": symbol, "limit": args.limit},
                        force=args.force_provider,
                        collect=lambda symbol=symbol: collector.collect_financial_indicators(
                            symbol=symbol,
                            asset_name=asset_name_for_symbol(session, symbol),
                            limit=args.limit,
                        ),
                    ),
                    runtime.run_task(
                        task="ashare_p2_valuation",
                        provider_key="stock_value_em",
                        parameters={"symbol": symbol, "limit": args.limit},
                        force=args.force_provider,
                        collect=lambda symbol=symbol: collector.collect_valuation(
                            symbol=symbol,
                            asset_name=asset_name_for_symbol(session, symbol),
                            limit=args.limit,
                        ),
                    ),
                ]
            )
        tasks.append(
            runtime.run_task(
                task="ashare_p2_performance_report",
                provider_key="stock_yjbb_em",
                parameters={
                    "date": args.report_date,
                    "report_type": "业绩报表",
                    "limit": args.limit,
                },
                force=args.force_provider,
                collect=lambda: collector.collect_performance_report(
                    date=args.report_date,
                    report_type="业绩报表",
                    limit=args.limit,
                ),
            )
        )
        tasks.append(
            runtime.run_task(
                task="ashare_p2_dividend_yield",
                provider_key="stock_a_gxl_lg",
                parameters={"limit": args.limit},
                force=args.force_provider,
                collect=lambda: collector.collect_dividend_yield(limit=args.limit),
            )
        )
        return tasks
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
        runtime.run_task(
            task="ashare_p2_dividend_yield",
            provider_key="stock_a_gxl_lg",
            parameters={"limit": args.limit},
            force=args.force_provider,
            collect=lambda: collector.collect_dividend_yield(limit=args.limit),
        ),
    ]


def run_ashare_risk(
    session: Any,
    args: argparse.Namespace,
    runtime: CollectionRuntime,
) -> list[CollectionTaskResult]:
    """执行 A 股风险和短线情绪采集。"""

    collector = AshareRiskSentimentCollector(session)
    task_type = task_type_name(args)
    if task_type == "risk_sentiment_refresh":
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
                    universe_name="A 股人气榜观察池",
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
                    universe_name=f"A 股涨停池-{args.risk_end}",
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
                universe_name="A 股人气榜观察池",
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
                universe_name=f"A 股涨停池-{args.risk_end}",
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
    task_type = task_type_name(args)
    crypto_market = crypto_market_name(args.crypto_market_type)
    if task_type == "calendar_refresh":
        return [
            runtime.run_task(
                task="crypto_calendar",
                provider_key="crypto_calendar_24x7",
                parameters={"market": crypto_market, "lookback": args.lookback},
                force=args.force_provider,
                collect=lambda: collect_crypto_calendar(
                    session,
                    market=crypto_market,
                    lookback=args.lookback,
                ),
            )
        ]
    if task_type == "universe_refresh":
        return [
            runtime.run_task(
                task="crypto_calendar",
                provider_key="crypto_calendar_24x7",
                parameters={"market": crypto_market, "lookback": args.lookback},
                force=args.force_provider,
                collect=lambda: collect_crypto_calendar(
                    session,
                    market=crypto_market,
                    lookback=args.lookback,
                ),
            ),
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
            )
        ]
    if task_type == "market_bars_backfill":
        tasks = [
            runtime.run_task(
                task="crypto_calendar",
                provider_key="crypto_calendar_24x7",
                parameters={"market": crypto_market, "lookback": args.lookback},
                force=args.force_provider,
                collect=lambda: collect_crypto_calendar(
                    session,
                    market=crypto_market,
                    lookback=args.lookback,
                ),
            ),
        ]
        symbols = resolve_crypto_collection_symbols(session, args, market=crypto_market)
        for symbol in symbols:
            tasks.append(
                runtime.run_task(
                    task="crypto_ohlcv",
                    provider_key=crypto_ohlcv_provider_key(args.crypto_market_type, symbol),
                    parameters={
                        "symbol": symbol,
                        "timeframe": args.crypto_timeframe,
                        "market_type": args.crypto_market_type,
                        "limit": args.limit,
                    },
                    force=args.force_provider,
                    collect=lambda symbol=symbol: collector.collect_ohlcv(
                        symbol=symbol,
                        timeframe=args.crypto_timeframe,
                        market_type=args.crypto_market_type,
                        limit=args.limit,
                    ),
                )
            )
        return tasks
    if task_type == "derivative_refresh":
        return [
            runtime.run_task(
                task="crypto_calendar",
                provider_key="crypto_calendar_24x7",
                parameters={"market": crypto_market, "lookback": args.lookback},
                force=args.force_provider,
                collect=lambda: collect_crypto_calendar(
                    session,
                    market=crypto_market,
                    lookback=args.lookback,
                ),
            ),
            runtime.run_task(
                task="crypto_derivative_snapshot",
                provider_key="binance_derivative_snapshot",
                parameters={"symbol": args.crypto_symbol},
                force=args.force_provider,
                collect=lambda: collector.collect_derivative_snapshot(symbol=args.crypto_symbol),
            )
        ]
    return [
        runtime.run_task(
            task="crypto_calendar",
            provider_key="crypto_calendar_24x7",
            parameters={"market": crypto_market, "lookback": args.lookback},
            force=args.force_provider,
            collect=lambda: collect_crypto_calendar(
                session,
                market=crypto_market,
                lookback=args.lookback,
            ),
        ),
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


def task_type_name(args: argparse.Namespace) -> str | None:
    """从参数中读取当前同步任务类型。"""

    return getattr(args, "sync_task_type", None)


def collect_ashare_calendar(
    session: Any,
    *,
    start: str,
    end: str,
) -> Any:
    """采集并写入 A 股交易日历。"""

    collected_at = datetime.now(tz=UTC)
    start_date = parse_yyyymmdd(start)
    end_date = parse_yyyymmdd(end)
    try:
        provider = AkshareProvider()
        trading_dates = provider.fetch_trade_dates(start_date=start_date, end_date=end_date)
        entries = MarketCalendarService().build_ashare_calendar_entries(
            trading_dates=trading_dates,
            start_date=start_date,
            end_date=end_date,
            source="akshare:tool_trade_date_hist_sina",
        )
        repo = MarketCalendarRepository(session)
        repo.replace_calendar_entries([entry.__dict__ for entry in entries])
        result = ProviderResult(
            provider_name="akshare",
            status="available" if entries else "unavailable",
            collected_at=collected_at,
            payload={
                "actual_source": "akshare:tool_trade_date_hist_sina",
                "row_count": len(entries),
                "trading_day_count": sum(1 for entry in entries if entry.is_trading_day),
            },
        )
    except Exception as exc:
        result = ProviderResult(
            provider_name="akshare",
            status="error",
            collected_at=collected_at,
            error_message=str(exc),
            payload={"actual_source": "akshare:tool_trade_date_hist_sina"},
        )
    return SimpleArchivedProviderResult(result=result, raw_record_id=None)


def collect_crypto_calendar(
    session: Any,
    *,
    market: str,
    lookback: str | None,
) -> Any:
    """写入数字货币 7x24 交易日历。"""

    collected_at = datetime.now(tz=UTC)
    end_date = collected_at.date()
    start_date = end_date - timedelta(days=parse_lookback_days(lookback, default_days=7))
    try:
        entries = MarketCalendarService().build_crypto_calendar_entries(
            start_date=start_date,
            end_date=end_date,
            market=market,
            source="internal:crypto_calendar_24x7",
        )
        repo = MarketCalendarRepository(session)
        repo.replace_calendar_entries([entry.__dict__ for entry in entries])
        result = ProviderResult(
            provider_name="internal",
            status="available",
            collected_at=collected_at,
            payload={
                "actual_source": "internal:crypto_calendar_24x7",
                "market": market,
                "row_count": len(entries),
            },
        )
    except Exception as exc:
        result = ProviderResult(
            provider_name="internal",
            status="error",
            collected_at=collected_at,
            error_message=str(exc),
            payload={"actual_source": "internal:crypto_calendar_24x7", "market": market},
        )
    return SimpleArchivedProviderResult(result=result, raw_record_id=None)


class SimpleArchivedProviderResult:
    """给内部日历任务复用 CollectionRuntime 摘要结构的轻量包装。"""

    def __init__(self, *, result: ProviderResult, raw_record_id: str | None) -> None:
        self.result = result
        self.raw_record_id = raw_record_id


def parse_yyyymmdd(value: str) -> date:
    """解析 YYYYMMDD 日期字符串。"""

    return datetime.strptime(value, "%Y%m%d").date()


def parse_lookback_days(value: str | None, *, default_days: int) -> int:
    """把配置中的 lookback 转换为天数。"""

    if not value:
        return default_days
    text = str(value).strip().lower()
    try:
        if text.endswith("h"):
            hours = int(text[:-1])
            return max(1, hours // 24)
        if text.endswith("d"):
            return max(1, int(text[:-1]))
        return max(1, int(text))
    except ValueError:
        return default_days


def crypto_market_name(market_type: str) -> str:
    """把 ccxt 市场类型转换为系统市场名。"""

    if market_type in {"future", "swap"}:
        return "crypto_future"
    return "crypto_spot"


def ashare_ohlcv_provider_key(symbol: str) -> str:
    """生成按标的隔离的 A 股 K 线熔断键。"""

    return f"stock_zh_a_hist_tx:{symbol}"


def crypto_ohlcv_provider_key(market_type: str, symbol: str) -> str:
    """生成按币对隔离的 Binance K 线熔断键。"""

    return f"ccxt_binance_fetch_ohlcv:{market_type}:{symbol.replace('/', '').upper()}"


def batch_ashare_symbols(
    session: Any,
    *,
    limit: int | None = None,
    fallback_symbol: str,
) -> list[str]:
    """按 K 线覆盖缺口选择 A 股补采标的。"""

    try:
        repo = AssetRepository(session)
        assets = repo.find_by_market("ashare")
        coverage = _fetch_ashare_bar_coverage(
            session,
            [asset.asset_id for asset in assets],
            timeframe="1d",
        )
        ranked_assets = sorted(
            assets,
            key=lambda asset: (
                coverage.get(asset.asset_id, (0, None))[0],
                coverage.get(asset.asset_id, (0, None))[1] or datetime.min.replace(tzinfo=UTC),
                asset.symbol,
            ),
        )
        symbols = [asset.symbol for asset in ranked_assets]
    except Exception:
        symbols = []
    if not symbols:
        symbols = [fallback_symbol]
    if limit:
        return symbols[:limit]
    return symbols


def _fetch_ashare_bar_coverage(
    session: Any,
    asset_ids: list[str],
    *,
    timeframe: str,
) -> dict[str, tuple[int, datetime | None]]:
    """查询 A 股标的已有 K 线覆盖情况。"""

    if not asset_ids:
        return {}
    statement = (
        select(
            MarketBarORM.asset_id,
            func.count(MarketBarORM.timestamp),
            func.max(MarketBarORM.timestamp),
        )
        .where(
            MarketBarORM.market == "ashare",
            MarketBarORM.timeframe == timeframe,
            MarketBarORM.asset_id.in_(asset_ids),
        )
        .group_by(MarketBarORM.asset_id)
    )
    return {
        str(asset_id): (int(count or 0), latest)
        for asset_id, count, latest in session.execute(statement)
    }


def resolve_ashare_collection_symbols(session: Any, args: argparse.Namespace) -> list[str]:
    """根据采集参数解析本次 A 股 K 线补采标的。"""

    symbol_source = str(getattr(args, "symbol_source", "") or "").strip()
    if symbol_source in {"market_assets", "universe"}:
        return batch_ashare_symbols(
            session,
            limit=getattr(args, "limit", None),
            fallback_symbol=args.ashare_symbol,
        )
    return [args.ashare_symbol]


def batch_crypto_symbols(
    session: Any,
    *,
    market: str,
    timeframe: str,
    limit: int | None = None,
    fallback_symbol: str,
) -> list[str]:
    """按 K 线覆盖缺口选择数字货币补采标的。"""

    try:
        repo = AssetRepository(session)
        assets = repo.find_by_market(market)
        coverage = _fetch_crypto_bar_coverage(
            session,
            [asset.asset_id for asset in assets],
            timeframe=timeframe,
            market=market,
        )
        ranked_assets = sorted(
            assets,
            key=lambda asset: (
                coverage.get(asset.asset_id, (0, None))[0],
                coverage.get(asset.asset_id, (0, None))[1] or datetime.min.replace(tzinfo=UTC),
                asset.symbol,
            ),
        )
        symbols = [asset.symbol for asset in ranked_assets]
    except Exception:
        symbols = []
    if not symbols:
        symbols = [fallback_symbol]
    if limit:
        return symbols[:limit]
    return symbols


def _fetch_crypto_bar_coverage(
    session: Any,
    asset_ids: list[str],
    *,
    timeframe: str,
    market: str,
) -> dict[str, tuple[int, datetime | None]]:
    """查询数字货币标的已有 K 线覆盖情况。"""

    if not asset_ids:
        return {}
    statement = (
        select(
            MarketBarORM.asset_id,
            func.count(MarketBarORM.timestamp),
            func.max(MarketBarORM.timestamp),
        )
        .where(
            MarketBarORM.market == market,
            MarketBarORM.timeframe == timeframe,
            MarketBarORM.asset_id.in_(asset_ids),
        )
        .group_by(MarketBarORM.asset_id)
    )
    return {
        str(asset_id): (int(count or 0), latest)
        for asset_id, count, latest in session.execute(statement)
    }


def resolve_crypto_collection_symbols(
    session: Any,
    args: argparse.Namespace,
    *,
    market: str,
) -> list[str]:
    """根据采集参数解析本次数字货币 K 线补采标的。"""

    symbol_source = str(getattr(args, "symbol_source", "") or "").strip()
    if symbol_source in {"market_assets", "universe"}:
        return batch_crypto_symbols(
            session,
            market=market,
            timeframe=args.crypto_timeframe,
            limit=getattr(args, "limit", None),
            fallback_symbol=args.crypto_symbol,
        )
    return [args.crypto_symbol]


def asset_name_for_symbol(session: Any, symbol: str) -> str | None:
    """查询资产名称，供财务快照回填。"""

    try:
        repo = AssetRepository(session)
        asset = repo.get_asset_or_none(f"ashare:{symbol}")
    except Exception:
        return None
    return asset.name if asset is not None else None


if __name__ == "__main__":
    main()
