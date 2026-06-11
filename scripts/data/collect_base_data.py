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
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
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
    AshareP0Collector,
    AshareP1Collector,
    AshareP2Collector,
    AshareRiskSentimentCollector,
    CryptoDataCollector,
    FundDataCollector,
)
from finance_agent.data.models import ProviderResult
from finance_agent.data.normalizers import (
    compact_crypto_symbol,
    normalize_ashare_symbol,
)
from finance_agent.data.providers import AkshareProvider
from finance_agent.data.source_rate_limiter import (
    build_source_rate_limiter,
    default_source_rate_limiter,
)
from finance_agent.scheduler.base_data_progress import BaseDataTaskProgressRecorder
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.orm import (
    AssetRecommendationORM,
    DataSyncWatermarkORM,
    EventRecordORM,
    FundNavSnapshotORM,
    MarketCalendarORM,
    MarketBarORM,
    WatchlistItemORM,
)
from finance_agent.storage.repositories import (
    AssetRepository,
    DataSyncWatermarkRepository,
    MarketCalendarRepository,
)

JsonDict = dict[str, Any]
logger = logging.getLogger(__name__)
COLLECTION_PROGRESS_RECORDER: BaseDataTaskProgressRecorder | None = None
COLLECTION_RUNTIME_ARGS: argparse.Namespace | None = None
ASHARE_MARKET_BAR_WATERMARK_PROVIDER = "akshare:stock_zh_a_hist_tx"
ASHARE_MARKET_BAR_DATA_DOMAIN = "market_bars"
ASHARE_FUNDAMENTAL_DATA_DOMAIN = "fundamentals"
ASHARE_VALUATION_DATA_DOMAIN = "valuation"
ASHARE_CAPITAL_FLOW_DATA_DOMAIN = "capital_flow"
ASHARE_RISK_SENTIMENT_DATA_DOMAIN = "risk_sentiment"
ASHARE_FINANCIAL_INDICATORS_PROVIDER = "stock_financial_analysis_indicator_em"
ASHARE_VALUATION_PROVIDER = "stock_value_em"
ASHARE_CAPITAL_FLOW_PROVIDER = "stock_individual_fund_flow_rank"
CRYPTO_MARKET_BAR_DATA_DOMAIN = "market_bars"
CRYPTO_MARKET_BAR_PROVIDER = "ccxt_binance_fetch_ohlcv"
CRYPTO_DERIVATIVE_DATA_DOMAIN = "derivatives"
CRYPTO_DERIVATIVE_PROVIDER = "binance_derivative_snapshot"
ASHARE_BAR_COVERAGE_QUERY_CHUNK_SIZE = 500
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
            build_ashare_full_asset_refresh_task(collector, args, runtime),
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
            def collect_symbol(symbol: str) -> CollectionTaskResult:
                wait_for_runtime_scheduler_job_resume(args)
                ohlcv_limit = ashare_market_bar_source_limit(args)
                if session_factory is not None and current_max_workers > 1:
                    with session_scope(session_factory) as worker_session:
                        worker_collector = AshareP0Collector(worker_session)
                        return attach_ashare_market_bar_retry_payload(
                            runtime.run_task(
                                task="ashare_p0_ohlcv",
                                provider_key=ashare_ohlcv_provider_key(symbol),
                                parameters={
                                    "symbol": symbol,
                                    "timeframe": args.ashare_timeframe,
                                    "start": args.ashare_start,
                                    "end": args.ashare_end,
                                    "adjust": args.ashare_adjust,
                                    "limit": ohlcv_limit,
                                    "is_closed": args.is_closed,
                                    "status": args.status,
                                },
                                force=args.force_provider,
                                collect=lambda: worker_collector.collect_ohlcv(
                                    symbol=symbol,
                                    timeframe=args.ashare_timeframe,
                                    start=args.ashare_start,
                                    end=args.ashare_end,
                                    limit=ohlcv_limit,
                                    adjust=args.ashare_adjust,
                                    is_closed=args.is_closed,
                                    status=args.status,
                                    source_gate=ashare_kline_source_gate,
                                ),
                            ),
                            schedule_retry=schedule_failure_retry,
                        )
                return attach_ashare_market_bar_retry_payload(
                    runtime.run_task(
                        task="ashare_p0_ohlcv",
                        provider_key=ashare_ohlcv_provider_key(symbol),
                        parameters={
                            "symbol": symbol,
                            "timeframe": args.ashare_timeframe,
                            "start": args.ashare_start,
                            "end": args.ashare_end,
                            "adjust": args.ashare_adjust,
                            "limit": ohlcv_limit,
                            "is_closed": args.is_closed,
                            "status": args.status,
                        },
                        force=args.force_provider,
                        collect=lambda: collector.collect_ohlcv(
                            symbol=symbol,
                            timeframe=args.ashare_timeframe,
                            start=args.ashare_start,
                            end=args.ashare_end,
                            limit=ohlcv_limit,
                            adjust=args.ashare_adjust,
                            is_closed=args.is_closed,
                            status=args.status,
                            source_gate=ashare_kline_source_gate,
                        ),
                    ),
                    schedule_retry=schedule_failure_retry,
                )

            batch_enriched_results: list[CollectionTaskResult | None] = [None] * len(batch_symbols)

            def handle_symbol_result(symbol: str, result: CollectionTaskResult, index: int) -> None:
                enriched_result = attach_batch_payload(
                    result,
                    batch_index=batch_index,
                    batch_count=batch_count,
                    batch_size=current_batch_size,
                    symbol_count=len(symbols),
                    sync_task_type=task_type,
                    requested_start=getattr(args, "ashare_start", None),
                    requested_end=getattr(args, "ashare_end", None),
                )
                batch_enriched_results[index] = enriched_result
                watermark_session_factory = session_factory if current_max_workers > 1 else None
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
    if task_type == "event_refresh":
        source_limit = list_source_limit(args)
        return [
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
    if task_type == "event_article_enrichment":
        return build_ashare_news_article_enrichment_tasks(
            session,
            collector,
            args,
            runtime,
            session_factory=session_factory,
        )
    return build_ashare_p1_default_tasks(
        session,
        collector,
        args,
        runtime,
        session_factory=session_factory,
    )


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
    source_limit = list_source_limit(args)

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
            parameters={"indicator": args.flow_window, "limit": source_limit},
            force=args.force_provider,
            collect=lambda: collector.collect_flow_rank(
                indicator=args.flow_window,
                limit=source_limit,
            ),
        )
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
    symbols = resolve_ashare_priority_news_symbols(session, args)

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
            if session_factory is not None and max_workers > 1:
                with session_scope(session_factory) as worker_session:
                    worker_collector = AshareP1Collector(worker_session)
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
                                asset_name=asset_name_for_symbol(worker_session, symbol),
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
            if session_factory is not None and max_workers > 1:
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
    for source_task in source_tasks:
        task = str(source_task["task"])
        provider_key = str(source_task["provider_key"])
        parameters = dict(source_task["parameters"])
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

            def collect_symbol(symbol: str) -> CollectionTaskResult:
                if symbol in skip_symbols:
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

            def collect_symbol(symbol: str) -> CollectionTaskResult:
                if symbol in skip_symbols:
                    return crypto_watermark_skip_result(
                        task="crypto_derivative_snapshot",
                        symbol=symbol,
                        market=crypto_market,
                        data_domain=CRYPTO_DERIVATIVE_DATA_DOMAIN,
                        provider=CRYPTO_DERIVATIVE_PROVIDER,
                        timeframe=derivative_timeframe,
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

                def wrap_result(result: CollectionTaskResult, *, worker_session: Any | None = None) -> CollectionTaskResult:
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

                def wrap_result(result: CollectionTaskResult, *, worker_session: Any | None = None) -> CollectionTaskResult:
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


def ashare_kline_source_gate(source_key: str, collect: Callable[[], Any]) -> Any:
    """按真实 K 线数据源拆分限流和进度状态。"""

    # A 股 K 线的有效并发由任务队列 worker 控制，源优先级和降级保留在 provider 内部。
    return collect()


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
) -> list[str]:
    """按覆盖情况和水位选择基金日 K 任务本轮要处理的代码。"""

    try:
        assets = [
            asset
            for asset in AssetRepository(session).find_by_market("fund")
            if str(getattr(asset, "asset_type", "") or "").strip() == asset_type
        ]
        symbol_by_asset_id = {
            str(asset.asset_id): str(getattr(asset, "symbol", "") or "").strip() for asset in assets
        }
        asset_ids = list(symbol_by_asset_id)
        coverage = _fetch_fund_bar_coverage(session, asset_ids, timeframe=timeframe)
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
                    )
                )
            ],
            key=lambda asset: (
                coverage.get(asset.asset_id, (0, None))[0],
                coverage.get(asset.asset_id, (0, None))[1] or datetime.min.replace(tzinfo=UTC),
                symbol_by_asset_id.get(asset.asset_id, ""),
            ),
        )
        symbols = [symbol_by_asset_id.get(asset.asset_id, "") for asset in ranked_assets]
    except Exception as exc:
        logger.warning("基金日 K 标的筛选失败 asset_type=%s error=%s", asset_type, exc, exc_info=True)
        symbols = []
    symbols = [symbol for symbol in symbols if symbol]
    if not symbols:
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
) -> list[str]:
    """按净值覆盖和失败水位选择开放式基金任务本轮要处理的代码。"""

    try:
        assets = [
            asset
            for asset in AssetRepository(session).find_by_market("fund")
            if str(getattr(asset, "asset_type", "") or "").strip() == "open_fund"
        ]
        symbol_by_asset_id = {
            str(asset.asset_id): str(getattr(asset, "symbol", "") or "").strip() for asset in assets
        }
        asset_ids = list(symbol_by_asset_id)
        coverage = _fetch_fund_nav_coverage(session, asset_ids)
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
                    )
                )
            ],
            key=lambda asset: (
                coverage.get(asset.asset_id, (0, None))[0],
                coverage.get(asset.asset_id, (0, None))[1] or date.min,
                symbol_by_asset_id.get(asset.asset_id, ""),
            ),
        )
        symbols = [symbol_by_asset_id.get(asset.asset_id, "") for asset in ranked_assets]
    except Exception as exc:
        logger.warning("开放式基金净值标的筛选失败 error=%s", exc, exc_info=True)
        symbols = []
    symbols = [symbol for symbol in symbols if symbol]
    if not symbols:
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
            MarketBarORM.asset_id.in_(asset_ids),
        )
        .group_by(MarketBarORM.asset_id)
    )
    return {
        str(asset_id): (int(count or 0), earliest, latest)
        for asset_id, count, earliest, latest in session.execute(statement)
    }


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
) -> dict[str, tuple[int, date | None]]:
    """读取开放式基金净值覆盖度。"""

    if not asset_ids:
        return {}
    statement = (
        select(
            FundNavSnapshotORM.asset_id,
            func.count(FundNavSnapshotORM.nav_date),
            func.max(FundNavSnapshotORM.nav_date),
        )
        .where(FundNavSnapshotORM.asset_id.in_(asset_ids))
        .group_by(FundNavSnapshotORM.asset_id)
    )
    return {
        str(asset_id): (int(count or 0), latest)
        for asset_id, count, latest in session.execute(statement)
    }


