"""基础数据层统一采集命令。

该命令用于按分组刷新推荐系统基础数据。它只调用 Provider 和 Collector，
不做因子计算、评分、Agent 分析或交易执行。
"""

from __future__ import annotations

import argparse
import json
import logging
import multiprocessing
import os
import sys
import threading
import time
from collections.abc import Callable, Collection, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from finance_agent.application.asset_eligibility_service import (
    TradeableAssetEligibilityService,
    is_tradeable_ashare_symbol,
)
from finance_agent.application.data_production_service import MarketCalendarService
from finance_agent.application.event_priority_service import EventPriorityResolver
from finance_agent.cache import create_cache_client
from finance_agent.data.collection_runtime import (
    CollectionRuntime,
    CollectionTaskResult,
    ProviderCircuitPolicy,
)
from finance_agent.data.collectors import (
    ArchivedProviderResult,
    AshareP0Collector,
    AshareP1Collector,
    AshareP2Collector,
    AshareRiskSentimentCollector,
    CryptoDataCollector,
    FundDataCollector,
)
from finance_agent.data.freshness import (
    ASHARE_FINANCIAL_INDICATOR_SOURCE,
    expected_ashare_report_period,
)
from finance_agent.data.models import AssetData, AssetListResult, ProviderResult
from finance_agent.data.normalizers import (
    compact_crypto_symbol,
    normalize_ashare_symbol,
)
from finance_agent.data.providers import (
    AkshareProvider,
    GotdxGatewayProvider,
    ParallelQuoteEvaluator,
)
from finance_agent.data.providers.eastmoney_curl import eastmoney_kline_cookie_health_status
from finance_agent.data.source_rate_limiter import (
    build_source_rate_limiter,
    default_source_rate_limiter,
)
from finance_agent.scheduler.base_data_progress import BaseDataTaskProgressRecorder
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.event_retention import DEFAULT_ARTICLE_FULL_TEXT_RETENTION_DAYS
from finance_agent.storage.event_validation import active_event_predicate
from finance_agent.storage.orm import (
    AssetRecommendationORM,
    AssistantTriggerEventORM,
    DataSyncWatermarkORM,
    EventRecordORM,
    FundamentalSnapshotORM,
    FundNavSnapshotORM,
    MarketBarORM,
    MarketCalendarORM,
    PositionORM,
    WatchlistItemORM,
)
from finance_agent.storage.repositories import (
    AssetRepository,
    DataSnapshotRepository,
    DataSyncWatermarkRepository,
    EventRepository,
    MarketCalendarRepository,
)
from finance_agent.storage.snapshot_contracts import build_data_snapshot

JsonDict = dict[str, Any]
logger = logging.getLogger(__name__)
COLLECTION_PROGRESS_RECORDER: BaseDataTaskProgressRecorder | None = None
COLLECTION_RUNTIME_ARGS: argparse.Namespace | None = None
ASHARE_MARKET_BAR_WATERMARK_PROVIDER = "akshare:stock_zh_a_hist_tx"
ASHARE_MARKET_BAR_DATA_DOMAIN = "market_bars"
ASHARE_FUNDAMENTAL_DATA_DOMAIN = "fundamentals"
ASHARE_VALUATION_DATA_DOMAIN = "valuation"
ASHARE_CAPITAL_FLOW_DATA_DOMAIN = "capital_flow"
ASHARE_EVENT_DATA_DOMAIN = "events"
ASHARE_RISK_SENTIMENT_DATA_DOMAIN = "risk_sentiment"
ASHARE_FINANCIAL_INDICATORS_PROVIDER = "stock_financial_analysis_indicator_em"
ASHARE_VALUATION_PROVIDER = "stock_value_em"
ASHARE_CAPITAL_FLOW_PROVIDER = "stock_individual_fund_flow_rank"
ASHARE_NORTHBOUND_FLOW_PROVIDER = "stock_hsgt_hist_em"
ASHARE_NORTHBOUND_INDIVIDUAL_PROVIDER = "stock_hsgt_individual_em"
ASHARE_NORTHBOUND_INDIVIDUAL_TIMEFRAME = "northbound"
CRYPTO_MARKET_BAR_DATA_DOMAIN = "market_bars"
CRYPTO_MARKET_BAR_PROVIDER = "ccxt_binance_fetch_ohlcv"
CRYPTO_DERIVATIVE_DATA_DOMAIN = "derivatives"
CRYPTO_DERIVATIVE_PROVIDER = "binance_derivative_snapshot"
ASHARE_BAR_COVERAGE_QUERY_CHUNK_SIZE = 500
ASHARE_FUNDAMENTAL_COVERAGE_QUERY_CHUNK_SIZE = 500
FUND_COVERAGE_QUERY_CHUNK_SIZE = 200
EASTMONEY_KLINE_COOKIE_PROGRESS_SOURCE = "eastmoney_kline_cookie"
FUND_MARKET_BAR_DATA_DOMAIN = "fund_market_bars"
FUND_NAV_DATA_DOMAIN = "fund_nav"
SOURCE_RATE_LIMITER = default_source_rate_limiter()
SOURCE_RATE_POLICY_FINGERPRINT: str | None = None
ASHARE_MARKET_BAR_TASK_TYPES = {
    "market_bars_backfill",
    "market_bars_full_history_backfill",
    "market_bars_midday_partial",
    "market_bars_close_final",
    "market_bars_revision",
}


def time_sleep(seconds: float) -> None:
    """运行期控制等待使用的可替换 sleep，便于测试暂停/继续。"""

    time.sleep(seconds)

ALL_GROUPS = ("ashare-p0", "ashare-p1", "ashare-p2", "ashare-risk", "fund", "crypto")
COLLECTION_ARG_DEFAULTS: JsonDict = {
    "group": ["ashare-p1"],
    "limit": 5,
    "sync_task_type": None,
    "title": None,
    "notes": [],
    "mode": None,
    "sources": [],
    "data_packages": [],
    "batch_size": 200,
    "max_workers": 4,
    "source_limit": None,
    "priority_symbol_limit": None,
    "scope": "priority",
    "partition_cursor": 0,
    "partition_count": None,
    "news_scope": "priority",
    "article_retention_days": DEFAULT_ARTICLE_FULL_TEXT_RETENTION_DAYS,
    "progress_job_name": None,
    "progress_run_id": None,
    "progress_ttl_seconds": None,
    "progress_cache_backend": "redis",
    "runtime_scheduler_config_file": None,
    "lookback": None,
    "symbol_source": "market_assets",
    "ashare_symbol": "000001",
    "ashare_name": "平安银行",
    "ashare_start": "20260501",
    "ashare_end": "20260514",
    "ashare_timeframe": "1d",
    "ashare_adjust": "qfq",
    "is_closed": True,
    "status": "available",
    "only_failed_or_stale": False,
    "schedule_failure_retry": True,
    "include_adjustment_check": False,
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
    "rate_policies": None,
    "crypto_symbol": "BTCUSDT",
    "crypto_timeframe": "1h",
    "crypto_market_type": "spot",
    "fund_symbol": "510300",
    "fund_asset_type": "etf",
    "fund_timeframe": "1d",
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


def configure_multiprocessing_spawn_executable() -> None:
    """固定 Windows spawn 子进程解释器，避免虚拟环境退回 base Python。"""

    multiprocessing.set_executable(sys.executable)


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

    global COLLECTION_PROGRESS_RECORDER, COLLECTION_RUNTIME_ARGS, SOURCE_RATE_LIMITER
    global SOURCE_RATE_POLICY_FINGERPRINT
    configure_logging_from_environment()
    configure_multiprocessing_spawn_executable()
    COLLECTION_RUNTIME_ARGS = args
    rate_policies = getattr(args, "rate_policies", None)
    if rate_policies:
        if hasattr(SOURCE_RATE_LIMITER, "update_policies"):
            SOURCE_RATE_LIMITER.update_policies(rate_policies)
        else:
            SOURCE_RATE_LIMITER = build_source_rate_limiter(rate_policies)
        SOURCE_RATE_POLICY_FINGERPRINT = source_rate_policy_fingerprint(rate_policies)
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
    COLLECTION_PROGRESS_RECORDER = BaseDataTaskProgressRecorder.from_cache_client(
        cache,
        cache_backend=getattr(cache_status, "backend", "redis"),
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
            results.extend(run_ashare_p0(session, args, runtime, session_factory=session_factory))
        if "ashare-p1" in selected_groups:
            results.extend(run_ashare_p1(session, args, runtime, session_factory=session_factory))
        if "ashare-p2" in selected_groups:
            results.extend(run_ashare_p2(session, args, runtime, session_factory=session_factory))
        if "ashare-risk" in selected_groups:
            results.extend(run_ashare_risk(session, args, runtime))
        if "crypto" in selected_groups:
            results.extend(run_crypto(session, args, runtime, session_factory=session_factory))
        if "fund" in selected_groups:
            results.extend(run_fund(session, args, runtime, session_factory=session_factory))

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
        choices=["all", "ashare-p0", "ashare-p1", "ashare-p2", "ashare-risk", "fund", "crypto"],
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
        "--batch-size",
        type=int,
        default=COLLECTION_ARG_DEFAULTS["batch_size"],
        help="按标的补采任务的单批标的数量",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=COLLECTION_ARG_DEFAULTS["max_workers"],
        help="按标的采集任务的批内最大并发数；建议 2-4，避免打满上游请求",
    )
    parser.add_argument(
        "--runtime-scheduler-config-file",
        default=COLLECTION_ARG_DEFAULTS["runtime_scheduler_config_file"],
        help="运行中热读取的调度配置文件；由调度器注入，人工直接运行通常不需要传",
    )
    parser.add_argument(
        "--source-limit",
        type=int,
        default=COLLECTION_ARG_DEFAULTS["source_limit"],
        help="列表型来源的临时采集上限；默认不限制，仅用于诊断或人工限流",
    )
    parser.add_argument(
        "--priority-symbol-limit",
        type=int,
        default=COLLECTION_ARG_DEFAULTS["priority_symbol_limit"],
        help="逐股新闻等重点资产任务的标的数量上限；默认使用 batch_size",
    )
    parser.add_argument(
        "--news-scope",
        choices=["priority", "full_tradeable"],
        default=COLLECTION_ARG_DEFAULTS["news_scope"],
        help="逐股新闻采集范围：priority=盘中重点池，full_tradeable=盘后可交易资产池全量",
    )
    parser.add_argument(
        "--article-retention-days",
        type=int,
        default=COLLECTION_ARG_DEFAULTS["article_retention_days"],
        help="新闻/公告事件热存保留天数；维护任务会删除更早的事件和证据行",
    )
    parser.add_argument(
        "--lookback",
        default=COLLECTION_ARG_DEFAULTS["lookback"],
        help="增量或日历任务回看窗口，例如 72h、30d；不传时使用任务内部默认值",
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
        "--is-closed",
        action=argparse.BooleanOptionalAction,
        default=COLLECTION_ARG_DEFAULTS["is_closed"],
        help="K 线是否为闭合数据；午盘临时日 K 使用 --no-is-closed",
    )
    parser.add_argument(
        "--status",
        default=COLLECTION_ARG_DEFAULTS["status"],
        choices=["available", "partial", "revised", "error"],
        help="K 线写库状态",
    )
    parser.add_argument(
        "--only-failed-or-stale",
        action=argparse.BooleanOptionalAction,
        default=COLLECTION_ARG_DEFAULTS["only_failed_or_stale"],
        help="K 线任务只选择失败重试到期、缺口或过旧标的",
    )
    parser.add_argument(
        "--schedule-failure-retry",
        action=argparse.BooleanOptionalAction,
        default=COLLECTION_ARG_DEFAULTS["schedule_failure_retry"],
        help="K 线失败后是否设置下一次自动重试时间",
    )
    parser.add_argument(
        "--include-adjustment-check",
        action=argparse.BooleanOptionalAction,
        default=COLLECTION_ARG_DEFAULTS["include_adjustment_check"],
        help="K 线任务是否包含复权修正检查",
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
    parser.add_argument(
        "--fund-symbol",
        default=COLLECTION_ARG_DEFAULTS["fund_symbol"],
        help="基金样例代码",
    )
    parser.add_argument(
        "--fund-asset-type",
        default=COLLECTION_ARG_DEFAULTS["fund_asset_type"],
        choices=["etf", "lof", "open_fund"],
        help="基金资产类型",
    )
    parser.add_argument(
        "--fund-timeframe",
        default=COLLECTION_ARG_DEFAULTS["fund_timeframe"],
        help="基金 K 线周期",
    )
    args = parser.parse_args()
    if args.group is None:
        args.group = list(COLLECTION_ARG_DEFAULTS["group"])
    return args


def run_ashare_p0(
    session: Any,
    args: argparse.Namespace,
    runtime: CollectionRuntime,
    *,
    session_factory: Any | None = None,
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
            build_ashare_full_asset_refresh_task(collector, args, runtime),
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
            build_ashare_parallel_realtime_task(session, args, runtime),
        ]
    if task_type in ASHARE_MARKET_BAR_TASK_TYPES:
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
        if should_refresh_asset_universe_before_incremental(session, market="ashare"):
            logger.info("A 股资产池为空或明显不完整，先刷新完整全 A Universe")
            preflight_result = build_ashare_full_asset_refresh_task(collector, args, runtime)
            tasks.append(preflight_result)
            commit_session_if_possible(session)
            if asset_universe_preflight_blocked(preflight_result):
                logger.warning(
                    "A 股资产池预刷新未完成，K 线任务等待下一轮 status=%s error=%s",
                    getattr(preflight_result, "status", None),
                    getattr(preflight_result, "error_message", None),
                )
                return tasks
        symbols = resolve_ashare_collection_symbols(session, args)
        backfill_windows_by_symbol: dict[str, list[tuple[str, str]]] = {}
        if task_type == "market_bars_full_history_backfill":
            required_start_at = parse_ashare_datetime_or_none(
                getattr(args, "ashare_start", None)
            )
            required_end_at = latest_ashare_trading_datetime(
                session,
                parse_ashare_datetime_or_none(getattr(args, "ashare_end", None)),
            )
            if required_start_at is not None and required_end_at is not None:
                try:
                    normalized_backfill_symbols = [
                        symbol
                        for symbol in (
                            normalize_ashare_symbol(str(item or "")) for item in symbols
                        )
                        if symbol
                    ]
                    trusted_leading_gap_symbols: set[str] = set()
                    try:
                        backfill_watermarks = _fetch_data_sync_watermarks(
                            session,
                            [f"ashare:{symbol}" for symbol in normalized_backfill_symbols],
                            data_domain=ASHARE_MARKET_BAR_DATA_DOMAIN,
                            provider=ASHARE_MARKET_BAR_WATERMARK_PROVIDER,
                            timeframe=args.ashare_timeframe,
                        )
                        trusted_leading_gap_symbols = {
                            symbol
                            for symbol in normalized_backfill_symbols
                            if _watermark_covers_request(
                                backfill_watermarks.get(f"ashare:{symbol}"),
                                required_start_at=required_start_at,
                                required_end_at=required_end_at,
                            )
                        }
                    except Exception as exc:
                        logger.debug(
                            "A 股 K 线水位读取失败，回退到原始窗口规划 symbol_count=%s error=%s",
                            len(normalized_backfill_symbols),
                            exc,
                        )
                    backfill_windows_by_symbol = plan_ashare_market_bar_backfill_windows(
                        session,
                        symbols,
                        timeframe=args.ashare_timeframe,
                        required_start_at=required_start_at,
                        required_end_at=required_end_at,
                        trusted_leading_gap_symbols=trusted_leading_gap_symbols,
                    )
                except Exception as exc:
                    logger.warning(
                        "A 股 K 线年度缺口规划失败，本轮退回原始请求窗口 error=%s",
                        exc,
                        exc_info=True,
                    )
        pending_symbols = list(symbols)
        schedule_failure_retry = should_schedule_ashare_failure_retry(args)
        initial_batch_size = runtime_collection_batch_size(args)
        initial_batch_count = estimate_dynamic_batch_count(
            batch_index=0,
            remaining_items=len(pending_symbols),
            batch_size=initial_batch_size,
        )
        logger.info(
            "A 股 K 线补采批次展开 task_type=%s symbols=%s batch_size=%s batch_count=%s "
            "is_closed=%s status=%s",
            task_type,
            len(symbols),
            initial_batch_size,
            initial_batch_count,
            args.is_closed,
            args.status,
        )
        batch_index = 0
        while pending_symbols:
            wait_for_runtime_scheduler_job_resume(args)
            current_batch_size = runtime_collection_batch_size(args)
            current_max_workers = runtime_collection_max_workers(args) if session_factory is not None else 1
            batch_symbols = pending_symbols[:current_batch_size]
            batch_index += 1
            batch_count = estimate_dynamic_batch_count(
                batch_index=batch_index,
                remaining_items=max(len(pending_symbols) - len(batch_symbols), 0),
                batch_size=current_batch_size,
            )
            logger.info(
                "A 股 K 线补采批次开始 batch=%s/%s size=%s max_workers=%s",
                batch_index,
                batch_count,
                len(batch_symbols),
                current_max_workers,
            )
            def collect_symbol(
                symbol: str,
                worker_count: int = current_max_workers,
            ) -> CollectionTaskResult:
                wait_for_runtime_scheduler_job_resume(args)
                should_skip, skip_result = should_skip_ashare_market_bar_for_open_circuit(
                    runtime,
                    symbol,
                    force=args.force_provider,
                )
                if should_skip and skip_result is not None:
                    return skip_result
                ohlcv_limit = ashare_market_bar_source_limit(args)
                planned_windows = backfill_windows_by_symbol.get(symbol)
                if planned_windows is not None and not planned_windows:
                    return CollectionTaskResult(
                        task="ashare_p0_ohlcv",
                        status="skipped",
                        raw_record_id=None,
                        item_count=0,
                        error_message="10 年目标窗口已完整覆盖",
                        payload={"backfill_windows": []},
                    )
                request_windows = planned_windows or [
                    (args.ashare_start, args.ashare_end)
                ]

                def execute_window(
                    target_collector: AshareP0Collector,
                    *,
                    window_start: str | None,
                    window_end: str | None,
                ) -> CollectionTaskResult:
                    return runtime.run_task(
                        task="ashare_p0_ohlcv",
                        provider_key=ashare_ohlcv_provider_key(symbol),
                        parameters={
                            "symbol": symbol,
                            "timeframe": args.ashare_timeframe,
                            "start": window_start,
                            "end": window_end,
                            "adjust": args.ashare_adjust,
                            "limit": ohlcv_limit,
                            "is_closed": args.is_closed,
                            "status": args.status,
                        },
                        force=args.force_provider,
                        collect=lambda: target_collector.collect_ohlcv(
                            symbol=symbol,
                            timeframe=args.ashare_timeframe,
                            start=window_start,
                            end=window_end,
                            limit=ohlcv_limit,
                            adjust=args.ashare_adjust,
                            is_closed=args.is_closed,
                            status=args.status,
                            source_gate=ashare_kline_source_gate,
                        ),
                    )

                window_results: list[CollectionTaskResult] = []
                for window_start, window_end in request_windows:
                    wait_for_runtime_scheduler_job_resume(args)
                    if session_factory is not None and worker_count > 1:
                        with session_scope(session_factory) as worker_session:
                            worker_collector = AshareP0Collector(worker_session)
                            window_results.append(
                                execute_window(
                                    worker_collector,
                                    window_start=window_start,
                                    window_end=window_end,
                                )
                            )
                    else:
                        window_results.append(
                            execute_window(
                                collector,
                                window_start=window_start,
                                window_end=window_end,
                            )
                        )
                if len(window_results) == 1:
                    result = window_results[0]
                else:
                    result = merge_ashare_market_bar_window_results(
                        window_results,
                        windows=request_windows,
                    )
                return attach_ashare_market_bar_retry_payload(
                    result,
                    schedule_retry=schedule_failure_retry,
                )

            batch_enriched_results: list[CollectionTaskResult | None] = [None] * len(batch_symbols)

            def handle_symbol_result(
                symbol: str,
                result: CollectionTaskResult,
                index: int,
                result_batch_index: int = batch_index,
                result_batch_count: int = batch_count,
                result_batch_size: int = current_batch_size,
                result_slots: list[CollectionTaskResult | None] = batch_enriched_results,
                worker_count: int = current_max_workers,
            ) -> None:
                enriched_result = attach_batch_payload(
                    result,
                    batch_index=result_batch_index,
                    batch_count=result_batch_count,
                    batch_size=result_batch_size,
                    symbol_count=len(symbols),
                    sync_task_type=task_type,
                    requested_start=getattr(args, "ashare_start", None),
                    requested_end=getattr(args, "ashare_end", None),
                )
                result_slots[index] = enriched_result
                watermark_session_factory = session_factory if worker_count > 1 else None
                record_ashare_market_bar_watermark(
                    session,
                    symbol=symbol,
                    timeframe=args.ashare_timeframe,
                    result=enriched_result,
                    session_factory=watermark_session_factory,
                    schedule_retry=schedule_failure_retry,
                )

            batch_results = run_symbol_task_batch(
                batch_symbols,
                max_workers=current_max_workers,
                collect_symbol=collect_symbol,
                on_symbol_result=handle_symbol_result,
                progress=collection_progress_context(args)[0],
                job_name=collection_progress_context(args)[1],
                run_id=collection_progress_context(args)[2],
                stage_key="ashare_p0_ohlcv",
                batch_index=batch_index,
                batch_count=batch_count,
                batch_size=current_batch_size,
                total_items=len(symbols),
                should_stop_before_symbol=(
                    build_runtime_batch_stop_checker(
                        args,
                        batch_size=current_batch_size,
                        max_workers=current_max_workers,
                        compare_max_workers=session_factory is not None,
                    )
                    if current_max_workers == 1
                    else None
                ),
            )
            processed_count = len(batch_results)
            del pending_symbols[:processed_count]
            for index, result in enumerate(batch_results):
                tasks.append(batch_enriched_results[index] or result)
            commit_session_if_possible(session)
        if task_type == "market_bars_close_final":
            statuses = {str(getattr(item, "status", "")) for item in tasks}
            if statuses and statuses.issubset({"available", "skipped"}):
                try:
                    cleared = AssetRepository(session).clear_intraday_quote_latest(market="ashare")
                    logger.info("收盘最终日 K 完成，已清理 A 股盘中临时行情 rows=%s", cleared)
                except Exception as exc:  # noqa: BLE001 - 清理失败必须可观测但不掩盖日 K 结果
                    logger.warning("收盘后清理盘中临时行情失败 error=%s", exc, exc_info=True)
            else:
                logger.warning("收盘最终日 K 存在失败结果，暂不清理盘中临时行情 statuses=%s", statuses)
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
        build_ashare_full_asset_refresh_task(collector, args, runtime),
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
                "is_closed": args.is_closed,
                "status": args.status,
            },
            force=args.force_provider,
            collect=lambda: collector.collect_ohlcv(
                symbol=args.ashare_symbol,
                timeframe=args.ashare_timeframe,
                start=args.ashare_start,
                end=args.ashare_end,
                limit=args.limit,
                adjust=args.ashare_adjust,
                is_closed=args.is_closed,
                status=args.status,
                source_gate=ashare_kline_source_gate,
            ),
        ),
    ]


@dataclass(frozen=True)
class RealtimeQuotePartition:
    """一次实时行情执行对应的稳定目标分区。"""

    symbols: tuple[str, ...]
    target_symbols: tuple[str, ...]
    metrics: JsonDict
    next_partition_payload: JsonDict | None


def build_realtime_quote_partition(
    *,
    universe: Collection[str],
    scope: str,
    limit: int | None,
    batch_size: int,
    partition_cursor: int,
) -> RealtimeQuotePartition:
    """按 scope 生成重点池或全市场稳定分区。"""

    normalized_scope = str(scope).strip()
    if normalized_scope not in {"priority", "market_sweep"}:
        raise ValueError("实时行情 scope 只能是 priority 或 market_sweep")
    normalized_batch_size = int(batch_size)
    normalized_cursor = int(partition_cursor)
    if normalized_batch_size < 1 or normalized_cursor < 0:
        raise ValueError("batch_size 必须大于 0，partition_cursor 不能小于 0")
    if limit is not None and int(limit) < 1:
        raise ValueError("实时行情 limit 必须为正整数或 null")
    normalized_universe = tuple(
        sorted({str(symbol).strip() for symbol in universe if str(symbol).strip()})
    )
    if normalized_scope == "priority":
        selected = normalized_universe[: int(limit) if limit is not None else len(normalized_universe)]
        return RealtimeQuotePartition(
            symbols=selected,
            target_symbols=selected,
            metrics={
                "scope": normalized_scope,
                "partition_cursor": 0,
                "partition_count": 1 if selected else 0,
                "target_count": len(selected),
            },
            next_partition_payload=None,
        )

    target_count = len(normalized_universe)
    partition_count = (
        (target_count + normalized_batch_size - 1) // normalized_batch_size
        if target_count
        else 0
    )
    if normalized_cursor >= partition_count:
        selected = ()
    else:
        start = normalized_cursor * normalized_batch_size
        selected = normalized_universe[start : start + normalized_batch_size]
    next_payload = (
        {
            "partition_cursor": normalized_cursor + 1,
            "partition_count": partition_count,
        }
        if normalized_cursor + 1 < partition_count
        else None
    )
    return RealtimeQuotePartition(
        symbols=selected,
        target_symbols=normalized_universe,
        metrics={
            "scope": normalized_scope,
            "partition_cursor": normalized_cursor,
            "partition_count": partition_count,
            "target_count": target_count,
        },
        next_partition_payload=next_payload,
    )


def build_realtime_quote_coverage_metrics(
    *,
    target_symbols: Collection[str],
    requested_symbols: Collection[str],
    rows: Collection[Mapping[str, Any]],
    written_count: int,
    captured_at: datetime,
    freshness_seconds: int,
    source_statuses: Mapping[str, str],
) -> JsonDict:
    """按唯一资产统计覆盖与滞后，避免双源行数虚增覆盖率。"""

    target_ids = {
        f"ashare:{symbol}"
        for raw_symbol in target_symbols
        if (symbol := normalize_ashare_symbol(str(raw_symbol)))
    }
    latest_lag_by_asset: dict[str, float] = {}
    for row in rows:
        asset_id = str(row.get("asset_id") or "").strip()
        as_of = row.get("as_of")
        if asset_id not in target_ids or not isinstance(as_of, datetime):
            continue
        normalized_as_of = as_of if as_of.tzinfo else as_of.replace(tzinfo=UTC)
        lag = max(0.0, (captured_at - normalized_as_of.astimezone(UTC)).total_seconds())
        previous = latest_lag_by_asset.get(asset_id)
        latest_lag_by_asset[asset_id] = lag if previous is None else min(previous, lag)
    fresh_count = sum(
        lag <= max(1, int(freshness_seconds))
        for lag in latest_lag_by_asset.values()
    )
    target_count = len(target_ids)
    return {
        "target_count": target_count,
        "requested_count": len({str(item) for item in requested_symbols}),
        "written_count": max(0, int(written_count)),
        "fresh_count": fresh_count,
        "coverage_ratio": fresh_count / target_count if target_count else 0.0,
        "max_lag_seconds": (
            round(max(latest_lag_by_asset.values()), 3)
            if latest_lag_by_asset
            else None
        ),
        "source_statuses": dict(source_statuses),
    }


