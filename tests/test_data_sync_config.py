from finance_agent.data.sync_config import (
    build_preset_config,
    export_scheduler_payload,
    parse_data_sync_config,
    preview_data_sync_config,
)


def test_timely_ashare_event_tasks_default_to_five_minutes() -> None:
    config = build_preset_config("personal-comprehensive")

    preview = preview_data_sync_config(config)
    preview_intervals = {
        task["task_key"]: task["interval_seconds"] for task in preview["tasks"]
    }

    assert preview_intervals["ashare.events"] == 5 * 60
    assert preview_intervals["ashare.risk_sentiment"] == 5 * 60


def test_scheduler_exports_timely_ashare_event_tasks_every_five_minutes() -> None:
    config = build_preset_config("personal-comprehensive")

    scheduler_payload = export_scheduler_payload(config)
    job_intervals = {
        job["name"]: job["interval_seconds"] for job in scheduler_payload["jobs"]
    }

    assert job_intervals["ashare.events"] == 5 * 60
    assert job_intervals["ashare.risk_sentiment"] == 5 * 60


def test_preview_exposes_cleaning_and_scheduled_analytics_processing_plan() -> None:
    config = build_preset_config("personal-comprehensive")

    preview = preview_data_sync_config(config)
    processing = preview["processing"]
    stage_keys = [stage["stage_key"] for stage in processing["stages"]]

    assert processing["normalization"]["execution"] == "inline_with_collection"
    assert processing["analytics"]["scheduler_status"] == "covered_by_analytics_jobs"
    assert processing["analytics"]["status"] == "active_scheduled"
    assert stage_keys == [
        "normalization.collection_payloads",
        "analytics.indicators",
        "analytics.factors",
        "analytics.screening",
        "analytics.scoring",
        "analytics.signals",
        "analytics.recommendations",
    ]


def test_scheduler_payload_registers_real_universe_recommendation_jobs() -> None:
    config = build_preset_config("personal-comprehensive")

    scheduler_payload = export_scheduler_payload(config)
    jobs = {job["name"]: job for job in scheduler_payload["jobs"]}
    processing = scheduler_payload["processing"]

    assert processing["analytics"]["scheduler_status"] == "covered_by_analytics_jobs"
    assert jobs["analytics.recommendations.ashare.all_a"]["job_type"] == "recommendation_pipeline"
    assert jobs["analytics.recommendations.ashare.all_a"]["params"]["universe_id"] == (
        "universe:merged:ashare:recommendation"
    )
    assert jobs["analytics.recommendations.ashare.all_a"]["params"]["min_bars"] == 60
    assert (
        jobs["analytics.recommendations.ashare.all_a"]["params"][
            "min_indicator_coverage_ratio"
        ]
        == 0.7
    )
    assert (
        jobs["analytics.recommendations.ashare.all_a"]["params"]["min_factor_coverage_ratio"]
        == 0.5
    )
    assert (
        jobs["analytics.recommendations.ashare.all_a"]["params"][
            "min_available_factor_groups"
        ]
        == 3
    )
    assert jobs["analytics.recommendations.ashare.all_a"]["params"]["candidate_source"] is None
    assert jobs["analytics.recommendations.ashare.all_a"]["params"]["avoid_universe_id"] == (
        "universe:avoid:ashare:system"
    )
    assert jobs["analytics.recommendations.ashare.all_a"]["params"]["strategy_id"] == (
        "strategy:ashare:short_swing"
    )
    assert jobs["analytics.recommendations.crypto_spot.binance"]["params"]["universe_id"] == (
        "universe:base:crypto:spot:binance"
    )
    assert jobs["analytics.recommendations.crypto_spot.binance"]["params"]["strategy_id"] == (
        "strategy:crypto:crypto_swing"
    )
    assert jobs["analytics.recommendations.crypto_spot.binance"]["params"]["min_bars"] == 120
    assert (
        jobs["analytics.recommendations.crypto_spot.binance"]["params"][
            "min_indicator_coverage_ratio"
        ]
        == 0.85
    )
    assert jobs["analytics.recommendations.crypto_future.binance"]["params"]["universe_id"] == (
        "universe:base:crypto:future:binance"
    )
    assert (
        jobs["analytics.recommendations.crypto_future.binance"]["params"][
            "min_available_factor_groups"
        ]
        == 3
    )


