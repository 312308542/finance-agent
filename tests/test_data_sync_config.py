from finance_agent.data.sync_config import (
    build_preset_config,
    export_scheduler_payload,
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
        "universe:base:ashare:p0:all_a"
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
    assert jobs["analytics.recommendations.crypto_spot.binance"]["params"]["universe_id"] == (
        "universe:base:crypto:spot:binance"
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

    assert jobs["ashare.bars.1d"]["params"]["lookback"] == "180d"
    assert jobs["crypto_spot.bars.1h"]["params"]["lookback"] == "168h"
    assert jobs["crypto_future.bars.1h"]["params"]["lookback"] == "168h"


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


def test_recommendation_jobs_enable_default_watchlist_intake() -> None:
    """推荐流水线默认应把非回避结果同步到私人观察池。"""

    config = build_preset_config("personal-comprehensive")

    scheduler_payload = export_scheduler_payload(config)
    jobs = {job["name"]: job for job in scheduler_payload["jobs"]}
    ashare_params = jobs["analytics.recommendations.ashare.all_a"]["params"]

    assert ashare_params["auto_sync_watchlist"] is True
    assert ashare_params["owner_id"] == "default-owner"
    assert ashare_params["watchlist_id"] == "watchlist:default-owner:ashare:recommendations"
    assert ashare_params["recommendation_intake_limit"] == 20