def _asset_requires_fund_bar_collection(
    asset: Any,
    *,
    coverage: dict[str, tuple[int, datetime | None]],
    watermark: Any,
    now: datetime,
    stale_before: datetime | None,
) -> bool:
    """判断基金日 K 是否需要补采。"""

    status = str(getattr(watermark, "status", "") or "").lower() if watermark else ""
    next_retry_at = getattr(watermark, "next_retry_at", None) if watermark else None
    if status == "error" and (next_retry_at is None or next_retry_at <= now):
        return True
    bar_count, latest_bar_at = coverage.get(asset.asset_id, (0, None))
    if bar_count <= 0 or latest_bar_at is None:
        return True
    if stale_before is not None and latest_bar_at < stale_before:
        return True
    return False


def _asset_requires_open_nav_collection(
    asset: Any,
    *,
    coverage: dict[str, tuple[int, date | None]],
    watermark: Any,
    now: datetime,
    stale_before: datetime | None,
) -> bool:
    """判断开放式基金净值是否需要补采。"""

    status = str(getattr(watermark, "status", "") or "").lower() if watermark else ""
    next_retry_at = getattr(watermark, "next_retry_at", None) if watermark else None
    if status == "error" and (next_retry_at is None or next_retry_at <= now):
        return True
    nav_count, latest_nav_date = coverage.get(asset.asset_id, (0, None))
    if nav_count <= 0 or latest_nav_date is None:
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
    """按财务/估值水位选择 A 股基本面刷新标的。"""

    selection_failed = False
    has_candidate_assets = False
    try:
        repo = AssetRepository(session)
        eligibility = TradeableAssetEligibilityService()
        assets = eligibility.filter_tradeable_assets(repo.find_by_market("ashare"))
        has_candidate_assets = bool(assets)
        asset_ids = [asset.asset_id for asset in assets]
        watermarks_by_domain: dict[str, dict[str, Any]] = {}
        if only_failed_or_stale:
            watermarks_by_domain = {
                ASHARE_FUNDAMENTAL_DATA_DOMAIN: _fetch_data_sync_watermarks(
                    session,
                    asset_ids,
                    data_domain=ASHARE_FUNDAMENTAL_DATA_DOMAIN,
                    provider=ASHARE_FINANCIAL_INDICATORS_PROVIDER,
                    timeframe="",
                ),
                ASHARE_VALUATION_DATA_DOMAIN: _fetch_data_sync_watermarks(
                    session,
                    asset_ids,
                    data_domain=ASHARE_VALUATION_DATA_DOMAIN,
                    provider=ASHARE_VALUATION_PROVIDER,
                    timeframe="",
                ),
            }
        current_time = now or datetime.now(tz=UTC)
        selected_assets = [
            asset
            for asset in assets
            if (
                not only_failed_or_stale
                or _asset_requires_ashare_fundamental_collection(
                    asset,
                    fundamental_watermark=watermarks_by_domain.get(
                        ASHARE_FUNDAMENTAL_DATA_DOMAIN,
                        {},
                    ).get(asset.asset_id),
                    valuation_watermark=watermarks_by_domain.get(
                        ASHARE_VALUATION_DATA_DOMAIN,
                        {},
                    ).get(asset.asset_id),
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


def _asset_requires_ashare_fundamental_collection(
    asset: Any,
    *,
    fundamental_watermark: Any,
    valuation_watermark: Any,
    now: datetime,
    stale_before: datetime | None,
) -> bool:
    """判断单个 A 股标的是否需要重新同步基本面或估值。"""

    return any(
        _snapshot_watermark_requires_collection(watermark, now=now, stale_before=stale_before)
        for watermark in (fundamental_watermark, valuation_watermark)
    )


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
    payload = getattr(watermark, "payload", None)
    if not isinstance(payload, dict):
        return False
    start_token = _format_ashare_request_date(required_start_at)
    end_token = _format_ashare_request_date(required_end_at)
    payload_start = str(payload.get("requested_start") or "").strip()
    payload_end = str(payload.get("requested_end") or "").strip()
    if start_token and (not payload_start or payload_start > start_token):
        return False
    if end_token and (not payload_end or payload_end < end_token):
        return False
    return bool(start_token or end_token)


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
) -> bool:
    """判断资产是否需要被 revision/补漏任务重新采集。"""

    status = str(getattr(watermark, "status", "") or "").lower() if watermark else ""
    next_retry_at = getattr(watermark, "next_retry_at", None) if watermark else None
    if status == "error" and (next_retry_at is None or next_retry_at <= now):
        return True
    if _watermark_covers_ashare_bar_request(
        watermark,
        required_start_at=required_start_at,
        required_end_at=required_end_at,
    ):
        return False
    coverage_value = coverage.get(asset.asset_id)
    bar_count = _coverage_bar_count(coverage_value)
    earliest_bar_at = _coverage_earliest_bar_at(coverage_value)
    latest_bar_at = _coverage_latest_bar_at(coverage_value)
    if bar_count <= 0 or latest_bar_at is None:
        return True
    if required_end_at is not None and _datetime_before(latest_bar_at, required_end_at):
        return True
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
    failure_payload = {
        "status": status,
        "item_count": result_item_count(result),
    } | _ashare_market_bar_watermark_metadata(result)
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


def ashare_risk_sentiment_watermark_timeframe(
    *,
    task: str,
    provider: str,
    parameters: Mapping[str, Any],
) -> str:
    """生成风险情绪子源水位粒度，确保不同日期窗口互不误伤。"""

    if task == "ashare_risk_stop_list":
        return "stop_list"
    if task == "ashare_sentiment_hot_rank":
        return "hot_rank"
    if task == "ashare_sentiment_zt_pool":
        return f"zt_pool:{parameters.get('date') or 'latest'}"
    if task in {"ashare_risk_lhb_detail", "ashare_risk_margin_sse"}:
        start_date = str(parameters.get("start_date") or "").strip() or "latest"
        end_date = str(parameters.get("end_date") or "").strip() or "latest"
        return f"{provider}:{start_date}:{end_date}"
    if task == "ashare_risk_block_trades":
        symbol = str(parameters.get("symbol") or "A股").strip() or "A股"
        start_date = str(parameters.get("start_date") or "").strip() or "latest"
        end_date = str(parameters.get("end_date") or "").strip() or "latest"
        return f"{provider}:{symbol}:{start_date}:{end_date}"
    if task == "ashare_risk_margin_szse":
        return f"margin_szse:{parameters.get('date') or 'latest'}"
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
        if task_type_name(args) == "market_bars_full_history_backfill":
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
            "limit": None,
            "fallback_symbol": getattr(args, "fund_symbol", COLLECTION_ARG_DEFAULTS["fund_symbol"]),
            "asset_type": asset_type,
            "timeframe": getattr(args, "fund_timeframe", "1d"),
        }
        if bool(getattr(args, "only_failed_or_stale", False)):
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
            "limit": None,
            "fallback_symbol": getattr(args, "fund_symbol", COLLECTION_ARG_DEFAULTS["fund_symbol"]),
        }
        if bool(getattr(args, "only_failed_or_stale", False)):
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
        if data_domain == FUND_NAV_DATA_DOMAIN:
            coverage = _fetch_fund_nav_coverage(session, [asset_id])
            latest_count, latest_at = coverage.get(asset_id, (0, None))
        else:
            coverage = _fetch_fund_bar_coverage(session, [asset_id], timeframe=timeframe)
            latest_count, latest_at = coverage.get(asset_id, (0, None))
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
            payload={"item_count": result_item_count(result), "latest_count": latest_count},
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
        payload={"status": status, "item_count": result_item_count(result)},
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
            limit=None,
            fallback_symbol=args.crypto_symbol,
        )
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
            limit=None,
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