def test_scheduler_payload_uses_long_enough_bar_lookback_for_analytics() -> None:
    """默认补采窗口应覆盖 analytics 的最小 K 线数量，避免 technical 长期缺失。"""

    config = build_preset_config("personal-comprehensive")

    scheduler_payload = export_scheduler_payload(config)
    jobs = {job["name"]: job for job in scheduler_payload["jobs"]}

    assert jobs["ashare.bars.1d.bootstrap"]["params"]["lookback"] == "10y"
    assert "ashare_start" not in jobs["ashare.bars.1d.bootstrap"]["params"]
    assert jobs["ashare.bars.1d.bootstrap"]["params"]["schedule_failure_retry"] is False
    assert jobs["ashare.bars.1d.bootstrap"]["max_retries"] == 0
    assert jobs["ashare.bars.1d.close_final"]["params"]["lookback"] == "180d"
    assert jobs["ashare.bars.1d.revision"]["params"]["lookback"] == "7d"
    assert jobs["crypto_spot.bars.1h"]["params"]["lookback"] == "168h"
    assert jobs["crypto_future.bars.1h"]["params"]["lookback"] == "168h"


def test_scheduler_payload_documents_historical_bootstrap_boundaries() -> None:
    """历史初始化任务应按资产类型拆分，并在中文说明中表达清楚边界。"""

    config = build_preset_config("personal-comprehensive", markets=["ashare", "fund"])

    scheduler_payload = export_scheduler_payload(config)
    jobs = {job["name"]: job for job in scheduler_payload["jobs"]}

    ashare_bootstrap = jobs["ashare.bars.1d.bootstrap"]
    assert ashare_bootstrap["params"]["sync_task_type"] == "market_bars_full_history_backfill"
    assert ashare_bootstrap["params"]["lookback"] == "10y"
    assert "A 股主板" in ashare_bootstrap["params"]["title"]
    assert "主板股票" in "；".join(ashare_bootstrap["params"]["notes"])

    assert jobs["fund.etf.bars.1d.bootstrap"]["params"]["fund_asset_type"] == "etf"
    assert jobs["fund.etf.bars.1d.bootstrap"]["params"]["data_packages"] == ["market_bars"]
    assert jobs["fund.lof.bars.1d.bootstrap"]["params"]["fund_asset_type"] == "lof"
    assert "tencent:direct:kline" in jobs["fund.lof.bars.1d.bootstrap"]["params"]["sources"]
    assert jobs["fund.lof.bars.1d.bootstrap"]["params"]["data_packages"] == ["market_bars"]
    assert jobs["fund.open.nav.bootstrap"]["params"]["fund_asset_type"] == "open_fund"
    assert jobs["fund.open.nav.bootstrap"]["params"]["sync_task_type"] == (
        "fund_nav_full_history_backfill"
    )
    assert jobs["fund.open.nav.bootstrap"]["params"]["data_packages"] == ["fund_nav"]


def test_scheduler_payload_splits_ashare_daily_bar_lifecycle_jobs() -> None:
    """A 股日 K 应拆分为初始化、午盘、收盘和凌晨修正，而不是每小时全量扫。"""

    config = build_preset_config("personal-comprehensive")

    scheduler_payload = export_scheduler_payload(config)
    jobs = {job["name"]: job for job in scheduler_payload["jobs"]}

    assert "ashare.bars.1d" not in jobs
    assert jobs["ashare.bars.1d.bootstrap"]["schedule_type"] == "manual"
    assert jobs["ashare.bars.1d.bootstrap"]["enabled"] is False
    assert jobs["ashare.bars.1d.bootstrap"]["params"]["sync_task_type"] == (
        "market_bars_full_history_backfill"
    )
    assert jobs["ashare.bars.1d.midday_partial"]["schedule_type"] == "daily_time"
    assert jobs["ashare.bars.1d.midday_partial"]["run_at"] == ["11:45"]
    assert jobs["ashare.bars.1d.midday_partial"]["trading_day_policy"] == (
        "trading_day_only"
    )
    assert jobs["ashare.bars.1d.midday_partial"]["params"]["is_closed"] is False
    assert jobs["ashare.bars.1d.midday_partial"]["params"]["status"] == "partial"
    assert jobs["ashare.bars.1d.close_final"]["run_at"] == ["15:50", "17:30"]
    assert jobs["ashare.bars.1d.close_final"]["params"]["is_closed"] is True
    assert jobs["ashare.bars.1d.close_final"]["params"]["status"] == "available"
    assert jobs["ashare.bars.1d.revision"]["run_at"] == ["02:10"]
    assert jobs["ashare.bars.1d.revision"]["trading_day_policy"] == "any_day"
    assert jobs["ashare.bars.1d.revision"]["params"]["sync_task_type"] == (
        "market_bars_revision"
    )
    assert jobs["ashare.bars.1d.revision"]["params"]["lookback"] == "7d"


