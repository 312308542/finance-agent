"""数据同步配置模型。

本模块把“全量 Universe 刷新 + 增量补采”的底层 JSON 抽象成可验证的
配置对象。CLI、TUI 和后续前端页面都应复用这里的预设、校验和预览逻辑，
避免让用户手写股票代码或理解底层采集任务细节。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

JsonDict = dict[str, Any]
DEFAULT_SCHEDULER_JOB_TIMEOUT_SECONDS = 6 * 60 * 60
DEFAULT_SCHEDULER_HEALTH_STALE_SECONDS = 300
DEFAULT_SCHEDULER_MAX_CONCURRENT_JOBS = 4

DataSyncPreset = Literal[
    "personal-comprehensive",
    "ashare-comprehensive",
    "crypto-comprehensive",
    "lightweight",
]

MARKETS = {"ashare", "crypto_spot", "crypto_future"}
PRESETS = {
    "personal-comprehensive",
    "ashare-comprehensive",
    "crypto-comprehensive",
    "lightweight",
}

ASHARE_UNIVERSE_SOURCES = {
    "all_ashare": "全 A 资产池",
    "index_members": "指数成分",
    "industry_members": "行业成分",
    "concept_members": "概念成分",
    "capital_flow_rank": "资金流榜",
    "hot_rank": "人气热度榜",
    "zt_pool": "涨停池",
}

ASHARE_DATA_PACKAGES = {
    "market_bars": "K 线行情",
    "realtime_quotes": "实时行情快照",
    "fundamentals": "财务指标和业绩",
    "valuation": "估值和股息率",
    "capital_flow": "资金流",
    "events": "新闻和公告",
    "risk_sentiment": "风险和短线情绪",
    "data_quality": "数据质量检查",
}

CRYPTO_UNIVERSE_SOURCES = {
    "binance_usdt": "Binance USDT 交易对",
}

CRYPTO_DATA_PACKAGES = {
    "market_bars": "K 线行情",
    "derivatives": "资金费率、未平仓量和多空比",
    "data_quality": "数据质量检查",
}

ASHARE_TIMELY_EVENT_INTERVAL_SECONDS = 5 * 60
DEFAULT_SYMBOL_FETCH_MAX_WORKERS = 4


@dataclass(frozen=True)
class MarketSyncConfig:
    """单个市场的数据同步配置。"""

    enabled: bool
    universe_sources: list[str] = field(default_factory=list)
    data_packages: list[str] = field(default_factory=list)
    interval_seconds: dict[str, int] = field(default_factory=dict)
    batch_size: int = 200
    max_workers: int = DEFAULT_SYMBOL_FETCH_MAX_WORKERS
    lookback_days: int = 30
    lookback_hours: int = 72
    timeframes: list[str] = field(default_factory=list)
    filters: JsonDict = field(default_factory=dict)


@dataclass(frozen=True)
class DataSyncConfig:
    """数据同步总配置。"""

    schema_version: str
    preset: str
    enabled: bool
    cache_backend: str
    resource_profile: str
    markets: dict[str, MarketSyncConfig]
    lock_ttl_seconds: int = 600
    circuit_failure_threshold: int = 3
    circuit_cooldown_seconds: int = 900
    loop_idle_seconds: int = 5
    max_concurrent_jobs: int = DEFAULT_SCHEDULER_MAX_CONCURRENT_JOBS

    def to_dict(self) -> JsonDict:
        """转换为可写入 JSON 的字典。"""

        return asdict(self)


@dataclass(frozen=True)
class DataSyncTaskPreview:
    """单个数据同步任务预览。"""

    task_key: str
    market: str
    task_type: str
    title: str
    interval_seconds: int
    mode: str
    batch_size: int | None = None
    max_workers: int | None = None
    lookback: str | None = None
    sources: list[str] = field(default_factory=list)
    data_packages: list[str] = field(default_factory=list)
    manual_symbol_required: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        """转换为 JSON 字典。"""

        return asdict(self)


@dataclass(frozen=True)
class DataSyncValidationResult:
    """数据同步配置校验结果。"""

    valid: bool
    errors: list[str]
    warnings: list[str]
    enabled_market_count: int
    task_count: int

    def to_dict(self) -> JsonDict:
        """转换为 JSON 字典。"""

        return asdict(self)


def build_preset_config(
    preset: str = "personal-comprehensive",
    *,
    markets: list[str] | None = None,
) -> DataSyncConfig:
    """根据预设生成数据同步配置。"""

    normalized_preset = normalize_preset(preset)
    selected_markets = normalize_markets(markets or preset_markets(normalized_preset))
    market_configs: dict[str, MarketSyncConfig] = {}
    for market in selected_markets:
        if market == "ashare":
            market_configs[market] = build_ashare_market_config(normalized_preset)
        elif market in {"crypto_spot", "crypto_future"}:
            market_configs[market] = build_crypto_market_config(
                market=market,
                preset=normalized_preset,
            )

    return DataSyncConfig(
        schema_version="1.0",
        preset=normalized_preset,
        enabled=True,
        cache_backend="redis",
        resource_profile="全面但限流友好",
        markets=market_configs,
    )


def load_data_sync_config(config_file: str | Path | None = None) -> DataSyncConfig:
    """从 JSON 文件读取数据同步配置；未传入时返回私人助手全面模式。"""

    if config_file is None:
        return build_preset_config()
    path = Path(config_file)
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("数据同步配置文件必须是 JSON 对象。")
    return parse_data_sync_config(payload)


def save_data_sync_config(config: DataSyncConfig, output: str | Path) -> Path:
    """保存数据同步配置。"""

    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(config.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


def parse_data_sync_config(payload: JsonDict) -> DataSyncConfig:
    """把字典解析为数据同步配置。"""

    markets_payload = payload.get("markets")
    if not isinstance(markets_payload, dict):
        raise ValueError("数据同步配置必须包含 markets 对象。")
    markets = {
        market: parse_market_config(market, config)
        for market, config in markets_payload.items()
    }
    return DataSyncConfig(
        schema_version=str(payload.get("schema_version") or "1.0"),
        preset=str(payload.get("preset") or "custom"),
        enabled=parse_bool(payload.get("enabled"), default=True),
        cache_backend=str(payload.get("cache_backend") or "redis"),
        resource_profile=str(payload.get("resource_profile") or "自定义"),
        lock_ttl_seconds=positive_int(payload.get("lock_ttl_seconds"), default=600),
        circuit_failure_threshold=positive_int(
            payload.get("circuit_failure_threshold"),
            default=3,
        ),
        circuit_cooldown_seconds=positive_int(
            payload.get("circuit_cooldown_seconds"),
            default=900,
        ),
        loop_idle_seconds=positive_int(payload.get("loop_idle_seconds"), default=5),
        max_concurrent_jobs=positive_int(
            payload.get("max_concurrent_jobs"),
            default=DEFAULT_SCHEDULER_MAX_CONCURRENT_JOBS,
        ),
        markets=markets,
    )


def parse_market_config(market: str, payload: object) -> MarketSyncConfig:
    """解析单市场配置。"""

    if not isinstance(payload, dict):
        raise ValueError(f"市场配置必须是对象：{market}")
    return MarketSyncConfig(
        enabled=parse_bool(payload.get("enabled"), default=True),
        universe_sources=string_list(payload.get("universe_sources")),
        data_packages=string_list(payload.get("data_packages")),
        interval_seconds=parse_interval_seconds(payload.get("interval_seconds")),
        batch_size=positive_int(payload.get("batch_size"), default=200),
        max_workers=positive_int(
            payload.get("max_workers"),
            default=DEFAULT_SYMBOL_FETCH_MAX_WORKERS,
        ),
        lookback_days=positive_int(payload.get("lookback_days"), default=30),
        lookback_hours=positive_int(payload.get("lookback_hours"), default=72),
        timeframes=string_list(payload.get("timeframes")),
        filters=dict(payload.get("filters") or {}),
    )


def validate_data_sync_config(config: DataSyncConfig) -> DataSyncValidationResult:
    """校验数据同步配置是否可用于生成任务。"""

    errors: list[str] = []
    warnings: list[str] = []
    if config.cache_backend not in {"auto", "redis", "null"}:
        errors.append("cache_backend 只能是 auto、redis 或 null。")
    if config.max_concurrent_jobs > 16:
        errors.append("max_concurrent_jobs 不能超过 16，避免本地采集线程过多。")
    if not config.markets:
        errors.append("至少需要启用一个市场。")

    enabled_market_count = 0
    for market, market_config in config.markets.items():
        if market not in MARKETS:
            errors.append(f"不支持的市场：{market}")
            continue
        if not market_config.enabled:
            continue
        enabled_market_count += 1
        validate_market_config(
            market=market,
            market_config=market_config,
            errors=errors,
            warnings=warnings,
        )
    tasks = preview_data_sync_tasks(config)
    if config.enabled and not tasks:
        errors.append("当前配置没有可执行的数据同步任务。")
    return DataSyncValidationResult(
        valid=not errors,
        errors=errors,
        warnings=warnings,
        enabled_market_count=enabled_market_count,
        task_count=len(tasks),
    )


def preview_data_sync_config(config: DataSyncConfig) -> JsonDict:
    """生成供 CLI、TUI 和前端展示的数据同步预览。"""

    tasks = preview_data_sync_tasks(config)
    validation = validate_data_sync_config_without_preview_recursion(config, tasks)
    return {
        "schema_version": config.schema_version,
        "preset": config.preset,
        "preset_label": preset_label(config.preset),
        "enabled": config.enabled,
        "resource_profile": config.resource_profile,
        "cache_backend": config.cache_backend,
        "max_concurrent_jobs": config.max_concurrent_jobs,
        "manual_symbol_required": any(task.manual_symbol_required for task in tasks),
        "enabled_markets": [
            market
            for market, market_config in config.markets.items()
            if market_config.enabled
        ],
        "tasks": [task.to_dict() for task in tasks],
        "processing": preview_data_processing_plan(config, tasks=tasks),
        "validation": validation.to_dict(),
    }


def preview_data_sync_tasks(config: DataSyncConfig) -> list[DataSyncTaskPreview]:
    """根据配置生成任务预览。"""

    if not config.enabled:
        return []
    tasks: list[DataSyncTaskPreview] = []
    for market, market_config in sorted(config.markets.items()):
        if not market_config.enabled:
            continue
        if market == "ashare":
            tasks.extend(preview_ashare_tasks(market_config))
        elif market in {"crypto_spot", "crypto_future"}:
            tasks.extend(preview_crypto_tasks(market, market_config))
    return tasks


def preview_data_processing_plan(
    config: DataSyncConfig,
    *,
    tasks: list[DataSyncTaskPreview] | None = None,
) -> JsonDict:
    """生成采集后清洗、指标、因子和推荐计算的展示计划。"""

    sync_tasks = tasks if tasks is not None else preview_data_sync_tasks(config)
    enabled_markets = [
        market
        for market, market_config in config.markets.items()
        if market_config.enabled
    ]
    collection_task_keys = [task.task_key for task in sync_tasks]
    normalization_status = "active_with_collection" if sync_tasks else "disabled"
    recommendation_jobs = build_recommendation_scheduler_jobs(config)
    analytics_status = "active_scheduled" if recommendation_jobs else "disabled"
    scheduler_status = "covered_by_analytics_jobs" if recommendation_jobs else "disabled"
    analytics_stages = [
        {
            "stage_key": "analytics.indicators",
            "title": "技术指标计算",
            "status": analytics_status,
            "execution": "post_collection_pipeline",
            "service": "IndicatorService.compute_for_asset",
            "trigger": "market_bars 刷新后按候选池成员批量计算",
            "frequency_policy": (
                "跟随 K 线采集频率；A 股日线通常每小时补采后重算，"
                "数字货币 1h 可 5 分钟检查一次增量窗口"
            ),
            "inputs": ["market_bars"],
            "outputs": ["indicator_frames"],
            "notes": ["计算 RSI、MACD、ATR、布林带、均线、收益率、波动率、回撤和成交额 z-score。"],
        },
        {
            "stage_key": "analytics.factors",
            "title": "因子归一化与分组",
            "status": analytics_status,
            "execution": "post_collection_pipeline",
            "service": "FactorService.compute_for_asset",
            "trigger": "指标、基本面、估值、资金流、事件或风险快照更新后计算",
            "frequency_policy": (
                "与最慢的基础输入做水位对齐；新闻和风险事件只触发增量因子刷新，"
                "不必每次全量重算全市场"
            ),
            "inputs": [
                "indicator_frames",
                "fundamental_snapshots",
                "capital_flow_snapshots",
                "event_records",
                "risk_findings",
                "crypto_derivative_snapshots",
            ],
            "outputs": ["factor_frames"],
            "notes": ["按市场过滤不适用因子组，记录缺失组和部分可用组。"],
        },
        {
            "stage_key": "analytics.screening",
            "title": "候选池初筛",
            "status": analytics_status,
            "execution": "post_collection_pipeline",
            "service": "ScreeningService.apply_rules",
            "trigger": "候选池和因子快照可用后执行",
            "frequency_policy": "与目标候选池刷新频率同步；热点池可更高频，全 A 池建议限流分批",
            "inputs": ["asset_universes", "asset_universe_members", "factor_frames"],
            "outputs": ["screening_results", "screening_result_items"],
            "notes": ["这是推荐排序前的规则过滤层。"],
        },
        {
            "stage_key": "analytics.scoring",
            "title": "多维评分",
            "status": analytics_status,
            "execution": "post_collection_pipeline",
            "service": "ScoringService.score_screening",
            "trigger": "初筛结果生成后执行",
            "frequency_policy": "与初筛同频",
            "inputs": ["screening_result_items", "factor_frames"],
            "outputs": ["asset_scores"],
            "notes": ["生成候选池内排序基础分。"],
        },
        {
            "stage_key": "analytics.signals",
            "title": "信号快照",
            "status": analytics_status,
            "execution": "post_collection_pipeline",
            "service": "SignalService.compute_for_asset",
            "trigger": "因子快照更新后执行",
            "frequency_policy": "与因子同频；观察池触发读取最新 signal_snapshots",
            "inputs": ["factor_frames"],
            "outputs": ["signal_snapshots"],
            "notes": ["生成方向、置信度、触发原因和解释字段。"],
        },
        {
            "stage_key": "analytics.recommendations",
            "title": "推荐排序",
            "status": analytics_status,
            "execution": "post_collection_pipeline",
            "service": "UniverseRecommendationPipeline.run_for_universe",
            "trigger": "明确 universe_id 后串联指标、因子、初筛、评分、信号和推荐",
            "frequency_policy": (
                "应在基础采集完成后触发；需要按 universe_id 去抖，"
                "避免新闻 5 分钟增量同步导致全市场重复重算"
            ),
            "inputs": [
                "asset_universes",
                "asset_universe_members",
                "asset_scores",
                "signal_snapshots",
            ],
            "outputs": [
                "recommendation_runs",
                "recommendation_run_universes",
                "asset_recommendations",
            ],
            "notes": ["基础数据调度器已把该流水线注册为独立 analytics job。"],
        },
    ]
    normalization_stage = {
        "stage_key": "normalization.collection_payloads",
        "title": "采集清洗与标准化",
        "status": normalization_status,
        "execution": "inline_with_collection",
        "service": "finance_agent.data.normalizers + collectors",
        "trigger": "每个基础采集任务写库前同步执行",
        "frequency_policy": "与基础采集任务完全同频",
        "task_keys": collection_task_keys,
        "inputs": ["provider_payloads", "raw_records"],
        "outputs": [
            "assets",
            "asset_universes",
            "asset_universe_members",
            "market_bars",
            "fundamental_snapshots",
            "capital_flow_snapshots",
            "event_records",
            "risk_findings",
            "crypto_derivative_snapshots",
        ],
        "notes": ["采集器负责字段命名、市场标识、时间戳、数值类型和标准表落库。"],
    }
    return {
        "normalization": {
            "title": "清洗与归一化",
            "status": normalization_status,
            "execution": "inline_with_collection",
            "scheduler_status": "covered_by_collection_jobs" if sync_tasks else "disabled",
            "frequency_policy": "与对应基础采集任务同步执行",
            "task_count": len(sync_tasks),
            "outputs": normalization_stage["outputs"],
        },
        "analytics": {
            "title": "指标、因子、评分、信号与推荐",
            "status": analytics_status,
            "execution": "post_collection_pipeline",
            "scheduler_status": scheduler_status,
            "default_pipeline": "UniverseRecommendationPipeline.run_for_universe",
            "required_runtime_input": None if recommendation_jobs else "universe_id",
            "candidate_universe_patterns": candidate_universe_patterns(enabled_markets),
            "scheduled_universe_ids": [
                job["params"]["universe_id"] for job in recommendation_jobs
            ],
            "outputs": [
                "indicator_frames",
                "factor_frames",
                "screening_results",
                "asset_scores",
                "signal_snapshots",
                "recommendation_runs",
                "asset_recommendations",
            ],
            "notes": [
                "基础数据调度器会在独立 analytics job 中运行推荐流水线。",
                "推荐流水线必须选择同市场候选池 universe_id 后再运行，不能使用 mixed 候选池。",
            ],
        },
        "stages": [normalization_stage, *analytics_stages],
    }


def candidate_universe_patterns(markets: list[str]) -> list[str]:
    """返回当前市场可用于后续推荐流水线的候选池 ID 模式。"""

    patterns: list[str] = []
    if "ashare" in markets:
        patterns.extend(
            [
                "universe:base:ashare:p0:all_a",
                "universe:base:ashare:p1:index:<index_code>",
                "universe:base:ashare:p1:industry:<industry_name>",
                "universe:base:ashare:p1:concept:<concept_name>",
                "universe:base:ashare:p2:sentiment:hot_rank",
                "universe:base:ashare:p2:sentiment:zt_pool:<date>",
            ]
        )
    if "crypto_spot" in markets:
        patterns.append("universe:base:crypto:spot:binance")
    if "crypto_future" in markets:
        patterns.append("universe:base:crypto:future:binance")
    return patterns


def export_scheduler_payload(config: DataSyncConfig) -> JsonDict:
    """导出供调度器或前端保存的任务计划。

    这里导出的是“数据同步任务计划”，不是旧版样例采集 JSON。计划中只描述
    Universe 来源、采集包、批大小和补采窗口，不要求用户填写单只股票代码。
    """

    tasks = preview_data_sync_tasks(config)
    return {
        "schema_version": "data-sync-scheduler-v1",
        "enabled": config.enabled,
        "cache_backend": config.cache_backend,
        "lock_ttl_seconds": config.lock_ttl_seconds,
        "circuit_failure_threshold": config.circuit_failure_threshold,
        "circuit_cooldown_seconds": config.circuit_cooldown_seconds,
        "loop_idle_seconds": config.loop_idle_seconds,
        "max_job_retries": 2,
        "retry_backoff_seconds": 30,
        "job_timeout_seconds": DEFAULT_SCHEDULER_JOB_TIMEOUT_SECONDS,
        "health_stale_seconds": DEFAULT_SCHEDULER_HEALTH_STALE_SECONDS,
        "max_concurrent_jobs": config.max_concurrent_jobs,
        "jobs": [
            *[build_scheduler_job(task) for task in tasks],
            *build_data_quality_scheduler_jobs(config),
            *build_recommendation_scheduler_jobs(config),
        ],
        "processing": preview_data_processing_plan(config, tasks=tasks),
        "notes": [
            "该计划由数据同步配置向导生成，不要求用户手填股票代码。",
            (
                "run_base_data_scheduler.py 可直接读取该计划或原始数据同步配置"
                "并执行 dry-run / run-once / loop。"
            ),
        ],
    }


def build_scheduler_job(task: DataSyncTaskPreview) -> JsonDict:
    """把逻辑数据同步任务映射为基础采集执行器可识别的 job。"""

    group = collection_group_for_task(task)
    params = {
        "sync_task_type": task.task_type,
        "mode": task.mode,
        "sources": task.sources,
        "data_packages": task.data_packages,
        "batch_size": task.batch_size,
        "lookback": task.lookback,
        "symbol_source": "market_assets",
    }
    if task.max_workers is not None:
        params["max_workers"] = task.max_workers
    if task.market == "ashare":
        params.update(build_ashare_collection_params(task))
    elif task.market in {"crypto_spot", "crypto_future"}:
        params.update(build_crypto_collection_params(task))

    return {
        "name": task.task_key,
        "job_type": "collection",
        "group": group,
        "enabled": True,
        "interval_seconds": task.interval_seconds,
        "limit": task.batch_size,
        "market": task.market,
        "params": params,
    }


def build_recommendation_scheduler_jobs(config: DataSyncConfig) -> list[JsonDict]:
    """为启用市场生成真实候选池推荐流水线任务。"""

    if not config.enabled:
        return []
    jobs: list[JsonDict] = []
    for market, market_config in sorted(config.markets.items()):
        if not market_config.enabled:
            continue
        if market == "ashare":
            jobs.append(
                build_recommendation_scheduler_job(
                    name="analytics.recommendations.ashare.all_a",
                    market="ashare",
                    universe_id="universe:base:ashare:p0:all_a",
                    interval_seconds=market_config.interval_seconds.get("market_bars", 60 * 60),
                    timeframe=(market_config.timeframes or ["1d"])[0],
                    limit=min(market_config.batch_size, 200),
                    min_bars=60,
                    min_indicator_coverage_ratio=0.7,
                    min_factor_coverage_ratio=0.5,
                    min_available_factor_groups=3,
                    auto_sync_watchlist=True,
                    owner_id="default-owner",
                    watchlist_id="watchlist:default-owner:ashare:recommendations",
                    recommendation_intake_limit=20,
                )
            )
        elif market == "crypto_spot":
            jobs.append(
                build_recommendation_scheduler_job(
                    name="analytics.recommendations.crypto_spot.binance",
                    market="crypto_spot",
                    universe_id="universe:base:crypto:spot:binance",
                    interval_seconds=market_config.interval_seconds.get("market_bars", 5 * 60),
                    timeframe=(market_config.timeframes or ["1h"])[0],
                    limit=min(market_config.batch_size, 150),
                    window=120,
                    min_bars=120,
                    min_indicator_coverage_ratio=0.85,
                    min_factor_coverage_ratio=0.6,
                    min_available_factor_groups=2,
                    auto_sync_watchlist=True,
                    owner_id="default-owner",
                    watchlist_id="watchlist:default-owner:crypto_spot:recommendations",
                    recommendation_intake_limit=20,
                )
            )
        elif market == "crypto_future":
            jobs.append(
                build_recommendation_scheduler_job(
                    name="analytics.recommendations.crypto_future.binance",
                    market="crypto_future",
                    universe_id="universe:base:crypto:future:binance",
                    interval_seconds=market_config.interval_seconds.get("market_bars", 5 * 60),
                    timeframe=(market_config.timeframes or ["1h"])[0],
                    limit=min(market_config.batch_size, 150),
                    window=120,
                    min_bars=120,
                    min_indicator_coverage_ratio=0.85,
                    min_factor_coverage_ratio=0.6,
                    min_available_factor_groups=3,
                    auto_sync_watchlist=True,
                    owner_id="default-owner",
                    watchlist_id="watchlist:default-owner:crypto_future:recommendations",
                    recommendation_intake_limit=20,
                )
            )
    return jobs


def build_data_quality_scheduler_jobs(config: DataSyncConfig) -> list[JsonDict]:
    """为启用 data_quality 的市场生成数据质量刷新任务。"""

    if not config.enabled:
        return []
    jobs: list[JsonDict] = []
    for market, market_config in sorted(config.markets.items()):
        if not market_config.enabled or "data_quality" not in market_config.data_packages:
            continue
        timeframe = (market_config.timeframes or ["1d" if market == "ashare" else "1h"])[0]
        if market == "ashare":
            min_bars = 60
            domains = [
                "market_bars",
                "realtime_quotes",
                "indicator_frames",
                "factor_frames",
                "recommendations",
            ]
            interval_seconds = market_config.interval_seconds.get("market_bars", 60 * 60)
            stale_after_seconds = 24 * 60 * 60
        else:
            min_bars = 120
            domains = [
                "market_bars",
                "indicator_frames",
                "factor_frames",
                "recommendations",
            ]
            interval_seconds = market_config.interval_seconds.get("market_bars", 5 * 60)
            stale_after_seconds = 2 * 60 * 60
        jobs.append(
            {
                "name": f"quality.{market}",
                "job_type": "data_quality_refresh",
                "group": "analytics",
                "enabled": True,
                "interval_seconds": interval_seconds,
                "limit": market_config.batch_size,
                "market": market,
                "params": {
                    "market": market,
                    "timeframe": timeframe,
                    "horizon": "swing",
                    "min_bars": min_bars,
                    "stale_after_seconds": stale_after_seconds,
                    "data_domains": domains,
                },
            }
        )
    return jobs


def build_recommendation_scheduler_job(
    *,
    name: str,
    market: str,
    universe_id: str,
    interval_seconds: int,
    timeframe: str,
    limit: int,
    window: int = 120,
    min_bars: int = 2,
    min_indicator_coverage_ratio: float = 0.5,
    min_factor_coverage_ratio: float = 0.0,
    min_available_factor_groups: int = 1,
    auto_sync_watchlist: bool = False,
    owner_id: str | None = None,
    watchlist_id: str | None = None,
    recommendation_intake_limit: int | None = None,
) -> JsonDict:
    """生成单个推荐流水线调度任务。"""

    return {
        "name": name,
        "job_type": "recommendation_pipeline",
        "group": "analytics",
        "enabled": True,
        "interval_seconds": interval_seconds,
        "limit": limit,
        "market": market,
        "params": {
            "universe_id": universe_id,
            "strategy": "balanced_swing_v1",
            "horizon": "swing",
            "timeframe": timeframe,
            "window": window,
            "min_bars": min_bars,
            "min_indicator_coverage_ratio": min_indicator_coverage_ratio,
            "min_factor_coverage_ratio": min_factor_coverage_ratio,
            "min_available_factor_groups": min_available_factor_groups,
            "auto_sync_watchlist": auto_sync_watchlist,
            "owner_id": owner_id,
            "watchlist_id": watchlist_id,
            "recommendation_intake_limit": recommendation_intake_limit or limit,
        },
    }


def collection_group_for_task(task: DataSyncTaskPreview) -> str | tuple[str, ...]:
    """返回逻辑任务对应的底层采集分组。"""

    if task.market == "ashare":
        return {
            "universe_refresh": ("ashare-p0", "ashare-p1", "ashare-risk"),
            "realtime_quote_refresh": "ashare-p0",
            "market_bars_backfill": "ashare-p0",
            "fundamental_refresh": "ashare-p2",
            "capital_flow_refresh": "ashare-p1",
            "event_refresh": "ashare-p1",
            "risk_sentiment_refresh": "ashare-risk",
        }[task.task_type]
    return "crypto"


def build_ashare_collection_params(task: DataSyncTaskPreview) -> JsonDict:
    """生成 A 股采集入口参数。"""

    params: JsonDict = {}
    if task.task_type == "universe_refresh":
        params["symbol_source"] = "universe"
        params["index_catalog_limit"] = 0
        params["industry_catalog_limit"] = 0
        params["concept_catalog_limit"] = 0
        params["catalog_member_limit"] = 0
    if task.task_type == "market_bars_backfill":
        params["group"] = ["ashare-p0"]
        params["ashare_timeframe"] = timeframe_from_task_key(task.task_key, default="1d")
    if task.task_type == "realtime_quote_refresh":
        params["group"] = ["ashare-p0"]
        params["ashare_timeframe"] = "1d"
    if task.task_type == "fundamental_refresh":
        params["group"] = ["ashare-p2"]
    if task.task_type == "capital_flow_refresh":
        params["group"] = ["ashare-p1"]
    if task.task_type == "event_refresh":
        params["group"] = ["ashare-p1"]
    if task.task_type == "risk_sentiment_refresh":
        params["group"] = ["ashare-risk"]
    return params


def build_crypto_collection_params(task: DataSyncTaskPreview) -> JsonDict:
    """生成数字货币采集入口参数。"""

    market_type = "future" if task.market == "crypto_future" else "spot"
    params: JsonDict = {"crypto_market_type": market_type}
    if task.task_type == "universe_refresh":
        params["group"] = ["crypto"]
        params["symbol_source"] = "universe"
    if task.task_type == "market_bars_backfill":
        params["group"] = ["crypto"]
        params["crypto_timeframe"] = timeframe_from_task_key(task.task_key, default="1h")
    if task.task_type == "derivative_refresh":
        params["group"] = ["crypto"]
    if task.task_type == "realtime_quote_refresh":
        params["group"] = ["crypto"]
    return params


def timeframe_from_task_key(task_key: str, *, default: str) -> str:
    """从任务 key 末尾提取周期。"""

    parts = task_key.rsplit(".", maxsplit=1)
    return parts[-1] if len(parts) == 2 else default


def preview_ashare_tasks(config: MarketSyncConfig) -> list[DataSyncTaskPreview]:
    """生成 A 股任务预览。"""

    tasks: list[DataSyncTaskPreview] = []
    if config.universe_sources:
        tasks.append(
            DataSyncTaskPreview(
                task_key="ashare.universe.all",
                market="ashare",
                task_type="universe_refresh",
                title="刷新 A 股 Universe 来源",
                interval_seconds=config.interval_seconds.get("universe_refresh", 24 * 60 * 60),
                mode="full_universe_refresh",
                batch_size=config.batch_size,
                sources=config.universe_sources,
                data_packages=["assets", "asset_universes", "asset_universe_members"],
                notes=["自动拉取全 A、指数、行业、概念、热度和资金流种子，不手填股票。"],
            )
        )
    if "market_bars" in config.data_packages:
        for timeframe in config.timeframes or ["1d"]:
            tasks.append(
                DataSyncTaskPreview(
                    task_key=f"ashare.bars.{timeframe}",
                    market="ashare",
                    task_type="market_bars_backfill",
                    title=f"补采 A 股 {timeframe} K 线",
                    interval_seconds=config.interval_seconds.get("market_bars", 60 * 60),
                    mode="incremental_backfill",
                    batch_size=config.batch_size,
                    max_workers=config.max_workers,
                    lookback=f"{config.lookback_days}d",
                    sources=["market_bars"],
                    data_packages=["market_bars"],
                )
            )
    if "realtime_quotes" in config.data_packages:
        tasks.append(
            DataSyncTaskPreview(
                task_key="ashare.realtime_quotes",
                market="ashare",
                task_type="realtime_quote_refresh",
                title="刷新 A 股实时行情快照",
                interval_seconds=config.interval_seconds.get("realtime_quotes", 5 * 60),
                mode="incremental_snapshot",
                batch_size=config.batch_size,
                sources=["stock_zh_a_spot"],
                data_packages=["realtime_quotes"],
                notes=["复用 A 股实时行情资产接口刷新价格和交易状态快照。"],
            )
        )
    if "fundamentals" in config.data_packages or "valuation" in config.data_packages:
        tasks.append(
            DataSyncTaskPreview(
                task_key="ashare.fundamentals",
                market="ashare",
                task_type="fundamental_refresh",
                title="刷新 A 股基本面和估值",
                interval_seconds=config.interval_seconds.get("fundamentals", 12 * 60 * 60),
                mode="incremental_snapshot",
                batch_size=config.batch_size,
                max_workers=config.max_workers,
                lookback=f"{config.lookback_days}d",
                sources=["financial_indicators", "performance_report", "valuation"],
                data_packages=[
                    package
                    for package in ("fundamentals", "valuation")
                    if package in config.data_packages
                ],
            )
        )
    if "capital_flow" in config.data_packages:
        tasks.append(
            DataSyncTaskPreview(
                task_key="ashare.capital_flow",
                market="ashare",
                task_type="capital_flow_refresh",
                title="刷新 A 股资金流",
                interval_seconds=config.interval_seconds.get("capital_flow", 30 * 60),
                mode="incremental_snapshot",
                batch_size=config.batch_size,
                lookback=f"{config.lookback_days}d",
                sources=["individual_fund_flow_rank"],
                data_packages=["capital_flow"],
            )
        )
    if "events" in config.data_packages:
        tasks.append(
            DataSyncTaskPreview(
                task_key="ashare.events",
                market="ashare",
                task_type="event_refresh",
                title="刷新 A 股新闻和公告",
                interval_seconds=config.interval_seconds.get(
                    "events",
                    ASHARE_TIMELY_EVENT_INTERVAL_SECONDS,
                ),
                mode="incremental_event_sync",
                batch_size=config.batch_size,
                max_workers=config.max_workers,
                lookback=f"{config.lookback_days}d",
                sources=["stock_news", "notice_report"],
                data_packages=["events"],
            )
        )
    if "risk_sentiment" in config.data_packages:
        tasks.append(
            DataSyncTaskPreview(
                task_key="ashare.risk_sentiment",
                market="ashare",
                task_type="risk_sentiment_refresh",
                title="刷新 A 股风险和短线情绪",
                interval_seconds=config.interval_seconds.get(
                    "risk_sentiment",
                    ASHARE_TIMELY_EVENT_INTERVAL_SECONDS,
                ),
                mode="incremental_snapshot",
                batch_size=config.batch_size,
                lookback=f"{config.lookback_days}d",
                sources=["stop_list", "hot_rank", "zt_pool", "lhb", "block_trade", "margin"],
                data_packages=["risk_sentiment"],
            )
        )
    return tasks


def preview_crypto_tasks(market: str, config: MarketSyncConfig) -> list[DataSyncTaskPreview]:
    """生成数字货币任务预览。"""

    tasks: list[DataSyncTaskPreview] = []
    if config.universe_sources:
        tasks.append(
            DataSyncTaskPreview(
                task_key=f"{market}.universe.binance",
                market=market,
                task_type="universe_refresh",
                title=f"刷新 {crypto_market_label(market)} Universe",
                interval_seconds=config.interval_seconds.get("universe_refresh", 60 * 60),
                mode="full_universe_refresh",
                batch_size=config.batch_size,
                sources=config.universe_sources,
                data_packages=["assets", "asset_universes", "asset_universe_members"],
                notes=["通过 ccxt/Binance 自动拉取交易对，不手填币种。"],
            )
        )
    if "market_bars" in config.data_packages:
        for timeframe in config.timeframes or ["1h"]:
            tasks.append(
                DataSyncTaskPreview(
                    task_key=f"{market}.bars.{timeframe}",
                    market=market,
                    task_type="market_bars_backfill",
                    title=f"补采 {crypto_market_label(market)} {timeframe} K 线",
                    interval_seconds=config.interval_seconds.get("market_bars", 5 * 60),
                    mode="incremental_backfill",
                    batch_size=config.batch_size,
                    max_workers=config.max_workers,
                    lookback=f"{config.lookback_hours}h",
                    sources=["ccxt_fetch_ohlcv"],
                    data_packages=["market_bars"],
                )
            )
    if market == "crypto_future" and "derivatives" in config.data_packages:
        tasks.append(
            DataSyncTaskPreview(
                task_key="crypto_future.derivatives",
                market=market,
                task_type="derivative_refresh",
                title="刷新 Binance 合约衍生品快照",
                interval_seconds=config.interval_seconds.get("derivatives", 5 * 60),
                mode="incremental_snapshot",
                batch_size=config.batch_size,
                max_workers=config.max_workers,
                lookback=f"{config.lookback_hours}h",
                sources=["funding_rate", "open_interest", "long_short_ratio"],
                data_packages=["derivatives"],
            )
        )
    return tasks


def validate_market_config(
    *,
    market: str,
    market_config: MarketSyncConfig,
    errors: list[str],
    warnings: list[str],
) -> None:
    """校验单市场配置。"""

    allowed_sources = (
        ASHARE_UNIVERSE_SOURCES if market == "ashare" else CRYPTO_UNIVERSE_SOURCES
    )
    allowed_packages = ASHARE_DATA_PACKAGES if market == "ashare" else CRYPTO_DATA_PACKAGES
    unknown_sources = sorted(set(market_config.universe_sources) - set(allowed_sources))
    if unknown_sources:
        errors.append(f"{market} 包含不支持的 Universe 来源：{', '.join(unknown_sources)}")
    unknown_packages = sorted(set(market_config.data_packages) - set(allowed_packages))
    if unknown_packages:
        errors.append(f"{market} 包含不支持的数据采集包：{', '.join(unknown_packages)}")
    if not market_config.universe_sources:
        errors.append(f"{market} 至少需要一个 Universe 来源。")
    if not market_config.data_packages:
        errors.append(f"{market} 至少需要一个数据采集包。")
    if market_config.batch_size > 500:
        warnings.append(f"{market} batch_size 较大，可能触发上游限流。")
    if market_config.max_workers > 16:
        errors.append(f"{market} max_workers 不能超过 16，避免并发请求过高。")
    elif market_config.max_workers > 8:
        warnings.append(f"{market} max_workers 较大，可能触发上游限流。")
    if market == "ashare" and "all_ashare" not in market_config.universe_sources:
        warnings.append("A 股未启用全 A，推荐系统覆盖面会依赖其他种子池。")


def validate_data_sync_config_without_preview_recursion(
    config: DataSyncConfig,
    tasks: list[DataSyncTaskPreview],
) -> DataSyncValidationResult:
    """校验配置，避免 preview 中再次递归生成 preview。"""

    errors: list[str] = []
    warnings: list[str] = []
    if config.cache_backend not in {"auto", "redis", "null"}:
        errors.append("cache_backend 只能是 auto、redis 或 null。")
    if config.max_concurrent_jobs > 16:
        errors.append("max_concurrent_jobs 不能超过 16，避免本地采集线程过多。")
    enabled_market_count = 0
    for market, market_config in config.markets.items():
        if market not in MARKETS:
            errors.append(f"不支持的市场：{market}")
            continue
        if not market_config.enabled:
            continue
        enabled_market_count += 1
        validate_market_config(
            market=market,
            market_config=market_config,
            errors=errors,
            warnings=warnings,
        )
    if config.enabled and not tasks:
        errors.append("当前配置没有可执行的数据同步任务。")
    return DataSyncValidationResult(
        valid=not errors,
        errors=errors,
        warnings=warnings,
        enabled_market_count=enabled_market_count,
        task_count=len(tasks),
    )


def build_ashare_market_config(preset: str) -> MarketSyncConfig:
    """生成 A 股默认配置。"""

    if preset == "lightweight":
        sources = ["all_ashare", "hot_rank"]
        packages = ["market_bars", "capital_flow", "risk_sentiment", "data_quality"]
        batch_size = 100
    else:
        sources = list(ASHARE_UNIVERSE_SOURCES)
        packages = list(ASHARE_DATA_PACKAGES)
        batch_size = 200
    return MarketSyncConfig(
        enabled=True,
        universe_sources=sources,
        data_packages=packages,
        interval_seconds={
            "universe_refresh": 24 * 60 * 60,
            "market_bars": 60 * 60,
            "fundamentals": 12 * 60 * 60,
            "capital_flow": 30 * 60,
            "events": ASHARE_TIMELY_EVENT_INTERVAL_SECONDS,
            "risk_sentiment": ASHARE_TIMELY_EVENT_INTERVAL_SECONDS,
        },
        batch_size=batch_size,
        lookback_days=180,
        timeframes=["1d"],
        filters={
            "exclude_st": True,
            "exclude_suspended": True,
            "min_turnover": None,
        },
    )


def build_crypto_market_config(*, market: str, preset: str) -> MarketSyncConfig:
    """生成数字货币默认配置。"""

    packages = ["market_bars", "data_quality"]
    if market == "crypto_future":
        packages.append("derivatives")
    if preset == "lightweight":
        batch_size = 80
    else:
        batch_size = 150
    return MarketSyncConfig(
        enabled=True,
        universe_sources=["binance_usdt"],
        data_packages=packages,
        interval_seconds={
            "universe_refresh": 60 * 60,
            "market_bars": 5 * 60,
            "derivatives": 5 * 60,
        },
        batch_size=batch_size,
        lookback_hours=168,
        timeframes=["1h"],
        filters={
            "quote_asset": "USDT",
            "tradable_only": True,
            "min_quote_volume": 50_000_000,
        },
    )


def normalize_preset(preset: str) -> str:
    """规范化预设名称。"""

    normalized = preset.strip().lower()
    alias = {
        "comprehensive": "personal-comprehensive",
        "personal": "personal-comprehensive",
        "ashare": "ashare-comprehensive",
        "crypto": "crypto-comprehensive",
        "lite": "lightweight",
    }.get(normalized, normalized)
    if alias not in PRESETS:
        raise ValueError(f"未知数据同步预设：{preset}")
    return alias


def normalize_markets(markets: list[str]) -> list[str]:
    """规范化市场列表。"""

    result: list[str] = []
    for item in markets:
        for value in str(item).split(","):
            market = value.strip().lower()
            if not market:
                continue
            if market == "crypto":
                for crypto_market in ("crypto_spot", "crypto_future"):
                    if crypto_market not in result:
                        result.append(crypto_market)
                continue
            if market not in MARKETS:
                raise ValueError(f"不支持的市场：{market}")
            if market not in result:
                result.append(market)
    return result


def preset_markets(preset: str) -> list[str]:
    """返回预设默认市场。"""

    if preset == "ashare-comprehensive":
        return ["ashare"]
    if preset == "crypto-comprehensive":
        return ["crypto_spot", "crypto_future"]
    if preset == "lightweight":
        return ["ashare", "crypto_spot"]
    return ["ashare", "crypto_spot", "crypto_future"]


def preset_label(preset: str) -> str:
    """返回预设中文名称。"""

    return {
        "personal-comprehensive": "私人助手全面模式",
        "ashare-comprehensive": "A 股全面模式",
        "crypto-comprehensive": "数字货币全面模式",
        "lightweight": "轻量模式",
    }.get(preset, "自定义模式")


def crypto_market_label(market: str) -> str:
    """返回数字货币市场中文名称。"""

    return "Binance 合约" if market == "crypto_future" else "Binance 现货"


def parse_bool(value: object, *, default: bool) -> bool:
    """解析布尔配置。"""

    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def positive_int(value: object, *, default: int) -> int:
    """解析正整数配置。"""

    if value is None:
        return default
    result = int(value)
    if result <= 0:
        raise ValueError(f"配置值必须大于 0：{value}")
    return result


def string_list(value: object) -> list[str]:
    """解析字符串列表。"""

    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ValueError(f"配置值必须是字符串或数组：{value!r}")


def parse_interval_seconds(value: object) -> dict[str, int]:
    """解析 interval_seconds 配置。"""

    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("interval_seconds 必须是对象。")
    return {str(key): positive_int(item, default=1) for key, item in value.items()}