def build_ashare_parallel_realtime_task(
    session: Any,
    args: argparse.Namespace,
    runtime: CollectionRuntime,
) -> CollectionTaskResult:
    """执行重点标的 gotdx/AKShare 并行快照，只覆盖盘中临时行情。"""

    scope = str(getattr(args, "scope", "priority") or "priority").strip()
    limit = getattr(args, "limit", None)
    batch_size = max(1, int(getattr(args, "batch_size", 200) or 200))
    partition_cursor = max(0, int(getattr(args, "partition_cursor", 0) or 0))
    if scope == "priority":
        max_symbols = max(1, int(limit if limit is not None else batch_size))
        universe = resolve_realtime_quote_symbols(session, max_symbols=max_symbols)
    else:
        universe = resolve_realtime_market_sweep_symbols(session)
    partition = build_realtime_quote_partition(
        universe=universe,
        scope=scope,
        limit=limit,
        batch_size=batch_size,
        partition_cursor=partition_cursor,
    )
    symbols = list(partition.symbols)
    gotdx_url = str(
        getattr(args, "gotdx_gateway_url", None)
        or os.getenv("FINANCE_AGENT_GOTDX_URL", "http://127.0.0.1:8790")
    ).strip()
    parameters = {
        "symbols": symbols,
        "limit": limit,
        **partition.metrics,
        "sources": ["gotdx:tdx_main", "akshare:stock_zh_a_spot"],
    }

    def collect() -> ArchivedProviderResult:
        captured_at = datetime.now(tz=UTC)
        if not symbols:
            return ArchivedProviderResult(
                result=AssetListResult(
                    provider_name="gotdx+akshare:parallel",
                    status="unavailable",
                    collected_at=captured_at,
                    assets=[],
                    error_message="实时行情目标资产池为空。",
                    payload={
                        "actual_source": [],
                        "source_statuses": {},
                        "rows_written": 0,
                        "metrics": {
                            **partition.metrics,
                            "requested_count": 0,
                            "written_count": 0,
                            "fresh_count": 0,
                            "coverage_ratio": 0.0,
                            "max_lag_seconds": None,
                            "source_statuses": {},
                        },
                        "temporary_storage": "intraday_quote_latest",
                    },
                ),
                raw_record_id=None,
            )
        evaluation_id = (
            "snapshot:ashare_realtime_quotes:parallel:"
            f"{captured_at.strftime('%Y%m%dT%H%M%S.%fZ')}"
        )
        gotdx_provider = GotdxGatewayProvider(base_url=gotdx_url)
        akshare_provider = AkshareProvider()
        evaluator = ParallelQuoteEvaluator(
            lambda requested: _fetch_gotdx_quote_rows(gotdx_provider, requested),
            lambda requested: _fetch_akshare_quote_rows(akshare_provider, requested),
        )
        result = evaluator.evaluate(symbols=tuple(symbols), data_snapshot_id=evaluation_id)
        statuses = {
            "gotdx:tdx_main": "error" if "gotdx:tdx_main" in result.errors else "available",
            "akshare:stock_zh_a_spot": (
                "error" if "akshare:stock_zh_a_spot" in result.errors else "available"
            ),
        }
        status = parallel_quote_status(result=result, source_statuses=statuses)
        as_of_candidates = [
            row["as_of"] for row in result.rows if isinstance(row.get("as_of"), datetime)
        ]
        as_of = max(as_of_candidates, default=captured_at)
        captured_at = max(captured_at, as_of)
        repository = AssetRepository(session)
        coverage_rows: Collection[Mapping[str, Any]] = result.rows
        list_latest = getattr(repository, "list_intraday_quote_latest", None)
        if callable(list_latest):
            try:
                coverage_rows = (
                    *list_latest(
                        asset_ids=tuple(
                            f"ashare:{symbol}"
                            for raw_symbol in partition.target_symbols
                            if (symbol := normalize_ashare_symbol(str(raw_symbol)))
                        ),
                        market="ashare",
                    ),
                    *result.rows,
                )
            except Exception as exc:  # noqa: BLE001 - 覆盖查询失败不丢弃本轮采集事实
                logger.warning("读取实时行情累计覆盖失败，回退本轮返回行 error=%s", exc)
        freshness_seconds = 120 if scope == "priority" else 10 * 60
        coverage_metrics = build_realtime_quote_coverage_metrics(
            target_symbols=partition.target_symbols,
            requested_symbols=symbols,
            rows=coverage_rows,
            written_count=len(result.rows),
            captured_at=captured_at,
            freshness_seconds=freshness_seconds,
            source_statuses=statuses,
        )
        metrics = {**result.metrics, **partition.metrics, **coverage_metrics}
        if (
            status == "available"
            and coverage_metrics["max_lag_seconds"] is not None
            and coverage_metrics["max_lag_seconds"] > freshness_seconds
        ):
            status = "stale"
        elif status == "available" and coverage_metrics["coverage_ratio"] < 1.0:
            status = "partial"
        snapshot = build_data_snapshot(
            snapshot_type="ashare_realtime_quotes",
            market="ashare",
            as_of=as_of,
            captured_at=captured_at,
            provider="gotdx+akshare:parallel",
            provider_version="parallel-v1",
            quality_status=status,
            payload={
                "rows": list(result.rows),
                "metrics": metrics,
                "errors": result.errors,
            },
            metadata={
                "source_statuses": statuses,
                "symbols": list(symbols),
                "evaluation_id": evaluation_id,
                "temporary_storage": "intraday_quote_latest",
            },
        )
        DataSnapshotRepository(session).insert_snapshot(snapshot)
        persisted_rows = tuple(
            dict(row, data_snapshot_id=snapshot.data_snapshot_id) for row in result.rows
        )
        rows_written = repository.upsert_intraday_quote_latest(persisted_rows)
        assets = [
            AssetData(
                asset_id=str(row["asset_id"]),
                symbol=str(row["symbol"]),
                name=str(row["symbol"]),
                market="ashare",
                asset_type="stock",
                payload=dict(row),
            )
            for row in result.rows
        ]
        provider_result = AssetListResult(
            provider_name="gotdx+akshare:parallel",
            status=status,
            collected_at=captured_at,
            assets=assets,
            error_message=("; ".join(f"{key}: {value}" for key, value in result.errors.items()) or None),
            payload={
                "actual_source": list(statuses),
                "source_statuses": statuses,
                "data_snapshot_id": snapshot.data_snapshot_id,
                "rows_written": rows_written,
                "metrics": metrics,
                "errors": result.errors,
                "temporary_storage": "intraday_quote_latest",
            },
        )
        return ArchivedProviderResult(result=provider_result, raw_record_id=None)

    task_result = runtime.run_task(
        task="ashare_realtime_quotes_parallel",
        provider_key="gotdx:tdx_main+akshare:stock_zh_a_spot",
        parameters=parameters,
        force=bool(getattr(args, "force_provider", False)),
        collect=collect,
    )
    if partition.next_partition_payload is None or not isinstance(
        task_result, CollectionTaskResult
    ):
        return task_result
    return CollectionTaskResult(
        task=task_result.task,
        status=task_result.status,
        raw_record_id=task_result.raw_record_id,
        item_count=task_result.item_count,
        error_message=task_result.error_message,
        payload={
            **task_result.payload,
            "next_partition_payload": partition.next_partition_payload,
        },
    )


def parallel_quote_status(*, result: Any, source_statuses: Mapping[str, str]) -> str:
    """汇总双源质量；冲突优先于部分可用，避免闸门误放行。"""

    if result.metrics.get("conflicts"):
        return "conflict"
    if not result.rows:
        return "unavailable"
    if result.errors or any(status != "available" for status in source_statuses.values()):
        return "partial"
    return "available"


def resolve_realtime_quote_symbols(session: Any, *, max_symbols: int = 100) -> list[str]:
    """从现有可交易资产中生成重点标的集合，控制双源请求量。"""

    try:
        assets = TradeableAssetEligibilityService().filter_tradeable_assets(
            AssetRepository(session).find_by_market("ashare")
        )
    except Exception as exc:  # noqa: BLE001 - 资产池不可用时交由 Provider 质量门处理
        logger.warning("解析实时重点标的失败 error=%s", exc)
        return []
    eligible_by_symbol = {
        normalize_ashare_symbol(str(asset.symbol or "")): asset
        for asset in assets
        if normalize_ashare_symbol(str(asset.symbol or ""))
    }
    symbols: list[str] = []

    def add_symbol(value: Any) -> None:
        symbol = normalize_ashare_symbol(str(value or ""))
        if symbol and symbol in eligible_by_symbol and symbol not in symbols:
            symbols.append(symbol)

    try:
        for position in session.scalars(
            select(PositionORM).where(
                PositionORM.market == "ashare",
                PositionORM.status.in_(("open", "available", "active")),
            )
        ):
            add_symbol(position.symbol)
        for item in session.scalars(
            select(WatchlistItemORM).where(
                WatchlistItemORM.market == "ashare",
                WatchlistItemORM.status == "active",
            )
        ):
            add_symbol(item.symbol)
        recent_cutoff = datetime.now(tz=UTC) - timedelta(days=7)
        for recommendation in session.scalars(
            select(AssetRecommendationORM).where(
                AssetRecommendationORM.market == "ashare",
                AssetRecommendationORM.created_at >= recent_cutoff,
                AssetRecommendationORM.action.in_(("buy_candidate", "strong_buy")),
            )
        ):
            add_symbol(recommendation.symbol)
        for event in session.scalars(
            select(AssistantTriggerEventORM).where(
                AssistantTriggerEventORM.asset_id.is_not(None),
                AssistantTriggerEventORM.triggered_at >= recent_cutoff,
                AssistantTriggerEventORM.status.in_(("pending", "dispatched")),
            )
        ):
            asset_id = str(event.asset_id or "")
            asset = next(
                (
                    candidate
                    for candidate in eligible_by_symbol.values()
                    if candidate.asset_id == asset_id
                ),
                None,
            )
            add_symbol(getattr(asset, "symbol", None))
    except Exception as exc:  # noqa: BLE001 - 重点集合不可用时仍可用全量资产池
        logger.warning("解析持仓/观察池/推荐重点标的失败，将回退资产池 error=%s", exc)

    for asset in sorted(assets, key=lambda item: normalize_ashare_symbol(str(item.symbol or ""))):
        symbol = normalize_ashare_symbol(str(asset.symbol or ""))
        if symbol and symbol not in symbols:
            symbols.append(symbol)
        if len(symbols) >= max(1, int(max_symbols)):
            break
    return symbols


def resolve_realtime_market_sweep_symbols(session: Any) -> list[str]:
    """从资产主数据解析稳定排序的全量可交易 A 股标的。"""

    try:
        assets = TradeableAssetEligibilityService().filter_tradeable_assets(
            AssetRepository(session).find_by_market("ashare")
        )
    except Exception as exc:  # noqa: BLE001 - 资产池故障由采集结果显式降级
        logger.warning("解析实时行情全市场标的失败 error=%s", exc, exc_info=True)
        return []
    return sorted(
        {
            symbol
            for asset in assets
            if (symbol := normalize_ashare_symbol(str(getattr(asset, "symbol", "") or "")))
            and is_tradeable_ashare_symbol(symbol)
        }
    )


def _fetch_gotdx_quote_rows(
    provider: GotdxGatewayProvider,
    symbols: tuple[str, ...],
) -> list[JsonDict]:
    """把 gotdx 标准对象转换为临时行情表行。"""

    rows: list[JsonDict] = []
    for quote in provider.fetch_quotes(symbols):
        rows.append(
            {
                "asset_id": quote.asset_id,
                "symbol": quote.symbol,
                "market": quote.market,
                "as_of": quote.as_of,
                "captured_at": quote.received_at,
                "freshness_ms": max(
                    0,
                    int((quote.received_at - quote.server_timestamp).total_seconds() * 1000),
                ),
                "last_price": quote.last_price,
                "prev_close": quote.prev_close,
                "open": quote.open_price,
                "high": quote.high,
                "low": quote.low,
                "volume": quote.volume,
                "amount": quote.amount,
                "turnover_rate": quote.turnover_rate,
                "change_amount": quote.change_amount,
                "change_percent": quote.change_percent,
                "bid_price": quote.bid_price,
                "ask_price": quote.ask_price,
                "status": quote.status,
                "quality_status": quote.quality_status,
                "payload": quote.payload,
            }
        )
    return rows


def _fetch_akshare_quote_rows(
    provider: AkshareProvider,
    symbols: tuple[str, ...],
) -> list[JsonDict]:
    """把 AKShare 全市场截面过滤为重点标的临时行情行。"""

    result = provider.fetch_assets(limit=None)
    if result.status not in {"available", "partial"}:
        raise RuntimeError(
            f"AKShare 行情 Provider 不可用 status={result.status} error={result.error_message}"
        )
    wanted = {
        normalize_ashare_symbol(str(symbol or ""))
        for symbol in symbols
        if normalize_ashare_symbol(str(symbol or ""))
    }
    rows: list[JsonDict] = []
    for asset in result.assets:
        symbol = normalize_ashare_symbol(str(asset.symbol or ""))
        if symbol not in wanted:
            continue
        payload = dict(asset.payload or {})
        rows.append(
            {
                "asset_id": asset.asset_id,
                "symbol": symbol,
                "market": "ashare",
                "as_of": result.collected_at,
                "captured_at": result.collected_at,
                "freshness_ms": 0,
                "last_price": _quote_decimal(payload, ("最新价", "最新")),
                "prev_close": _quote_decimal(payload, ("昨收", "昨收价")),
                "open": _quote_decimal(payload, ("今开", "开盘")),
                "high": _quote_decimal(payload, ("最高",)),
                "low": _quote_decimal(payload, ("最低",)),
                "volume": _quote_decimal(payload, ("成交量",)),
                "amount": _quote_decimal(payload, ("成交额",)),
                "turnover_rate": _quote_decimal(payload, ("换手率",)),
                "change_amount": _quote_decimal(payload, ("涨跌额",)),
                "change_percent": _quote_decimal(payload, ("涨跌幅", "日增长率")),
                "status": asset.status,
                "quality_status": "available" if asset.status == "available" else asset.status,
                "payload": payload,
            }
        )
    return rows


def _quote_decimal(payload: Mapping[str, Any], keys: tuple[str, ...]) -> Decimal | None:
    """从 Provider payload 读取一个可选数值。"""

    for key in keys:
        value = payload.get(key)
        if value is None or str(value).strip() in {"", "-", "--", "nan", "None"}:
            continue
        try:
            return Decimal(str(value).replace(",", "").replace("%", "").strip())
        except Exception:  # noqa: BLE001 - 单字段异常不应丢弃整批行情
            continue
    return None


def build_ashare_full_asset_refresh_task(
    collector: AshareP0Collector,
    args: argparse.Namespace,
    runtime: CollectionRuntime,
) -> CollectionTaskResult:
    """构建完整 A 股资产池刷新任务；该任务不受单批采集大小限制。"""

    progress, job_name, run_id = collection_progress_context(args)
    if progress is not None and job_name and run_id:
        progress.batch_started(
            job_name=job_name,
            run_id=run_id,
            stage_key="ashare_p0_assets",
            total_items=1,
            batch_index=1,
            batch_count=1,
            batch_size=1,
        )
    try:
        result = runtime.run_task(
            task="ashare_p0_assets",
            provider_key="stock_zh_a_spot",
            parameters={"limit": None},
            force=args.force_provider,
            collect=lambda: collector.collect_assets(
                universe_id="universe:base:ashare:p0:all_a",
                universe_name="基础数据全 A 候选池",
                strategy_context="base_data_collect",
                limit=None,
            ),
        )
    except Exception as exc:
        if progress is not None and job_name and run_id:
            progress.symbol_completed(
                job_name=job_name,
                run_id=run_id,
                stage_key="ashare_p0_assets",
                symbol="全市场资产池",
                status="failed",
                item_count=0,
                batch_index=1,
                batch_count=1,
                error_message=str(exc),
            )
        raise
    if progress is not None and job_name and run_id:
        progress.symbol_completed(
            job_name=job_name,
            run_id=run_id,
            stage_key="ashare_p0_assets",
            symbol="全市场资产池",
            status=result_status_name(result),
            item_count=result_item_count(result),
            batch_index=1,
            batch_count=1,
            error_message=result_error_message(result),
        )
    return result


def asset_universe_preflight_blocked(result: Any) -> bool:
    """资产池预刷新未成功时，增量任务应等待下一轮，避免退化为单票采集。"""

    return result_status_name(result) in {"failed", "locked", "skipped"}


def run_ashare_p1(
    session: Any,
    args: argparse.Namespace,
    runtime: CollectionRuntime,
    *,
    session_factory: Any | None = None,
) -> list[CollectionTaskResult]:
    """执行 A 股 P1 行业、概念、资金流和新闻采集。"""

    collector = AshareP1Collector(session)
    task_type = task_type_name(args)
    if task_type == "universe_refresh":
        return build_ashare_p1_universe_tasks(collector, args, runtime)
    if task_type == "capital_flow_backfill":
        symbol = normalize_ashare_symbol(str(args.ashare_symbol or ""))
        if not symbol:
            return [
                CollectionTaskResult(
                    task="ashare_p1_individual_flow",
                    status="failed",
                    raw_record_id=None,
                    item_count=0,
                    error_message="资金流历史补跑缺少股票代码。",
                    payload={"provider_key": "stock_individual_fund_flow"},
                )
            ]
        start_at = parse_ashare_datetime_or_none(args.ashare_start)
        end_at = parse_ashare_datetime_or_none(args.ashare_end)
        return [
            runtime.run_task(
                task="ashare_p1_individual_flow",
                provider_key="stock_individual_fund_flow",
                parameters={
                    "symbol": symbol,
                    "start": args.ashare_start,
                    "end": args.ashare_end,
                    "limit": args.limit,
                },
                force=args.force_provider,
                collect=lambda: collector.collect_individual_flow(
                    symbol=symbol,
                    start_date=start_at.date() if start_at else None,
                    end_date=end_at.date() if end_at else None,
                    limit=args.limit,
                ),
            )
        ]
    if task_type == "capital_flow_refresh":
        source_limit = list_source_limit(args)
        if not ashare_capital_flow_watermark_allows_collection(session, indicator=args.flow_window):
            return [
                CollectionTaskResult(
                    task="ashare_p1_flow_rank",
                    status="skipped",
                    raw_record_id=None,
                    item_count=0,
                    error_message="资金流榜单处于失败冷却期，等待下次重跑。",
                    payload={
                        "provider_key": ASHARE_CAPITAL_FLOW_PROVIDER,
                        "indicator": args.flow_window,
                    },
                )
            ]
        result = runtime.run_task(
            task="ashare_p1_flow_rank",
            provider_key=ASHARE_CAPITAL_FLOW_PROVIDER,
            parameters={"indicator": args.flow_window, "limit": source_limit},
            force=args.force_provider,
            collect=lambda: collector.collect_flow_rank(
                indicator=args.flow_window,
                limit=source_limit,
            ),
        )
        record_ashare_capital_flow_watermark(
            session,
            indicator=args.flow_window,
            result=result,
        )
        return [result]
    if task_type == "northbound_flow_refresh":
        source_limit = list_source_limit(args)
        results = [
            runtime.run_task(
                task="ashare_p1_northbound_flow",
                provider_key=ASHARE_NORTHBOUND_FLOW_PROVIDER,
                parameters={"symbol": "北向资金", "limit": source_limit},
                force=args.force_provider,
                collect=lambda: collector.collect_northbound_flow(
                    symbol="北向资金",
                    limit=source_limit,
                ),
            )
        ]
        for symbol in resolve_ashare_northbound_symbols(session, args):
            result = runtime.run_task(
                task="ashare_p1_northbound_flow",
                provider_key=ASHARE_NORTHBOUND_INDIVIDUAL_PROVIDER,
                parameters={"symbol": symbol, "limit": source_limit},
                force=args.force_provider,
                collect=lambda symbol=symbol: collector.collect_northbound_flow(
                    symbol=symbol,
                    limit=source_limit,
                ),
            )
            record_ashare_northbound_individual_watermark(
                session,
                symbol=symbol,
                result=result,
            )
            results.append(result)
        return results
    if task_type == "event_refresh":
        source_limit = list_source_limit(args)
        results = [
            *build_ashare_stock_news_tasks(
                session,
                collector,
                args,
                runtime,
                session_factory=session_factory,
            ),
            runtime.run_task(
                task="ashare_p1_notice_reports",
                provider_key="stock_notice_report",
                parameters={"symbol": "全部", "date": args.risk_end, "limit": source_limit},
                force=args.force_provider,
                collect=lambda: run_rate_limited_collection(
                    "stock_notice_report",
                    lambda: collector.collect_notice_reports(
                        symbol="全部",
                        date=args.risk_end,
                        limit=source_limit,
                    ),
                ),
            ),
        ]
        record_ashare_event_watermark(session, results=results)
        return results
    if task_type == "event_article_enrichment":
        return build_ashare_news_article_enrichment_tasks(
            session,
            collector,
            args,
            runtime,
            session_factory=session_factory,
        )
    if task_type == "event_article_retention":
        return [run_event_article_retention(session, args)]
    return build_ashare_p1_default_tasks(
        session,
        collector,
        args,
        runtime,
        session_factory=session_factory,
    )


def run_event_article_retention(
    session: Any,
    args: argparse.Namespace,
) -> CollectionTaskResult:
    """删除过期新闻/公告事件和证据整行，保留原始审计记录。"""

    retention_days = int(
        getattr(args, "article_retention_days", DEFAULT_ARTICLE_FULL_TEXT_RETENTION_DAYS)
        or DEFAULT_ARTICLE_FULL_TEXT_RETENTION_DAYS
    )
    if retention_days <= 0:
        raise ValueError("article_retention_days 必须大于 0")

    cutoff = datetime.now(tz=UTC) - timedelta(days=retention_days)
    result = EventRepository(session).delete_expired_article_events(cutoff=cutoff)
    item_count = int(result.get("total") or 0)
    payload: JsonDict = {
        **result,
        "cutoff": cutoff.isoformat(),
        "article_retention_days": retention_days,
    }
    logger.info(
        "过期新闻/公告事件清理完成 cutoff=%s retention_days=%s event_records=%s evidence=%s total=%s",
        cutoff.isoformat(),
        retention_days,
        result.get("event_records", 0),
        result.get("evidence", 0),
        item_count,
    )
    return CollectionTaskResult(
        task="ashare_p1_news_retention",
        status="available",
        raw_record_id=None,
        item_count=item_count,
        error_message=None,
        payload=payload,
    )


def build_ashare_p1_universe_tasks(
    collector: AshareP1Collector,
    args: argparse.Namespace,
    runtime: CollectionRuntime,
) -> list[CollectionTaskResult]:
    """按目录自动展开 A 股 P1 的 universe_refresh 任务。"""

    tasks: list[CollectionTaskResult] = []
    progress, job_name, run_id = collection_progress_context(args)
    catalog_stage_key = "ashare_p1_catalog_discovery"
    catalog_batch_count = 3
    if progress is not None and job_name and run_id:
        progress.stage_planned(
            job_name=job_name,
            run_id=run_id,
            stage_key=catalog_stage_key,
            total_items=catalog_batch_count,
        )

    def record_catalog_started(*, symbol: str, batch_index: int) -> None:
        if progress is None or not job_name or not run_id:
            return
        progress.batch_started(
            job_name=job_name,
            run_id=run_id,
            stage_key=catalog_stage_key,
            total_items=catalog_batch_count,
            batch_index=batch_index,
            batch_count=catalog_batch_count,
            batch_size=1,
        )

    def record_catalog_completed(
        *,
        symbol: str,
        status: str,
        item_count: int,
        batch_index: int,
        error_message: str | None = None,
    ) -> None:
        if progress is None or not job_name or not run_id:
            return
        progress.symbol_completed(
            job_name=job_name,
            run_id=run_id,
            stage_key=catalog_stage_key,
            symbol=symbol,
            status=status,
            item_count=item_count,
            batch_index=batch_index,
            batch_count=catalog_batch_count,
            error_message=error_message,
        )

    def fetch_catalog_with_progress(
        *,
        symbol: str,
        batch_index: int,
        fetch: Any,
        key: str,
        default: list[Any],
    ) -> list[Any]:
        record_catalog_started(symbol=symbol, batch_index=batch_index)
        try:
            entries = fetch_catalog_entries(fetch(), key=key, default=default)
        except Exception as exc:
            record_catalog_completed(
                symbol=symbol,
                status="failed",
                item_count=0,
                batch_index=batch_index,
                error_message=str(exc),
            )
            raise
        record_catalog_completed(
            symbol=symbol,
            status="available",
            item_count=len(entries),
            batch_index=batch_index,
        )
        return entries
    index_sources = fetch_catalog_with_progress(
        symbol="index_catalog",
        batch_index=1,
        fetch=lambda: collector.sector_provider.fetch_index_catalog(
            limit=positive_limit(args.index_catalog_limit)
        ),
        key="indexes",
        default=[{"code": "000300", "name": "沪深300"}],
    )
    industry_sources = fetch_catalog_with_progress(
        symbol="industry_catalog",
        batch_index=2,
        fetch=lambda: collector.sector_provider.fetch_industry_names(
            limit=positive_limit(args.industry_catalog_limit)
        ),
        key="names",
        default=[args.industry],
    )
    concept_sources = fetch_catalog_with_progress(
        symbol="concept_catalog",
        batch_index=3,
        fetch=lambda: collector.sector_provider.fetch_concept_names(
            limit=positive_limit(args.concept_catalog_limit)
        ),
        key="names",
        default=[args.concept],
    )
    member_limit = normalize_member_limit(args.catalog_member_limit)
    source_limit = list_source_limit(args)
    index_progress_items = [
        item for item in index_sources if str(item.get("code") or "").strip()
    ]
    industry_progress_items = [
        item for item in industry_sources if str(item).strip()
    ]
    concept_progress_items = [
        item for item in concept_sources if str(item).strip()
    ]
    if progress is not None and job_name and run_id:
        for stage_key, total_items in [
            ("ashare_p1_index_members", len(index_progress_items)),
            ("ashare_p1_industry_members", len(industry_progress_items)),
            ("ashare_p1_concept_members", len(concept_progress_items)),
            ("ashare_p1_flow_rank", 1),
        ]:
            progress.stage_planned(
                job_name=job_name,
                run_id=run_id,
                stage_key=stage_key,
                total_items=total_items,
            )

    def record_p1_stage_started(
        *,
        stage_key: str,
        batch_index: int,
        batch_count: int,
    ) -> None:
        if progress is None or not job_name or not run_id:
            return
        progress.batch_started(
            job_name=job_name,
            run_id=run_id,
            stage_key=stage_key,
            total_items=batch_count,
            batch_index=batch_index,
            batch_count=batch_count,
            batch_size=1,
        )

    def record_p1_stage_result(
        *,
        stage_key: str,
        symbol: str,
        result: CollectionTaskResult,
        batch_index: int,
        batch_count: int,
    ) -> None:
        if progress is None or not job_name or not run_id:
            return
        emit_symbol_progress(
            progress,
            job_name=job_name,
            run_id=run_id,
            stage_key=stage_key,
            symbol=symbol,
            result=result,
            batch_index=batch_index,
            batch_count=batch_count,
        )

    for index, item in enumerate(index_progress_items, start=1):
        index_code = str(item.get("code") or "").strip()
        index_name = str(item.get("name") or index_code).strip()
        stage_key = "ashare_p1_index_members"
        batch_count = len(index_progress_items)
        record_p1_stage_started(
            stage_key=stage_key,
            batch_index=index,
            batch_count=batch_count,
        )
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
        record_p1_stage_result(
            stage_key=stage_key,
            symbol=index_code,
            result=tasks[-1],
            batch_index=index,
            batch_count=batch_count,
        )

    for index, industry_name in enumerate(industry_sources, start=1):
        normalized_name = str(industry_name).strip()
        if not normalized_name:
            continue
        stage_key = "ashare_p1_industry_members"
        batch_count = len(industry_progress_items)
        record_p1_stage_started(
            stage_key=stage_key,
            batch_index=index,
            batch_count=batch_count,
        )
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
        record_p1_stage_result(
            stage_key=stage_key,
            symbol=normalized_name,
            result=tasks[-1],
            batch_index=index,
            batch_count=batch_count,
        )

    for index, concept_name in enumerate(concept_sources, start=1):
        normalized_name = str(concept_name).strip()
        if not normalized_name:
            continue
        stage_key = "ashare_p1_concept_members"
        batch_count = len(concept_progress_items)
        record_p1_stage_started(
            stage_key=stage_key,
            batch_index=index,
            batch_count=batch_count,
        )
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
        record_p1_stage_result(
            stage_key=stage_key,
            symbol=normalized_name,
            result=tasks[-1],
            batch_index=index,
            batch_count=batch_count,
        )

    stage_key = "ashare_p1_flow_rank"
    record_p1_stage_started(stage_key=stage_key, batch_index=1, batch_count=1)
    tasks.append(
        runtime.run_task(
            task="ashare_p1_flow_rank",
            provider_key="stock_individual_fund_flow_rank",
            parameters={"indicator": args.flow_window, "limit": source_limit},
            force=args.force_provider,
            collect=lambda: collector.collect_flow_rank(
                indicator=args.flow_window,
                limit=source_limit,
            ),
        )
    )
    record_p1_stage_result(
        stage_key=stage_key,
        symbol=str(args.flow_window or "capital_flow"),
        result=tasks[-1],
        batch_index=1,
        batch_count=1,
    )
    return tasks