def test_scheduler_payload_marks_market_bar_batch_size_as_symbol_batch_size() -> None:
    """K 线补采导出的 batch_size 应表达单批标的数量，而不是资产总上限。"""

    config = build_preset_config("personal-comprehensive")

    scheduler_payload = export_scheduler_payload(config)
    jobs = {job["name"]: job for job in scheduler_payload["jobs"]}

    assert jobs["ashare.bars.1d.bootstrap"]["limit"] == 50
    assert jobs["ashare.bars.1d.bootstrap"]["params"]["batch_size"] == 50
    assert jobs["ashare.bars.1d.close_final"]["limit"] == 200
    assert jobs["ashare.bars.1d.close_final"]["params"]["batch_size"] == 200
    assert jobs["ashare.bars.1d.revision"]["limit"] == 200
    assert jobs["ashare.bars.1d.revision"]["params"]["batch_size"] == 200
    assert jobs["crypto_spot.bars.1h"]["limit"] == 150
    assert jobs["crypto_spot.bars.1h"]["params"]["batch_size"] == 150
    assert jobs["crypto_future.bars.1h"]["limit"] == 150
    assert jobs["crypto_future.bars.1h"]["params"]["batch_size"] == 150


def test_scheduler_payload_exports_low_symbol_fetch_concurrency() -> None:
    """按标的采集任务应默认使用低并发，避免上游请求被打满。"""

    config = build_preset_config("personal-comprehensive")

    scheduler_payload = export_scheduler_payload(config)
    jobs = {job["name"]: job for job in scheduler_payload["jobs"]}

    assert jobs["ashare.bars.1d.bootstrap"]["params"]["max_workers"] == 2
    assert jobs["ashare.bars.1d.close_final"]["params"]["max_workers"] == 4
    assert jobs["ashare.bars.1d.revision"]["params"]["max_workers"] == 4
    assert jobs["ashare.events"]["params"]["max_workers"] == 4
    assert jobs["ashare.fundamentals"]["params"]["max_workers"] == 4
    assert jobs["crypto_spot.bars.1h"]["params"]["max_workers"] == 4
    assert jobs["crypto_future.bars.1h"]["params"]["max_workers"] == 4


def test_scheduler_payload_exports_ashare_northbound_flow_job() -> None:
    """A 股资金流包应拆出北向资金任务，供因子侧消费外资偏好。"""

    config = build_preset_config("personal-comprehensive")

    scheduler_payload = export_scheduler_payload(config)
    jobs = {job["name"]: job for job in scheduler_payload["jobs"]}

    assert jobs["ashare.northbound"]["job_type"] == "collection"
    assert jobs["ashare.northbound"]["group"] == "ashare-p1"
    assert jobs["ashare.northbound"]["params"]["sync_task_type"] == "northbound_flow_refresh"
    assert jobs["ashare.northbound"]["params"]["group"] == ["ashare-p1"]
    assert jobs["ashare.northbound"]["params"]["source_limit"] == jobs["ashare.northbound"]["limit"]
    assert "stock_hsgt_hist_em" in jobs["ashare.northbound"]["params"]["sources"]
    assert "stock_hsgt_individual_em" in jobs["ashare.northbound"]["params"]["sources"]


