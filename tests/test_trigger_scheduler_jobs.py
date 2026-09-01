"""触发引擎常驻唤醒调度任务配置测试。"""

from __future__ import annotations

from finance_agent.data.sync_config import (
    build_preset_config,
    build_trigger_scheduler_jobs,
    export_scheduler_payload,
)


def _jobs_by_name(jobs: list[dict]) -> dict[str, dict]:
    """按任务名索引调度任务。"""

    return {str(job["name"]): job for job in jobs}


def test_build_trigger_scheduler_jobs_exports_programmatic_evaluation_jobs() -> None:
    """默认计划只负责程序触发评估，由 Hermes Webhook 按事件唤醒模型。"""

    config = build_preset_config("personal-comprehensive")

    jobs = _jobs_by_name(build_trigger_scheduler_jobs(config))

    assert set(jobs) == {
        "analytics.triggers.evaluate.daily",
        "analytics.triggers.evaluate.intraday",
        "analytics.high_risk_reviews.sweep",
        "analytics.reviews.due",
    }

    daily = jobs["analytics.triggers.evaluate.daily"]
    assert daily["job_type"] == "trigger_evaluation"
    assert daily["group"] == "analytics"
    assert daily["enabled"] is True
    assert daily["schedule_type"] == "after_success"
    assert daily["depends_on"] == ["ashare.bars.1d.close_final"]
    assert daily["params"]["sync_task_type"] == "analytics.triggers.evaluate"
    assert daily["params"]["owner_id"] == "default-owner"
    assert daily["params"]["dispatch"] is True
    assert daily["params"]["agent_runtime"] == "hermes_agent"
    assert daily["params"]["max_events_per_run"] == 50
    assert daily["params"]["trigger_groups"] == [
        "position",
        "signal",
        "watchlist",
        "recommendation",
        "risk",
        "data_quality",
    ]

    intraday = jobs["analytics.triggers.evaluate.intraday"]
    assert intraday["job_type"] == "trigger_evaluation"
    assert intraday["enabled"] is True
    assert intraday["schedule_type"] == "interval"
    assert intraday["interval_seconds"] == 15 * 60
    assert intraday["trading_day_policy"] == "ashare"
    assert intraday["params"]["trigger_groups"] == ["intraday_volatility", "position"]
    assert intraday["params"]["intraday_sharp_drop_threshold"] == "-0.04"
    assert intraday["params"]["intraday_volume_surge_multiplier"] == "3"
    assert intraday["params"]["cooldown_minutes"] == 120

    high_risk_sweep = jobs["analytics.high_risk_reviews.sweep"]
    assert high_risk_sweep["job_type"] == "high_risk_reviews"
    assert high_risk_sweep["group"] == "analytics"
    assert high_risk_sweep["interval_seconds"] == 60 * 60
    assert high_risk_sweep["params"]["sync_task_type"] == "analytics.high_risk_reviews"
    assert high_risk_sweep["params"]["owner_id"] == "default-owner"
    assert high_risk_sweep["params"]["limit"] == 10

    reviews_due = jobs["analytics.reviews.due"]
    assert reviews_due["job_type"] == "reviews_due"
    assert reviews_due["group"] == "analytics"
    assert reviews_due["interval_seconds"] == 60 * 60
    assert reviews_due["params"]["sync_task_type"] == "analytics.reviews.due"
    assert reviews_due["params"]["owner_id"] == "default-owner"
    assert reviews_due["params"]["limit"] == 20


def test_scheduler_payload_includes_trigger_jobs_with_intraday_enabled() -> None:
    """导出的调度计划应默认启用盘中触发评估，形成盘中观察闭环。"""

    config = build_preset_config("personal-comprehensive")

    payload = export_scheduler_payload(config)
    jobs = _jobs_by_name(payload["jobs"])

    assert jobs["analytics.triggers.evaluate.daily"]["schedule_type"] == "after_success"
    assert jobs["analytics.triggers.evaluate.daily"]["depends_on"] == [
        "ashare.bars.1d.close_final"
    ]
    assert jobs["analytics.triggers.evaluate.intraday"]["enabled"] is True
    assert jobs["analytics.triggers.evaluate.intraday"]["schedule_type"] == "interval"
    assert jobs["analytics.high_risk_reviews.sweep"]["interval_seconds"] == 60 * 60
    assert jobs["analytics.reviews.due"]["job_type"] == "reviews_due"