def build_ashare_p1_default_tasks(
    session: Any | None,
    collector: AshareP1Collector,
    args: argparse.Namespace,
    runtime: CollectionRuntime,
    *,
    session_factory: Any | None = None,
) -> list[CollectionTaskResult]:
    """构建 A 股 P1 的默认任务包。"""

    source_limit = list_source_limit(args)
    return [
        *build_ashare_p1_universe_tasks(collector, args, runtime),
        *build_ashare_stock_news_tasks(
            session,
            collector,
            args,
            runtime,
            session_factory=session_factory,
        ),
        runtime.run_task(
            task="ashare_p1_notice_reports",
            provider_key="stock_notice_report",
            parameters={
                "symbol": "全部",
                "date": args.risk_end,
                "limit": source_limit,
            },
            force=args.force_provider,
            collect=lambda: run_rate_limited_collection(
                "stock_notice_report",
                lambda: collector.collect_notice_reports(
                    symbol="全部",
                    date=args.risk_end,
                    limit=source_limit,
                ),
            ),
        ),
    ]


def build_ashare_stock_news_tasks(
    session: Any | None,
    collector: AshareP1Collector,
    args: argparse.Namespace,
    runtime: CollectionRuntime,
    *,
    session_factory: Any | None = None,
) -> list[CollectionTaskResult]:
    """按重点资产池分批构建个股新闻采集任务。"""

    source_limit = list_source_limit(args)
    tasks: list[CollectionTaskResult] = []
    symbols = resolve_ashare_stock_news_symbols(session, args)
    if session is not None:
        commit_session_if_possible(session)

    batches = split_symbol_batches(symbols, batch_size=collection_batch_size(args))
    max_workers = collection_max_workers(args) if session_factory is not None else 1
    logger.info(
        "A 股重点个股新闻刷新批次展开 symbols=%s batch_size=%s batch_count=%s",
        len(symbols),
        collection_batch_size(args),
        len(batches),
    )
    for batch_index, batch_symbols in enumerate(batches, start=1):
        logger.info(
            "A 股重点个股新闻刷新批次开始 batch=%s/%s size=%s",
            batch_index,
            len(batches),
            len(batch_symbols),
        )
        def collect_symbol(symbol: str) -> CollectionTaskResult:
            if session_factory is not None:
                with session_scope(session_factory) as worker_session:
                    worker_collector = AshareP1Collector(worker_session)
                    asset_name = asset_name_for_symbol(worker_session, symbol)
                    commit_session_if_possible(worker_session)
                    return runtime.run_task(
                        task="ashare_p1_stock_news",
                        provider_key=stock_news_provider_key(symbol),
                        parameters={
                            "symbol": symbol,
                            "limit": source_limit,
                            "enrich_articles": False,
                        },
                        force=args.force_provider,
                        collect=lambda: run_rate_limited_collection(
                            "stock_news_em",
                            lambda: worker_collector.collect_stock_news(
                                symbol=symbol,
                                asset_name=asset_name,
                                limit=source_limit,
                                enrich_articles=False,
                            ),
                        ),
                    )
            return runtime.run_task(
                task="ashare_p1_stock_news",
                provider_key=stock_news_provider_key(symbol),
                parameters={
                    "symbol": symbol,
                    "limit": source_limit,
                    "enrich_articles": False,
                },
                force=args.force_provider,
                collect=lambda: run_rate_limited_collection(
                    "stock_news_em",
                    lambda: collector.collect_stock_news(
                        symbol=symbol,
                        asset_name=(
                            asset_name_for_symbol(session, symbol)
                            if session is not None
                            else args.ashare_name
                        ),
                        limit=source_limit,
                        enrich_articles=False,
                    ),
                ),
            )

        for result in run_symbol_task_batch(
            batch_symbols,
            max_workers=max_workers,
            collect_symbol=collect_symbol,
            progress=collection_progress_context(args)[0],
            job_name=collection_progress_context(args)[1],
            run_id=collection_progress_context(args)[2],
            stage_key="ashare_p1_stock_news",
            batch_index=batch_index,
            batch_count=len(batches),
            batch_size=collection_batch_size(args),
            total_items=len(symbols),
        ):
            tasks.append(
                attach_batch_payload(
                    result,
                    batch_index=batch_index,
                    batch_count=len(batches),
                    batch_size=collection_batch_size(args),
                    symbol_count=len(symbols),
                )
            )
        if session is not None:
            commit_session_if_possible(session)
    return tasks


def build_ashare_news_article_enrichment_tasks(
    session: Any | None,
    collector: AshareP1Collector,
    args: argparse.Namespace,
    runtime: CollectionRuntime,
    *,
    session_factory: Any | None = None,
) -> list[CollectionTaskResult]:
    """异步低并发补抓已入库新闻正文。"""

    if session is None:
        return []
    candidates = resolve_ashare_news_article_candidates(session, args)
    commit_session_if_possible(session)
    event_ids = [str(item["event_id"]) for item in candidates]
    candidate_by_id = {str(item["event_id"]): item for item in candidates}
    batches = split_symbol_batches(event_ids, batch_size=collection_batch_size(args))
    max_workers = collection_max_workers(args) if session_factory is not None else 1
    tasks: list[CollectionTaskResult] = []
    logger.info(
        "A 股新闻正文补抓批次展开 events=%s batch_size=%s batch_count=%s max_workers=%s",
        len(event_ids),
        collection_batch_size(args),
        len(batches),
        max_workers,
    )
    for batch_index, batch_event_ids in enumerate(batches, start=1):

        def collect_event(event_id: str) -> CollectionTaskResult:
            candidate = candidate_by_id[event_id]
            if session_factory is not None:
                with session_scope(session_factory) as worker_session:
                    worker_collector = AshareP1Collector(worker_session)
                    return runtime.run_task(
                        task="ashare_p1_news_article",
                        provider_key=stock_news_article_provider_key(event_id),
                        parameters={"event_id": event_id, "url": candidate["url"]},
                        force=args.force_provider,
                        collect=lambda: run_rate_limited_collection(
                            "stock_news_article",
                            lambda: worker_collector.enrich_existing_stock_news_article(
                                **candidate,
                            ),
                        ),
                    )
            return runtime.run_task(
                task="ashare_p1_news_article",
                provider_key=stock_news_article_provider_key(event_id),
                parameters={"event_id": event_id, "url": candidate["url"]},
                force=args.force_provider,
                collect=lambda: run_rate_limited_collection(
                    "stock_news_article",
                    lambda: collector.enrich_existing_stock_news_article(**candidate),
                ),
            )

        for result in run_symbol_task_batch(
            batch_event_ids,
            max_workers=max_workers,
            collect_symbol=collect_event,
            progress=collection_progress_context(args)[0],
            job_name=collection_progress_context(args)[1],
            run_id=collection_progress_context(args)[2],
            stage_key="ashare_p1_news_article",
            batch_index=batch_index,
            batch_count=len(batches),
            batch_size=collection_batch_size(args),
            total_items=len(event_ids),
        ):
            tasks.append(
                attach_batch_payload(
                    result,
                    batch_index=batch_index,
                    batch_count=len(batches),
                    batch_size=collection_batch_size(args),
                    symbol_count=len(event_ids),
                )
            )
        commit_session_if_possible(session)
    return tasks


def resolve_ashare_news_article_candidates(
    session: Any,
    args: argparse.Namespace,
) -> list[JsonDict]:
    """选择需要补抓正文的 A 股新闻事件。"""

    target_limit = (
        positive_limit(getattr(args, "priority_symbol_limit", None))
        or positive_limit(getattr(args, "source_limit", None))
        or collection_batch_size(args)
    )
    scan_limit = max(target_limit * 5, target_limit)
    rows = list(
        session.scalars(
            select(EventRecordORM)
            .where(
                EventRecordORM.market == "ashare",
                EventRecordORM.event_type == "news",
                EventRecordORM.url.is_not(None),
                active_event_predicate(EventRecordORM),
            )
            .order_by(
                EventRecordORM.published_at.desc().nullslast(),
                EventRecordORM.collected_at.desc(),
            )
            .limit(scan_limit)
        )
    )
    candidates: list[JsonDict] = []
    for event in rows:
        if not news_event_needs_article_enrichment(getattr(event, "payload", None)):
            continue
        url = str(getattr(event, "url", "") or "").strip()
        if not url:
            continue
        candidates.append(
            {
                "event_id": str(event.event_id),
                "url": url,
                "asset_id": getattr(event, "asset_id", None),
                "symbol": getattr(event, "symbol", None),
                "title": getattr(event, "title", None),
                "source_excerpt": getattr(event, "summary", None),
            }
        )
        if len(candidates) >= target_limit:
            break
    return candidates


def news_event_needs_article_enrichment(payload: Any) -> bool:
    """判断新闻事件是否仍需要补抓正文。"""

    if not isinstance(payload, dict):
        return True
    article = payload.get("article")
    if not isinstance(article, dict):
        return True
    return str(article.get("status") or "").lower() != "available"


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


def list_source_limit(args: argparse.Namespace) -> int | None:
    """列表型来源默认完整刷新；显式 source_limit 可用于临时限流排查。"""

    return positive_limit(getattr(args, "source_limit", None))


def run_ashare_p2(
    session: Any,
    args: argparse.Namespace,
    runtime: CollectionRuntime,
    *,
    session_factory: Any | None = None,
) -> list[CollectionTaskResult]:
    """执行 A 股 P2 财务、估值和业绩采集。"""

    collector = AshareP2Collector(session)
    task_type = task_type_name(args)
    if task_type == "valuation_backfill":
        symbol = normalize_ashare_symbol(str(args.ashare_symbol or ""))
        if not symbol:
            return [
                CollectionTaskResult(
                    task="ashare_p2_valuation",
                    status="failed",
                    raw_record_id=None,
                    item_count=0,
                    error_message="估值历史补跑缺少股票代码。",
                    payload={"provider_key": ASHARE_VALUATION_PROVIDER},
                )
            ]
        asset_name = asset_name_for_symbol(session, symbol)
        result = runtime.run_task(
            task="ashare_p2_valuation",
            provider_key=ASHARE_VALUATION_PROVIDER,
            parameters={"symbol": symbol, "limit": args.limit},
            force=args.force_provider,
            collect=lambda: collector.collect_valuation(
                symbol=symbol,
                asset_name=asset_name,
                limit=args.limit,
            ),
        )
        record_ashare_fundamental_watermark(
            session,
            symbol=symbol,
            data_domain=ASHARE_VALUATION_DATA_DOMAIN,
            provider=ASHARE_VALUATION_PROVIDER,
            result=result,
            session_factory=session_factory,
        )
        return [result]
    if task_type == "fundamental_refresh":
        preflight_tasks: list[CollectionTaskResult] = []
        if should_refresh_asset_universe_before_incremental(session, market="ashare"):
            logger.info("A 股资产池为空或明显不完整，基本面刷新前先刷新完整全 A Universe")
            p0_collector = AshareP0Collector(session)
            preflight_result = build_ashare_full_asset_refresh_task(p0_collector, args, runtime)
            preflight_tasks.append(preflight_result)
            commit_session_if_possible(session)
            if asset_universe_preflight_blocked(preflight_result):
                logger.warning(
                    "A 股资产池预刷新未完成，基本面任务等待下一轮 status=%s error=%s",
                    getattr(preflight_result, "status", None),
                    getattr(preflight_result, "error_message", None),
                )
                return preflight_tasks

        symbols = resolve_ashare_fundamental_symbols(session, args)
        tasks: list[CollectionTaskResult] = []
        batches = split_symbol_batches(symbols, batch_size=collection_batch_size(args))
        max_workers = collection_max_workers(args) if session_factory is not None else 1
        logger.info(
            "A 股基本面估值刷新批次展开 symbols=%s batch_size=%s batch_count=%s",
            len(symbols),
            collection_batch_size(args),
            len(batches),
        )
        for batch_index, batch_symbols in enumerate(batches, start=1):
            logger.info(
                "A 股基本面估值刷新批次开始 batch=%s/%s size=%s",
                batch_index,
                len(batches),
                len(batch_symbols),
            )
            def collect_symbol(symbol: str) -> list[CollectionTaskResult]:
                if session_factory is not None and max_workers > 1:
                    with session_scope(session_factory) as worker_session:
                        worker_collector = AshareP2Collector(worker_session)
                        asset_name = asset_name_for_symbol(worker_session, symbol)
                        return [
                            runtime.run_task(
                                task="ashare_p2_financial_indicators",
                                provider_key=ASHARE_FINANCIAL_INDICATORS_PROVIDER,
                                parameters={"symbol": symbol, "limit": args.limit},
                                force=args.force_provider,
                                collect=lambda: worker_collector.collect_financial_indicators(
                                    symbol=symbol,
                                    asset_name=asset_name,
                                    limit=args.limit,
                                ),
                            ),
                            runtime.run_task(
                                task="ashare_p2_valuation",
                                provider_key=ASHARE_VALUATION_PROVIDER,
                                parameters={"symbol": symbol, "limit": args.limit},
                                force=args.force_provider,
                                collect=lambda: worker_collector.collect_valuation(
                                    symbol=symbol,
                                    asset_name=asset_name,
                                    limit=args.limit,
                                ),
                            ),
                        ]
                asset_name = asset_name_for_symbol(session, symbol)
                return [
                    runtime.run_task(
                        task="ashare_p2_financial_indicators",
                        provider_key=ASHARE_FINANCIAL_INDICATORS_PROVIDER,
                        parameters={"symbol": symbol, "limit": args.limit},
                        force=args.force_provider,
                        collect=lambda: collector.collect_financial_indicators(
                            symbol=symbol,
                            asset_name=asset_name,
                            limit=args.limit,
                        ),
                    ),
                    runtime.run_task(
                        task="ashare_p2_valuation",
                        provider_key=ASHARE_VALUATION_PROVIDER,
                        parameters={"symbol": symbol, "limit": args.limit},
                        force=args.force_provider,
                        collect=lambda: collector.collect_valuation(
                            symbol=symbol,
                            asset_name=asset_name,
                            limit=args.limit,
                        ),
                    ),
                ]

            def handle_symbol_result(symbol: str, symbol_results: list[CollectionTaskResult], index: int) -> None:
                watermark_session_factory = session_factory if max_workers > 1 else None
                for result in symbol_results:
                    if result.task == "ashare_p2_financial_indicators":
                        record_ashare_fundamental_watermark(
                            session,
                            symbol=symbol,
                            data_domain=ASHARE_FUNDAMENTAL_DATA_DOMAIN,
                            provider=ASHARE_FINANCIAL_INDICATORS_PROVIDER,
                            result=result,
                            session_factory=watermark_session_factory,
                        )
                    elif result.task == "ashare_p2_valuation":
                        record_ashare_fundamental_watermark(
                            session,
                            symbol=symbol,
                            data_domain=ASHARE_VALUATION_DATA_DOMAIN,
                            provider=ASHARE_VALUATION_PROVIDER,
                            result=result,
                            session_factory=watermark_session_factory,
                        )

            for symbol_results in run_symbol_task_batch(
                batch_symbols,
                max_workers=max_workers,
                collect_symbol=collect_symbol,
                on_symbol_result=handle_symbol_result,
                progress=collection_progress_context(args)[0],
                job_name=collection_progress_context(args)[1],
                run_id=collection_progress_context(args)[2],
                stage_key="ashare_p2_symbol",
                batch_index=batch_index,
                batch_count=len(batches),
                batch_size=collection_batch_size(args),
                total_items=len(symbols),
            ):
                for result in symbol_results:
                    tasks.append(
                        attach_batch_payload(
                            result,
                            batch_index=batch_index,
                            batch_count=len(batches),
                            batch_size=collection_batch_size(args),
                            symbol_count=len(symbols),
                        )
                    )
            commit_session_if_possible(session)
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
        return [*preflight_tasks, *tasks]
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
    source_limit = list_source_limit(args)
    progress, job_name, run_id = collection_progress_context(args)
    if task_type_name(args) == "restricted_release_refresh":
        provider_key = "stock_restricted_release_detail_em"
        parameters = {
            "start_date": args.risk_start,
            "end_date": args.risk_end,
            "limit": source_limit,
            "risk_window_days": 30,
            "risk_ratio_threshold": "0.05",
        }
        timeframe = ashare_risk_sentiment_watermark_timeframe(
            task="ashare_risk_restricted_release",
            provider=provider_key,
            parameters=parameters,
        )
        if not args.force_provider and not ashare_risk_sentiment_watermark_allows_collection(
            session,
            provider=provider_key,
            timeframe=timeframe,
        ):
            return [
                CollectionTaskResult(
                    task="ashare_risk_restricted_release",
                    status="skipped",
                    raw_record_id=None,
                    item_count=0,
                    error_message="风险情绪源处于失败冷却期，等待下次重跑。",
                    payload={
                        "provider_key": provider_key,
                        "data_domain": ASHARE_RISK_SENTIMENT_DATA_DOMAIN,
                        "timeframe": timeframe,
                    },
                )
            ]
        result = runtime.run_task(
            task="ashare_risk_restricted_release",
            provider_key=provider_key,
            parameters=parameters,
            force=args.force_provider,
            collect=lambda: collector.collect_restricted_release(
                start_date=args.risk_start,
                end_date=args.risk_end,
                limit=source_limit,
                risk_window_days=30,
                risk_ratio_threshold=Decimal("0.05"),
            ),
        )
        record_ashare_risk_sentiment_watermark(
            session,
            task="ashare_risk_restricted_release",
            provider=provider_key,
            timeframe=timeframe,
            result=result,
        )
        return [result]
    if task_type_name(args) == "pledge_risk_refresh":
        provider_key = "stock_gpzy_pledge_ratio_em"
        parameters = {
            "date": args.risk_end,
            "limit": source_limit,
            "risk_ratio_threshold": "0.30",
        }
        timeframe = ashare_risk_sentiment_watermark_timeframe(
            task="ashare_risk_pledge_ratio",
            provider=provider_key,
            parameters=parameters,
        )
        if not args.force_provider and not ashare_risk_sentiment_watermark_allows_collection(
            session,
            provider=provider_key,
            timeframe=timeframe,
        ):
            return [
                CollectionTaskResult(
                    task="ashare_risk_pledge_ratio",
                    status="skipped",
                    raw_record_id=None,
                    item_count=0,
                    error_message="风险情绪源处于失败冷却期，等待下次重跑。",
                    payload={
                        "provider_key": provider_key,
                        "data_domain": ASHARE_RISK_SENTIMENT_DATA_DOMAIN,
                        "timeframe": timeframe,
                    },
                )
            ]
        result = runtime.run_task(
            task="ashare_risk_pledge_ratio",
            provider_key=provider_key,
            parameters=parameters,
            force=args.force_provider,
            collect=lambda: collector.collect_pledge_ratio(
                date=args.risk_end,
                limit=source_limit,
                risk_ratio_threshold=Decimal("0.30"),
            ),
        )
        record_ashare_risk_sentiment_watermark(
            session,
            task="ashare_risk_pledge_ratio",
            provider=provider_key,
            timeframe=timeframe,
            result=result,
        )
        return [result]
    source_tasks: list[dict[str, Any]] = [
        {
            "task": "ashare_risk_stop_list",
            "provider_key": "stock_zh_a_stop_em",
            "parameters": {"limit": source_limit},
            "collect": lambda: collector.collect_stop_list(limit=source_limit),
        },
        {
            "task": "ashare_sentiment_hot_rank",
            "provider_key": "stock_hot_rank_em",
            "parameters": {"limit": source_limit},
            "collect": lambda: collector.collect_hot_rank(
                universe_id="universe:base:ashare:p2:sentiment:hot_rank",
                universe_name="A 股人气榜观察池",
                strategy_context="base_data_collect",
                limit=source_limit,
            ),
        },
        {
            "task": "ashare_sentiment_zt_pool",
            "provider_key": "stock_zt_pool_em",
            "parameters": {"date": args.risk_end, "limit": source_limit},
            "collect": lambda: collector.collect_zt_pool(
                date=args.risk_end,
                universe_id=f"universe:base:ashare:p2:sentiment:zt_pool:{args.risk_end}",
                universe_name=f"A 股涨停池-{args.risk_end}",
                strategy_context="base_data_collect",
                limit=source_limit,
            ),
        },
        {
            "task": "ashare_risk_lhb_detail",
            "provider_key": "stock_lhb_detail_em",
            "parameters": {
                "start_date": args.risk_start,
                "end_date": args.risk_end,
                "limit": source_limit,
            },
            "collect": lambda: collector.collect_lhb_detail(
                start_date=args.risk_start,
                end_date=args.risk_end,
                limit=source_limit,
            ),
        },
        {
            "task": "ashare_risk_block_trades",
            "provider_key": "stock_dzjy_mrmx",
            "parameters": {
                "symbol": args.risk_block_symbol,
                "start_date": args.risk_start,
                "end_date": args.risk_end,
                "limit": source_limit,
            },
            "collect": lambda: collector.collect_block_trades(
                symbol=args.risk_block_symbol,
                start_date=args.risk_start,
                end_date=args.risk_end,
                limit=source_limit,
            ),
        },
        {
            "task": "ashare_risk_margin_sse",
            "provider_key": "stock_margin_sse",
            "parameters": {
                "start_date": args.risk_start,
                "end_date": args.risk_end,
                "limit": source_limit,
            },
            "collect": lambda: collector.collect_margin_sse(
                start_date=args.risk_start,
                end_date=args.risk_end,
                limit=source_limit,
            ),
        },
        {
            "task": "ashare_risk_margin_szse",
            "provider_key": "stock_margin_szse",
            "parameters": {"date": args.risk_end, "limit": source_limit},
            "collect": lambda: collector.collect_margin_szse(date=args.risk_end, limit=source_limit),
        },
    ]
    results: list[CollectionTaskResult] = []
    stage_key = "ashare_risk_sentiment_sources"
    batch_count = len(source_tasks)
    if progress is not None and job_name and run_id:
        progress.stage_planned(
            job_name=job_name,
            run_id=run_id,
            stage_key=stage_key,
            total_items=batch_count,
        )

    def record_risk_stage_started(*, batch_index: int) -> None:
        if progress is None or not job_name or not run_id:
            return
        progress.batch_started(
            job_name=job_name,
            run_id=run_id,
            stage_key=stage_key,
            total_items=batch_count,
            batch_index=batch_index,
            batch_count=batch_count,
            batch_size=1,
        )

    def record_risk_stage_result(
        *,
        symbol: str,
        result: CollectionTaskResult,
        batch_index: int,
    ) -> None:
        if progress is None or not job_name or not run_id:
            return
        emit_symbol_progress(
            progress,
            job_name=job_name,
            run_id=run_id,
            stage_key=stage_key,
            symbol=symbol,
            result=result,
            batch_index=batch_index,
            batch_count=batch_count,
        )

    for batch_index, source_task in enumerate(source_tasks, start=1):
        task = str(source_task["task"])
        provider_key = str(source_task["provider_key"])
        parameters = dict(source_task["parameters"])
        record_risk_stage_started(batch_index=batch_index)
        timeframe = ashare_risk_sentiment_watermark_timeframe(
            task=task,
            provider=provider_key,
            parameters=parameters,
        )
        if not args.force_provider and not ashare_risk_sentiment_watermark_allows_collection(
            session,
            provider=provider_key,
            timeframe=timeframe,
        ):
            results.append(
                CollectionTaskResult(
                    task=task,
                    status="skipped",
                    raw_record_id=None,
                    item_count=0,
                    error_message="风险情绪源处于失败冷却期，等待下次重跑。",
                    payload={
                        "provider_key": provider_key,
                        "data_domain": ASHARE_RISK_SENTIMENT_DATA_DOMAIN,
                        "timeframe": timeframe,
                    },
                )
            )
            record_risk_stage_result(symbol=task, result=results[-1], batch_index=batch_index)
            continue
        result = runtime.run_task(
            task=task,
            provider_key=provider_key,
            parameters=parameters,
            force=args.force_provider,
            collect=source_task["collect"],
        )
        record_ashare_risk_sentiment_watermark(
            session,
            task=task,
            provider=provider_key,
            timeframe=timeframe,
            result=result,
        )
        results.append(result)
        record_risk_stage_result(symbol=task, result=result, batch_index=batch_index)
    return results