def test_scheduler_payload_marks_ashare_fundamentals_as_incremental_resume() -> None:
    """基本面/估值定时任务应默认按水位增量补齐，避免每轮重复扫完整资产池。"""

    config = build_preset_config("personal-comprehensive")

    scheduler_payload = export_scheduler_payload(config)
    jobs = {job["name"]: job for job in scheduler_payload["jobs"]}

    assert jobs["ashare.fundamentals"]["params"]["only_failed_or_stale"] is True


def test_scheduler_payload_exports_source_rate_policies() -> None:
    """调度计划应携带数据源限频策略，便于采集进程按配置执行。"""

    config = build_preset_config("personal-comprehensive")

    scheduler_payload = export_scheduler_payload(config)
    policies = scheduler_payload["rate_policies"]

    assert policies["eastmoney_kline"]["max_concurrency"] == 1
    assert policies["eastmoney_kline"]["backoff"]["cooldown_seconds"] == 900
    assert policies["tencent_kline"]["max_concurrency"] == 2
    assert policies["tencent_kline"]["min_interval_seconds"] >= 0.5
    assert policies["stock_zh_a_hist_tx"]["max_concurrency"] == 2
    assert policies["stock_zh_a_hist_tx"]["min_interval_seconds"] >= 0.5
    assert policies["stock_news_em"]["min_interval_seconds"] >= 2.0
    assert policies["stock_zh_a_hist_tx"]["backoff"]["cooldown_seconds"] == 900


def test_parse_data_sync_config_backfills_default_source_rate_policies() -> None:
    """旧版运行时配置缺少 rate_policies 时，应自动补齐默认源级限频策略。"""

    config = build_preset_config("personal-comprehensive")
    payload = config.to_dict()
    payload.pop("rate_policies", None)

    parsed_config = parse_data_sync_config(payload)
    scheduler_payload = export_scheduler_payload(parsed_config)
    policies = scheduler_payload["rate_policies"]

    assert policies["eastmoney_kline"]["max_concurrency"] == 1
    assert policies["tencent_kline"]["max_concurrency"] == 2
    assert policies["stock_zh_a_hist_tx"]["max_concurrency"] == 2
    assert policies["stock_zh_a_hist_tx"]["backoff"]["cooldown_seconds"] == 900


def test_scheduler_payload_does_not_cap_universe_refresh_by_batch_size() -> None:
    """Universe 刷新应完整展开资产池，batch_size 只用于后续按标的分批。"""

    config = build_preset_config("personal-comprehensive")

    scheduler_payload = export_scheduler_payload(config)
    jobs = {job["name"]: job for job in scheduler_payload["jobs"]}
    ashare_params = jobs["ashare.universe.all"]["params"]

    assert ashare_params["index_catalog_limit"] == 0
    assert ashare_params["industry_catalog_limit"] == 0
    assert ashare_params["concept_catalog_limit"] == 0
    assert ashare_params["catalog_member_limit"] == 0
    assert "limit" not in jobs["ashare.realtime_quotes"]["params"]


def test_scheduler_payload_does_not_cap_list_sources_by_batch_size() -> None:
    """资金流、事件和风险情绪等列表来源默认不应被 batch_size 截断。"""

    config = build_preset_config("personal-comprehensive")

    scheduler_payload = export_scheduler_payload(config)
    jobs = {job["name"]: job for job in scheduler_payload["jobs"]}

    for job_name in [
        "ashare.capital_flow",
        "ashare.events",
        "ashare.risk_sentiment",
    ]:
        assert "source_limit" not in jobs[job_name]["params"]


def test_fund_market_preview_and_scheduler_jobs_are_exported() -> None:
    """基金市场启用后，应导出资产池、ETF/LOF 日 K 和开放式基金净值任务。"""

    config = build_preset_config("personal-comprehensive", markets=["fund"])

    preview = preview_data_sync_config(config)
    scheduler_payload = export_scheduler_payload(config)
    task_keys = {task["task_key"] for task in preview["tasks"]}
    jobs = {job["name"]: job for job in scheduler_payload["jobs"]}

    assert preview["enabled_markets"] == ["fund"]
    assert {
        "fund.universe.all",
        "fund.etf.bars.1d.bootstrap",
        "fund.lof.bars.1d.bootstrap",
        "fund.bars.1d.close_final",
        "fund.open.nav.bootstrap",
        "fund.open.nav.daily",
    }.issubset(task_keys)
    assert jobs["fund.etf.bars.1d.bootstrap"]["schedule_type"] == "manual"
    assert jobs["fund.etf.bars.1d.bootstrap"]["limit"] is None
    assert jobs["fund.etf.bars.1d.bootstrap"]["params"]["fund_asset_type"] == "etf"
    assert jobs["fund.lof.bars.1d.bootstrap"]["params"]["fund_asset_type"] == "lof"
    assert jobs["fund.lof.bars.1d.bootstrap"]["limit"] is None
    assert jobs["fund.open.nav.bootstrap"]["params"]["fund_asset_type"] == "open_fund"
    assert jobs["fund.open.nav.bootstrap"]["limit"] is None
    assert jobs["fund.open.nav.daily"]["params"]["sync_task_type"] == "fund_nav_daily"
    assert jobs["fund.open.nav.daily"]["limit"] is None


def test_scheduler_payload_limits_priority_stock_news_symbols() -> None:
    """逐股新闻应只覆盖重点标的，数量上限独立于列表型公告来源。"""

    config = build_preset_config("personal-comprehensive")

    scheduler_payload = export_scheduler_payload(config)
    jobs = {job["name"]: job for job in scheduler_payload["jobs"]}
    event_params = jobs["ashare.events"]["params"]

    assert event_params["priority_symbol_limit"] == 200
    assert "source_limit" not in event_params


def test_scheduler_payload_uses_timeout_long_enough_for_full_market_bar_batches() -> None:
    """默认调度超时应允许全市场 K 线在一次任务中按批跑完。"""

    config = build_preset_config("personal-comprehensive")

    scheduler_payload = export_scheduler_payload(config)

    assert scheduler_payload["job_timeout_seconds"] >= 6 * 60 * 60


def test_scheduler_payload_registers_data_quality_jobs() -> None:
    """启用 data_quality 包时，调度器应显式刷新数据质量快照表。"""

    config = build_preset_config("personal-comprehensive")

    scheduler_payload = export_scheduler_payload(config)
    jobs = {job["name"]: job for job in scheduler_payload["jobs"]}

    assert jobs["quality.ashare"]["job_type"] == "data_quality_refresh"
    assert jobs["quality.ashare"]["params"]["market"] == "ashare"
    assert jobs["quality.ashare"]["params"]["min_bars"] == 60
    assert "market_bars" in jobs["quality.ashare"]["params"]["data_domains"]
    assert jobs["quality.crypto_spot"]["job_type"] == "data_quality_refresh"
    assert jobs["quality.crypto_spot"]["params"]["min_bars"] == 120


def test_ashare_analytics_jobs_run_after_close_final_not_midday_partial() -> None:
    """A 股正式分析链路应由收盘最终日 K 触发，午盘 partial 只用于观察。"""

    config = build_preset_config("personal-comprehensive")

    scheduler_payload = export_scheduler_payload(config)
    jobs = {job["name"]: job for job in scheduler_payload["jobs"]}

    assert jobs["quality.ashare"]["schedule_type"] == "after_success"
    assert jobs["quality.ashare"]["depends_on"] == ["ashare.bars.1d.close_final"]
    assert jobs["analytics.technical_screening.ashare.main_board"]["schedule_type"] == (
        "after_success"
    )
    assert jobs["analytics.technical_screening.ashare.main_board"]["depends_on"] == [
        "ashare.bars.1d.close_final"
    ]
    ashare_recommendation = jobs["analytics.recommendations.ashare.all_a"]
    assert ashare_recommendation["schedule_type"] == "after_success"
    assert ashare_recommendation["depends_on"] == [
        "analytics.universe.merge.ashare.recommendation"
    ]
    assert not [
        job
        for job in jobs.values()
        if "ashare.bars.1d.midday_partial" in job.get("depends_on", [])
    ]