def run_crypto(
    session: Any,
    args: argparse.Namespace,
    runtime: CollectionRuntime,
    *,
    session_factory: Any | None = None,
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
            build_crypto_full_market_refresh_task(collector, args, runtime),
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
        if should_refresh_asset_universe_before_incremental(session, market=crypto_market):
            logger.info(
                "数字货币资产池为空或明显不完整，先刷新完整 Binance Universe market=%s",
                crypto_market,
            )
            tasks.append(build_crypto_full_market_refresh_task(collector, args, runtime))
            commit_session_if_possible(session)
        symbols = resolve_crypto_collection_symbols(session, args, market=crypto_market)
        batches = split_symbol_batches(symbols, batch_size=collection_batch_size(args))
        max_workers = collection_max_workers(args) if session_factory is not None else 1
        logger.info(
            "数字货币 K 线补采批次展开 market=%s symbols=%s batch_size=%s batch_count=%s",
            crypto_market,
            len(symbols),
            collection_batch_size(args),
            len(batches),
        )
        for batch_index, batch_symbols in enumerate(batches, start=1):
            logger.info(
                "数字货币 K 线补采批次开始 market=%s batch=%s/%s size=%s",
                crypto_market,
                batch_index,
                len(batches),
                len(batch_symbols),
            )
            skip_symbols = (
                set()
                if args.force_provider
                else crypto_symbols_in_failure_cooldown(
                    session,
                    batch_symbols,
                    market=crypto_market,
                    data_domain=CRYPTO_MARKET_BAR_DATA_DOMAIN,
                    provider=CRYPTO_MARKET_BAR_PROVIDER,
                    timeframe=args.crypto_timeframe,
                )
            )

            def collect_symbol(
                symbol: str,
                cooldown_symbols: set[str] = skip_symbols,
            ) -> CollectionTaskResult:
                if symbol in cooldown_symbols:
                    return crypto_watermark_skip_result(
                        task="crypto_ohlcv",
                        symbol=symbol,
                        market=crypto_market,
                        data_domain=CRYPTO_MARKET_BAR_DATA_DOMAIN,
                        provider=CRYPTO_MARKET_BAR_PROVIDER,
                        timeframe=args.crypto_timeframe,
                    )
                if session_factory is not None and max_workers > 1:
                    with session_scope(session_factory) as worker_session:
                        worker_collector = CryptoDataCollector(worker_session)
                        return runtime.run_task(
                            task="crypto_ohlcv",
                            provider_key=crypto_ohlcv_provider_key(args.crypto_market_type, symbol),
                            parameters={
                                "symbol": symbol,
                                "timeframe": args.crypto_timeframe,
                                "market_type": args.crypto_market_type,
                                "limit": args.limit,
                            },
                            force=args.force_provider,
                            collect=lambda: run_rate_limited_collection(
                                "ccxt_binance_fetch_ohlcv",
                                lambda: worker_collector.collect_ohlcv(
                                    symbol=symbol,
                                    timeframe=args.crypto_timeframe,
                                    market_type=args.crypto_market_type,
                                    limit=args.limit,
                                ),
                            ),
                        )
                return runtime.run_task(
                    task="crypto_ohlcv",
                    provider_key=crypto_ohlcv_provider_key(args.crypto_market_type, symbol),
                    parameters={
                        "symbol": symbol,
                        "timeframe": args.crypto_timeframe,
                        "market_type": args.crypto_market_type,
                        "limit": args.limit,
                    },
                    force=args.force_provider,
                    collect=lambda: run_rate_limited_collection(
                        "ccxt_binance_fetch_ohlcv",
                        lambda: collector.collect_ohlcv(
                            symbol=symbol,
                            timeframe=args.crypto_timeframe,
                            market_type=args.crypto_market_type,
                            limit=args.limit,
                        ),
                    ),
                )

            batch_results = run_symbol_task_batch(
                batch_symbols,
                max_workers=max_workers,
                collect_symbol=collect_symbol,
                progress=collection_progress_context(args)[0],
                job_name=collection_progress_context(args)[1],
                run_id=collection_progress_context(args)[2],
                stage_key="crypto_ohlcv",
                batch_index=batch_index,
                batch_count=len(batches),
                batch_size=collection_batch_size(args),
                total_items=len(symbols),
            )
            for symbol, result in zip(batch_symbols, batch_results, strict=False):
                enriched_result = attach_batch_payload(
                    result,
                    batch_index=batch_index,
                    batch_count=len(batches),
                    batch_size=collection_batch_size(args),
                    symbol_count=len(symbols),
                )
                if result_status_name(enriched_result) != "skipped":
                    record_crypto_symbol_watermark(
                        session,
                        symbol=symbol,
                        market=crypto_market,
                        data_domain=CRYPTO_MARKET_BAR_DATA_DOMAIN,
                        provider=CRYPTO_MARKET_BAR_PROVIDER,
                        timeframe=args.crypto_timeframe,
                        result=enriched_result,
                    )
                tasks.append(enriched_result)
            commit_session_if_possible(session)
        return tasks
    if task_type == "derivative_refresh":
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
        if should_refresh_asset_universe_before_incremental(session, market=crypto_market):
            logger.info("合约资产池为空或明显不完整，衍生品刷新前先刷新完整 Binance Universe")
            tasks.append(build_crypto_full_market_refresh_task(collector, args, runtime))
            commit_session_if_possible(session)
        symbols = resolve_crypto_derivative_collection_symbols(session, args, market=crypto_market)
        batches = split_symbol_batches(symbols, batch_size=collection_batch_size(args))
        max_workers = collection_max_workers(args) if session_factory is not None else 1
        logger.info(
            "数字货币衍生品快照批次展开 market=%s symbols=%s batch_size=%s batch_count=%s",
            crypto_market,
            len(symbols),
            collection_batch_size(args),
            len(batches),
        )
        for batch_index, batch_symbols in enumerate(batches, start=1):
            logger.info(
                "数字货币衍生品快照批次开始 market=%s batch=%s/%s size=%s",
                crypto_market,
                batch_index,
                len(batches),
                len(batch_symbols),
            )
            derivative_timeframe = str(args.crypto_market_type or "future")
            skip_symbols = (
                set()
                if args.force_provider
                else crypto_symbols_in_failure_cooldown(
                    session,
                    batch_symbols,
                    market=crypto_market,
                    data_domain=CRYPTO_DERIVATIVE_DATA_DOMAIN,
                    provider=CRYPTO_DERIVATIVE_PROVIDER,
                    timeframe=derivative_timeframe,
                )
            )

            def collect_symbol(
                symbol: str,
                cooldown_symbols: set[str] = skip_symbols,
                target_timeframe: str = derivative_timeframe,
            ) -> CollectionTaskResult:
                if symbol in cooldown_symbols:
                    return crypto_watermark_skip_result(
                        task="crypto_derivative_snapshot",
                        symbol=symbol,
                        market=crypto_market,
                        data_domain=CRYPTO_DERIVATIVE_DATA_DOMAIN,
                        provider=CRYPTO_DERIVATIVE_PROVIDER,
                        timeframe=target_timeframe,
                    )
                if session_factory is not None and max_workers > 1:
                    with session_scope(session_factory) as worker_session:
                        worker_collector = CryptoDataCollector(worker_session)
                        return runtime.run_task(
                            task="crypto_derivative_snapshot",
                            provider_key=crypto_derivative_provider_key(
                                args.crypto_market_type,
                                symbol,
                            ),
                            parameters={"symbol": symbol},
                            force=args.force_provider,
                            collect=lambda: run_rate_limited_collection(
                                CRYPTO_DERIVATIVE_PROVIDER,
                                lambda: worker_collector.collect_derivative_snapshot(
                                    symbol=symbol
                                ),
                            ),
                        )
                return runtime.run_task(
                    task="crypto_derivative_snapshot",
                    provider_key=crypto_derivative_provider_key(args.crypto_market_type, symbol),
                    parameters={"symbol": symbol},
                    force=args.force_provider,
                    collect=lambda: run_rate_limited_collection(
                        CRYPTO_DERIVATIVE_PROVIDER,
                        lambda: collector.collect_derivative_snapshot(symbol=symbol),
                    ),
                )

            batch_results = run_symbol_task_batch(
                batch_symbols,
                max_workers=max_workers,
                collect_symbol=collect_symbol,
                progress=collection_progress_context(args)[0],
                job_name=collection_progress_context(args)[1],
                run_id=collection_progress_context(args)[2],
                stage_key="crypto_derivative_snapshot",
                batch_index=batch_index,
                batch_count=len(batches),
                batch_size=collection_batch_size(args),
                total_items=len(symbols),
            )
            for symbol, result in zip(batch_symbols, batch_results, strict=False):
                enriched_result = attach_batch_payload(
                    result,
                    batch_index=batch_index,
                    batch_count=len(batches),
                    batch_size=collection_batch_size(args),
                    symbol_count=len(symbols),
                )
                if result_status_name(enriched_result) != "skipped":
                    record_crypto_symbol_watermark(
                        session,
                        symbol=symbol,
                        market=crypto_market,
                        data_domain=CRYPTO_DERIVATIVE_DATA_DOMAIN,
                        provider=CRYPTO_DERIVATIVE_PROVIDER,
                        timeframe=derivative_timeframe,
                        result=enriched_result,
                    )
                tasks.append(enriched_result)
            commit_session_if_possible(session)
        return tasks
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
        build_crypto_full_market_refresh_task(collector, args, runtime),
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
            collect=lambda: run_rate_limited_collection(
                "ccxt_binance_fetch_ohlcv",
                lambda: collector.collect_ohlcv(
                    symbol=args.crypto_symbol,
                    timeframe=args.crypto_timeframe,
                    market_type=args.crypto_market_type,
                    limit=args.limit,
                ),
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


def run_fund(
    session: Any,
    args: argparse.Namespace,
    runtime: CollectionRuntime,
    *,
    session_factory: Any | None = None,
) -> list[CollectionTaskResult]:
    """执行基金资产池、场内基金日 K 和开放式基金净值采集。"""

    collector = FundDataCollector(session)
    task_type = task_type_name(args)
    if task_type == "universe_refresh":
        return [
            runtime.run_task(
                task="fund_universe",
                provider_key="fund_universe_refresh",
                parameters={"limit": None},
                force=args.force_provider,
                collect=lambda: collector.collect_universe(
                    universe_id="universe:base:fund:all",
                    universe_name="基础数据采集基金候选池",
                    strategy_context="base_data_collect",
                ),
            )
        ]
    if task_type in {"market_bars_full_history_backfill", "market_bars_close_final"}:
        asset_type = str(getattr(args, "fund_asset_type", "etf") or "etf").strip()
        if asset_type not in {"etf", "lof"}:
            asset_type = "etf"
        if should_refresh_asset_universe_before_incremental(session, market="fund", min_asset_count=20):
            logger.info("基金资产池为空或明显不完整，基金日 K 任务前先刷新基金资产池")
            warmup_result = runtime.run_task(
                task="fund_universe",
                provider_key="fund_universe_refresh",
                parameters={"limit": None},
                force=args.force_provider,
                collect=lambda: collector.collect_universe(
                    universe_id="universe:base:fund:all",
                    universe_name="基础数据采集基金候选池",
                    strategy_context="base_data_collect",
                ),
            )
            commit_session_if_possible(session)
            preflight_tasks = [warmup_result]
        else:
            preflight_tasks = []
        symbols = resolve_fund_bar_collection_symbols(session, args, asset_type=asset_type)
        batches = split_symbol_batches(symbols, batch_size=collection_batch_size(args))
        max_workers = collection_max_workers(args) if session_factory is not None else 1
        tasks = list(preflight_tasks)
        provider_source = fund_bar_watermark_provider(asset_type)
        for batch_index, batch_symbols in enumerate(batches, start=1):
            def collect_symbol(symbol: str) -> CollectionTaskResult:
                def execute(target_collector: FundDataCollector) -> Any:
                    if asset_type == "lof":
                        return run_rate_limited_collection(
                            "fund_lof_hist_em",
                            lambda: target_collector.collect_lof_ohlcv(
                                symbol=symbol,
                                start=getattr(args, "ashare_start", None),
                                end=getattr(args, "ashare_end", None),
                                limit=args.limit,
                                is_closed=getattr(args, "is_closed", True),
                                status=getattr(args, "status", "available"),
                            ),
                        )
                    return run_rate_limited_collection(
                        "fund_etf_hist_em",
                        lambda: target_collector.collect_etf_ohlcv(
                            symbol=symbol,
                            start=getattr(args, "ashare_start", None),
                            end=getattr(args, "ashare_end", None),
                            limit=args.limit,
                            is_closed=getattr(args, "is_closed", True),
                            status=getattr(args, "status", "available"),
                        ),
                    )

                def wrap_result(
                    result: CollectionTaskResult,
                    *,
                    worker_session: Any | None = None,
                ) -> CollectionTaskResult:
                    occurred_at = datetime.now(tz=UTC)
                    target_session = worker_session if worker_session is not None else session
                    schedule_retry = bool(getattr(args, "schedule_failure_retry", True))
                    record_fund_symbol_watermark(
                        target_session,
                        symbol=symbol,
                        asset_type=asset_type,
                        data_domain=FUND_MARKET_BAR_DATA_DOMAIN,
                        provider=provider_source,
                        timeframe=getattr(args, "fund_timeframe", "1d"),
                        result=result,
                        occurred_at=occurred_at,
                        session_factory=None if worker_session is not None else session_factory,
                        schedule_retry=schedule_retry,
                        requested_start=getattr(args, "ashare_start", None),
                        requested_end=getattr(args, "ashare_end", None),
                        sync_task_type=task_type,
                    )
                    return attach_fund_retry_payload(
                        result,
                        provider=provider_source,
                        occurred_at=occurred_at,
                        schedule_retry=schedule_retry,
                    )

                if session_factory is not None and max_workers > 1:
                    with session_scope(session_factory) as worker_session:
                        worker_collector = FundDataCollector(worker_session)
                        result = runtime.run_task(
                            task=f"fund_{asset_type}_ohlcv",
                            provider_key=f"{provider_source}:{symbol}",
                            parameters={"symbol": symbol, "asset_type": asset_type},
                            force=args.force_provider,
                            collect=lambda: execute(worker_collector),
                        )
                        return wrap_result(result, worker_session=worker_session)
                result = runtime.run_task(
                    task=f"fund_{asset_type}_ohlcv",
                    provider_key=f"{provider_source}:{symbol}",
                    parameters={"symbol": symbol, "asset_type": asset_type},
                    force=args.force_provider,
                    collect=lambda: execute(collector),
                )
                return wrap_result(result)

            for result in run_symbol_task_batch(
                batch_symbols,
                max_workers=max_workers,
                collect_symbol=collect_symbol,
                progress=collection_progress_context(args)[0],
                job_name=collection_progress_context(args)[1],
                run_id=collection_progress_context(args)[2],
                stage_key=f"fund_{asset_type}_ohlcv",
                batch_index=batch_index,
                batch_count=len(batches),
                batch_size=collection_batch_size(args),
                total_items=len(symbols),
            ):
                tasks.append(
                    attach_batch_payload(
                        result,
                        batch_index=batch_index,
                        batch_count=len(batches),
                        batch_size=collection_batch_size(args),
                        symbol_count=len(symbols),
                    )
                )
            commit_session_if_possible(session)
        return tasks
    if task_type in {"fund_nav_full_history_backfill", "fund_nav_daily"}:
        if should_refresh_asset_universe_before_incremental(session, market="fund", min_asset_count=20):
            logger.info("基金资产池为空或明显不完整，基金净值任务前先刷新基金资产池")
            warmup_result = runtime.run_task(
                task="fund_universe",
                provider_key="fund_universe_refresh",
                parameters={"limit": None},
                force=args.force_provider,
                collect=lambda: collector.collect_universe(
                    universe_id="universe:base:fund:all",
                    universe_name="基础数据采集基金候选池",
                    strategy_context="base_data_collect",
                ),
            )
            commit_session_if_possible(session)
            preflight_tasks = [warmup_result]
        else:
            preflight_tasks = []
        symbols = resolve_fund_nav_collection_symbols(session, args)
        batches = split_symbol_batches(symbols, batch_size=collection_batch_size(args))
        max_workers = collection_max_workers(args) if session_factory is not None else 1
        tasks = list(preflight_tasks)
        for batch_index, batch_symbols in enumerate(batches, start=1):
            def collect_symbol(symbol: str) -> CollectionTaskResult:
                def execute(target_collector: FundDataCollector) -> Any:
                    return run_rate_limited_collection(
                        "fund_open_fund_info_em",
                        lambda: target_collector.collect_open_fund_nav(
                            symbol=symbol,
                            limit=args.limit,
                        ),
                    )

                def wrap_result(
                    result: CollectionTaskResult,
                    *,
                    worker_session: Any | None = None,
                ) -> CollectionTaskResult:
                    occurred_at = datetime.now(tz=UTC)
                    target_session = worker_session if worker_session is not None else session
                    schedule_retry = bool(getattr(args, "schedule_failure_retry", True))
                    record_fund_symbol_watermark(
                        target_session,
                        symbol=symbol,
                        asset_type="open_fund",
                        data_domain=FUND_NAV_DATA_DOMAIN,
                        provider="akshare:fund_open_fund_info_em",
                        timeframe="1d",
                        result=result,
                        occurred_at=occurred_at,
                        session_factory=None if worker_session is not None else session_factory,
                        schedule_retry=schedule_retry,
                        requested_start=getattr(args, "ashare_start", None),
                        requested_end=getattr(args, "ashare_end", None),
                        sync_task_type=task_type,
                    )
                    return attach_fund_retry_payload(
                        result,
                        provider="akshare:fund_open_fund_info_em",
                        occurred_at=occurred_at,
                        schedule_retry=schedule_retry,
                    )

                if session_factory is not None and max_workers > 1:
                    with session_scope(session_factory) as worker_session:
                        worker_collector = FundDataCollector(worker_session)
                        result = runtime.run_task(
                            task="fund_open_nav",
                            provider_key=f"akshare:fund_open_fund_info_em:{symbol}",
                            parameters={"symbol": symbol},
                            force=args.force_provider,
                            collect=lambda: execute(worker_collector),
                        )
                        return wrap_result(result, worker_session=worker_session)
                result = runtime.run_task(
                    task="fund_open_nav",
                    provider_key=f"akshare:fund_open_fund_info_em:{symbol}",
                    parameters={"symbol": symbol},
                    force=args.force_provider,
                    collect=lambda: execute(collector),
                )
                return wrap_result(result)

            for result in run_symbol_task_batch(
                batch_symbols,
                max_workers=max_workers,
                collect_symbol=collect_symbol,
                progress=collection_progress_context(args)[0],
                job_name=collection_progress_context(args)[1],
                run_id=collection_progress_context(args)[2],
                stage_key="fund_open_nav",
                batch_index=batch_index,
                batch_count=len(batches),
                batch_size=collection_batch_size(args),
                total_items=len(symbols),
            ):
                tasks.append(
                    attach_batch_payload(
                        result,
                        batch_index=batch_index,
                        batch_count=len(batches),
                        batch_size=collection_batch_size(args),
                        symbol_count=len(symbols),
                    )
                )
            commit_session_if_possible(session)
        return tasks
    return [
        runtime.run_task(
            task="fund_universe",
            provider_key="fund_universe_refresh",
            parameters={"limit": None},
            force=args.force_provider,
            collect=lambda: collector.collect_universe(
                universe_id="universe:base:fund:all",
                universe_name="基础数据采集基金候选池",
                strategy_context="base_data_collect",
            ),
        )
    ]


def build_crypto_full_market_refresh_task(
    collector: CryptoDataCollector,
    args: argparse.Namespace,
    runtime: CollectionRuntime,
) -> CollectionTaskResult:
    """构建完整 Binance 交易对刷新任务；该任务不受单批采集大小限制。"""

    return runtime.run_task(
        task="crypto_markets",
        provider_key="ccxt_binance_load_markets",
        parameters={"market_type": args.crypto_market_type, "limit": None},
        force=args.force_provider,
        collect=lambda: collector.collect_markets(
            market_type=args.crypto_market_type,
            universe_id=f"universe:base:crypto:{args.crypto_market_type}:binance",
            universe_name=f"基础数据采集 Binance {args.crypto_market_type} 候选池",
            strategy_context="base_data_collect",
            limit=None,
        ),
    )


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


def ashare_market_bar_circuit_skip_result(
    symbol: str,
    *,
    provider_key: str | None = None,
    circuit_state: Mapping[str, Any] | None = None,
) -> CollectionTaskResult:
    """构造 A 股 K 线 Provider 熔断冷却跳过结果。"""

    key = provider_key or ashare_ohlcv_provider_key(symbol)
    return CollectionTaskResult(
        task="ashare_p0_ohlcv",
        status="skipped",
        raw_record_id=None,
        item_count=0,
        error_message="Provider 熔断冷却中，等待后续批次重跑。",
        payload={
            "symbol": symbol,
            "provider_key": key,
            "circuit_state": dict(circuit_state or {}),
        },
    )


def should_skip_ashare_market_bar_for_open_circuit(
    runtime: Any,
    symbol: str,
    *,
    force: bool = False,
) -> tuple[bool, CollectionTaskResult | None]:
    """派发单标的年度窗口前，先判断运行时熔断是否仍处于打开状态。"""

    if force:
        return False, None
    provider_key = ashare_ohlcv_provider_key(symbol)
    get_provider_state = getattr(runtime, "get_provider_state", None)
    is_circuit_open = getattr(runtime, "is_circuit_open", None)
    if not callable(get_provider_state) or not callable(is_circuit_open):
        return False, None
    try:
        state = get_provider_state(provider_key)
        if is_circuit_open(state):
            return True, ashare_market_bar_circuit_skip_result(
                symbol,
                provider_key=provider_key,
                circuit_state=state if isinstance(state, Mapping) else {},
            )
    except Exception as exc:
        logger.debug("A 股 K 线熔断状态预检查失败，继续按正常流程采集 symbol=%s error=%s", symbol, exc)
    return False, None


def ashare_kline_source_gate(source_key: str, collect: Callable[[], Any]) -> Any:
    """按真实 K 线数据源拆分限流和进度状态。"""

    # A 股 K 线的有效并发由任务队列 worker 控制，源优先级和降级保留在 provider 内部。
    try:
        result = collect()
    except Exception:
        if source_key == "eastmoney_kline":
            emit_eastmoney_kline_cookie_health_progress()
        raise
    if source_key == "eastmoney_kline":
        emit_eastmoney_kline_cookie_health_progress()
    return result


def stock_news_provider_key(symbol: str) -> str:
    """生成按标的隔离的 A 股个股新闻熔断键。"""

    return f"stock_news_em:{symbol}"


def stock_news_article_provider_key(event_id: str) -> str:
    """生成按新闻事件隔离的正文抓取熔断键。"""

    return f"stock_news_article:{event_id}"


def run_rate_limited_collection(source_key: str, collect: Callable[[], Any]) -> Any:
    """按数据源限流执行实际采集函数。"""

    refresh_source_rate_limiter_from_runtime_config()
    with SOURCE_RATE_LIMITER.acquire(source_key):
        try:
            result = collect()
        except Exception as exc:
            SOURCE_RATE_LIMITER.record_failure(source_key, str(exc))
            emit_source_rate_progress(source_key)
            raise
        provider_result = getattr(result, "result", result)
        status = str(getattr(provider_result, "status", "") or "").lower()
        if status in {"error", "failed", "unavailable"}:
            SOURCE_RATE_LIMITER.record_failure(
                source_key,
                str(getattr(provider_result, "error_message", "") or ""),
            )
        else:
            SOURCE_RATE_LIMITER.record_success(source_key)
        emit_source_rate_progress(source_key)
        return result


def refresh_source_rate_limiter_from_runtime_config() -> None:
    """在源请求前热加载最新限频配置，让长任务可在线调速。"""

    global SOURCE_RATE_LIMITER, SOURCE_RATE_POLICY_FINGERPRINT
    args = COLLECTION_RUNTIME_ARGS
    if args is None:
        return
    rate_policies = runtime_scheduler_rate_policies(args)
    if not rate_policies:
        return
    fingerprint = source_rate_policy_fingerprint(rate_policies)
    if fingerprint == SOURCE_RATE_POLICY_FINGERPRINT:
        return
    if hasattr(SOURCE_RATE_LIMITER, "update_policies"):
        SOURCE_RATE_LIMITER.update_policies(rate_policies)
    else:
        SOURCE_RATE_LIMITER = build_source_rate_limiter(rate_policies)
    SOURCE_RATE_POLICY_FINGERPRINT = fingerprint


def source_rate_policy_fingerprint(rate_policies: Mapping[str, Any]) -> str:
    """生成限频配置指纹，用于避免重复刷新同一份配置。"""

    return json.dumps(rate_policies, ensure_ascii=False, sort_keys=True, default=str)


def emit_source_rate_progress(source_key: str) -> None:
    """把当前数据源退避状态写入运行态进度。"""

    progress = COLLECTION_PROGRESS_RECORDER
    if progress is None or not hasattr(progress, "source_rate_updated"):
        return
    if not hasattr(SOURCE_RATE_LIMITER, "adaptive_snapshot"):
        return
    snapshot = SOURCE_RATE_LIMITER.adaptive_snapshot(source_key)
    if not snapshot:
        return
    progress.source_rate_updated(source_key=source_key, snapshot=snapshot)


def emit_eastmoney_kline_cookie_health_progress() -> None:
    """把东方财富 K 线 Cookie 健康状态写入任务监控。"""

    progress = COLLECTION_PROGRESS_RECORDER
    if progress is None or not hasattr(progress, "source_rate_updated"):
        return
    status = eastmoney_kline_cookie_health_status()
    state = str(status.get("state") or "unknown")
    snapshot: JsonDict = {
        "state": state,
        "cooldown_remaining_seconds": int(status.get("cooldown_remaining_seconds") or 0),
        "last_error_message": str(status.get("last_error_message") or ""),
        "failure_rate": 1.0 if state == "cooling" else 0.0,
        "effective_max_concurrency": 0 if state == "cooling" else 1,
    }
    if "probe_ok" in status:
        snapshot["probe_ok"] = bool(status.get("probe_ok"))
    progress.source_rate_updated(source_key=EASTMONEY_KLINE_COOKIE_PROGRESS_SOURCE, snapshot=snapshot)


def crypto_ohlcv_provider_key(market_type: str, symbol: str) -> str:
    """生成按币对隔离的 Binance K 线熔断键。"""

    return f"ccxt_binance_fetch_ohlcv:{market_type}:{symbol.replace('/', '').upper()}"


def crypto_derivative_provider_key(market_type: str, symbol: str) -> str:
    """生成按币对隔离的 Binance 衍生品快照熔断键。"""

    return f"binance_derivative_snapshot:{market_type}:{symbol.replace('/', '').upper()}"


def crypto_symbol_asset_id(market: str, symbol: str) -> str:
    """生成库内数字货币交易对资产 ID。"""

    return f"{market}:{compact_crypto_symbol(symbol)}"


def crypto_symbols_in_failure_cooldown(
    session: Any,
    symbols: list[str],
    *,
    market: str,
    data_domain: str,
    provider: str,
    timeframe: str,
    now: datetime | None = None,
) -> set[str]:
    """批量判断数字货币交易对是否仍处于失败冷却期。"""

    if not symbols:
        return set()
    asset_id_by_symbol = {symbol: crypto_symbol_asset_id(market, symbol) for symbol in symbols}
    try:
        watermarks = _fetch_data_sync_watermarks(
            session,
            list(asset_id_by_symbol.values()),
            data_domain=data_domain,
            provider=provider,
            timeframe=timeframe,
        )
    except Exception as exc:
        logger.warning(
            "Crypto 水位读取失败，本轮继续尝试采集 market=%s domain=%s provider=%s error=%s",
            market,
            data_domain,
            provider,
            exc,
        )
        return set()
    current_time = now or datetime.now(tz=UTC)
    return {
        symbol
        for symbol, asset_id in asset_id_by_symbol.items()
        if not _watermark_allows_collection(watermarks.get(asset_id), now=current_time)
    }


def crypto_watermark_skip_result(
    *,
    task: str,
    symbol: str,
    market: str,
    data_domain: str,
    provider: str,
    timeframe: str,
) -> CollectionTaskResult:
    """构造 Crypto 交易对失败冷却跳过结果。"""

    return CollectionTaskResult(
        task=task,
        status="skipped",
        raw_record_id=None,
        item_count=0,
        error_message="Crypto 交易对处于失败冷却期，等待下次重跑。",
        payload={
            "symbol": symbol,
            "market": market,
            "data_domain": data_domain,
            "provider_key": provider,
            "timeframe": timeframe,
        },
    )


def record_crypto_symbol_watermark(
    session: Any,
    *,
    symbol: str,
    market: str,
    data_domain: str,
    provider: str,
    timeframe: str,
    result: Any,
    occurred_at: datetime | None = None,
    schedule_retry: bool = True,
) -> None:
    """根据单个 Crypto 交易对采集结果更新水位。"""

    try:
        _record_crypto_symbol_watermark(
            session,
            symbol=symbol,
            market=market,
            data_domain=data_domain,
            provider=provider,
            timeframe=timeframe,
            result=result,
            occurred_at=occurred_at,
            schedule_retry=schedule_retry,
        )
    except SQLAlchemyError as exc:
        rollback_session_if_possible(session)
        logger.warning(
            "Crypto 水位记录失败，已回滚当前事务 symbol=%s market=%s domain=%s error=%s",
            symbol,
            market,
            data_domain,
            exc,
            exc_info=True,
        )


def _record_crypto_symbol_watermark(
    session: Any,
    *,
    symbol: str,
    market: str,
    data_domain: str,
    provider: str,
    timeframe: str,
    result: Any,
    occurred_at: datetime | None = None,
    schedule_retry: bool = True,
) -> None:
    """执行 Crypto 交易对水位写入，事务边界由外层负责。"""

    status = str(getattr(result, "status", "") or "").strip().lower()
    if status not in {"available", "error", "unavailable", "failed"}:
        return
    compact_symbol = compact_crypto_symbol(symbol)
    now = occurred_at or datetime.now(tz=UTC)
    repository = DataSyncWatermarkRepository(session)
    payload = {
        "status": status,
        "item_count": result_item_count(result),
        "task": getattr(result, "task", None),
        "provider": provider,
        "timeframe": timeframe,
    } | _result_payload_metadata(result)
    if status == "available":
        repository.record_success(
            asset_id=crypto_symbol_asset_id(market, compact_symbol),
            symbol=compact_symbol,
            market=market,
            data_domain=data_domain,
            provider=provider,
            timeframe=timeframe,
            watermark_at=now,
            occurred_at=now,
            payload=payload,
        )
        return
    repository.record_failure(
        asset_id=crypto_symbol_asset_id(market, compact_symbol),
        symbol=compact_symbol,
        market=market,
        data_domain=data_domain,
        provider=provider,
        timeframe=timeframe,
        occurred_at=now,
        retry_after=timedelta(minutes=15) if schedule_retry else None,
        error_message=result_error_message(result),
        payload=payload,
    )


def should_refresh_asset_universe_before_incremental(
    session: Any,
    *,
    market: str,
    min_asset_count: int | None = None,
) -> bool:
    """判断按资产增量任务前是否需要先刷新完整资产池。"""

    threshold = (
        min_asset_count
        if min_asset_count is not None
        else minimum_asset_count_for_market(market)
    )
    try:
        assets = AssetRepository(session).find_by_market(market)
    except Exception:
        return True
    return len(assets) < threshold


def minimum_asset_count_for_market(market: str) -> int:
    """给不同市场设置识别旧截断资产池的最低数量。"""

    if market == "ashare":
        return 1000
    if market in {"crypto_spot", "crypto_future"}:
        return 20
    if market == "fund":
        return 20
    return 1


def fund_bar_watermark_provider(asset_type: str) -> str:
    """返回基金日 K 对应的数据源标识。"""

    return "akshare:fund_lof_hist_em" if asset_type == "lof" else "akshare:fund_etf_hist_em"


def batch_fund_bar_symbols(
    session: Any,
    *,
    asset_type: str,
    limit: int | None = None,
    fallback_symbol: str,
    timeframe: str = "1d",
    now: datetime | None = None,
    only_failed_or_stale: bool = False,
    stale_before: datetime | None = None,
    required_start_at: datetime | None = None,
    required_end_at: datetime | None = None,
) -> list[str]:
    """按覆盖情况和水位选择基金日 K 任务本轮要处理的代码。"""

    selection_failed = False
    has_candidate_assets = False
    try:
        assets = [
            asset
            for asset in AssetRepository(session).find_by_market("fund")
            if str(getattr(asset, "asset_type", "") or "").strip() == asset_type
        ]
        has_candidate_assets = bool(assets)
        symbol_by_asset_id = {
            str(asset.asset_id): str(getattr(asset, "symbol", "") or "").strip() for asset in assets
        }
        asset_ids = list(symbol_by_asset_id)
        coverage = _fetch_fund_bar_coverage(session, asset_ids, timeframe=timeframe)
        year_coverage: dict[str, dict[int, tuple[int, Any, Any]]] = {}
        if required_start_at is not None and required_end_at is not None:
            year_coverage = _fetch_fund_bar_year_coverage(
                session,
                asset_ids,
                timeframe=timeframe,
                start_at=required_start_at,
                end_at=required_end_at,
            )
        watermarks = _fetch_data_sync_watermarks(
            session,
            asset_ids,
            data_domain=FUND_MARKET_BAR_DATA_DOMAIN,
            provider=fund_bar_watermark_provider(asset_type),
            timeframe=timeframe,
        )
        current_time = now or datetime.now(tz=UTC)
        ranked_assets = sorted(
            [
                asset
                for asset in assets
                if _watermark_allows_collection(watermarks.get(asset.asset_id), now=current_time)
                and (
                    not only_failed_or_stale
                    or _asset_requires_fund_bar_collection(
                        asset,
                        coverage=coverage,
                        watermark=watermarks.get(asset.asset_id),
                        now=current_time,
                        stale_before=stale_before,
                        required_start_at=required_start_at,
                        required_end_at=required_end_at,
                        year_coverage=year_coverage.get(asset.asset_id),
                    )
                )
            ],
            key=lambda asset: (
                _coverage_bar_count(coverage.get(asset.asset_id)),
                _coverage_latest_bar_at(coverage.get(asset.asset_id))
                or datetime.min.replace(tzinfo=UTC),
                symbol_by_asset_id.get(asset.asset_id, ""),
            ),
        )
        symbols = [symbol_by_asset_id.get(asset.asset_id, "") for asset in ranked_assets]
    except Exception as exc:
        logger.warning("基金日 K 标的筛选失败 asset_type=%s error=%s", asset_type, exc, exc_info=True)
        if only_failed_or_stale and has_candidate_assets:
            raise RuntimeError(
                "基金日 K 补采标的筛选失败，已停止任务，避免回退默认标的导致误报完成。"
            ) from exc
        selection_failed = True
        symbols = []
    symbols = [symbol for symbol in symbols if symbol]
    if not symbols:
        if only_failed_or_stale and has_candidate_assets and not selection_failed:
            return []
        symbols = [fallback_symbol]
    return symbols[:limit] if limit else symbols


def batch_open_fund_nav_symbols(
    session: Any,
    *,
    limit: int | None = None,
    fallback_symbol: str,
    now: datetime | None = None,
    only_failed_or_stale: bool = False,
    stale_before: datetime | None = None,
    required_start_at: datetime | None = None,
    required_end_at: datetime | None = None,
) -> list[str]:
    """按净值覆盖和失败水位选择开放式基金任务本轮要处理的代码。"""

    selection_failed = False
    has_candidate_assets = False
    try:
        assets = [
            asset
            for asset in AssetRepository(session).find_by_market("fund")
            if str(getattr(asset, "asset_type", "") or "").strip() == "open_fund"
        ]
        has_candidate_assets = bool(assets)
        symbol_by_asset_id = {
            str(asset.asset_id): str(getattr(asset, "symbol", "") or "").strip() for asset in assets
        }
        asset_ids = list(symbol_by_asset_id)
        coverage = _fetch_fund_nav_coverage(session, asset_ids)
        year_coverage: dict[str, dict[int, tuple[int, Any, Any]]] = {}
        if required_start_at is not None and required_end_at is not None:
            year_coverage = _fetch_fund_nav_year_coverage(
                session,
                asset_ids,
                start_at=required_start_at,
                end_at=required_end_at,
            )
        watermarks = _fetch_data_sync_watermarks(
            session,
            asset_ids,
            data_domain=FUND_NAV_DATA_DOMAIN,
            provider="akshare:fund_open_fund_info_em",
            timeframe="1d",
        )
        current_time = now or datetime.now(tz=UTC)
        ranked_assets = sorted(
            [
                asset
                for asset in assets
                if _watermark_allows_collection(watermarks.get(asset.asset_id), now=current_time)
                and (
                    not only_failed_or_stale
                    or _asset_requires_open_nav_collection(
                        asset,
                        coverage=coverage,
                        watermark=watermarks.get(asset.asset_id),
                        now=current_time,
                        stale_before=stale_before,
                        required_start_at=required_start_at,
                        required_end_at=required_end_at,
                        year_coverage=year_coverage.get(asset.asset_id),
                    )
                )
            ],
            key=lambda asset: (
                coverage.get(asset.asset_id, (0, None))[0],
                _coverage_latest_nav_date(coverage.get(asset.asset_id)) or date.min,
                symbol_by_asset_id.get(asset.asset_id, ""),
            ),
        )
        symbols = [symbol_by_asset_id.get(asset.asset_id, "") for asset in ranked_assets]
    except Exception as exc:
        logger.warning("开放式基金净值标的筛选失败 error=%s", exc, exc_info=True)
        if only_failed_or_stale and has_candidate_assets:
            raise RuntimeError(
                "开放式基金净值补采标的筛选失败，已停止任务，避免回退默认标的导致误报完成。"
            ) from exc
        selection_failed = True
        symbols = []
    symbols = [symbol for symbol in symbols if symbol]
    if not symbols:
        if only_failed_or_stale and has_candidate_assets and not selection_failed:
            return []
        symbols = [fallback_symbol]
    return symbols[:limit] if limit else symbols


def _fetch_fund_bar_coverage(
    session: Any,
    asset_ids: list[str],
    *,
    timeframe: str,
) -> dict[str, tuple[int, datetime | None, datetime | None]]:
    """读取基金日 K 覆盖度。"""

    if not asset_ids:
        return {}
    coverage: dict[str, tuple[int, datetime | None, datetime | None]] = {}
    unique_asset_ids = list(dict.fromkeys(asset_ids))
    for offset in range(0, len(unique_asset_ids), FUND_COVERAGE_QUERY_CHUNK_SIZE):
        chunk_asset_ids = unique_asset_ids[offset : offset + FUND_COVERAGE_QUERY_CHUNK_SIZE]
        statement = (
            select(
                MarketBarORM.asset_id,
                func.count(MarketBarORM.timestamp),
                func.min(MarketBarORM.timestamp),
                func.max(MarketBarORM.timestamp),
            )
            .where(
                MarketBarORM.market == "fund",
                MarketBarORM.timeframe == timeframe,
                MarketBarORM.asset_id.in_(chunk_asset_ids),
            )
            .group_by(MarketBarORM.asset_id)
        )
        coverage.update(
            {
                str(asset_id): (int(count or 0), earliest, latest)
                for asset_id, count, earliest, latest in session.execute(statement)
            }
        )
    return coverage


def _fetch_fund_bar_year_coverage(
    session: Any,
    asset_ids: list[str],
    *,
    timeframe: str,
    start_at: datetime,
    end_at: datetime,
) -> dict[str, dict[int, tuple[int, datetime | None, datetime | None]]]:
    """按自然年读取基金日 K 覆盖，用于初始化任务识别中间年份缺口。"""

    if not asset_ids:
        return {}
    coverage: dict[str, dict[int, tuple[int, datetime | None, datetime | None]]] = {}
    unique_asset_ids = list(dict.fromkeys(asset_ids))
    year_expr = func.extract("year", MarketBarORM.timestamp).label("bar_year")
    for offset in range(0, len(unique_asset_ids), FUND_COVERAGE_QUERY_CHUNK_SIZE):
        chunk_asset_ids = unique_asset_ids[offset : offset + FUND_COVERAGE_QUERY_CHUNK_SIZE]
        statement = (
            select(
                MarketBarORM.asset_id,
                year_expr,
                func.count(MarketBarORM.timestamp),
                func.min(MarketBarORM.timestamp),
                func.max(MarketBarORM.timestamp),
            )
            .where(
                MarketBarORM.market == "fund",
                MarketBarORM.timeframe == timeframe,
                MarketBarORM.asset_id.in_(chunk_asset_ids),
                MarketBarORM.timestamp >= start_at,
                MarketBarORM.timestamp <= end_at,
            )
            .group_by(MarketBarORM.asset_id, year_expr)
        )
        for asset_id, bar_year, count, earliest, latest in session.execute(statement):
            coverage.setdefault(str(asset_id), {})[int(bar_year)] = (
                int(count or 0),
                earliest,
                latest,
            )
    return coverage


def _coverage_bar_count(value: Any) -> int:
    """从 K 线覆盖度元组中读取数量，兼容旧的二元组测试数据。"""

    if not value:
        return 0
    try:
        return max(int(value[0] or 0), 0)
    except (TypeError, ValueError, IndexError):
        return 0


def _coverage_earliest_bar_at(value: Any) -> datetime | None:
    """从 K 线覆盖度元组中读取最早时间；旧二元组没有该信息。"""

    if not value or len(value) < 3:
        return None
    return value[1]


def _coverage_latest_bar_at(value: Any) -> datetime | None:
    """从 K 线覆盖度元组中读取最新时间，兼容旧的二元组测试数据。"""

    if not value:
        return None
    if len(value) >= 3:
        return value[2]
    if len(value) >= 2:
        return value[1]
    return None


def _as_utc_datetime(value: datetime) -> datetime:
    """统一数据库时间的时区，避免朴素时间和 aware 时间直接比较报错。"""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _datetime_before(left: datetime | None, right: datetime | None) -> bool:
    """安全判断 left 是否早于 right。"""

    if left is None or right is None:
        return False
    return _as_utc_datetime(left) < _as_utc_datetime(right)


def _datetime_after(left: datetime | None, right: datetime | None) -> bool:
    """安全判断 left 是否晚于 right。"""

    if left is None or right is None:
        return False
    return _as_utc_datetime(left) > _as_utc_datetime(right)


def _fetch_fund_nav_coverage(
    session: Any,
    asset_ids: list[str],
) -> dict[str, tuple[int, date | None, date | None]]:
    """读取开放式基金净值覆盖度。"""

    if not asset_ids:
        return {}
    coverage: dict[str, tuple[int, date | None, date | None]] = {}
    unique_asset_ids = list(dict.fromkeys(asset_ids))
    for offset in range(0, len(unique_asset_ids), FUND_COVERAGE_QUERY_CHUNK_SIZE):
        chunk_asset_ids = unique_asset_ids[offset : offset + FUND_COVERAGE_QUERY_CHUNK_SIZE]
        statement = (
            select(
                FundNavSnapshotORM.asset_id,
                func.count(FundNavSnapshotORM.nav_date),
                func.min(FundNavSnapshotORM.nav_date),
                func.max(FundNavSnapshotORM.nav_date),
            )
            .where(FundNavSnapshotORM.asset_id.in_(chunk_asset_ids))
            .group_by(FundNavSnapshotORM.asset_id)
        )
        coverage.update(
            {
                str(asset_id): (int(count or 0), earliest, latest)
                for asset_id, count, earliest, latest in session.execute(statement)
            }
        )
    return coverage


def _fetch_fund_nav_year_coverage(
    session: Any,
    asset_ids: list[str],
    *,
    start_at: datetime,
    end_at: datetime,
) -> dict[str, dict[int, tuple[int, date | None, date | None]]]:
    """按自然年读取开放式基金净值覆盖，用于初始化任务识别中间年份缺口。"""

    if not asset_ids:
        return {}
    start_date = start_at.date()
    end_date = end_at.date()
    coverage: dict[str, dict[int, tuple[int, date | None, date | None]]] = {}
    unique_asset_ids = list(dict.fromkeys(asset_ids))
    year_expr = func.extract("year", FundNavSnapshotORM.nav_date).label("nav_year")
    for offset in range(0, len(unique_asset_ids), FUND_COVERAGE_QUERY_CHUNK_SIZE):
        chunk_asset_ids = unique_asset_ids[offset : offset + FUND_COVERAGE_QUERY_CHUNK_SIZE]
        statement = (
            select(
                FundNavSnapshotORM.asset_id,
                year_expr,
                func.count(FundNavSnapshotORM.nav_date),
                func.min(FundNavSnapshotORM.nav_date),
                func.max(FundNavSnapshotORM.nav_date),
            )
            .where(
                FundNavSnapshotORM.asset_id.in_(chunk_asset_ids),
                FundNavSnapshotORM.nav_date >= start_date,
                FundNavSnapshotORM.nav_date <= end_date,
            )
            .group_by(FundNavSnapshotORM.asset_id, year_expr)
        )
        for asset_id, nav_year, count, earliest, latest in session.execute(statement):
            coverage.setdefault(str(asset_id), {})[int(nav_year)] = (
                int(count or 0),
                earliest,
                latest,
            )
    return coverage


def _coverage_earliest_nav_date(value: Any) -> date | None:
    """读取净值覆盖的最早日期，兼容旧的二元组测试数据。"""

    if not value or len(value) < 3:
        return None
    return value[1]


def _coverage_latest_nav_date(value: Any) -> date | None:
    """读取净值覆盖的最新日期，兼容旧的二元组测试数据。"""

    if not value:
        return None
    if len(value) >= 3:
        return value[2]
    if len(value) >= 2:
        return value[1]
    return None


def _date_before(left: date | None, right: datetime | None) -> bool:
    """安全判断日期是否早于 datetime 对应日期。"""

    if left is None or right is None:
        return False
    return left < right.date()


def _date_after(left: date | None, right: datetime | None) -> bool:
    """安全判断日期是否晚于 datetime 对应日期。"""

    if left is None or right is None:
        return False
    return left > right.date()


def _year_coverage_has_missing_year(
    year_coverage: dict[int, tuple[Any, ...]] | None,
    *,
    required_start_at: datetime,
    required_end_at: datetime,
    trust_leading_gap: bool = False,
    trust_trailing_gap: bool = False,
) -> bool:
    """判断起止年份之间是否存在整年覆盖缺口。"""

    if required_start_at > required_end_at:
        return False
    coverage = year_coverage or {}
    covered_years = sorted(
        year for year, value in coverage.items() if _coverage_bar_count(value) > 0
    )
    first_covered_year = covered_years[0] if covered_years else None
    last_covered_year = covered_years[-1] if covered_years else None
    for year in range(required_start_at.year, required_end_at.year + 1):
        if _coverage_bar_count(coverage.get(year)) <= 0:
            if trust_leading_gap and first_covered_year is not None and year < first_covered_year:
                continue
            if trust_trailing_gap and last_covered_year is not None and year > last_covered_year:
                continue
            return True
    return False


def _asset_requires_fund_bar_collection(
    asset: Any,
    *,
    coverage: dict[str, tuple[int, datetime | None, datetime | None]],
    watermark: Any,
    now: datetime,
    stale_before: datetime | None,
    required_start_at: datetime | None = None,
    required_end_at: datetime | None = None,
    year_coverage: dict[int, tuple[int, Any, Any]] | None = None,
) -> bool:
    """判断基金日 K 是否需要补采。"""

    status = str(getattr(watermark, "status", "") or "").lower() if watermark else ""
    next_retry_at = getattr(watermark, "next_retry_at", None) if watermark else None
    watermark_covers_request = _watermark_covers_request(
        watermark,
        required_start_at=required_start_at,
        required_end_at=required_end_at,
    )
    if (
        status == "error"
        and (next_retry_at is None or next_retry_at <= now)
        and not watermark_covers_request
    ):
        return True
    latest_coverage = coverage.get(asset.asset_id)
    bar_count = _coverage_bar_count(latest_coverage)
    earliest_bar_at = _coverage_earliest_bar_at(latest_coverage)
    latest_bar_at = _coverage_latest_bar_at(latest_coverage)
    if bar_count <= 0 or latest_bar_at is None:
        return True
    if _datetime_before(latest_bar_at, required_end_at) and not watermark_covers_request:
        return True
    if (
        required_start_at is not None
        and required_end_at is not None
        and _year_coverage_has_missing_year(
            year_coverage,
            required_start_at=required_start_at,
            required_end_at=required_end_at,
            trust_leading_gap=watermark_covers_request,
            trust_trailing_gap=watermark_covers_request,
        )
    ):
        return True
    if watermark_covers_request:
        return False
    if _datetime_after(earliest_bar_at, required_start_at):
        return True
    if stale_before is not None and latest_bar_at < stale_before:
        return True
    return False


def _asset_requires_open_nav_collection(
    asset: Any,
    *,
    coverage: dict[str, tuple[Any, ...]],
    watermark: Any,
    now: datetime,
    stale_before: datetime | None,
    required_start_at: datetime | None = None,
    required_end_at: datetime | None = None,
    year_coverage: dict[int, tuple[int, Any, Any]] | None = None,
) -> bool:
    """判断开放式基金净值是否需要补采。"""

    status = str(getattr(watermark, "status", "") or "").lower() if watermark else ""
    next_retry_at = getattr(watermark, "next_retry_at", None) if watermark else None
    watermark_covers_request = _watermark_covers_request(
        watermark,
        required_start_at=required_start_at,
        required_end_at=required_end_at,
    )
    watermark_trusts_leading_gap = status in {"available", "completed", "success"} and (
        _watermark_covers_request(
            watermark,
            required_start_at=required_start_at,
            required_end_at=None,
        )
    )
    if status == "unavailable" and watermark_covers_request:
        return False
    if (
        status == "error"
        and (next_retry_at is None or next_retry_at <= now)
        and not watermark_covers_request
    ):
        return True
    coverage_value = coverage.get(asset.asset_id, (0, None))
    nav_count = _coverage_bar_count(coverage_value)
    earliest_nav_date = _coverage_earliest_nav_date(coverage_value)
    latest_nav_date = _coverage_latest_nav_date(coverage_value)
    if nav_count <= 0 or latest_nav_date is None:
        return True
    if _date_before(latest_nav_date, required_end_at) and not watermark_covers_request:
        return True
    if (
        required_start_at is not None
        and required_end_at is not None
        and _year_coverage_has_missing_year(
            year_coverage,
            required_start_at=required_start_at,
            required_end_at=required_end_at,
            trust_leading_gap=watermark_trusts_leading_gap,
            trust_trailing_gap=watermark_covers_request,
        )
    ):
        return True
    if watermark_covers_request:
        return False
    if _date_after(earliest_nav_date, required_start_at) and not watermark_trusts_leading_gap:
        return True
    if stale_before is not None and latest_nav_date < stale_before.date():
        return True
    return False


def batch_ashare_symbols(
    session: Any,
    *,
    limit: int | None = None,
    fallback_symbol: str,
    timeframe: str = "1d",
    now: datetime | None = None,
    only_failed_or_stale: bool = False,
    stale_before: datetime | None = None,
    required_start_at: datetime | None = None,
    required_end_at: datetime | None = None,
) -> list[str]:
    """按 K 线覆盖缺口选择 A 股补采标的。"""

    selection_failed = False
    has_candidate_assets = False
    try:
        repo = AssetRepository(session)
        eligibility = TradeableAssetEligibilityService()
        assets = eligibility.filter_tradeable_assets(repo.find_by_market("ashare"))
        has_candidate_assets = bool(assets)
        symbol_by_asset_id = {
            asset.asset_id: normalize_ashare_symbol(str(getattr(asset, "symbol", "") or ""))
            for asset in assets
        }
        asset_ids = [asset.asset_id for asset in assets]
        coverage = _fetch_ashare_bar_coverage(
            session,
            asset_ids,
            timeframe=timeframe,
        )
        year_coverage: dict[str, dict[int, tuple[int, datetime | None, datetime | None]]] = {}
        if required_start_at is not None and required_end_at is not None:
            try:
                year_coverage = _fetch_ashare_bar_year_coverage(
                    session,
                    asset_ids,
                    timeframe=timeframe,
                    start_at=required_start_at,
                    end_at=required_end_at,
                )
            except Exception as exc:
                logger.warning(
                    "A 股 K 线年度覆盖读取失败，本轮退回首尾水位判断 error=%s",
                    exc,
                    exc_info=True,
                )
        try:
            watermarks = _fetch_data_sync_watermarks(
                session,
                asset_ids,
                data_domain=ASHARE_MARKET_BAR_DATA_DOMAIN,
                provider=ASHARE_MARKET_BAR_WATERMARK_PROVIDER,
                timeframe=timeframe,
            )
        except Exception as exc:
            logger.warning("A 股 K 线水位读取失败，本轮仅按覆盖度排序 error=%s", exc, exc_info=True)
            watermarks = {}
        current_time = now or datetime.now(tz=UTC)
        retryable_assets = [
            asset
            for asset in assets
            if _watermark_allows_collection(watermarks.get(asset.asset_id), now=current_time)
            and (
                not only_failed_or_stale
                or _asset_requires_ashare_bar_collection(
                    asset,
                    coverage=coverage,
                    watermark=watermarks.get(asset.asset_id),
                    now=current_time,
                    stale_before=stale_before,
                    required_start_at=required_start_at,
                    required_end_at=required_end_at,
                    year_coverage=year_coverage.get(asset.asset_id),
                )
            )
        ]
        ranked_assets = sorted(
            retryable_assets,
            key=lambda asset: (
                _coverage_bar_count(coverage.get(asset.asset_id)),
                _coverage_latest_bar_at(coverage.get(asset.asset_id))
                or datetime.min.replace(tzinfo=UTC),
                symbol_by_asset_id.get(asset.asset_id, ""),
            ),
        )
        symbols = []
        for asset in ranked_assets:
            _append_unique_ashare_symbol(symbols, symbol_by_asset_id.get(asset.asset_id, ""))
    except Exception as exc:
        logger.warning(
            "A 股 K 线补采标的筛选失败 fallback_symbol=%s error=%s",
            fallback_symbol,
            exc,
            exc_info=True,
        )
        if only_failed_or_stale and has_candidate_assets:
            raise RuntimeError(
                "A 股 K 线补采标的筛选失败，已停止任务，避免回退默认标的导致误报完成。"
            ) from exc
        selection_failed = True
        symbols = []
    if not symbols:
        if only_failed_or_stale and has_candidate_assets and not selection_failed:
            return []
        symbols = [fallback_symbol]
    if limit:
        return symbols[:limit]
    return symbols


def batch_ashare_fundamental_symbols(
    session: Any,
    *,
    limit: int | None = None,
    fallback_symbol: str,
    now: datetime | None = None,
    only_failed_or_stale: bool = False,
    stale_before: datetime | None = None,
) -> list[str]:
    """按财报报告期和估值水位选择 A 股基本面刷新标的。"""

    selection_failed = False
    has_candidate_assets = False
    try:
        repo = AssetRepository(session)
        eligibility = TradeableAssetEligibilityService()
        assets = eligibility.filter_tradeable_assets(repo.find_by_market("ashare"))
        has_candidate_assets = bool(assets)
        asset_ids = [asset.asset_id for asset in assets]
        valuation_watermarks: dict[str, Any] = {}
        financial_report_periods: dict[str, str] = {}
        if only_failed_or_stale:
            financial_report_periods = _fetch_ashare_financial_report_periods(
                session,
                asset_ids,
            )
            valuation_watermarks = _fetch_data_sync_watermarks(
                session,
                asset_ids,
                data_domain=ASHARE_VALUATION_DATA_DOMAIN,
                provider=ASHARE_VALUATION_PROVIDER,
                timeframe="",
            )
        current_time = now or datetime.now(tz=UTC)
        expected_report_period = expected_ashare_report_period(current_time)
        selected_assets = [
            asset
            for asset in assets
            if (
                not only_failed_or_stale
                or _asset_requires_ashare_fundamental_collection(
                    latest_report_period=financial_report_periods.get(asset.asset_id),
                    expected_report_period=expected_report_period,
                    valuation_watermark=valuation_watermarks.get(asset.asset_id),
                    now=current_time,
                    stale_before=stale_before,
                )
            )
        ]
        symbols = []
        for asset in sorted(
            selected_assets,
            key=lambda item: normalize_ashare_symbol(str(getattr(item, "symbol", "") or "")),
        ):
            _append_unique_ashare_symbol(
                symbols,
                normalize_ashare_symbol(str(getattr(asset, "symbol", "") or "")),
            )
    except Exception as exc:
        logger.warning(
            "A 股基本面刷新标的筛选失败 fallback_symbol=%s error=%s",
            fallback_symbol,
            exc,
            exc_info=True,
        )
        if only_failed_or_stale and has_candidate_assets:
            raise RuntimeError(
                "A 股基本面刷新标的筛选失败，已停止任务，避免回退默认标的导致误报完成。"
            ) from exc
        selection_failed = True
        symbols = []
    if not symbols:
        if only_failed_or_stale and has_candidate_assets and not selection_failed:
            return []
        symbols = [fallback_symbol]
    if limit:
        return symbols[:limit]
    return symbols


def batch_ashare_northbound_symbols(
    session: Any,
    *,
    limit: int | None = None,
    fallback_symbol: str,
    now: datetime | None = None,
) -> list[str]:
    """按北向个股水位选择需要刷新的主板标的。"""

    symbols: list[str] = []
    try:
        repo = AssetRepository(session)
        eligibility = TradeableAssetEligibilityService()
        assets = eligibility.filter_tradeable_assets(repo.find_by_market("ashare"))
        asset_ids = [str(asset.asset_id) for asset in assets]
        watermarks = _fetch_data_sync_watermarks(
            session,
            asset_ids,
            data_domain=ASHARE_CAPITAL_FLOW_DATA_DOMAIN,
            provider=ASHARE_NORTHBOUND_INDIVIDUAL_PROVIDER,
            timeframe=ASHARE_NORTHBOUND_INDIVIDUAL_TIMEFRAME,
        )
        current_time = now or datetime.now(tz=UTC)
        eligible_assets = [
            asset
            for asset in assets
            if _watermark_allows_collection(watermarks.get(str(asset.asset_id)), now=current_time)
        ]
        ranked_assets = sorted(
            eligible_assets,
            key=lambda asset: (
                _northbound_watermark_time(watermarks.get(str(asset.asset_id)))
                or datetime.min.replace(tzinfo=UTC),
                normalize_ashare_symbol(str(getattr(asset, "symbol", "") or "")),
            ),
        )
        for asset in ranked_assets:
            _append_unique_ashare_symbol(
                symbols,
                normalize_ashare_symbol(str(getattr(asset, "symbol", "") or "")),
            )
    except Exception as exc:
        logger.warning(
            "北向个股采集标的筛选失败 fallback_symbol=%s error=%s",
            fallback_symbol,
            exc,
            exc_info=True,
        )
        symbols = []
    if not symbols:
        symbols = [fallback_symbol]
    if limit:
        return symbols[:limit]
    return symbols


def _northbound_watermark_time(watermark: Any) -> datetime | None:
    """读取北向个股水位时间，用于小批轮转覆盖。"""

    if watermark is None:
        return None
    return getattr(watermark, "watermark_at", None) or getattr(
        watermark,
        "last_success_at",
        None,
    )


def _asset_requires_ashare_fundamental_collection(
    *,
    latest_report_period: str | date | datetime | None,
    expected_report_period: date,
    valuation_watermark: Any,
    now: datetime,
    stale_before: datetime | None,
) -> bool:
    """判断单个 A 股标的是否需要重新同步基本面或估值。"""

    return _ashare_report_period_requires_collection(
        latest_report_period,
        expected_report_period=expected_report_period,
    ) or _snapshot_watermark_requires_collection(
        valuation_watermark,
        now=now,
        stale_before=stale_before,
    )


def _ashare_report_period_requires_collection(
    latest_report_period: str | date | datetime | None,
    *,
    expected_report_period: date,
) -> bool:
    """报告期缺失或早于当前最低应有报告期时触发财务补采。"""

    normalized = _parse_ashare_report_period(latest_report_period)
    return normalized is None or normalized < expected_report_period


def _parse_ashare_report_period(value: str | date | datetime | None) -> date | None:
    """解析财务快照中的 YYYYMMDD 或 ISO 报告期。"""

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text_value = str(value or "").strip()
    if not text_value:
        return None
    for date_format in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text_value, date_format).date()
        except ValueError:
            continue
    return None


def _fetch_ashare_financial_report_periods(
    session: Any,
    asset_ids: list[str],
) -> dict[str, str]:
    """分块查询主要财务源每个标的的最新报告期。"""

    if not asset_ids:
        return {}
    periods: dict[str, str] = {}
    unique_asset_ids = list(dict.fromkeys(asset_ids))
    for offset in range(
        0,
        len(unique_asset_ids),
        ASHARE_FUNDAMENTAL_COVERAGE_QUERY_CHUNK_SIZE,
    ):
        chunk_asset_ids = unique_asset_ids[
            offset : offset + ASHARE_FUNDAMENTAL_COVERAGE_QUERY_CHUNK_SIZE
        ]
        statement = (
            select(
                FundamentalSnapshotORM.asset_id,
                func.max(FundamentalSnapshotORM.report_period),
            )
            .where(
                FundamentalSnapshotORM.asset_id.in_(chunk_asset_ids),
                FundamentalSnapshotORM.source == ASHARE_FINANCIAL_INDICATOR_SOURCE,
                FundamentalSnapshotORM.report_period.is_not(None),
            )
            .group_by(FundamentalSnapshotORM.asset_id)
        )
        for asset_id, report_period in session.execute(statement):
            if asset_id and report_period:
                periods[str(asset_id)] = str(report_period)
    return periods


def _snapshot_watermark_requires_collection(
    watermark: Any,
    *,
    now: datetime,
    stale_before: datetime | None,
) -> bool:
    """根据快照型数据水位判断是否需要采集。"""

    if watermark is None:
        return True
    status = str(getattr(watermark, "status", "") or "").lower()
    next_retry_at = getattr(watermark, "next_retry_at", None)
    if status == "error":
        return next_retry_at is None or next_retry_at <= now
    if status not in {"available", "completed", "success"}:
        return True
    watermark_at = getattr(watermark, "watermark_at", None) or getattr(
        watermark,
        "last_success_at",
        None,
    )
    if stale_before is None:
        return False
    if watermark_at is None:
        return True
    return _datetime_before(watermark_at, stale_before)


def _fetch_ashare_bar_coverage(
    session: Any,
    asset_ids: list[str],
    *,
    timeframe: str,
) -> dict[str, tuple[int, datetime | None, datetime | None]]:
    """查询 A 股标的已有 K 线覆盖情况。"""

    if not asset_ids:
        return {}
    coverage: dict[str, tuple[int, datetime | None, datetime | None]] = {}
    unique_asset_ids = list(dict.fromkeys(asset_ids))
    for offset in range(0, len(unique_asset_ids), ASHARE_BAR_COVERAGE_QUERY_CHUNK_SIZE):
        chunk_asset_ids = unique_asset_ids[
            offset : offset + ASHARE_BAR_COVERAGE_QUERY_CHUNK_SIZE
        ]
        statement = (
            select(
                MarketBarORM.asset_id,
                func.count(MarketBarORM.timestamp),
                func.min(MarketBarORM.timestamp),
                func.max(MarketBarORM.timestamp),
            )
            .where(
                MarketBarORM.market == "ashare",
                MarketBarORM.timeframe == timeframe,
                MarketBarORM.asset_id.in_(chunk_asset_ids),
            )
            .group_by(MarketBarORM.asset_id)
        )
        coverage.update(
            {
                str(asset_id): (int(count or 0), earliest, latest)
                for asset_id, count, earliest, latest in session.execute(statement)
            }
        )
    return coverage


def _fetch_ashare_bar_year_coverage(
    session: Any,
    asset_ids: list[str],
    *,
    timeframe: str,
    start_at: datetime,
    end_at: datetime,
) -> dict[str, dict[int, tuple[int, datetime | None, datetime | None]]]:
    """按自然年查询 A 股 K 线覆盖，用于 10 年初始化的缺口窗口规划。"""

    if not asset_ids:
        return {}
    coverage: dict[str, dict[int, tuple[int, datetime | None, datetime | None]]] = {}
    unique_asset_ids = list(dict.fromkeys(asset_ids))
    year_expr = func.extract("year", MarketBarORM.timestamp).label("bar_year")
    for offset in range(0, len(unique_asset_ids), ASHARE_BAR_COVERAGE_QUERY_CHUNK_SIZE):
        chunk_asset_ids = unique_asset_ids[
            offset : offset + ASHARE_BAR_COVERAGE_QUERY_CHUNK_SIZE
        ]
        statement = (
            select(
                MarketBarORM.asset_id,
                year_expr,
                func.count(MarketBarORM.timestamp),
                func.min(MarketBarORM.timestamp),
                func.max(MarketBarORM.timestamp),
            )
            .where(
                MarketBarORM.market == "ashare",
                MarketBarORM.timeframe == timeframe,
                MarketBarORM.asset_id.in_(chunk_asset_ids),
                MarketBarORM.timestamp >= start_at,
                MarketBarORM.timestamp <= end_at,
            )
            .group_by(MarketBarORM.asset_id, year_expr)
        )
        for asset_id, bar_year, count, earliest, latest in session.execute(statement):
            asset_coverage = coverage.setdefault(str(asset_id), {})
            asset_coverage[int(bar_year)] = (int(count or 0), earliest, latest)
    return coverage


def plan_ashare_market_bar_backfill_windows(
    session: Any,
    symbols: list[str],
    *,
    timeframe: str,
    required_start_at: datetime,
    required_end_at: datetime,
    trusted_leading_gap_symbols: Collection[str] | None = None,
) -> dict[str, list[tuple[str, str]]]:
    """为 10 年 A 股日 K 初始化规划实际需要请求的年度缺口窗口。"""

    normalized_symbols: list[str] = []
    for symbol in symbols:
        normalized = normalize_ashare_symbol(str(symbol or ""))
        if normalized:
            normalized_symbols.append(normalized)
    asset_ids = [f"ashare:{symbol}" for symbol in normalized_symbols]
    trusted_symbols = {
        normalize_ashare_symbol(str(symbol or ""))
        for symbol in (trusted_leading_gap_symbols or ())
    }
    year_coverage = _fetch_ashare_bar_year_coverage(
        session,
        asset_ids,
        timeframe=timeframe,
        start_at=required_start_at,
        end_at=required_end_at,
    )
    return {
        symbol: _ashare_bar_missing_year_windows(
            year_coverage.get(f"ashare:{symbol}", {}),
            required_start_at=required_start_at,
            required_end_at=required_end_at,
            trust_leading_gap=symbol in trusted_symbols,
        )
        for symbol in normalized_symbols
    }


def _fetch_data_sync_watermarks(
    session: Any,
    asset_ids: list[str],
    *,
    data_domain: str,
    provider: str,
    timeframe: str,
) -> dict[str, DataSyncWatermarkORM]:
    """按资产读取指定数据域的采集水位，用于跳过仍在冷却期的失败标的。"""

    if not asset_ids:
        return {}
    statement = select(DataSyncWatermarkORM).where(
        DataSyncWatermarkORM.asset_id.in_(asset_ids),
        DataSyncWatermarkORM.data_domain == data_domain,
        DataSyncWatermarkORM.provider == provider,
        DataSyncWatermarkORM.timeframe == timeframe,
    )
    return {str(item.asset_id): item for item in session.scalars(statement)}


def _watermark_allows_collection(watermark: Any, *, now: datetime) -> bool:
    """失败冷却期未到的标的不参与本轮采集，避免连续请求不稳定数据源。"""

    if watermark is None:
        return True
    status = str(getattr(watermark, "status", "") or "").lower()
    next_retry_at = getattr(watermark, "next_retry_at", None)
    if status != "error" or next_retry_at is None:
        return True
    return next_retry_at <= now


def _watermark_covers_request(
    watermark: Any,
    *,
    required_start_at: datetime | None,
    required_end_at: datetime | None,
) -> bool:
    """判断水位 payload 是否能证明本次请求窗口曾经被完整验证。"""

    if watermark is None:
        return False
    payload = getattr(watermark, "payload", None)
    if not isinstance(payload, dict):
        return False
    start_token = _format_ashare_request_date(required_start_at)
    end_token = _format_ashare_request_date(required_end_at)
    status = str(getattr(watermark, "status", "") or "").lower()
    if status in {"available", "completed", "success"}:
        payload_start = str(payload.get("requested_start") or "").strip()
        payload_end = str(payload.get("requested_end") or "").strip()
    else:
        payload_start = str(payload.get("verified_requested_start") or "").strip()
        payload_end = str(payload.get("verified_requested_end") or "").strip()
    if start_token and (not payload_start or payload_start > start_token):
        return False
    if end_token and (not payload_end or payload_end < end_token):
        return False
    return bool(start_token or end_token)


def _watermark_covers_ashare_bar_request(
    watermark: Any,
    *,
    required_start_at: datetime | None,
    required_end_at: datetime | None,
) -> bool:
    """判断成功水位是否已经覆盖本次 A 股 K 线请求窗口。"""

    if watermark is None:
        return False
    status = str(getattr(watermark, "status", "") or "").lower()
    if status not in {"available", "completed", "success"}:
        return False
    return _watermark_covers_request(
        watermark,
        required_start_at=required_start_at,
        required_end_at=required_end_at,
    )


def _ashare_bar_missing_year_windows(
    year_coverage: dict[int, tuple[int, datetime | None, datetime | None]] | None,
    *,
    required_start_at: datetime,
    required_end_at: datetime,
    trust_leading_gap: bool = False,
) -> list[tuple[str, str]]:
    """根据年度覆盖情况生成需要请求的 A 股 K 线窗口。"""

    if required_start_at > required_end_at:
        return []
    coverage = year_coverage or {}
    covered_years = sorted(
        year for year, value in coverage.items() if _coverage_bar_count(value) > 0
    )
    if not covered_years:
        return [
            (
                _format_ashare_request_date(required_start_at) or "",
                _format_ashare_request_date(required_end_at) or "",
            )
        ]

    first_covered_year = covered_years[0]
    windows: list[tuple[str, str]] = []
    for year in range(required_start_at.year, required_end_at.year + 1):
        window_start = datetime(year, 1, 1, tzinfo=UTC)
        window_end = datetime(year, 12, 31, tzinfo=UTC)
        if year == required_start_at.year:
            window_start = max(window_start, _as_utc_datetime(required_start_at))
        if year == required_end_at.year:
            window_end = min(window_end, _as_utc_datetime(required_end_at))
        if window_start > window_end:
            continue

        coverage_value = coverage.get(year)
        if _coverage_bar_count(coverage_value) <= 0:
            if trust_leading_gap and year < first_covered_year:
                continue
            windows.append(
                (
                    _format_ashare_request_date(window_start) or "",
                    _format_ashare_request_date(window_end) or "",
                )
            )
            continue

        if year == required_end_at.year and _datetime_before(
            _coverage_latest_bar_at(coverage_value),
            window_end,
        ):
            windows.append(
                (
                    _format_ashare_request_date(window_start) or "",
                    _format_ashare_request_date(window_end) or "",
                )
            )
    return windows


def _format_ashare_request_date(value: Any) -> str | None:
    """把请求日期统一成 YYYYMMDD 字符串，便于水位覆盖比较。"""

    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    text = str(value).strip()
    if len(text) >= 8 and text[:8].isdigit():
        return text[:8]
    return None


def _asset_requires_ashare_bar_collection(
    asset: Any,
    *,
    coverage: dict[str, tuple[Any, ...]],
    watermark: Any,
    now: datetime,
    stale_before: datetime | None,
    required_start_at: datetime | None = None,
    required_end_at: datetime | None = None,
    year_coverage: dict[int, tuple[int, datetime | None, datetime | None]] | None = None,
) -> bool:
    """判断资产是否需要被 revision/补漏任务重新采集。"""

    status = str(getattr(watermark, "status", "") or "").lower() if watermark else ""
    next_retry_at = getattr(watermark, "next_retry_at", None) if watermark else None
    watermark_covers_request = _watermark_covers_request(
        watermark,
        required_start_at=required_start_at,
        required_end_at=required_end_at,
    )
    if (
        status == "error"
        and (next_retry_at is None or next_retry_at <= now)
        and not watermark_covers_request
    ):
        return True
    coverage_value = coverage.get(asset.asset_id)
    bar_count = _coverage_bar_count(coverage_value)
    earliest_bar_at = _coverage_earliest_bar_at(coverage_value)
    latest_bar_at = _coverage_latest_bar_at(coverage_value)
    if bar_count <= 0 or latest_bar_at is None:
        return True
    if required_end_at is not None and _datetime_before(latest_bar_at, required_end_at):
        return True
    if (
        required_start_at is not None
        and required_end_at is not None
        and year_coverage is not None
        and _ashare_bar_missing_year_windows(
            year_coverage,
            required_start_at=required_start_at,
            required_end_at=required_end_at,
            trust_leading_gap=watermark_covers_request,
        )
    ):
        return True
    if watermark_covers_request:
        return False
    if required_start_at is not None:
        if earliest_bar_at is None or _datetime_after(earliest_bar_at, required_start_at):
            return True
        return False
    if stale_before is not None and _datetime_before(latest_bar_at, stale_before):
        return True
    return False


def record_ashare_market_bar_watermark(
    session: Any,
    *,
    symbol: str,
    timeframe: str,
    result: Any,
    occurred_at: datetime | None = None,
    session_factory: Any | None = None,
    schedule_retry: bool = True,
) -> None:
    """根据单标的 K 线采集结果更新成功水位或失败重试时间。"""

    if session_factory is not None:
        watermark_session = session_factory()
        try:
            _record_ashare_market_bar_watermark(
                watermark_session,
                symbol=symbol,
                timeframe=timeframe,
                result=result,
                occurred_at=occurred_at,
                schedule_retry=schedule_retry,
            )
            watermark_session.commit()
        except SQLAlchemyError as exc:
            rollback_session_if_possible(watermark_session)
            logger.warning(
                "A 股 K 线水位记录失败，已回滚独立事务并跳过本次水位更新 symbol=%s timeframe=%s error=%s",
                symbol,
                timeframe,
                exc,
                exc_info=True,
            )
        finally:
            close = getattr(watermark_session, "close", None)
            if callable(close):
                close()
        return

    try:
        _record_ashare_market_bar_watermark(
            session,
            symbol=symbol,
            timeframe=timeframe,
            result=result,
            occurred_at=occurred_at,
            schedule_retry=schedule_retry,
        )
    except SQLAlchemyError as exc:
        rollback_session_if_possible(session)
        logger.warning(
            "A 股 K 线水位记录失败，已回滚当前事务并跳过本次水位更新 symbol=%s timeframe=%s error=%s",
            symbol,
            timeframe,
            exc,
            exc_info=True,
        )


def _record_ashare_market_bar_watermark(
    session: Any,
    *,
    symbol: str,
    timeframe: str,
    result: Any,
    occurred_at: datetime | None = None,
    schedule_retry: bool = True,
) -> None:
    """执行单个 A 股 K 线水位写入；事务边界由外层函数负责。"""

    status = str(getattr(result, "status", "") or "").strip().lower()
    if status not in {"available", "error", "unavailable", "failed"}:
        return

    asset_id = f"ashare:{symbol}"
    now = occurred_at or datetime.now(tz=UTC)
    repository = DataSyncWatermarkRepository(session)
    if status == "available":
        coverage = _fetch_ashare_bar_coverage(session, [asset_id], timeframe=timeframe)
        coverage_value = coverage.get(asset_id)
        latest_bar_count = _coverage_bar_count(coverage_value)
        latest_bar_at = _coverage_latest_bar_at(coverage_value)
        success_payload = {
            "item_count": result_item_count(result),
            "latest_bar_count": latest_bar_count,
        } | _ashare_market_bar_watermark_metadata(result)
        repository.record_success(
            asset_id=asset_id,
            symbol=symbol,
            market="ashare",
            data_domain=ASHARE_MARKET_BAR_DATA_DOMAIN,
            provider=ASHARE_MARKET_BAR_WATERMARK_PROVIDER,
            timeframe=timeframe,
            watermark_at=latest_bar_at,
            occurred_at=now,
            payload=success_payload,
        )
        return

    error_message = result_error_message(result)
    try:
        previous_watermark = _fetch_data_sync_watermarks(
            session,
            [asset_id],
            data_domain=ASHARE_MARKET_BAR_DATA_DOMAIN,
            provider=ASHARE_MARKET_BAR_WATERMARK_PROVIDER,
            timeframe=timeframe,
        ).get(asset_id)
    except Exception as exc:
        previous_watermark = None
        logger.debug("读取 A 股 K 线既有水位失败，失败记录不携带已验证窗口 symbol=%s error=%s", symbol, exc)
    failure_payload = {
        "status": status,
        "item_count": result_item_count(result),
    } | _ashare_market_bar_watermark_metadata(result)
    if previous_watermark is not None:
        previous_payload = getattr(previous_watermark, "payload", None)
        if isinstance(previous_payload, dict):
            previous_task_type = previous_payload.get("sync_task_type")
            if previous_task_type not in (None, ""):
                failure_payload.setdefault("sync_task_type", previous_task_type)
            if _watermark_covers_request(
                previous_watermark,
                required_start_at=parse_ashare_datetime_or_none(failure_payload.get("requested_start")),
                required_end_at=parse_ashare_datetime_or_none(failure_payload.get("requested_end")),
            ):
                verified_start = (
                    failure_payload.get("requested_start")
                    or previous_payload.get("verified_requested_start")
                    or previous_payload.get("requested_start")
                )
                verified_end = (
                    failure_payload.get("requested_end")
                    or previous_payload.get("verified_requested_end")
                    or previous_payload.get("requested_end")
                )
                if verified_start not in (None, ""):
                    failure_payload.setdefault("verified_requested_start", verified_start)
                if verified_end not in (None, ""):
                    failure_payload.setdefault("verified_requested_end", verified_end)
    repository.record_failure(
        asset_id=asset_id,
        symbol=symbol,
        market="ashare",
        data_domain=ASHARE_MARKET_BAR_DATA_DOMAIN,
        provider=ASHARE_MARKET_BAR_WATERMARK_PROVIDER,
        timeframe=timeframe,
        occurred_at=now,
        retry_after=timedelta(minutes=15) if schedule_retry else None,
        error_message=error_message,
        payload=failure_payload,
    )


def _ashare_market_bar_watermark_metadata(result: Any) -> dict[str, Any]:
    """挑选可用于断点续跑判断的轻量元数据写入水位。"""

    payload = getattr(result, "payload", None)
    if not isinstance(payload, dict):
        return {}
    metadata: dict[str, Any] = {}
    for key in ("sync_task_type", "requested_start", "requested_end"):
        value = payload.get(key)
        if value is not None and value != "":
            metadata[key] = value
    return metadata


def merge_ashare_market_bar_window_results(
    results: list[CollectionTaskResult],
    *,
    windows: list[tuple[str | None, str | None]],
) -> CollectionTaskResult:
    """把同一标的多个年度缺口窗口的采集摘要合并为标的级结果。"""

    if not results:
        return CollectionTaskResult(
            task="ashare_p0_ohlcv",
            status="unavailable",
            raw_record_id=None,
            item_count=0,
            error_message="没有年度缺口窗口采集结果",
            payload={"backfill_windows": windows},
        )

    failed_windows: list[JsonDict] = []
    completed_windows: list[JsonDict] = []
    skipped_windows: list[JsonDict] = []
    empty_windows: list[JsonDict] = []
    item_count = 0
    actual_sources: list[Any] = []
    error_message_counts: dict[str, int] = {}
    for index, result in enumerate(results):
        window = windows[index] if index < len(windows) else (None, None)
        window_status = result_status_name(result)
        window_payload = {
            "start": window[0],
            "end": window[1],
            "status": window_status,
            "item_count": result_item_count(result),
        }
        if window_status in {"available", "completed"}:
            item_count += result_item_count(result)
            completed_windows.append(window_payload)
            actual_source = getattr(result, "payload", {}).get("actual_source")
            if actual_source:
                actual_sources.append(actual_source)
            continue
        if window_status in {"skipped", "locked"}:
            skipped_windows.append(window_payload | {"error_message": result_error_message(result)})
            if result_error_message(result):
                message = str(result_error_message(result))
                error_message_counts[message] = error_message_counts.get(message, 0) + 1
            continue
        if window_status == "failed" and result_error_message(result) is None:
            empty_windows.append(
                window_payload
                | {
                    "status": str(getattr(result, "status", "") or "unavailable"),
                    "raw_record_id": getattr(result, "raw_record_id", None),
                }
            )
            continue
        failed_windows.append(
            window_payload | {"error_message": result_error_message(result)}
        )
        if result_error_message(result):
            message = str(result_error_message(result))
            error_message_counts[message] = error_message_counts.get(message, 0) + 1

    distinct_error_messages = list(error_message_counts)

    status = "available"
    if failed_windows:
        status = "error"
    elif skipped_windows:
        status = "skipped"
    elif empty_windows:
        status = "skipped"
    return CollectionTaskResult(
        task="ashare_p0_ohlcv",
        status=status,
        raw_record_id=None,
        item_count=item_count,
        error_message=None
        if status == "available"
        else "; ".join(distinct_error_messages),
        payload={
            "actual_source": actual_sources[0] if len(actual_sources) == 1 else actual_sources,
            "backfill_windows": [
                {"start": start, "end": end} for start, end in windows
            ],
            "completed_windows": completed_windows,
            "skipped_windows": skipped_windows,
            "empty_windows": empty_windows,
            "failed_windows": failed_windows,
            "error_message_counts": error_message_counts,
            "window_count": len(windows),
        },
    )


def attach_ashare_market_bar_retry_payload(
    result: Any,
    *,
    occurred_at: datetime | None = None,
    schedule_retry: bool = True,
) -> Any:
    """给 A 股 K 线失败结果补充下一次可重试时间，供任务监控页展示。"""

    status = str(getattr(result, "status", "") or "").strip().lower()
    if status not in {"error", "unavailable", "failed"}:
        return result
    now = occurred_at or datetime.now(tz=UTC)
    payload = {
        "provider_key": ASHARE_MARKET_BAR_WATERMARK_PROVIDER,
        "error_category": classify_collection_error(result_error_message(result)),
    }
    if schedule_retry:
        retry_after = timedelta(minutes=15)
        payload.update(
            retry_after_seconds=int(retry_after.total_seconds()),
            next_retry_at=(now + retry_after).isoformat(),
        )
    return attach_batch_payload(result, **payload)


def record_ashare_fundamental_watermark(
    session: Any,
    *,
    symbol: str,
    data_domain: str,
    provider: str,
    result: Any,
    occurred_at: datetime | None = None,
    session_factory: Any | None = None,
    schedule_retry: bool = True,
) -> None:
    """根据单股票财务/估值采集结果更新快照型水位。"""

    if session_factory is not None:
        watermark_session = session_factory()
        try:
            _record_ashare_fundamental_watermark(
                watermark_session,
                symbol=symbol,
                data_domain=data_domain,
                provider=provider,
                result=result,
                occurred_at=occurred_at,
                schedule_retry=schedule_retry,
            )
            watermark_session.commit()
        except SQLAlchemyError as exc:
            rollback_session_if_possible(watermark_session)
            logger.warning(
                "A 股基本面水位记录失败，已回滚独立事务 symbol=%s domain=%s error=%s",
                symbol,
                data_domain,
                exc,
                exc_info=True,
            )
        finally:
            close = getattr(watermark_session, "close", None)
            if callable(close):
                close()
        return

    try:
        _record_ashare_fundamental_watermark(
            session,
            symbol=symbol,
            data_domain=data_domain,
            provider=provider,
            result=result,
            occurred_at=occurred_at,
            schedule_retry=schedule_retry,
        )
    except SQLAlchemyError as exc:
        rollback_session_if_possible(session)
        logger.warning(
            "A 股基本面水位记录失败，已回滚当前事务 symbol=%s domain=%s error=%s",
            symbol,
            data_domain,
            exc,
            exc_info=True,
        )


def _record_ashare_fundamental_watermark(
    session: Any,
    *,
    symbol: str,
    data_domain: str,
    provider: str,
    result: Any,
    occurred_at: datetime | None = None,
    schedule_retry: bool = True,
) -> None:
    """执行单股票财务/估值水位写入，事务边界由外层负责。"""

    status = str(getattr(result, "status", "") or "").strip().lower()
    if status not in {"available", "error", "unavailable", "failed"}:
        return

    asset_id = f"ashare:{symbol}"
    now = occurred_at or datetime.now(tz=UTC)
    repository = DataSyncWatermarkRepository(session)
    payload = {
        "status": status,
        "item_count": result_item_count(result),
        "task": getattr(result, "task", None),
        "provider": provider,
    } | _result_payload_metadata(result)
    if status == "available":
        repository.record_success(
            asset_id=asset_id,
            symbol=symbol,
            market="ashare",
            data_domain=data_domain,
            provider=provider,
            timeframe="",
            watermark_at=now,
            occurred_at=now,
            payload=payload,
        )
        return

    repository.record_failure(
        asset_id=asset_id,
        symbol=symbol,
        market="ashare",
        data_domain=data_domain,
        provider=provider,
        timeframe="",
        occurred_at=now,
        retry_after=timedelta(minutes=15) if schedule_retry else None,
        error_message=result_error_message(result),
        payload=payload,
    )


def _result_payload_metadata(result: Any) -> dict[str, Any]:
    """抽取采集结果 payload 中适合写入水位的轻量元数据。"""

    payload = getattr(result, "payload", None)
    if not isinstance(payload, dict):
        return {}
    metadata: dict[str, Any] = {}
    for key in ("actual_source", "raw_record_id", "batch_index", "batch_count", "sync_task_type"):
        value = payload.get(key)
        if value is not None and value != "":
            metadata[key] = value
    return metadata


def ashare_capital_flow_watermark_asset_id(indicator: str) -> str:
    """生成资金流榜单逻辑水位 item 标识。"""

    safe_indicator = str(indicator or "default").strip() or "default"
    return f"ashare:capital_flow:rank:{safe_indicator}"


def _compact_risk_date(value: Any, *, month_day: bool = False) -> str:
    """将 YYYYMMDD 日期压缩为水位短键片段。"""

    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        return text[4:] if month_day else text[2:]
    return ""


def _compact_risk_date_window(parameters: Mapping[str, Any]) -> str:
    """生成不超过 11 字符的风险窗口短键，例如 260501-0514。"""

    start_token = _compact_risk_date(parameters.get("start_date"))
    end_token = _compact_risk_date(parameters.get("end_date"), month_day=True)
    if start_token and end_token:
        return f"{start_token}-{end_token}"
    end_full_token = _compact_risk_date(parameters.get("end_date") or parameters.get("date"))
    if end_full_token:
        return end_full_token
    if start_token:
        return start_token
    return "latest"


def ashare_risk_sentiment_watermark_timeframe(
    *,
    task: str,
    provider: str,
    parameters: Mapping[str, Any],
) -> str:
    """生成风险情绪子源水位粒度，压缩到 data_sync_watermarks 字段限制内。"""

    if task == "ashare_risk_stop_list":
        return "stop_list"
    if task == "ashare_sentiment_hot_rank":
        return "hot_rank"
    if task == "ashare_sentiment_zt_pool":
        return f"zt_pool:{parameters.get('date') or 'latest'}"
    if task in {"ashare_risk_lhb_detail", "ashare_risk_margin_sse"}:
        prefix = "lhb" if task == "ashare_risk_lhb_detail" else "msse"
        return f"{prefix}:{_compact_risk_date_window(parameters)}"
    if task == "ashare_risk_block_trades":
        return f"dzjy:{_compact_risk_date_window(parameters)}"
    if task == "ashare_risk_restricted_release":
        end_date = str(parameters.get("end_date") or "").strip() or "latest"
        return f"rr:{end_date}"
    if task == "ashare_risk_pledge_ratio":
        return "pledge_ratio"
    if task == "ashare_risk_margin_szse":
        return f"mszse:{_compact_risk_date_window(parameters)}"
    return task or provider


def ashare_risk_sentiment_watermark_asset_id(provider: str, *, timeframe: str) -> str:
    """生成风险情绪子源逻辑水位 item 标识。"""

    safe_provider = str(provider or "unknown").strip() or "unknown"
    safe_timeframe = str(timeframe or "default").strip() or "default"
    return f"ashare:risk_sentiment:{safe_provider}:{safe_timeframe}"


def ashare_risk_sentiment_watermark_allows_collection(
    session: Any,
    *,
    provider: str,
    timeframe: str,
    now: datetime | None = None,
) -> bool:
    """判断指定风险情绪子源是否处于可采集状态。"""

    asset_id = ashare_risk_sentiment_watermark_asset_id(provider, timeframe=timeframe)
    try:
        watermarks = _fetch_data_sync_watermarks(
            session,
            [asset_id],
            data_domain=ASHARE_RISK_SENTIMENT_DATA_DOMAIN,
            provider=provider,
            timeframe=timeframe,
        )
    except Exception as exc:
        logger.warning(
            "风险情绪水位读取失败，本轮继续尝试采集 provider=%s timeframe=%s error=%s",
            provider,
            timeframe,
            exc,
        )
        return True
    return _watermark_allows_collection(watermarks.get(asset_id), now=now or datetime.now(tz=UTC))


def record_ashare_event_watermark(
    session: Any,
    *,
    results: list[Any],
    occurred_at: datetime | None = None,
) -> None:
    """仅在事件刷新全部成功后推进市场级 events 水位。"""

    if not results:
        return
    statuses = [
        str(getattr(result, "status", "") or "").strip().lower()
        for result in results
    ]
    now = occurred_at or datetime.now(tz=UTC)
    provider = "event_refresh"
    payload = {
        "status": "available" if all(item == "available" for item in statuses) else "failed",
        "task_statuses": statuses,
        "item_count": sum(result_item_count(result) for result in results),
        "provider": provider,
    }
    try:
        repository = DataSyncWatermarkRepository(session)
        if payload["status"] == "available":
            repository.record_success(
                asset_id="market:ashare:events",
                symbol="events",
                market="ashare",
                data_domain=ASHARE_EVENT_DATA_DOMAIN,
                provider=provider,
                timeframe="window",
                watermark_at=now,
                occurred_at=now,
                payload=payload,
            )
            return
        repository.record_failure(
            asset_id="market:ashare:events",
            symbol="events",
            market="ashare",
            data_domain=ASHARE_EVENT_DATA_DOMAIN,
            provider=provider,
            timeframe="window",
            occurred_at=now,
            retry_after=timedelta(minutes=15),
            error_message="event_refresh_incomplete",
            payload=payload,
        )
    except Exception as exc:
        rollback_session_if_possible(session)
        logger.warning("事件域水位记录失败，已回滚当前事务 error=%s", exc, exc_info=True)


def record_ashare_risk_sentiment_watermark(
    session: Any,
    *,
    task: str,
    provider: str,
    timeframe: str,
    result: Any,
    occurred_at: datetime | None = None,
    schedule_retry: bool = True,
) -> None:
    """根据风险情绪子源采集结果更新独立水位。"""

    try:
        _record_ashare_risk_sentiment_watermark(
            session,
            task=task,
            provider=provider,
            timeframe=timeframe,
            result=result,
            occurred_at=occurred_at,
            schedule_retry=schedule_retry,
        )
    except SQLAlchemyError as exc:
        rollback_session_if_possible(session)
        logger.warning(
            "风险情绪水位记录失败，已回滚当前事务 task=%s provider=%s error=%s",
            task,
            provider,
            exc,
            exc_info=True,
        )


def _record_ashare_risk_sentiment_watermark(
    session: Any,
    *,
    task: str,
    provider: str,
    timeframe: str,
    result: Any,
    occurred_at: datetime | None = None,
    schedule_retry: bool = True,
) -> None:
    """执行风险情绪子源水位写入，事务边界由外层负责。"""

    status = str(getattr(result, "status", "") or "").strip().lower()
    if status not in {"available", "error", "unavailable", "failed"}:
        return
    asset_id = ashare_risk_sentiment_watermark_asset_id(provider, timeframe=timeframe)
    now = occurred_at or datetime.now(tz=UTC)
    repository = DataSyncWatermarkRepository(session)
    payload = {
        "status": status,
        "item_count": result_item_count(result),
        "task": task,
        "provider": provider,
        "timeframe": timeframe,
    } | _result_payload_metadata(result)
    if status == "available":
        repository.record_success(
            asset_id=asset_id,
            symbol=provider,
            market="ashare",
            data_domain=ASHARE_RISK_SENTIMENT_DATA_DOMAIN,
            provider=provider,
            timeframe=timeframe,
            watermark_at=now,
            occurred_at=now,
            payload=payload,
        )
        return
    repository.record_failure(
        asset_id=asset_id,
        symbol=provider,
        market="ashare",
        data_domain=ASHARE_RISK_SENTIMENT_DATA_DOMAIN,
        provider=provider,
        timeframe=timeframe,
        occurred_at=now,
        retry_after=timedelta(minutes=15) if schedule_retry else None,
        error_message=result_error_message(result),
        payload=payload,
    )


def ashare_capital_flow_watermark_allows_collection(
    session: Any,
    *,
    indicator: str,
    now: datetime | None = None,
) -> bool:
    """判断资金流榜单是否处于可采集状态。"""

    asset_id = ashare_capital_flow_watermark_asset_id(indicator)
    try:
        watermarks = _fetch_data_sync_watermarks(
            session,
            [asset_id],
            data_domain=ASHARE_CAPITAL_FLOW_DATA_DOMAIN,
            provider=ASHARE_CAPITAL_FLOW_PROVIDER,
            timeframe=str(indicator or ""),
        )
    except Exception as exc:
        logger.warning("资金流水位读取失败，本轮继续尝试采集 indicator=%s error=%s", indicator, exc)
        return True
    return _watermark_allows_collection(watermarks.get(asset_id), now=now or datetime.now(tz=UTC))


def record_ashare_capital_flow_watermark(
    session: Any,
    *,
    indicator: str,
    result: Any,
    occurred_at: datetime | None = None,
    schedule_retry: bool = True,
) -> None:
    """根据资金流榜单采集结果更新榜单级水位。"""

    status = str(getattr(result, "status", "") or "").strip().lower()
    if status not in {"available", "error", "unavailable", "failed"}:
        return
    asset_id = ashare_capital_flow_watermark_asset_id(indicator)
    now = occurred_at or datetime.now(tz=UTC)
    repository = DataSyncWatermarkRepository(session)
    payload = {
        "status": status,
        "item_count": result_item_count(result),
        "task": getattr(result, "task", None),
        "provider": ASHARE_CAPITAL_FLOW_PROVIDER,
        "indicator": indicator,
    } | _result_payload_metadata(result)
    if status == "available":
        repository.record_success(
            asset_id=asset_id,
            symbol=str(indicator or ""),
            market="ashare",
            data_domain=ASHARE_CAPITAL_FLOW_DATA_DOMAIN,
            provider=ASHARE_CAPITAL_FLOW_PROVIDER,
            timeframe=str(indicator or ""),
            watermark_at=now,
            occurred_at=now,
            payload=payload,
        )
        return
    repository.record_failure(
        asset_id=asset_id,
        symbol=str(indicator or ""),
        market="ashare",
        data_domain=ASHARE_CAPITAL_FLOW_DATA_DOMAIN,
        provider=ASHARE_CAPITAL_FLOW_PROVIDER,
        timeframe=str(indicator or ""),
        occurred_at=now,
        retry_after=timedelta(minutes=15) if schedule_retry else None,
        error_message=result_error_message(result),
        payload=payload,
    )


def record_ashare_northbound_individual_watermark(
    session: Any,
    *,
    symbol: str,
    result: Any,
    occurred_at: datetime | None = None,
    schedule_retry: bool = True,
) -> None:
    """根据北向个股采集结果更新标的级水位。"""

    status = str(getattr(result, "status", "") or "").strip().lower()
    if status not in {"available", "error", "unavailable", "failed"}:
        return
    normalized_symbol = normalize_ashare_symbol(symbol)
    if not is_tradeable_ashare_symbol(normalized_symbol):
        return
    now = occurred_at or datetime.now(tz=UTC)
    asset_id = f"ashare:{normalized_symbol}"
    repository = DataSyncWatermarkRepository(session)
    payload = {
        "status": status,
        "item_count": result_item_count(result),
        "task": getattr(result, "task", None),
        "provider": ASHARE_NORTHBOUND_INDIVIDUAL_PROVIDER,
    } | _result_payload_metadata(result)
    if status == "available":
        repository.record_success(
            asset_id=asset_id,
            symbol=normalized_symbol,
            market="ashare",
            data_domain=ASHARE_CAPITAL_FLOW_DATA_DOMAIN,
            provider=ASHARE_NORTHBOUND_INDIVIDUAL_PROVIDER,
            timeframe=ASHARE_NORTHBOUND_INDIVIDUAL_TIMEFRAME,
            watermark_at=now,
            occurred_at=now,
            payload=payload,
        )
        return
    repository.record_failure(
        asset_id=asset_id,
        symbol=normalized_symbol,
        market="ashare",
        data_domain=ASHARE_CAPITAL_FLOW_DATA_DOMAIN,
        provider=ASHARE_NORTHBOUND_INDIVIDUAL_PROVIDER,
        timeframe=ASHARE_NORTHBOUND_INDIVIDUAL_TIMEFRAME,
        occurred_at=now,
        retry_after=timedelta(minutes=15) if schedule_retry else None,
        error_message=result_error_message(result),
        payload=payload,
    )


def classify_collection_error(error_message: str | None) -> str:
    """按常见错误文案粗分失败类型，避免前端只能看到长异常文本。"""

    message = (error_message or "").lower()
    network_tokens = (
        "connection",
        "connecttimeout",
        "readtimeout",
        "timeout",
        "tls",
        "ssl",
        "curl: (56)",
        "remote end closed",
        "connection closed",
        "connection reset",
    )
    if any(token in message for token in network_tokens):
        return "network"
    if "rate limit" in message or "429" in message or "too many requests" in message:
        return "rate_limit"
    if "empty" in message or "no data" in message or "暂无" in message:
        return "empty"
    return "unknown"


def resolve_ashare_collection_symbols(session: Any, args: argparse.Namespace) -> list[str]:
    """根据采集参数解析本次 A 股 K 线补采标的。"""

    symbol_source = str(getattr(args, "symbol_source", "") or "").strip()
    if symbol_source in {"market_assets", "universe"}:
        kwargs: JsonDict = {
            "limit": None,
            "fallback_symbol": args.ashare_symbol,
        }
        timeframe = getattr(args, "ashare_timeframe", "1d")
        if timeframe != "1d":
            kwargs["timeframe"] = timeframe
        task_type = task_type_name(args)
        if task_type in {"market_bars_full_history_backfill", "market_bars_close_final"}:
            kwargs["only_failed_or_stale"] = True
            kwargs["required_start_at"] = parse_ashare_datetime_or_none(
                getattr(args, "ashare_start", None)
            )
            kwargs["required_end_at"] = latest_ashare_trading_datetime(
                session,
                parse_ashare_datetime_or_none(getattr(args, "ashare_end", None)),
            )
        elif bool(getattr(args, "only_failed_or_stale", False)):
            kwargs["only_failed_or_stale"] = True
            kwargs["stale_before"] = parse_ashare_datetime_or_none(
                getattr(args, "ashare_start", None)
            )
        return batch_ashare_symbols(session, **kwargs)
    return [args.ashare_symbol]


def resolve_ashare_fundamental_symbols(session: Any, args: argparse.Namespace) -> list[str]:
    """根据采集参数解析本次 A 股基本面和估值刷新标的。"""

    symbol_source = str(getattr(args, "symbol_source", "") or "").strip()
    if symbol_source in {"market_assets", "universe"}:
        kwargs: JsonDict = {
            "limit": None,
            "fallback_symbol": args.ashare_symbol,
        }
        if bool(getattr(args, "only_failed_or_stale", False)):
            kwargs["only_failed_or_stale"] = True
            kwargs["stale_before"] = parse_ashare_datetime_or_none(
                getattr(args, "ashare_start", None)
            )
        return batch_ashare_fundamental_symbols(session, **kwargs)
    return [args.ashare_symbol]


def resolve_ashare_northbound_symbols(session: Any, args: argparse.Namespace) -> list[str]:
    """根据采集参数解析本次北向个股刷新标的。"""

    symbol_source = str(getattr(args, "symbol_source", "") or "").strip()
    if symbol_source in {"market_assets", "universe"}:
        return batch_ashare_northbound_symbols(
            session,
            limit=getattr(args, "source_limit", None),
            fallback_symbol=args.ashare_symbol,
        )
    return TradeableAssetEligibilityService().filter_tradeable_ashare_symbols([args.ashare_symbol])


def resolve_fund_bar_collection_symbols(
    session: Any,
    args: argparse.Namespace,
    *,
    asset_type: str,
) -> list[str]:
    """根据采集参数解析本次基金日 K 补采标的。"""

    symbol_source = str(getattr(args, "symbol_source", "") or "").strip()
    if symbol_source in {"market_assets", "universe"}:
        kwargs: JsonDict = {
            "limit": getattr(args, "source_limit", None),
            "fallback_symbol": getattr(args, "fund_symbol", COLLECTION_ARG_DEFAULTS["fund_symbol"]),
            "asset_type": asset_type,
            "timeframe": getattr(args, "fund_timeframe", "1d"),
        }
        task_type = task_type_name(args)
        if task_type in {"market_bars_full_history_backfill", "market_bars_close_final"}:
            kwargs["only_failed_or_stale"] = True
            required_end_at = latest_ashare_trading_datetime(
                session,
                parse_ashare_datetime_or_none(getattr(args, "ashare_end", None)),
            )
            kwargs["required_start_at"] = parse_ashare_datetime_or_none(
                getattr(args, "ashare_start", None)
            )
            kwargs["required_end_at"] = required_end_at
        elif bool(getattr(args, "only_failed_or_stale", False)):
            kwargs["only_failed_or_stale"] = True
            kwargs["stale_before"] = parse_ashare_datetime_or_none(getattr(args, "ashare_start", None))
        return batch_fund_bar_symbols(session, **kwargs)
    return [getattr(args, "fund_symbol", COLLECTION_ARG_DEFAULTS["fund_symbol"])]


def resolve_fund_nav_collection_symbols(
    session: Any,
    args: argparse.Namespace,
) -> list[str]:
    """根据采集参数解析本次开放式基金净值补采标的。"""

    symbol_source = str(getattr(args, "symbol_source", "") or "").strip()
    if symbol_source in {"market_assets", "universe"}:
        kwargs: JsonDict = {
            "limit": getattr(args, "source_limit", None),
            "fallback_symbol": getattr(args, "fund_symbol", COLLECTION_ARG_DEFAULTS["fund_symbol"]),
        }
        if task_type_name(args) == "fund_nav_full_history_backfill":
            kwargs["only_failed_or_stale"] = True
            kwargs["required_start_at"] = parse_ashare_datetime_or_none(
                getattr(args, "ashare_start", None)
            )
            kwargs["required_end_at"] = parse_ashare_datetime_or_none(
                getattr(args, "ashare_end", None)
            )
        elif bool(getattr(args, "only_failed_or_stale", False)):
            kwargs["only_failed_or_stale"] = True
            kwargs["stale_before"] = parse_ashare_datetime_or_none(getattr(args, "ashare_start", None))
        return batch_open_fund_nav_symbols(session, **kwargs)
    return [getattr(args, "fund_symbol", COLLECTION_ARG_DEFAULTS["fund_symbol"])]


def parse_ashare_datetime_or_none(value: Any) -> datetime | None:
    """解析 A 股采集日期为 UTC datetime，无法解析时返回空。"""

    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def latest_ashare_trading_datetime(session: Any, end_at: datetime | None) -> datetime | None:
    """把 A 股 K 线请求结束日校准到不晚于 end_at 的最近交易日。"""

    if end_at is None:
        return None
    try:
        statement = select(func.max(MarketCalendarORM.trade_date)).where(
            MarketCalendarORM.market == "ashare",
            MarketCalendarORM.trade_date <= end_at.date(),
            MarketCalendarORM.is_trading_day.is_(True),
        )
        latest_trade_date = session.scalar(statement)
    except Exception as exc:
        logger.debug("读取 A 股最近交易日失败，回退使用自然日 end=%s error=%s", end_at, exc)
        return end_at
    if latest_trade_date is None:
        return end_at
    return datetime.combine(latest_trade_date, datetime.min.time(), tzinfo=UTC)


def record_fund_symbol_watermark(
    session: Any,
    *,
    symbol: str,
    asset_type: str,
    data_domain: str,
    provider: str,
    timeframe: str,
    result: Any,
    occurred_at: datetime | None = None,
    session_factory: Any | None = None,
    schedule_retry: bool = True,
    requested_start: str | None = None,
    requested_end: str | None = None,
    sync_task_type: str | None = None,
) -> None:
    """根据基金单标的采集结果更新成功水位或失败重试时间。"""

    if session_factory is not None:
        watermark_session = session_factory()
        try:
            _record_fund_symbol_watermark(
                watermark_session,
                symbol=symbol,
                asset_type=asset_type,
                data_domain=data_domain,
                provider=provider,
                timeframe=timeframe,
                result=result,
                occurred_at=occurred_at,
                schedule_retry=schedule_retry,
                requested_start=requested_start,
                requested_end=requested_end,
                sync_task_type=sync_task_type,
            )
            watermark_session.commit()
        except SQLAlchemyError as exc:
            rollback_session_if_possible(watermark_session)
            logger.warning("基金水位记录失败，已回滚独立事务 symbol=%s error=%s", symbol, exc, exc_info=True)
        finally:
            close = getattr(watermark_session, "close", None)
            if callable(close):
                close()
        return
    try:
        _record_fund_symbol_watermark(
            session,
            symbol=symbol,
            asset_type=asset_type,
            data_domain=data_domain,
            provider=provider,
            timeframe=timeframe,
            result=result,
            occurred_at=occurred_at,
            schedule_retry=schedule_retry,
            requested_start=requested_start,
            requested_end=requested_end,
            sync_task_type=sync_task_type,
        )
    except SQLAlchemyError as exc:
        rollback_session_if_possible(session)
        logger.warning("基金水位记录失败，已回滚当前事务 symbol=%s error=%s", symbol, exc, exc_info=True)


def _record_fund_symbol_watermark(
    session: Any,
    *,
    symbol: str,
    asset_type: str,
    data_domain: str,
    provider: str,
    timeframe: str,
    result: Any,
    occurred_at: datetime | None = None,
    schedule_retry: bool = True,
    requested_start: str | None = None,
    requested_end: str | None = None,
    sync_task_type: str | None = None,
) -> None:
    """执行单个基金标的水位写入；事务边界由外层负责。"""

    status = str(getattr(result, "status", "") or "").strip().lower()
    if status not in {"available", "error", "unavailable", "failed"}:
        return
    asset_id = (
        f"fund:open:{symbol}"
        if asset_type == "open_fund"
        else f"fund:{asset_type}:{symbol}"
    )
    now = occurred_at or datetime.now(tz=UTC)
    repository = DataSyncWatermarkRepository(session)
    if status == "available":
        payload_watermark_at = result_watermark_at(result)
        if payload_watermark_at is not None:
            latest_count = result_item_count(result)
            latest_at = payload_watermark_at
        elif data_domain == FUND_NAV_DATA_DOMAIN:
            coverage = _fetch_fund_nav_coverage(session, [asset_id])
            latest_coverage = coverage.get(asset_id)
            latest_count = _coverage_bar_count(latest_coverage)
            latest_at = _coverage_latest_nav_date(latest_coverage)
        else:
            coverage = _fetch_fund_bar_coverage(session, [asset_id], timeframe=timeframe)
            latest_coverage = coverage.get(asset_id)
            latest_count = _coverage_bar_count(latest_coverage)
            latest_at = _coverage_latest_bar_at(latest_coverage)
        success_payload: JsonDict = {
            "item_count": result_item_count(result),
            "latest_count": latest_count,
        }
        for key, value in (
            ("requested_start", requested_start),
            ("requested_end", requested_end),
            ("sync_task_type", sync_task_type),
        ):
            if value not in (None, ""):
                success_payload[key] = value
        repository.record_success(
            asset_id=asset_id,
            symbol=symbol,
            market="fund",
            data_domain=data_domain,
            provider=provider,
            timeframe=timeframe,
            watermark_at=(
                datetime.combine(latest_at, datetime.min.time(), tzinfo=UTC)
                if isinstance(latest_at, date) and not isinstance(latest_at, datetime)
                else latest_at
            ),
            occurred_at=now,
            payload=success_payload,
        )
        return
    failure_payload: JsonDict = {
        "status": status,
        "item_count": result_item_count(result),
    }
    for key, value in (
        ("requested_start", requested_start),
        ("requested_end", requested_end),
        ("sync_task_type", sync_task_type),
    ):
        if value not in (None, ""):
            failure_payload[key] = value
    if status == "unavailable" and not schedule_retry:
        if requested_start not in (None, ""):
            failure_payload["verified_requested_start"] = requested_start
        if requested_end not in (None, ""):
            failure_payload["verified_requested_end"] = requested_end
        repository.record_unavailable(
            asset_id=asset_id,
            symbol=symbol,
            market="fund",
            data_domain=data_domain,
            provider=provider,
            timeframe=timeframe,
            occurred_at=now,
            error_message=result_error_message(result),
            payload=failure_payload,
        )
        return
    repository.record_failure(
        asset_id=asset_id,
        symbol=symbol,
        market="fund",
        data_domain=data_domain,
        provider=provider,
        timeframe=timeframe,
        occurred_at=now,
        retry_after=timedelta(minutes=15) if schedule_retry else None,
        error_message=result_error_message(result),
        payload=failure_payload,
    )


def attach_fund_retry_payload(
    result: Any,
    *,
    provider: str,
    occurred_at: datetime | None = None,
    schedule_retry: bool = True,
) -> Any:
    """给基金失败结果补充重试元数据，供任务监控页展示。"""

    status = str(getattr(result, "status", "") or "").strip().lower()
    if status not in {"error", "unavailable", "failed"}:
        return result
    now = occurred_at or datetime.now(tz=UTC)
    payload = {
        "provider_key": provider,
        "error_category": classify_collection_error(result_error_message(result)),
    }
    if schedule_retry:
        retry_after = timedelta(minutes=15)
        payload.update(
            retry_after_seconds=int(retry_after.total_seconds()),
            next_retry_at=(now + retry_after).isoformat(),
        )
    return attach_batch_payload(result, **payload)


def resolve_ashare_priority_news_symbols(
    session: Any | None,
    args: argparse.Namespace,
) -> list[str]:
    """解析逐股新闻的重点标的，避免高频任务全市场逐股请求新闻源。"""

    max_symbols = positive_limit(getattr(args, "priority_symbol_limit", None))
    if max_symbols is None:
        max_symbols = collection_batch_size(args)

    symbols: list[str] = []
    if session is not None:
        resolved_by_priority_pool = False
        try:
            for symbol in EventPriorityResolver(session).resolve_ashare_symbols(
                limit=max_symbols,
            ):
                _append_unique_ashare_symbol(symbols, symbol)
                if len(symbols) >= max_symbols:
                    break
            resolved_by_priority_pool = True
        except Exception as exc:
            logger.warning("解析事件重点池失败，回退旧新闻重点名单逻辑 error=%s", exc, exc_info=True)

        if not resolved_by_priority_pool:
            for symbol in _fetch_active_watchlist_symbols(session, limit=max_symbols):
                _append_unique_ashare_symbol(symbols, symbol)
            if len(symbols) < max_symbols:
                for symbol in _fetch_recent_recommendation_symbols(
                    session,
                    limit=max_symbols * 2,
                ):
                    _append_unique_ashare_symbol(symbols, symbol)
                    if len(symbols) >= max_symbols:
                        break

    _append_unique_ashare_symbol(symbols, getattr(args, "ashare_symbol", ""))
    if not symbols:
        return [getattr(args, "ashare_symbol", COLLECTION_ARG_DEFAULTS["ashare_symbol"])]
    return symbols[:max_symbols]


def resolve_ashare_stock_news_symbols(
    session: Any | None,
    args: argparse.Namespace,
) -> list[str]:
    """根据新闻任务范围解析逐股新闻标的。"""

    news_scope = str(getattr(args, "news_scope", "priority") or "priority").strip()
    if news_scope == "full_tradeable":
        return resolve_ashare_full_tradeable_news_symbols(session, args)
    return resolve_ashare_priority_news_symbols(session, args)


def resolve_ashare_full_tradeable_news_symbols(
    session: Any | None,
    args: argparse.Namespace,
) -> list[str]:
    """解析盘后全量新闻补采标的：仅覆盖可交易 A 股主板资产。"""

    max_symbols = positive_limit(getattr(args, "priority_symbol_limit", None))
    symbols: list[str] = []
    if session is not None:
        try:
            repo = AssetRepository(session)
            eligibility = TradeableAssetEligibilityService()
            assets = eligibility.filter_tradeable_assets(repo.find_by_market("ashare"))
            for asset in sorted(
                assets,
                key=lambda item: normalize_ashare_symbol(
                    str(getattr(item, "symbol", "") or "")
                ),
            ):
                _append_unique_ashare_symbol(symbols, getattr(asset, "symbol", ""))
                if max_symbols is not None and len(symbols) >= max_symbols:
                    break
        except Exception as exc:
            logger.warning("解析盘后全量新闻标的失败，回退样例代码 error=%s", exc, exc_info=True)
    _append_unique_ashare_symbol(symbols, getattr(args, "ashare_symbol", ""))
    if not symbols:
        return [getattr(args, "ashare_symbol", COLLECTION_ARG_DEFAULTS["ashare_symbol"])]
    return symbols[:max_symbols] if max_symbols is not None else symbols


def _append_unique_ashare_symbol(symbols: list[str], symbol: Any) -> None:
    """按用户可交易的 A 股主板 6 位代码清洗并去重。"""

    normalized = normalize_ashare_symbol(str(symbol or ""))
    if not is_tradeable_ashare_symbol(normalized):
        return
    if normalized not in symbols:
        symbols.append(normalized)


def _fetch_active_watchlist_symbols(session: Any, *, limit: int) -> list[str]:
    """读取活跃观察池中的 A 股标的，失败时返回空列表。"""

    try:
        statement = (
            select(WatchlistItemORM.symbol)
            .where(WatchlistItemORM.market == "ashare", WatchlistItemORM.status == "active")
            .order_by(
                WatchlistItemORM.next_review_at.asc().nullslast(),
                WatchlistItemORM.updated_at.desc(),
            )
            .limit(limit)
        )
        return [str(symbol) for symbol in session.scalars(statement)]
    except Exception as exc:
        logger.warning("读取观察池重点新闻标的失败 error=%s", exc, exc_info=True)
        return []


def _fetch_recent_recommendation_symbols(session: Any, *, limit: int) -> list[str]:
    """读取最近推荐中的 A 股非回避标的，失败时返回空列表。"""

    try:
        statement = (
            select(AssetRecommendationORM.symbol)
            .where(
                AssetRecommendationORM.market == "ashare",
                AssetRecommendationORM.action != "avoid",
            )
            .order_by(
                AssetRecommendationORM.created_at.desc(),
                AssetRecommendationORM.rank.asc(),
            )
            .limit(limit)
        )
        return [str(symbol) for symbol in session.scalars(statement)]
    except Exception as exc:
        logger.warning("读取推荐重点新闻标的失败 error=%s", exc, exc_info=True)
        return []


def _lookback_start_datetime_or_none(
    value: str | None,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """把 168h / 30d / 1y 这类 lookback 转为 UTC 起点时间。"""

    text = str(value or "").strip().lower()
    if not text:
        return None
    try:
        if text.endswith("h"):
            delta = timedelta(hours=max(int(text[:-1]), 1))
        elif text.endswith("d"):
            delta = timedelta(days=max(int(text[:-1]), 1))
        elif text.endswith("y"):
            delta = timedelta(days=max(int(text[:-1]), 1) * 365)
        else:
            delta = timedelta(days=max(int(text), 1))
    except ValueError:
        return None
    return (now or datetime.now(tz=UTC)) - delta


def _asset_requires_crypto_bar_collection(
    asset: Any,
    *,
    coverage: dict[str, tuple[int, datetime | None]],
    stale_before: datetime | None,
    min_bar_count: int | None,
) -> bool:
    """判断 Crypto K 线是否缺少最近窗口覆盖。"""

    bar_count, latest_bar_at = coverage.get(asset.asset_id, (0, None))
    if bar_count <= 0 or latest_bar_at is None:
        return True
    if min_bar_count is not None and bar_count < min_bar_count:
        return True
    if stale_before is not None and latest_bar_at < stale_before:
        return True
    return False


def batch_crypto_symbols(
    session: Any,
    *,
    market: str,
    timeframe: str,
    limit: int | None = None,
    fallback_symbol: str,
    only_failed_or_stale: bool = False,
    stale_before: datetime | None = None,
    min_bar_count: int | None = None,
) -> list[str]:
    """按 K 线覆盖缺口选择数字货币补采标的。"""

    selection_failed = False
    has_candidate_assets = False
    try:
        repo = AssetRepository(session)
        assets = repo.find_by_market(market)
        has_candidate_assets = bool(assets)
        coverage = _fetch_crypto_bar_coverage(
            session,
            [asset.asset_id for asset in assets],
            timeframe=timeframe,
            market=market,
        )
        ranked_assets = sorted(
            [
                asset
                for asset in assets
                if (
                    not only_failed_or_stale
                    or _asset_requires_crypto_bar_collection(
                        asset,
                        coverage=coverage,
                        stale_before=stale_before,
                        min_bar_count=min_bar_count,
                    )
                )
            ],
            key=lambda asset: (
                coverage.get(asset.asset_id, (0, None))[0],
                coverage.get(asset.asset_id, (0, None))[1] or datetime.min.replace(tzinfo=UTC),
                asset.symbol,
            ),
        )
        symbols = [asset.symbol for asset in ranked_assets]
    except Exception:
        selection_failed = True
        symbols = []
    if not symbols:
        if only_failed_or_stale and has_candidate_assets and not selection_failed:
            return []
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
        only_failed_or_stale = bool(getattr(args, "only_failed_or_stale", False))
        kwargs: JsonDict = {
            "market": market,
            "timeframe": args.crypto_timeframe,
            "limit": None,
            "fallback_symbol": args.crypto_symbol,
        }
        if only_failed_or_stale:
            kwargs.update(
                {
                    "only_failed_or_stale": True,
                    "stale_before": _lookback_start_datetime_or_none(
                        getattr(args, "lookback", None)
                    ),
                    "min_bar_count": runtime_collection_limit(args),
                }
            )
        return batch_crypto_symbols(session, **kwargs)
    return [args.crypto_symbol]


def batch_crypto_derivative_symbols(
    session: Any,
    *,
    market: str,
    limit: int | None = None,
    fallback_symbol: str,
) -> list[str]:
    """选择需要刷新衍生品快照的合约标的。"""

    try:
        repo = AssetRepository(session)
        assets = repo.find_by_market(market)
        symbols = sorted(
            asset.symbol
            for asset in assets
            if str(getattr(asset, "quote_asset", "") or "").upper() in {"USDT", "USD"}
        )
        if not symbols:
            symbols = sorted(asset.symbol for asset in assets)
    except Exception:
        symbols = []
    if not symbols:
        symbols = [fallback_symbol]
    if limit:
        return symbols[:limit]
    return symbols


def resolve_crypto_derivative_collection_symbols(
    session: Any,
    args: argparse.Namespace,
    *,
    market: str,
) -> list[str]:
    """根据采集参数解析本次数字货币衍生品快照标的。"""

    symbol_source = str(getattr(args, "symbol_source", "") or "").strip()
    if symbol_source in {"market_assets", "universe"}:
        return batch_crypto_derivative_symbols(
            session,
            market=market,
            limit=runtime_collection_limit(args),
            fallback_symbol=args.crypto_symbol,
        )
    return [args.crypto_symbol]


def collection_batch_size(args: argparse.Namespace) -> int:
    """读取本次按标的补采的单批标的数量。"""

    raw_value = getattr(args, "batch_size", None) or getattr(args, "limit", None) or 1
    try:
        batch_size = int(raw_value)
    except (TypeError, ValueError):
        batch_size = 1
    return max(batch_size, 1)


def runtime_scheduler_job_payload(args: argparse.Namespace) -> JsonDict:
    """从调度器主配置中读取当前任务完整配置，供长任务热加载使用。"""

    config_file = str(getattr(args, "runtime_scheduler_config_file", "") or "").strip()
    job_name = str(getattr(args, "progress_job_name", "") or "").strip()
    if not config_file or not job_name:
        return {}

    try:
        payload = json.loads(Path(config_file).read_text(encoding="utf-8-sig"))
    except Exception as exc:
        logger.debug(
            "读取运行期调度配置失败 file=%s job=%s error=%s",
            config_file,
            job_name,
            exc,
        )
        return {}

    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        return {}
    for job_payload in jobs:
        if not isinstance(job_payload, dict):
            continue
        if str(job_payload.get("name") or "").strip() != job_name:
            continue
        return dict(job_payload)
    return {}


def runtime_scheduler_job_params(args: argparse.Namespace) -> JsonDict:
    """从调度器主配置中读取当前任务的最新 params，用于长任务热加载。"""

    job_payload = runtime_scheduler_job_payload(args)
    params = job_payload.get("params") or {}
    return dict(params) if isinstance(params, dict) else {}


def runtime_scheduler_rate_policies(args: argparse.Namespace) -> JsonDict:
    """从调度器主配置读取最新源级限频策略，缺失时回退启动参数。"""

    config_file = str(getattr(args, "runtime_scheduler_config_file", "") or "").strip()
    if config_file:
        try:
            payload = json.loads(Path(config_file).read_text(encoding="utf-8-sig"))
        except Exception as exc:
            logger.debug(
                "读取运行期限频配置失败 file=%s error=%s",
                config_file,
                exc,
            )
        else:
            rate_policies = payload.get("rate_policies")
            if isinstance(rate_policies, Mapping):
                return dict(rate_policies)
    fallback = getattr(args, "rate_policies", None)
    return dict(fallback) if isinstance(fallback, Mapping) else {}


def runtime_scheduler_job_control(args: argparse.Namespace) -> JsonDict:
    """读取当前任务的运行期控制状态，例如暂停/继续。"""

    job_payload = runtime_scheduler_job_payload(args)
    control = job_payload.get("control") or {}
    return dict(control) if isinstance(control, dict) else {}


def runtime_collection_limit(args: argparse.Namespace) -> int | None:
    """读取最新单次上限；无法读取主配置时回退到启动参数。"""

    job_payload = runtime_scheduler_job_payload(args)
    raw_value = job_payload.get("limit") if "limit" in job_payload else getattr(args, "limit", None)
    if raw_value is None:
        return None
    try:
        return max(int(raw_value), 1)
    except (TypeError, ValueError):
        value = getattr(args, "limit", None)
        try:
            return max(int(value), 1) if value is not None else None
        except (TypeError, ValueError):
            return None


def wait_for_runtime_scheduler_job_resume(args: argparse.Namespace) -> None:
    """若当前任务被暂停，则在下一只标的提交前等待继续。"""

    announced = False
    progress, job_name, _run_id = collection_progress_context(args)
    while bool(runtime_scheduler_job_control(args).get("paused", False)):
        if not announced:
            logger.info("调度任务已暂停，等待 Web 页面继续 job=%s", job_name or "-")
            if progress is not None and job_name:
                try:
                    progress.job_paused(job_name=job_name, message="采集进程已收到暂停控制，正在等待继续。")
                except Exception as exc:  # pragma: no cover - 进度写入失败不应影响采集等待
                    logger.debug("写入暂停进度失败 job=%s error=%s", job_name, exc)
            announced = True
        time_sleep(1.0)
    if announced and progress is not None and job_name:
        try:
            progress.job_resumed(job_name=job_name, message="采集进程已收到继续控制，恢复提交后续标的。")
        except Exception as exc:  # pragma: no cover - 进度写入失败不应影响采集恢复
            logger.debug("写入继续进度失败 job=%s error=%s", job_name, exc)


def runtime_collection_batch_size(args: argparse.Namespace) -> int:
    """读取最新批大小；无法读取主配置时回退到启动参数。"""

    params = runtime_scheduler_job_params(args)
    raw_value = params.get("batch_size") if "batch_size" in params else None
    if raw_value is None:
        return collection_batch_size(args)
    try:
        return max(int(raw_value), 1)
    except (TypeError, ValueError):
        return collection_batch_size(args)


def runtime_collection_max_workers(args: argparse.Namespace) -> int:
    """读取最新批内并发；无法读取主配置时回退到启动参数。"""

    params = runtime_scheduler_job_params(args)
    raw_value = params.get("max_workers") if "max_workers" in params else None
    if raw_value is None:
        return collection_max_workers(args)
    try:
        workers = int(raw_value)
    except (TypeError, ValueError):
        return collection_max_workers(args)
    return max(1, min(workers, 16))


def estimate_dynamic_batch_count(*, batch_index: int, remaining_items: int, batch_size: int) -> int:
    """根据当前批大小估算总批次数，供进度展示使用。"""

    size = max(int(batch_size), 1)
    return max(batch_index, batch_index + ((remaining_items + size - 1) // size))


def build_runtime_batch_stop_checker(
    args: argparse.Namespace,
    *,
    batch_size: int,
    max_workers: int,
    compare_max_workers: bool = True,
) -> Callable[[], bool]:
    """构建顺序批次热加载检查器；配置变化时让外层循环重新切分批次。"""

    def should_stop() -> bool:
        if runtime_collection_batch_size(args) != batch_size:
            return True
        return compare_max_workers and runtime_collection_max_workers(args) != max_workers

    return should_stop


def collection_max_workers(args: argparse.Namespace) -> int:
    """读取按标的采集的批内最大并发数。"""

    raw_value = getattr(args, "max_workers", None)
    if raw_value is None:
        raw_value = COLLECTION_ARG_DEFAULTS["max_workers"]
    try:
        workers = int(raw_value)
    except (TypeError, ValueError):
        workers = int(COLLECTION_ARG_DEFAULTS["max_workers"])
    return max(1, min(workers, 16))


def ashare_market_bar_source_limit(args: argparse.Namespace) -> int | None:
    """解析单只 A 股 K 线的拉取条数上限；全量历史任务只用日期范围控制。"""

    if task_type_name(args) == "market_bars_full_history_backfill":
        return None
    return runtime_collection_limit(args)


def should_schedule_ashare_failure_retry(args: argparse.Namespace) -> bool:
    """读取 A 股 K 线失败后是否安排自动重试，默认保持旧逻辑。"""

    raw_value = getattr(args, "schedule_failure_retry", True)
    if isinstance(raw_value, str):
        return raw_value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(raw_value)


def collection_progress_context(
    args: argparse.Namespace,
) -> tuple[BaseDataTaskProgressRecorder | None, str | None, str | None]:
    """读取当前采集任务的进度上下文。"""

    return (
        COLLECTION_PROGRESS_RECORDER,
        getattr(args, "progress_job_name", None),
        getattr(args, "progress_run_id", None),
    )


def split_symbol_batches(symbols: list[str], *, batch_size: int) -> list[list[str]]:
    """按批大小切分标的列表。"""

    if not symbols:
        return []
    size = max(int(batch_size), 1)
    return [symbols[index : index + size] for index in range(0, len(symbols), size)]


def run_symbol_task_batch(
    symbols: list[str],
    *,
    max_workers: int,
    collect_symbol: Callable[[str], Any],
    on_symbol_result: Callable[[str, Any, int], None] | None = None,
    progress: BaseDataTaskProgressRecorder | None = None,
    job_name: str | None = None,
    run_id: str | None = None,
    stage_key: str | None = None,
    batch_index: int | None = None,
    batch_count: int | None = None,
    batch_size: int | None = None,
    total_items: int | None = None,
    should_stop_before_symbol: Callable[[], bool] | None = None,
    ) -> list[Any]:
    """按低并发执行单批标的采集，并按输入顺序返回结果。"""

    if not symbols:
        return []
    if (
        progress is not None
        and job_name
        and run_id
        and stage_key
        and batch_index is not None
        and batch_count is not None
    ):
        progress.batch_started(
            job_name=job_name,
            run_id=run_id,
            stage_key=stage_key,
            total_items=total_items or len(symbols),
            batch_index=batch_index,
            batch_count=batch_count,
            batch_size=batch_size or len(symbols),
            max_workers=max_workers,
        )
    has_progress_context = (
        progress is not None
        and job_name
        and run_id
        and stage_key
        and batch_index is not None
        and batch_count is not None
    )
    workers = max(1, min(int(max_workers), len(symbols)))
    batch_label = (
        f"{batch_index}/{batch_count}"
        if batch_index is not None and batch_count is not None
        else "-"
    )

    def collect_symbol_with_observability(symbol: str, index: int) -> Any:
        """包装单标的采集，输出 worker 级耗时日志。"""

        worker_name = threading.current_thread().name
        started = time.perf_counter()
        logger.info(
            "标的采集开始 stage=%s symbol=%s worker=%s batch=%s index=%s/%s "
            "max_workers=%s",
            stage_key,
            symbol,
            worker_name,
            batch_label,
            index + 1,
            len(symbols),
            workers,
        )
        try:
            result = collect_symbol(symbol)
        except Exception as exc:
            elapsed = round(time.perf_counter() - started, 3)
            logger.exception(
                "标的采集异常 stage=%s symbol=%s worker=%s batch=%s "
                "elapsed_seconds=%.3f error=%s",
                stage_key,
                symbol,
                worker_name,
                batch_label,
                elapsed,
                exc,
            )
            raise
        elapsed = round(time.perf_counter() - started, 3)
        logger.info(
            "标的采集完成 stage=%s symbol=%s worker=%s batch=%s status=%s "
            "item_count=%s elapsed_seconds=%.3f",
            stage_key,
            symbol,
            worker_name,
            batch_label,
            result_status_name(result),
            result_item_count(result),
            elapsed,
        )
        return result

    if workers == 1:
        results = []
        for index, symbol in enumerate(symbols):
            if results and should_stop_before_symbol is not None and should_stop_before_symbol():
                logger.info("运行期配置已变化，提前结束当前顺序批次并重新切分")
                break
            result = collect_symbol_with_observability(symbol, index)
            if has_progress_context:
                emit_symbol_progress(
                    progress,
                    job_name=job_name,
                    run_id=run_id,
                    stage_key=stage_key,
                    symbol=symbol,
                    result=result,
                    batch_index=batch_index,
                    batch_count=batch_count,
                )
            if on_symbol_result is not None:
                on_symbol_result(symbol, result, len(results))
            results.append(result)
        return results

    results: list[Any | None] = [None] * len(symbols)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="symbol-fetch") as executor:
        future_to_index = {
            executor.submit(collect_symbol_with_observability, symbol, index): index
            for index, symbol in enumerate(symbols)
        }
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            result = future.result()
            results[index] = result
            if has_progress_context:
                emit_symbol_progress(
                    progress,
                    job_name=job_name,
                    run_id=run_id,
                    stage_key=stage_key,
                    symbol=symbols[index],
                    result=result,
                    batch_index=batch_index,
                    batch_count=batch_count,
                )
            if on_symbol_result is not None:
                on_symbol_result(symbols[index], result, index)
    return [result for result in results if result is not None]


def emit_symbol_progress(
    progress: BaseDataTaskProgressRecorder,
    *,
    job_name: str,
    run_id: str,
    stage_key: str,
    symbol: str,
    result: Any,
    batch_index: int,
    batch_count: int,
) -> None:
    """把标的结果转换成进度事件。"""

    status = result_status_name(result)
    item_count = result_item_count(result)
    error_message = result_error_message(result)
    progress.symbol_completed(
        job_name=job_name,
        run_id=run_id,
        stage_key=stage_key,
        symbol=symbol,
        status=status,
        item_count=item_count,
        batch_index=batch_index,
        batch_count=batch_count,
        error_message=error_message,
        retry_count=optional_int_result_payload(result, "retry_count"),
        retry_after_seconds=optional_number_result_payload(result, "retry_after_seconds"),
        next_retry_at=optional_str_result_payload(result, "next_retry_at"),
        provider_key=optional_str_result_payload(result, "provider_key"),
        error_category=optional_str_result_payload(result, "error_category"),
    )


def result_status_name(result: Any) -> str:
    """尽量把采集结果归一成进度状态名。"""

    if isinstance(result, list):
        statuses = [result_status_name(item) for item in result]
        if any(status == "failed" for status in statuses):
            return "failed"
        if any(status == "completed" for status in statuses):
            return "completed"
        if any(status in {"skipped", "locked"} for status in statuses):
            return next(status for status in statuses if status in {"skipped", "locked"})
        return "completed"
    status = str(getattr(result, "status", "") or "").strip().lower()
    if status in {"available", "executed", "completed", "success"}:
        return "completed"
    if status in {"error", "unavailable", "failed"}:
        return "failed"
    if status in {"skipped", "locked"}:
        return status
    return "completed"


def result_item_count(result: Any) -> int:
    """计算采集结果的条目数。"""

    value = getattr(result, "item_count", None)
    if value is not None:
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            return 0
    if isinstance(result, list):
        total = 0
        for item in result:
            total += result_item_count(item)
        return total
    return 0


def result_watermark_at(result: Any) -> datetime | None:
    """从采集摘要 payload 中读取最新数据日期。"""

    value = optional_result_payload(result, "latest_at")
    if not payload_value_present(value):
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed_datetime = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed_date = date.fromisoformat(text)
        except ValueError:
            return None
        return datetime.combine(parsed_date, datetime.min.time(), tzinfo=UTC)
    if parsed_datetime.tzinfo is None:
        return parsed_datetime.replace(tzinfo=UTC)
    return parsed_datetime.astimezone(UTC)


def result_error_message(result: Any) -> str | None:
    """尽量从结果中提取失败信息。"""

    if isinstance(result, list):
        for item in result:
            message = result_error_message(item)
            if message:
                return message
        return None
    value = getattr(result, "error_message", None)
    return str(value) if value else None


def optional_result_payload(result: Any, key: str) -> Any:
    """从结果 payload 中读取第一个非空字段，支持列表结果。"""

    if isinstance(result, list):
        for item in result:
            value = optional_result_payload(item, key)
            if payload_value_present(value):
                return value
        return None
    payload = getattr(result, "payload", None)
    if isinstance(payload, dict):
        return payload.get(key)
    return None


def optional_str_result_payload(result: Any, key: str) -> str | None:
    """读取字符串型 payload 字段。"""

    value = optional_result_payload(result, key)
    return str(value) if payload_value_present(value) else None


def optional_number_result_payload(result: Any, key: str) -> int | float | None:
    """读取数值型 payload 字段。"""

    value = optional_result_payload(result, key)
    if not payload_value_present(value):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return int(numeric) if numeric.is_integer() else numeric


def optional_int_result_payload(result: Any, key: str) -> int | None:
    """读取整数型 payload 字段。"""

    value = optional_number_result_payload(result, key)
    return int(value) if value is not None else None


def payload_value_present(value: Any) -> bool:
    """判断 payload 字段是否有可展示的值。"""

    return value is not None and value != ""


def attach_batch_payload(result: Any, **payload: Any) -> Any:
    """给单标的采集结果补充本轮批次信息，便于日志和前端展示进度。"""

    merged_payload = dict(getattr(result, "payload", {}) or {}) | payload
    if isinstance(result, CollectionTaskResult):
        return CollectionTaskResult(
            task=result.task,
            status=result.status,
            raw_record_id=result.raw_record_id,
            item_count=result.item_count,
            error_message=result.error_message,
            payload=merged_payload,
        )
    if hasattr(result, "payload"):
        result.payload = merged_payload
    return result


def commit_session_if_possible(session: Any) -> None:
    """批次完成后尽早提交，避免长任务超时导致已完成写入整体回滚。"""

    commit = getattr(session, "commit", None)
    if callable(commit):
        commit()


def rollback_session_if_possible(session: Any) -> None:
    """数据库事务异常后尽快回滚，避免后续 SQL 继续落入 aborted 状态。"""

    rollback = getattr(session, "rollback", None)
    if callable(rollback):
        rollback()


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