def test_scheduler_payload_registers_technical_screening_jobs() -> None:
    """技术初筛应作为收盘行情后的独立 analytics 任务进入调度计划。"""

    config = build_preset_config("personal-comprehensive")

    scheduler_payload = export_scheduler_payload(config)
    jobs = {job["name"]: job for job in scheduler_payload["jobs"]}
    ashare_job = jobs["analytics.technical_screening.ashare.main_board"]

    assert ashare_job["job_type"] == "technical_screening_refresh"
    assert ashare_job["group"] == "analytics"
    assert ashare_job["schedule_type"] == "after_success"
    assert ashare_job["depends_on"] == ["ashare.bars.1d.close_final"]
    assert ashare_job["params"]["market"] == "ashare"
    assert ashare_job["params"]["universe_id"] == "universe:technical:ashare:main_board"
    assert ashare_job["params"]["source_type"] == "technical_screening"
    assert ashare_job["params"]["min_bars"] == 250
    assert ashare_job["params"]["ttl_days"] == 3

    recommendation_job = jobs["analytics.recommendations.ashare.all_a"]
    assert recommendation_job["depends_on"] == [
        "analytics.universe.merge.ashare.recommendation"
    ]


def test_scheduler_payload_registers_universe_merge_and_avoid_pool_jobs() -> None:
    """推荐前置链应先合并候选池，并定期重建同市场回避池。"""

    config = build_preset_config("personal-comprehensive")

    scheduler_payload = export_scheduler_payload(config)
    jobs = {job["name"]: job for job in scheduler_payload["jobs"]}

    merge_job = jobs["analytics.universe.merge.ashare.recommendation"]
    assert merge_job["job_type"] == "universe_merge"
    assert merge_job["group"] == "analytics"
    assert merge_job["schedule_type"] == "after_success"
    assert merge_job["depends_on"] == ["analytics.technical_screening.ashare.main_board"]
    assert merge_job["params"]["target_universe_id"] == "universe:merged:ashare:recommendation"
    assert merge_job["params"]["source_universe_ids"] == [
        "universe:base:ashare:p0:all_a",
        "universe:technical:ashare:main_board",
    ]
    assert merge_job["params"]["source_weights"] == {
        "universe:base:ashare:p0:all_a": 1.0,
        "universe:technical:ashare:main_board": 2.0,
    }

    avoid_job = jobs["analytics.universe.rebuild_avoid_pool.ashare"]
    assert avoid_job["job_type"] == "universe_avoid_pool_rebuild"
    assert avoid_job["group"] == "analytics"
    assert avoid_job["schedule_type"] == "after_success"
    assert avoid_job["depends_on"] == ["ashare.risk_sentiment"]
    assert avoid_job["params"]["universe_id"] == "universe:avoid:ashare:system"
    assert avoid_job["params"]["market"] == "ashare"


def test_scheduler_payload_splits_stock_news_article_enrichment_task() -> None:
    """新闻正文二次抓取应是独立低并发任务，不阻塞新闻列表入库。"""

    config = build_preset_config("personal-comprehensive")

    scheduler_payload = export_scheduler_payload(config)
    jobs = {job["name"]: job for job in scheduler_payload["jobs"]}

    article_job = jobs["ashare.news_articles"]
    assert article_job["job_type"] == "collection"
    assert article_job["group"] == "ashare-p1"
    assert article_job["schedule_type"] == "after_success"
    assert article_job["depends_on"] == ["ashare.events"]
    assert article_job["params"]["sync_task_type"] == "event_article_enrichment"
    assert article_job["params"]["sources"] == ["stock_news_article"]
    assert article_job["params"]["max_workers"] == 1


def test_recommendation_jobs_enable_default_watchlist_intake() -> None:
    """推荐流水线默认应把非回避结果同步到系统研究跟踪池。"""

    config = build_preset_config("personal-comprehensive")

    scheduler_payload = export_scheduler_payload(config)
    jobs = {job["name"]: job for job in scheduler_payload["jobs"]}
    ashare_params = jobs["analytics.recommendations.ashare.all_a"]["params"]

    assert ashare_params["auto_sync_watchlist"] is True
    assert ashare_params["owner_id"] == "default-owner"
    assert ashare_params["watchlist_id"] == "watchlist:default-owner:ashare:research"
    assert ashare_params["recommendation_intake_limit"] == 20
