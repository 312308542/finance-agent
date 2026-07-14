import json
import logging
import sys
import threading
import time
from argparse import Namespace
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import SQLAlchemyError

from finance_agent.agents.personal_assistant import PersonalFinanceAgentService
from finance_agent.scheduler import (
    BaseDataScheduler,
    BaseDataSchedulerConfig,
    BaseDataSchedulerJob,
    load_scheduler_config,
    parse_scheduler_config,
)
from finance_agent.scheduler import base_data_scheduler as scheduler_module
from finance_agent.scheduler.base_data_scheduler import (
    collect_base_data_with_timeout,
    default_watchlist_name,
    import_collection_module,
    next_run_at_for_job,
    replace_file_with_retry,
    seconds_until_next_run,
)


def test_parse_scheduler_config_accepts_recommendation_pipeline_job() -> None:
    """调度配置应能表达采集之外的真实推荐流水线任务。"""

    config = parse_scheduler_config(
        {
            "enabled": True,
            "jobs": [
                {
                    "name": "analytics.recommendations.ashare.all_a",
                    "job_type": "recommendation_pipeline",
                    "group": "analytics",
                    "enabled": True,
                    "interval_seconds": 3600,
                    "limit": 20,
                    "market": "ashare",
                    "params": {
                        "universe_id": "universe:base:ashare:p0:all_a",
                        "strategy": "balanced_swing_v1",
                        "horizon": "swing",
                    },
                }
            ],
        }
    )

    assert config.jobs[0].job_type == "recommendation_pipeline"
    assert config.jobs[0].group == "analytics"


def test_recommendation_pipeline_job_kwargs_include_strategy_and_avoid_pool() -> None:
    """推荐调度任务应把评分策略和回避池 ID 透传给流水线。"""

    job = BaseDataSchedulerJob(
        name="analytics.recommendations.ashare.all_a",
        job_type="recommendation_pipeline",
        group="analytics",
        enabled=True,
        interval_seconds=3600,
        limit=20,
        market="ashare",
        params={
            "universe_id": "universe:merged:ashare:recommendation",
            "strategy": "balanced_swing_v1",
            "strategy_id": "strategy:ashare:short_swing",
            "avoid_universe_id": "universe:avoid:ashare:system",
            "horizon": "swing",
        },
    )
    scheduler = BaseDataScheduler(BaseDataSchedulerConfig(jobs=(job,)))

    kwargs = scheduler.build_recommendation_pipeline_kwargs(job)

    assert kwargs["strategy_id"] == "strategy:ashare:short_swing"
    assert kwargs["avoid_universe_id"] == "universe:avoid:ashare:system"


def test_parse_scheduler_config_accepts_data_quality_refresh_job() -> None:
    """调度配置应能表达数据质量快照刷新任务。"""

    config = parse_scheduler_config(
        {
            "enabled": True,
            "jobs": [
                {
                    "name": "quality.ashare",
                    "job_type": "data_quality_refresh",
                    "group": "analytics",
                    "enabled": True,
                    "interval_seconds": 3600,
                    "limit": 200,
                    "market": "ashare",
                    "params": {
                        "market": "ashare",
                        "timeframe": "1d",
                        "min_bars": 60,
                        "data_domains": ["market_bars", "indicator_frames"],
                    },
                }
            ],
        }
    )

    assert config.jobs[0].job_type == "data_quality_refresh"
    assert config.jobs[0].group == "analytics"


def test_parse_scheduler_config_accepts_technical_screening_job() -> None:
    """调度配置应能表达技术初筛刷新任务。"""

    config = parse_scheduler_config(
        {
            "enabled": True,
            "jobs": [
                {
                    "name": "analytics.technical_screening.ashare.main_board",
                    "job_type": "technical_screening_refresh",
                    "group": "analytics",
                    "enabled": True,
                    "schedule_type": "after_success",
                    "depends_on": ["ashare.bars.1d.close_final"],
                    "market": "ashare",
                    "limit": 200,
                    "params": {
                        "market": "ashare",
                        "universe_id": "universe:technical:ashare:main_board",
                        "min_bars": 250,
                        "ttl_days": 3,
                    },
                }
            ],
        }
    )

    assert config.jobs[0].job_type == "technical_screening_refresh"
    assert config.jobs[0].group == "analytics"
    assert config.jobs[0].depends_on == ("ashare.bars.1d.close_final",)


def test_parse_scheduler_config_accepts_trigger_and_agent_loop_jobs() -> None:
    """调度配置应能表达触发评估和 Agent 事件消费任务。"""

    config = parse_scheduler_config(
        {
            "enabled": True,
            "jobs": [
                {
                    "name": "analytics.triggers.evaluate.daily",
                    "job_type": "trigger_evaluation",
                    "group": "analytics",
                    "enabled": True,
                    "schedule_type": "after_success",
                    "depends_on": ["ashare.bars.1d.close_final"],
                    "params": {
                        "sync_task_type": "analytics.triggers.evaluate",
                        "owner_id": "default-owner",
                    },
                },
                {
                    "name": "agent.loop.consume.after_trigger",
                    "job_type": "agent_loop_consume",
                    "group": "agent",
                    "enabled": True,
                    "schedule_type": "after_success",
                    "depends_on": ["analytics.triggers.evaluate.daily"],
                    "params": {
                        "sync_task_type": "agent.loop.consume",
                        "owner_id": "default-owner",
                    },
                },
            ],
        }
    )

    assert config.jobs[0].job_type == "trigger_evaluation"
    assert config.jobs[0].group == "analytics"
    assert config.jobs[1].job_type == "agent_loop_consume"
    assert config.jobs[1].group == "agent"


def test_parse_scheduler_config_accepts_high_risk_reviews_job() -> None:
    """调度配置应能表达高风险复核批处理任务。"""

    config = parse_scheduler_config(
        {
            "enabled": True,
            "jobs": [
                {
                    "name": "analytics.high_risk_reviews.after_agent",
                    "job_type": "high_risk_reviews",
                    "group": "analytics",
                    "enabled": True,
                    "schedule_type": "after_success",
                    "depends_on": ["agent.loop.consume.after_trigger"],
                    "params": {
                        "sync_task_type": "analytics.high_risk_reviews",
                        "owner_id": "default-owner",
                        "limit": 10,
                    },
                }
            ],
        }
    )

    assert config.jobs[0].job_type == "high_risk_reviews"
    assert config.jobs[0].group == "analytics"
    assert config.jobs[0].depends_on == ("agent.loop.consume.after_trigger",)


def test_parse_scheduler_config_accepts_reviews_due_job() -> None:
    """调度配置应能表达到期执行复盘批处理任务。"""

    config = parse_scheduler_config(
        {
            "enabled": True,
            "jobs": [
                {
                    "name": "analytics.reviews.due",
                    "job_type": "reviews_due",
                    "group": "analytics",
                    "enabled": True,
                    "interval_seconds": 60 * 60,
                    "params": {
                        "sync_task_type": "analytics.reviews.due",
                        "owner_id": "default-owner",
                        "limit": 20,
                    },
                }
            ],
        }
    )

    assert config.jobs[0].job_type == "reviews_due"
    assert config.jobs[0].group == "analytics"


def test_parse_scheduler_config_accepts_backtest_run_job() -> None:
    """调度配置应能表达低频策略回测任务。"""

    config = parse_scheduler_config(
        {
            "enabled": True,
            "jobs": [
                {
                    "name": "analytics.backtest.weekly",
                    "job_type": "backtest_run",
                    "group": "analytics",
                    "enabled": True,
                    "interval_seconds": 7 * 24 * 60 * 60,
                    "market": "ashare",
                    "depends_on": ["analytics.recommendations.ashare.all_a"],
                    "params": {
                        "sync_task_type": "analytics.backtest.weekly",
                        "strategy": "factor_score_topn",
                        "universe_id": "universe:merged:ashare:recommendation",
                        "strategy_id": "strategy:ashare:short_swing",
                        "years": 5,
                        "score_mode": "replayed",
                    },
                }
            ],
        }
    )

    assert config.jobs[0].job_type == "backtest_run"
    assert config.jobs[0].group == "analytics"
    assert config.jobs[0].depends_on == ("analytics.recommendations.ashare.all_a",)


def test_parse_scheduler_config_accepts_universe_merge_and_avoid_pool_jobs() -> None:
    """调度配置应能表达候选池合并和回避池重建任务。"""

    config = parse_scheduler_config(
        {
            "enabled": True,
            "jobs": [
                {
                    "name": "analytics.universe.merge.ashare.recommendation",
                    "job_type": "universe_merge",
                    "group": "analytics",
                    "enabled": True,
                    "schedule_type": "after_success",
                    "depends_on": ["analytics.technical_screening.ashare.main_board"],
                    "market": "ashare",
                    "params": {
                        "sync_task_type": "analytics.universe.merge",
                        "target_universe_id": "universe:merged:ashare:recommendation",
                        "name": "A 股推荐合并候选池",
                        "source_universe_ids": [
                            "universe:base:ashare:p0:all_a",
                            "universe:technical:ashare:main_board",
                        ],
                    },
                },
                {
                    "name": "analytics.universe.rebuild_avoid_pool.ashare",
                    "job_type": "universe_avoid_pool_rebuild",
                    "group": "analytics",
                    "enabled": True,
                    "schedule_type": "after_success",
                    "depends_on": ["ashare.risk_sentiment"],
                    "market": "ashare",
                    "params": {
                        "sync_task_type": "analytics.universe.rebuild_avoid_pool",
                        "universe_id": "universe:avoid:ashare:system",
                        "name": "A 股系统回避池",
                        "market": "ashare",
                    },
                },
            ],
        }
    )

    merge_job, avoid_job = config.jobs
    assert merge_job.job_type == "universe_merge"
    assert merge_job.group == "analytics"
    assert merge_job.depends_on == ("analytics.technical_screening.ashare.main_board",)
    assert avoid_job.job_type == "universe_avoid_pool_rebuild"
    assert avoid_job.group == "analytics"
    assert avoid_job.depends_on == ("ashare.risk_sentiment",)


def test_parse_scheduler_config_accepts_calendar_schedule_fields() -> None:
    """调度配置应能表达固定时间、手动和依赖成功触发的任务。"""

    config = parse_scheduler_config(
        {
            "enabled": True,
            "jobs": [
                {
                    "name": "ashare.bars.1d.revision",
                    "job_type": "collection",
                    "group": "ashare-p0",
                    "enabled": True,
                    "schedule_type": "daily_time",
                    "run_at": ["02:10"],
                    "timezone": "Asia/Shanghai",
                    "trading_day_policy": "any_day",
                    "market": "ashare",
                    "params": {"sync_task_type": "market_bars_revision"},
                },
                {
                    "name": "ashare.bars.1d.bootstrap",
                    "job_type": "collection",
                    "group": "ashare-p0",
                    "enabled": False,
                    "schedule_type": "manual",
                    "market": "ashare",
                    "params": {"sync_task_type": "market_bars_full_history_backfill"},
                },
                {
                    "name": "quality.after.close",
                    "job_type": "data_quality_refresh",
                    "group": "analytics",
                    "enabled": True,
                    "schedule_type": "after_success",
                    "depends_on": ["ashare.bars.1d.close_final"],
                    "market": "ashare",
                    "params": {"market": "ashare"},
                },
            ],
        }
    )

    revision, bootstrap, quality = config.jobs
    assert revision.schedule_type == "daily_time"
    assert revision.run_at == ("02:10",)
    assert revision.timezone == "Asia/Shanghai"
    assert revision.trading_day_policy == "any_day"
    assert revision.interval_seconds == 24 * 60 * 60
    assert bootstrap.schedule_type == "manual"
    assert bootstrap.interval_seconds == 0
    assert quality.schedule_type == "after_success"
    assert quality.depends_on == ("ashare.bars.1d.close_final",)


def test_parse_scheduler_config_accepts_mutex_key() -> None:
    """调度配置应能声明任务互斥键，避免同一资源链路并发写入。"""

    config = parse_scheduler_config(
        {
            "enabled": True,
            "jobs": [
                {
                    "name": "ashare.events",
                    "job_type": "collection",
                    "group": "ashare-p1",
                    "enabled": True,
                    "interval_seconds": 300,
                    "market": "ashare",
                    "mutex_key": "ashare.event_records",
                    "params": {"sync_task_type": "event_refresh"},
                }
            ],
        }
    )

    assert config.jobs[0].mutex_key == "ashare.event_records"


def test_parse_scheduler_config_accepts_job_priority_and_resource_pool() -> None:
    """调度任务应能声明优先级和资源池，供 loop 排队时做资源隔离。"""

    config = parse_scheduler_config(
        {
            "enabled": True,
            "resource_pools": {
                "realtime": {"max_concurrent_jobs": 1, "description": "盘中轻任务"},
            },
            "jobs": [
                {
                    "name": "ashare.realtime_quotes",
                    "job_type": "collection",
                    "group": "ashare-p0",
                    "enabled": True,
                    "interval_seconds": 300,
                    "market": "ashare",
                    "priority": 800,
                    "resource_pool": "realtime",
                    "params": {"sync_task_type": "realtime_quote_refresh"},
                }
            ],
        }
    )

    assert config.resource_pools["realtime"]["max_concurrent_jobs"] == 1
    assert config.jobs[0].priority == 800
    assert config.jobs[0].resource_pool == "realtime"


def test_parse_scheduler_config_defaults_resource_pool_and_priority() -> None:
    """旧版任务配置缺少资源池字段时应落到 default 池和默认优先级。"""

    config = parse_scheduler_config(
        {
            "enabled": True,
            "jobs": [
                {
                    "name": "ashare.capital_flow",
                    "job_type": "collection",
                    "group": "ashare-p1",
                    "enabled": True,
                    "interval_seconds": 1800,
                    "market": "ashare",
                    "params": {"sync_task_type": "capital_flow_refresh"},
                }
            ],
        }
    )

    assert config.resource_pools["default"]["max_concurrent_jobs"] == config.max_concurrent_jobs
    assert config.jobs[0].priority == 100
    assert config.jobs[0].resource_pool == "default"


def test_scheduler_passes_rate_policies_to_collection_args() -> None:
    """调度器应把 top-level rate_policies 传给采集入口。"""

    config = parse_scheduler_config(
        {
            "enabled": True,
            "rate_policies": {
                "stock_zh_a_hist_tx": {
                    "max_concurrency": 1,
                    "min_interval_seconds": 1.0,
                }
            },
            "jobs": [
                {
                    "name": "ashare.bars.1d.close_final",
                    "job_type": "collection",
                    "group": "ashare-p0",
                    "enabled": True,
                    "interval_seconds": 3600,
                    "market": "ashare",
                    "params": {"sync_task_type": "market_bars_close_final"},
                }
            ],
        }
    )
    scheduler = BaseDataScheduler(
        config,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
    )

    args = scheduler.build_collection_args(config.jobs[0])

    assert args.rate_policies["stock_zh_a_hist_tx"]["min_interval_seconds"] == 1.0


def test_next_run_at_for_daily_time_uses_configured_timezone() -> None:
    """daily_time 应按配置时区计算下一次本地执行时间。"""

    job = BaseDataSchedulerJob(
        name="ashare.bars.1d.revision",
        group="ashare-p0",
        interval_seconds=24 * 60 * 60,
        schedule_type="daily_time",
        run_at=("02:10",),
        timezone="Asia/Shanghai",
    )

    next_run = next_run_at_for_job(
        job,
        now=datetime(2026, 6, 3, 17, 0, tzinfo=UTC),
    )

    assert next_run == datetime(2026, 6, 3, 18, 10, tzinfo=UTC)
    assert next_run_at_for_job(
        job,
        now=datetime(2026, 6, 3, 18, 20, tzinfo=UTC),
    ) == datetime(2026, 6, 4, 18, 10, tzinfo=UTC)


def test_next_run_at_for_trading_day_only_skips_weekend() -> None:
    """交易日任务遇到周末应顺延到下一个工作日。"""

    job = BaseDataSchedulerJob(
        name="ashare.bars.1d.close_final",
        group="ashare-p0",
        interval_seconds=24 * 60 * 60,
        schedule_type="daily_time",
        run_at=("15:50",),
        timezone="Asia/Shanghai",
        trading_day_policy="trading_day_only",
    )

    next_run = next_run_at_for_job(
        job,
        now=datetime(2026, 6, 5, 10, 0, tzinfo=UTC),
    )

    assert next_run == datetime(2026, 6, 8, 7, 50, tzinfo=UTC)


def test_next_run_at_for_manual_and_after_success_are_not_due_initially() -> None:
    """手动任务和依赖成功任务不应在 loop 启动时自动到期。"""

    now = datetime(2026, 6, 4, 1, 0, tzinfo=UTC)

    assert next_run_at_for_job(
        BaseDataSchedulerJob(
            name="manual",
            group="ashare-p0",
            interval_seconds=0,
            schedule_type="manual",
        ),
        now=now,
    ) is None
    assert next_run_at_for_job(
        BaseDataSchedulerJob(
            name="dependent",
            group="analytics",
            interval_seconds=0,
            job_type="data_quality_refresh",
            schedule_type="after_success",
            depends_on=("source",),
        ),
        now=now,
    ) is None


def test_scheduler_executes_recommendation_pipeline_job_without_collection() -> None:
    """推荐流水线任务应调用 analytics 执行器，而不是误走基础采集入口。"""

    calls: list[dict[str, Any]] = []

    def run_recommendation_pipeline(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "status": "available",
            "universe_id": kwargs["universe_id"],
            "recommendation_count": 3,
            "recommendation_run_id": "run:real",
        }

    def collect_base_data(_: Any) -> dict[str, Any]:
        raise AssertionError("推荐流水线任务不应调用基础采集入口")

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        jobs=(
            BaseDataSchedulerJob(
                name="analytics.recommendations.ashare.all_a",
                job_type="recommendation_pipeline",
                group="analytics",
                interval_seconds=3600,
                limit=20,
                market="ashare",
                params={
                    "universe_id": "universe:base:ashare:p0:all_a",
                    "strategy": "balanced_swing_v1",
                    "horizon": "swing",
                    "timeframe": "1d",
                    "min_bars": 60,
                    "min_indicator_coverage_ratio": 0.7,
                    "min_factor_coverage_ratio": 0.5,
                    "min_available_factor_groups": 3,
                },
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=collect_base_data,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        run_recommendation_pipeline_func=run_recommendation_pipeline,
    )

    result = scheduler.run_once()

    assert result["jobs"][0]["status"] == "executed"
    assert result["jobs"][0]["summary"]["recommendation_run_id"] == "run:real"
    assert calls == [
        {
            "universe_id": "universe:base:ashare:p0:all_a",
            "strategy": "balanced_swing_v1",
            "horizon": "swing",
            "timeframe": "1d",
            "limit": 20,
            "min_bars": 60,
            "min_available_factor_groups": 3,
            "min_indicator_coverage_ratio": 0.7,
            "min_factor_coverage_ratio": 0.5,
        }
    ]


def test_scheduler_runs_data_quality_refresh_without_collection() -> None:
    """数据质量任务应调用质量刷新执行器，而不是误走基础采集入口。"""

    calls: list[dict[str, Any]] = []

    def run_data_quality_refresh(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"status": "available", "snapshot_count": 2}

    def collect_base_data(_: Any) -> dict[str, Any]:
        raise AssertionError("数据质量任务不应调用基础采集入口")

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        jobs=(
            BaseDataSchedulerJob(
                name="quality.ashare",
                job_type="data_quality_refresh",
                group="analytics",
                interval_seconds=3600,
                limit=200,
                market="ashare",
                params={
                    "market": "ashare",
                    "timeframe": "1d",
                    "min_bars": 60,
                    "stale_after_seconds": 86400,
                    "data_domains": ["market_bars", "indicator_frames"],
                },
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=collect_base_data,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        run_data_quality_refresh_func=run_data_quality_refresh,
    )

    result = scheduler.run_once()

    assert result["jobs"][0]["status"] == "executed"
    assert result["jobs"][0]["summary"]["snapshot_count"] == 2
    assert calls == [
        {
            "market": "ashare",
            "timeframe": "1d",
            "limit": 200,
            "min_bars": 60,
            "stale_after_seconds": 86400,
            "data_domains": ["market_bars", "indicator_frames"],
        }
    ]


def test_scheduler_runs_technical_screening_without_collection() -> None:
    """技术初筛任务应调用 analytics 执行器，而不是误走基础采集入口。"""

    calls: list[dict[str, Any]] = []

    def run_technical_screening_refresh(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "status": "available",
            "screening_id": "screen:technical:ashare:main_board:20260609T073000Z",
            "accepted_count": 8,
        }

    def collect_base_data(_: Any) -> dict[str, Any]:
        raise AssertionError("技术初筛任务不应调用基础采集入口")

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        jobs=(
            BaseDataSchedulerJob(
                name="analytics.technical_screening.ashare.main_board",
                job_type="technical_screening_refresh",
                group="analytics",
                interval_seconds=0,
                schedule_type="after_success",
                depends_on=("ashare.bars.1d.close_final",),
                limit=200,
                market="ashare",
                params={
                    "market": "ashare",
                    "universe_id": "universe:technical:ashare:main_board",
                    "timeframe": "1d",
                    "min_bars": 250,
                    "ttl_days": 3,
                },
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=collect_base_data,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        run_technical_screening_refresh_func=run_technical_screening_refresh,
    )

    result = scheduler.run_once()

    assert result["jobs"][0]["status"] == "executed"
    assert result["jobs"][0]["summary"]["accepted_count"] == 8
    assert calls == [
        {
            "market": "ashare",
            "universe_id": "universe:technical:ashare:main_board",
            "timeframe": "1d",
            "limit": 200,
            "min_bars": 250,
            "ttl_days": 3,
        }
    ]


def test_scheduler_runs_trigger_evaluation_without_collection() -> None:
    """触发评估任务应调用触发执行器，而不是误走基础采集入口。"""

    calls: list[dict[str, Any]] = []

    def run_trigger_evaluation(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "status": "available",
            "created_count": 2,
            "dispatched_count": 2,
            "skipped_count": 1,
            "cooldown_count": 0,
        }

    def collect_base_data(_: Any) -> dict[str, Any]:
        raise AssertionError("触发评估任务不应调用基础采集入口")

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        jobs=(
            BaseDataSchedulerJob(
                name="analytics.triggers.evaluate.daily",
                job_type="trigger_evaluation",
                group="analytics",
                interval_seconds=0,
                schedule_type="after_success",
                depends_on=("ashare.bars.1d.close_final",),
                params={
                    "sync_task_type": "analytics.triggers.evaluate",
                    "owner_id": "default-owner",
                    "dispatch": True,
                    "max_events_per_run": 50,
                    "trigger_groups": [
                        "position",
                        "signal",
                        "watchlist",
                        "recommendation",
                        "risk",
                        "data_quality",
                    ],
                },
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=collect_base_data,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        run_trigger_evaluation_func=run_trigger_evaluation,
    )

    result = scheduler.run_once()

    assert result["jobs"][0]["status"] == "executed"
    assert result["jobs"][0]["summary"]["created_count"] == 2
    assert calls == [
        {
            "owner_id": "default-owner",
            "dispatch": True,
            "max_events_per_run": 50,
            "trigger_groups": [
                "position",
                "signal",
                "watchlist",
                "recommendation",
                "risk",
                "data_quality",
            ],
        }
    ]


def test_scheduler_runs_agent_loop_consume_without_collection() -> None:
    """Agent 事件消费任务应调用 Agent Loop 执行器，而不是误走基础采集入口。"""

    calls: list[dict[str, Any]] = []

    def run_agent_loop_consume(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "status": "available",
            "consumed": 3,
            "succeeded": 2,
            "failed": 1,
            "fallback_used": True,
        }

    def collect_base_data(_: Any) -> dict[str, Any]:
        raise AssertionError("Agent 事件消费任务不应调用基础采集入口")

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        jobs=(
            BaseDataSchedulerJob(
                name="agent.loop.consume.after_trigger",
                job_type="agent_loop_consume",
                group="agent",
                interval_seconds=0,
                schedule_type="after_success",
                depends_on=("analytics.triggers.evaluate.daily",),
                params={
                    "sync_task_type": "agent.loop.consume",
                    "owner_id": "default-owner",
                    "limit": 10,
                    "use_model_planner": "false",
                },
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=collect_base_data,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        run_agent_loop_consume_func=run_agent_loop_consume,
    )

    result = scheduler.run_once()

    assert result["jobs"][0]["status"] == "executed"
    assert result["jobs"][0]["summary"]["consumed"] == 3
    assert calls == [
        {
            "owner_id": "default-owner",
            "limit": 10,
            "use_model_planner": False,
        }
    ]


def test_scheduler_runs_high_risk_reviews_without_collection() -> None:
    """高风险复核任务应调用复核执行器，而不是误走基础采集入口。"""

    calls: list[dict[str, Any]] = []

    def run_high_risk_reviews(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "status": "available",
            "processed_count": 2,
            "approved_count": 1,
            "rejected_count": 1,
            "unavailable_count": 0,
        }

    def collect_base_data(_: Any) -> dict[str, Any]:
        raise AssertionError("高风险复核任务不应调用基础采集入口")

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        jobs=(
            BaseDataSchedulerJob(
                name="analytics.high_risk_reviews.after_agent",
                job_type="high_risk_reviews",
                group="analytics",
                interval_seconds=0,
                schedule_type="after_success",
                depends_on=("agent.loop.consume.after_trigger",),
                params={
                    "sync_task_type": "analytics.high_risk_reviews",
                    "owner_id": "default-owner",
                    "limit": 10,
                },
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=collect_base_data,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        run_high_risk_reviews_func=run_high_risk_reviews,
    )

    result = scheduler.run_once()

    assert result["jobs"][0]["status"] == "executed"
    assert result["jobs"][0]["summary"]["processed_count"] == 2
    assert calls == [
        {
            "owner_id": "default-owner",
            "limit": 10,
        }
    ]


def test_scheduler_runs_reviews_due_without_collection() -> None:
    """到期复盘任务应调用人工操作闭环复盘执行器，而不是误走基础采集入口。"""

    calls: list[dict[str, Any]] = []

    def run_reviews_due(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "status": "available",
            "processed_count": 2,
            "completed_count": 1,
            "partial_count": 1,
            "failed_count": 0,
        }

    def collect_base_data(_: Any) -> dict[str, Any]:
        raise AssertionError("到期复盘任务不应调用基础采集入口")

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        jobs=(
            BaseDataSchedulerJob(
                name="analytics.reviews.due",
                job_type="reviews_due",
                group="analytics",
                interval_seconds=60 * 60,
                params={
                    "sync_task_type": "analytics.reviews.due",
                    "owner_id": "default-owner",
                    "limit": 20,
                },
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=collect_base_data,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        run_reviews_due_func=run_reviews_due,
    )

    result = scheduler.run_once()

    assert result["jobs"][0]["status"] == "executed"
    assert result["jobs"][0]["summary"]["processed_count"] == 2
    assert calls == [
        {
            "owner_id": "default-owner",
            "limit": 20,
        }
    ]


def test_scheduler_runs_universe_merge_without_collection() -> None:
    """候选池合并任务应调用数据生产执行器，而不是误走基础采集入口。"""

    calls: list[dict[str, Any]] = []

    def run_universe_merge(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "status": "available",
            "target_universe_id": kwargs["target_universe_id"],
            "member_count": 88,
        }

    def collect_base_data(_: Any) -> dict[str, Any]:
        raise AssertionError("候选池合并任务不应调用基础采集入口")

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        jobs=(
            BaseDataSchedulerJob(
                name="analytics.universe.merge.ashare.recommendation",
                job_type="universe_merge",
                group="analytics",
                interval_seconds=0,
                schedule_type="after_success",
                depends_on=("analytics.technical_screening.ashare.main_board",),
                market="ashare",
                params={
                    "target_universe_id": "universe:merged:ashare:recommendation",
                    "name": "A 股推荐合并候选池",
                    "source_universe_ids": [
                        "universe:base:ashare:p0:all_a",
                        "universe:technical:ashare:main_board",
                    ],
                    "source_weights": {
                        "universe:base:ashare:p0:all_a": 1.0,
                        "universe:technical:ashare:main_board": 2.0,
                    },
                    "strategy_context": "recommendation_universe_merge",
                },
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=collect_base_data,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        run_universe_merge_func=run_universe_merge,
    )

    result = scheduler.run_once()

    assert result["jobs"][0]["status"] == "executed"
    assert result["jobs"][0]["summary"]["member_count"] == 88
    assert calls == [
        {
            "target_universe_id": "universe:merged:ashare:recommendation",
            "name": "A 股推荐合并候选池",
            "source_universe_ids": [
                "universe:base:ashare:p0:all_a",
                "universe:technical:ashare:main_board",
            ],
            "source_weights": {
                "universe:base:ashare:p0:all_a": 1.0,
                "universe:technical:ashare:main_board": 2.0,
            },
            "strategy_context": "recommendation_universe_merge",
        }
    ]


def test_scheduler_runs_backtest_without_collection() -> None:
    """回测任务应调用回测执行器，而不是误走基础采集入口。"""

    calls: list[dict[str, Any]] = []

    def run_backtest(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "status": "available",
            "backtest_id": "bt:ashare:short_swing:20260613",
            "metrics": {"cagr": 0.12},
        }

    def collect_base_data(_: Any) -> dict[str, Any]:
        raise AssertionError("回测任务不应调用基础采集入口")

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        jobs=(
            BaseDataSchedulerJob(
                name="analytics.backtest.weekly",
                job_type="backtest_run",
                group="analytics",
                interval_seconds=7 * 24 * 60 * 60,
                market="ashare",
                depends_on=("analytics.recommendations.ashare.all_a",),
                params={
                    "strategy": "factor_score_topn",
                    "universe_id": "universe:merged:ashare:recommendation",
                    "strategy_id": "strategy:ashare:short_swing",
                    "years": 5,
                    "score_mode": "replayed",
                    "topn": 20,
                    "rebalance": "once",
                    "timeframe": "1d",
                },
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=collect_base_data,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        run_backtest_func=run_backtest,
    )

    result = scheduler.run_once()

    assert result["jobs"][0]["status"] == "executed"
    assert result["jobs"][0]["summary"]["backtest_id"] == "bt:ashare:short_swing:20260613"
    assert calls == [
        {
            "market": "ashare",
            "strategy": "factor_score_topn",
            "universe_id": "universe:merged:ashare:recommendation",
            "strategy_id": "strategy:ashare:short_swing",
            "years": 5,
            "score_mode": "replayed",
            "topn": 20,
            "rebalance": "once",
            "timeframe": "1d",
        }
    ]


def test_scheduler_runs_avoid_pool_rebuild_without_collection() -> None:
    """回避池重建任务应调用数据生产执行器，而不是误走基础采集入口。"""

    calls: list[dict[str, Any]] = []

    def run_avoid_pool_rebuild(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "status": "available",
            "universe_id": kwargs["universe_id"],
            "member_count": 3,
        }

    def collect_base_data(_: Any) -> dict[str, Any]:
        raise AssertionError("回避池重建任务不应调用基础采集入口")

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        jobs=(
            BaseDataSchedulerJob(
                name="analytics.universe.rebuild_avoid_pool.ashare",
                job_type="universe_avoid_pool_rebuild",
                group="analytics",
                interval_seconds=0,
                schedule_type="after_success",
                depends_on=("ashare.risk_sentiment",),
                market="ashare",
                params={
                    "universe_id": "universe:avoid:ashare:system",
                    "name": "A 股系统回避池",
                    "market": "ashare",
                    "strategy_context": "avoid_pool",
                },
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=collect_base_data,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        run_avoid_pool_rebuild_func=run_avoid_pool_rebuild,
    )

    result = scheduler.run_once()

    assert result["jobs"][0]["status"] == "executed"
    assert result["jobs"][0]["summary"]["member_count"] == 3
    assert calls == [
        {
            "universe_id": "universe:avoid:ashare:system",
            "name": "A 股系统回避池",
            "market": "ashare",
            "strategy_context": "avoid_pool",
        }
    ]


def test_recommendation_job_passes_watchlist_intake_options() -> None:
    """推荐任务的研究跟踪池入池选项应透传给执行器。"""

    calls: list[dict[str, Any]] = []

    def run_recommendation_pipeline(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"status": "available", "recommendation_run_id": "run:real"}

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        jobs=(
            BaseDataSchedulerJob(
                name="analytics.recommendations.ashare.all_a",
                job_type="recommendation_pipeline",
                group="analytics",
                interval_seconds=3600,
                limit=20,
                market="ashare",
                params={
                    "universe_id": "universe:base:ashare:p0:all_a",
                    "auto_sync_watchlist": True,
                    "owner_id": "default-owner",
                    "watchlist_id": "watchlist:default-owner:ashare:research",
                    "recommendation_intake_limit": 20,
                },
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=lambda _args: {"status": "unexpected"},
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        run_recommendation_pipeline_func=run_recommendation_pipeline,
    )

    result = scheduler.run_once()

    assert result["jobs"][0]["status"] == "executed"
    assert calls == [
        {
            "universe_id": "universe:base:ashare:p0:all_a",
            "strategy": "balanced_swing_v1",
            "horizon": "swing",
            "limit": 20,
            "auto_sync_watchlist": True,
            "owner_id": "default-owner",
            "watchlist_id": "watchlist:default-owner:ashare:research",
            "recommendation_intake_limit": 20,
        }
    ]


def test_default_watchlist_name_uses_research_pool_wording() -> None:
    """调度器自动同步目标应展示为系统研究跟踪池，不再叫推荐观察池。"""

    assert default_watchlist_name("ashare") == "A 股系统研究跟踪池"


def test_recommendation_intake_records_system_research_semantics() -> None:
    """推荐同步入池应表达为系统研究跟踪，而不是用户观察确认。"""

    class FakeRecommendations:
        def __init__(self, recommendations: list[Namespace]) -> None:
            self.recommendations = recommendations

        def list_top_recommendations(self, *, run_id: str, limit: int) -> list[Namespace]:
            assert run_id == "run:research"
            assert limit == 3
            return self.recommendations[:limit]

    class FakeWatchlists:
        def __init__(self) -> None:
            self.items: list[dict[str, Any]] = []
            self.events: list[dict[str, Any]] = []

        def add_or_update_item(self, **kwargs: Any) -> Namespace:
            self.items.append(kwargs)
            return Namespace(
                watchlist_item_id=kwargs["watchlist_item_id"],
                status=kwargs["status"],
            )

        def record_event(self, **kwargs: Any) -> None:
            self.events.append(kwargs)

        def get_research_intake_cooldown(self, **kwargs: Any) -> None:
            return None

    class FakeMemory:
        def record_alert(self, **kwargs: Any) -> Namespace:
            return Namespace(alert_id=kwargs["alert_id"])

        def record_decision(self, **kwargs: Any) -> Namespace:
            return Namespace(decision_id=kwargs["decision_id"])

        def upsert_memory(self, **kwargs: Any) -> Namespace:
            return Namespace(memory_id=kwargs["memory_id"])

        def link_memory_edge(self, **kwargs: Any) -> None:
            return None

        def schedule_review(self, **kwargs: Any) -> Namespace:
            return Namespace(review_task_id=kwargs["review_task_id"])

    class FakeWorkflowAudit:
        def __init__(self) -> None:
            self.events: list[dict[str, Any]] = []

        def start_run(self, **kwargs: Any) -> None:
            return None

        def record_event(self, **kwargs: Any) -> None:
            self.events.append(kwargs)

        def finish_run(self, **kwargs: Any) -> None:
            return None

    def recommendation(asset_id: str, symbol: str, action: str, rank: int) -> Namespace:
        return Namespace(
            recommendation_id=f"asset_rec:{asset_id}",
            asset_id=asset_id,
            symbol=symbol,
            name=symbol,
            market="ashare",
            action=action,
            rank=rank,
            total_score=Decimal("88.120000"),
            confidence=Decimal("0.760000"),
            conviction="medium",
            summary=f"{symbol} 进入系统研究跟踪。",
            watch_conditions={"conditions": ["趋势保持"]},
            invalid_if={"conditions": ["信号转弱"]},
            score_id=f"score:{symbol}",
            factor_frame_id=f"factor:{symbol}",
            risk_ids=(),
            evidence_ids=(),
            signal_ids=(),
        )

    service = PersonalFinanceAgentService.__new__(PersonalFinanceAgentService)
    service.recommendations = FakeRecommendations(
        [
            recommendation("ashare:600519", "600519", "watch", 1),
            recommendation("ashare:000001", "000001", "avoid", 2),
            recommendation("ashare:600036", "600036", "reject", 3),
        ]
    )
    service.watchlists = FakeWatchlists()
    service.memory = FakeMemory()
    service.workflow_audit = FakeWorkflowAudit()
    as_of = datetime(2026, 6, 9, 10, 30, tzinfo=UTC)

    result = service.sync_recommendations_to_watchlist(
        owner_id="default-owner",
        recommendation_run_id="run:research",
        watchlist_id="watchlist:default-owner:ashare:research",
        as_of=as_of,
        limit=3,
        workflow_run_id="workflow:research",
    )

    assert result.watchlist_item_ids == (
        "watchlist_item:watchlist:default-owner:ashare:research:ashare:600519",
    )
    assert len(service.watchlists.items) == 1
    item_payload = service.watchlists.items[0]["payload"]
    expires_at = as_of + timedelta(days=3)
    assert service.watchlists.items[0]["next_review_at"] == expires_at
    assert item_payload["promotion_status"] == "system_research"
    assert item_payload["expires_at"] == expires_at.isoformat()
    assert item_payload["recommendation_run_id"] == "run:research"
    assert service.watchlists.events[0]["event_type"] == "research_intake"
    skipped_actions = {
        event["payload"]["action"]
        for event in service.workflow_audit.events
        if event["event_type"] == "recommendation_skipped"
    }
    assert skipped_actions == {"avoid", "reject"}


def test_recommendation_intake_applies_score_and_confidence_thresholds() -> None:
    """研究跟踪入池应支持分数和置信度阈值，低质量推荐不入池。"""

    class FakeRecommendations:
        def list_top_recommendations(self, *, run_id: str, limit: int) -> list[Namespace]:
            return [
                recommendation("ashare:600519", "600519", Decimal("88.00"), Decimal("0.80")),
                recommendation("ashare:000001", "000001", Decimal("59.00"), Decimal("0.90")),
                recommendation("ashare:600036", "600036", Decimal("90.00"), Decimal("0.30")),
            ]

    class FakeWatchlists:
        def __init__(self) -> None:
            self.items: list[dict[str, Any]] = []

        def add_or_update_item(self, **kwargs: Any) -> Namespace:
            self.items.append(kwargs)
            return Namespace(watchlist_item_id=kwargs["watchlist_item_id"], status=kwargs["status"])

        def record_event(self, **kwargs: Any) -> None:
            return None

        def get_research_intake_cooldown(self, **kwargs: Any) -> None:
            return None

    class FakeMemory:
        def record_alert(self, **kwargs: Any) -> Namespace:
            return Namespace(alert_id=kwargs["alert_id"])

        def record_decision(self, **kwargs: Any) -> Namespace:
            return Namespace(decision_id=kwargs["decision_id"])

        def upsert_memory(self, **kwargs: Any) -> Namespace:
            return Namespace(memory_id=kwargs["memory_id"])

        def link_memory_edge(self, **kwargs: Any) -> None:
            return None

        def schedule_review(self, **kwargs: Any) -> Namespace:
            return Namespace(review_task_id=kwargs["review_task_id"])

    class FakeWorkflowAudit:
        def __init__(self) -> None:
            self.events: list[dict[str, Any]] = []

        def start_run(self, **kwargs: Any) -> None:
            return None

        def record_event(self, **kwargs: Any) -> None:
            self.events.append(kwargs)

        def finish_run(self, **kwargs: Any) -> None:
            return None

    def recommendation(
        asset_id: str,
        symbol: str,
        total_score: Decimal,
        confidence: Decimal,
    ) -> Namespace:
        return Namespace(
            recommendation_id=f"asset_rec:{asset_id}",
            asset_id=asset_id,
            symbol=symbol,
            name=symbol,
            market="ashare",
            action="watch",
            rank=1,
            total_score=total_score,
            confidence=confidence,
            conviction="medium",
            summary=f"{symbol} 进入系统研究跟踪。",
            watch_conditions={},
            invalid_if={},
            score_id=None,
            factor_frame_id=None,
            risk_ids=(),
            evidence_ids=(),
            signal_ids=(),
        )

    service = PersonalFinanceAgentService.__new__(PersonalFinanceAgentService)
    service.recommendations = FakeRecommendations()
    service.watchlists = FakeWatchlists()
    service.memory = FakeMemory()
    service.workflow_audit = FakeWorkflowAudit()

    result = service.sync_recommendations_to_watchlist(
        owner_id="default-owner",
        recommendation_run_id="run:research",
        watchlist_id="watchlist:default-owner:ashare:research",
        as_of=datetime(2026, 6, 9, 10, 30, tzinfo=UTC),
        limit=3,
        workflow_run_id="workflow:research",
        min_total_score=Decimal("60"),
        min_confidence=Decimal("0.70"),
    )

    assert result.watchlist_item_ids == (
        "watchlist_item:watchlist:default-owner:ashare:research:ashare:600519",
    )
    skipped_reasons = [
        event["payload"]["reason"]
        for event in service.workflow_audit.events
        if event["event_type"] == "recommendation_skipped"
    ]
    assert skipped_reasons == ["below_min_total_score", "below_min_confidence"]


def test_recommendation_intake_skips_recently_removed_research_item() -> None:
    """研究池冷却期内的标的应跳过自动入池，并写入 recommendation_skipped 审计。"""

    class FakeRecommendations:
        def list_top_recommendations(self, *, run_id: str, limit: int) -> list[Namespace]:
            return [
                Namespace(
                    recommendation_id="asset_rec:ashare:600519",
                    asset_id="ashare:600519",
                    symbol="600519",
                    name="贵州茅台",
                    market="ashare",
                    action="watch",
                    rank=1,
                    total_score=Decimal("88.00"),
                    confidence=Decimal("0.80"),
                    conviction="medium",
                    summary="贵州茅台进入系统研究跟踪。",
                    watch_conditions={},
                    invalid_if={},
                    score_id=None,
                    factor_frame_id=None,
                    risk_ids=(),
                    evidence_ids=("evidence:600519",),
                    signal_ids=(),
                )
            ]

    class FakeWatchlists:
        def __init__(self) -> None:
            self.items: list[dict[str, Any]] = []

        def get_research_intake_cooldown(self, **kwargs: Any) -> dict[str, Any] | None:
            assert kwargs["watchlist_id"] == "watchlist:default-owner:ashare:research"
            assert kwargs["asset_id"] == "ashare:600519"
            assert kwargs["cooldown_days"] == 7
            return {
                "reason": "cooldown",
                "event_type": "research_removed",
                "last_exit_at": "2026-06-10T09:30:00+00:00",
                "cooldown_until": "2026-06-17T09:30:00+00:00",
                "cooldown_days": 7,
            }

        def add_or_update_item(self, **kwargs: Any) -> Namespace:
            self.items.append(kwargs)
            return Namespace(watchlist_item_id=kwargs["watchlist_item_id"], status=kwargs["status"])

        def record_event(self, **kwargs: Any) -> None:
            return None

    class FakeMemory:
        def record_alert(self, **kwargs: Any) -> Namespace:
            raise AssertionError("冷却期内不应创建提醒")

        def record_decision(self, **kwargs: Any) -> Namespace:
            raise AssertionError("冷却期内不应创建决策")

        def upsert_memory(self, **kwargs: Any) -> Namespace:
            raise AssertionError("冷却期内不应写入记忆")

        def link_memory_edge(self, **kwargs: Any) -> None:
            raise AssertionError("冷却期内不应写入记忆边")

        def schedule_review(self, **kwargs: Any) -> Namespace:
            raise AssertionError("冷却期内不应创建复盘任务")

    class FakeWorkflowAudit:
        def __init__(self) -> None:
            self.events: list[dict[str, Any]] = []

        def start_run(self, **kwargs: Any) -> None:
            return None

        def record_event(self, **kwargs: Any) -> None:
            self.events.append(kwargs)

        def finish_run(self, **kwargs: Any) -> None:
            return None

    service = PersonalFinanceAgentService.__new__(PersonalFinanceAgentService)
    service.recommendations = FakeRecommendations()
    service.watchlists = FakeWatchlists()
    service.memory = FakeMemory()
    service.workflow_audit = FakeWorkflowAudit()

    result = service.sync_recommendations_to_watchlist(
        owner_id="default-owner",
        recommendation_run_id="run:research",
        watchlist_id="watchlist:default-owner:ashare:research",
        as_of=datetime(2026, 6, 12, 9, 30, tzinfo=UTC),
        limit=1,
        workflow_run_id="workflow:research",
    )

    assert result.watchlist_item_ids == ()
    assert service.watchlists.items == []
    skipped_events = [
        event
        for event in service.workflow_audit.events
        if event["event_type"] == "recommendation_skipped"
    ]
    assert len(skipped_events) == 1
    assert skipped_events[0]["payload"]["reason"] == "cooldown"
    assert skipped_events[0]["payload"]["cooldown_until"] == "2026-06-17T09:30:00+00:00"


def test_scheduler_logs_job_progress_to_standard_logging(caplog) -> None:
    """调度任务应输出标准日志，方便 PyCharm 控制台直接观察执行进度。"""

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        jobs=(
            BaseDataSchedulerJob(
                name="ashare.test.job",
                group="ashare-p0",
                interval_seconds=3600,
                market="ashare",
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=lambda _args: {"status": "ok", "total_tasks": 1, "available": 1},
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
    )
    caplog.set_level(logging.INFO, logger="finance_agent.scheduler.base_data_scheduler")

    result = scheduler.run_once()

    messages = [record.getMessage() for record in caplog.records]
    assert result["jobs"][0]["status"] == "executed"
    assert any(
        "调度任务开始" in message and "job=ashare.test.job" in message
        for message in messages
    )
    assert any(
        "调度任务完成" in message and "job=ashare.test.job" in message
        for message in messages
    )


def test_scheduler_converts_ashare_market_bars_lookback_to_collection_dates() -> None:
    """A 股 K 线调度任务应把 lookback 转换成动态采集日期。"""

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        jobs=(
            BaseDataSchedulerJob(
                name="ashare.bars.1d",
                job_type="collection",
                group="ashare-p0",
                interval_seconds=3600,
                limit=200,
                market="ashare",
                params={
                    "sync_task_type": "market_bars_backfill",
                    "lookback": "30d",
                    "symbol_source": "market_assets",
                    "ashare_timeframe": "1d",
                },
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
    )

    args = scheduler.build_collection_args(config.jobs[0])

    assert args.ashare_end != "20260514"
    assert int(args.ashare_end) > int(args.ashare_start)
    start_date = datetime.strptime(args.ashare_start, "%Y%m%d").replace(tzinfo=UTC)
    end_date = datetime.strptime(args.ashare_end, "%Y%m%d").replace(tzinfo=UTC)
    assert (end_date - start_date).days == 30
    assert args.symbol_source == "market_assets"


def test_scheduler_sets_dynamic_end_for_ashare_full_history_without_lookback() -> None:
    """A 股全量历史 K 线不使用 lookback 时，结束日期应动态取当前日期。"""

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        jobs=(
            BaseDataSchedulerJob(
                name="ashare.bars.1d.bootstrap",
                job_type="collection",
                group="ashare-p0",
                interval_seconds=0,
                limit=200,
                market="ashare",
                params={
                    "sync_task_type": "market_bars_full_history_backfill",
                    "lookback": None,
                    "ashare_start": "19900101",
                    "symbol_source": "market_assets",
                    "ashare_timeframe": "1d",
                },
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
    )

    args = scheduler.build_collection_args(config.jobs[0])

    assert args.ashare_start == "19900101"
    assert args.ashare_end != "20260514"
    assert int(args.ashare_end) >= int(datetime.now(tz=UTC).strftime("%Y%m%d"))


def test_scheduler_converts_ashare_ten_year_bootstrap_lookback_to_collection_dates() -> None:
    """A 股 10 年日 K 初始化任务应动态换算采集日期，避免继续拉上市以来全量。"""

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        jobs=(
            BaseDataSchedulerJob(
                name="ashare.bars.1d.bootstrap",
                job_type="collection",
                group="ashare-p0",
                interval_seconds=0,
                limit=200,
                market="ashare",
                params={
                    "sync_task_type": "market_bars_full_history_backfill",
                    "lookback": "10y",
                    "symbol_source": "market_assets",
                    "ashare_timeframe": "1d",
                },
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
    )

    args = scheduler.build_collection_args(config.jobs[0])

    start_date = datetime.strptime(args.ashare_start, "%Y%m%d").replace(tzinfo=UTC)
    end_date = datetime.strptime(args.ashare_end, "%Y%m%d").replace(tzinfo=UTC)
    assert 3649 <= (end_date - start_date).days <= 3653
    assert args.symbol_source == "market_assets"


@pytest.mark.parametrize(
    ("job_name", "sync_task_type", "fund_asset_type"),
    (
        ("fund.etf.bars.1d.bootstrap", "market_bars_full_history_backfill", "etf"),
        ("fund.open.nav.bootstrap", "fund_nav_full_history_backfill", "open_fund"),
    ),
)
def test_scheduler_converts_fund_ten_year_bootstrap_lookback_to_collection_dates(
    job_name: str,
    sync_task_type: str,
    fund_asset_type: str,
) -> None:
    """基金 10 年初始化任务也应动态换算采集日期，避免落回脚本默认样例日期。"""

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        jobs=(
            BaseDataSchedulerJob(
                name=job_name,
                job_type="collection",
                group="fund",
                interval_seconds=0,
                limit=None,
                market="fund",
                params={
                    "sync_task_type": sync_task_type,
                    "lookback": "10y",
                    "symbol_source": "market_assets",
                    "fund_timeframe": "1d",
                    "fund_asset_type": fund_asset_type,
                },
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
    )

    today_before = datetime.now(tz=UTC).date()
    args = scheduler.build_collection_args(config.jobs[0])
    today_after = datetime.now(tz=UTC).date()

    start_date = datetime.strptime(args.ashare_start, "%Y%m%d").replace(tzinfo=UTC)
    end_date = datetime.strptime(args.ashare_end, "%Y%m%d").replace(tzinfo=UTC)
    assert 3649 <= (end_date - start_date).days <= 3653
    assert end_date.date() in {
        today_before - timedelta(days=1),
        today_after - timedelta(days=1),
    }
    assert args.symbol_source == "market_assets"


def test_scheduler_fund_full_history_jobs_clear_default_sample_limit() -> None:
    """基金全历史初始化必须覆盖采集脚本的 5 条样例上限。"""

    for name, sync_task_type in (
        ("fund.etf.bars.1d.bootstrap", "market_bars_full_history_backfill"),
        ("fund.open.nav.bootstrap", "fund_nav_full_history_backfill"),
    ):
        config = BaseDataSchedulerConfig(
            job_timeout_seconds=0,
            jobs=(
                BaseDataSchedulerJob(
                    name=name,
                    job_type="collection",
                    group="fund",
                    interval_seconds=0,
                    limit=None,
                    market="fund",
                    params={
                        "sync_task_type": sync_task_type,
                        "lookback": "10y",
                        "symbol_source": "market_assets",
                    },
                ),
            ),
        )

        def build_args(**kwargs: Any) -> Namespace:
            values = {"limit": 5}
            values.update(kwargs)
            return Namespace(**values)

        args = BaseDataScheduler(
            config,
            default_collection_args_func=build_args,
        ).build_collection_args(config.jobs[0])

        assert args.limit is None


def test_scheduler_converts_fund_nav_lookback_to_collection_dates() -> None:
    """基金净值初始化和日常维护也应把 lookback 转成筛选窗口，供断点续跑使用。"""

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        jobs=(
            BaseDataSchedulerJob(
                name="fund.open.nav.daily",
                job_type="collection",
                group="fund",
                interval_seconds=24 * 60 * 60,
                limit=None,
                market="fund",
                params={
                    "sync_task_type": "fund_nav_daily",
                    "lookback": "30d",
                    "symbol_source": "market_assets",
                    "fund_asset_type": "open_fund",
                    "only_failed_or_stale": True,
                },
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
    )

    args = scheduler.build_collection_args(config.jobs[0])

    start_date = datetime.strptime(args.ashare_start, "%Y%m%d").replace(tzinfo=UTC)
    end_date = datetime.strptime(args.ashare_end, "%Y%m%d").replace(tzinfo=UTC)
    assert (end_date - start_date).days == 30
    assert args.only_failed_or_stale is True


def test_scheduler_job_max_retries_overrides_global_retry_count() -> None:
    """单任务 max_retries=0 时，任务级失败只记录一次，不走全局自动重试。"""

    attempts = 0

    def failing_collect_base_data(_: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("bootstrap failed")

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        max_job_retries=2,
        jobs=(
            BaseDataSchedulerJob(
                name="ashare.bars.1d.bootstrap",
                group="ashare-p0",
                interval_seconds=0,
                market="ashare",
                schedule_type="manual",
                max_retries=0,
                params={"sync_task_type": "market_bars_full_history_backfill"},
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=failing_collect_base_data,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        sleep_func=lambda _: None,
    )

    result = scheduler.run_job(config.jobs[0])

    assert attempts == 1
    assert result["status"] == "failed"
    assert result["attempt_count"] == 1


def test_scheduler_uses_global_retry_count_when_job_max_retries_is_unset() -> None:
    """未设置单任务 max_retries 时，其他任务继续沿用全局重试逻辑。"""

    attempts = 0

    def failing_collect_base_data(_: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("normal job failed")

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        max_job_retries=2,
        jobs=(
            BaseDataSchedulerJob(
                name="ashare.bars.1d.close_final",
                group="ashare-p0",
                interval_seconds=3600,
                market="ashare",
                params={"sync_task_type": "market_bars_close_final"},
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=failing_collect_base_data,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        sleep_func=lambda _: None,
    )

    result = scheduler.run_job(config.jobs[0])

    assert attempts == 3
    assert result["status"] == "failed"
    assert result["attempt_count"] == 3


def test_collection_progress_uses_symbol_worker_concurrency() -> None:
    """任务监控里的并发数应展示按标的采集并发，而不是调度器任务槽位数。"""

    captured_progress: dict[str, Any] = {}

    class FakeProgressRecorder:
        cache_backend = "redis"

        def job_started(self, **kwargs: Any) -> str:
            captured_progress.update(kwargs)
            return "run-progress"

        def job_completed(self, **_: Any) -> None:
            return None

        def job_failed(self, **_: Any) -> None:
            return None

    config = BaseDataSchedulerConfig(
        cache_backend="null",
        max_concurrent_jobs=1,
        job_timeout_seconds=0,
        jobs=(
            BaseDataSchedulerJob(
                name="ashare.bars.1d.bootstrap",
                group="ashare-p0",
                interval_seconds=0,
                market="ashare",
                schedule_type="manual",
                params={
                    "sync_task_type": "market_bars_full_history_backfill",
                    "max_workers": 3,
                },
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=lambda args: {"status": "ok", "max_workers": args.max_workers},
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
    )
    scheduler._progress = FakeProgressRecorder()  # type: ignore[assignment]

    result = scheduler.run_job(config.jobs[0])

    assert result["status"] == "executed"
    assert captured_progress["max_workers"] == 3


def test_scheduler_loop_runs_due_jobs_concurrently() -> None:
    """loop 模式应把到期任务提交到后台线程，避免慢任务阻塞其它任务。"""

    started: list[str] = []
    release_slow_job = threading.Event()
    fast_job_finished = threading.Event()

    def collect_base_data(args: Namespace) -> dict[str, Any]:
        started.append(args.name)
        if args.name == "slow":
            release_slow_job.wait(timeout=2)
        if args.name == "fast":
            fast_job_finished.set()
            release_slow_job.set()
        return {"status": "ok", "name": args.name}

    def sleep(_: float) -> None:
        if fast_job_finished.wait(timeout=2):
            release_slow_job.set()

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        max_concurrent_jobs=2,
        loop_idle_seconds=1,
        jobs=(
            BaseDataSchedulerJob(
                name="slow",
                group="ashare-p0",
                interval_seconds=60,
                params={"name": "slow"},
            ),
            BaseDataSchedulerJob(
                name="fast",
                group="ashare-p1",
                interval_seconds=60,
                params={"name": "fast"},
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=collect_base_data,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        sleep_func=sleep,
    )

    started_at = time.perf_counter()
    result = scheduler.run_loop(max_cycles=1)

    assert result["cycles"] == 1
    assert {job["job"] for job in result["jobs"]} == {"slow", "fast"}
    assert set(started) == {"slow", "fast"}
    assert time.perf_counter() - started_at < 1.5


def test_scheduler_respects_resource_pool_limit() -> None:
    """同一资源池达到上限时应跳过队首同池任务，先执行其它可用资源池任务。"""

    started: list[str] = []
    overlaps: list[str] = []
    collection_running = threading.Event()
    realtime_started = threading.Event()

    def collect_base_data(args: Namespace) -> dict[str, Any]:
        started.append(args.name)
        if args.name == "collection.slow":
            collection_running.set()
            realtime_started.wait(timeout=1.5)
            collection_running.clear()
        elif args.name == "collection.second" and collection_running.is_set():
            overlaps.append(args.name)
        elif args.name == "realtime.quote":
            realtime_started.set()
        return {"status": "ok", "name": args.name}

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        max_concurrent_jobs=2,
        loop_idle_seconds=0.01,
        resource_pools={
            "collection_heavy": {"max_concurrent_jobs": 1},
            "realtime": {"max_concurrent_jobs": 1},
        },
        jobs=(
            BaseDataSchedulerJob(
                name="collection.slow",
                group="ashare-p0",
                interval_seconds=60,
                resource_pool="collection_heavy",
                params={"name": "collection.slow"},
            ),
            BaseDataSchedulerJob(
                name="collection.second",
                group="ashare-p1",
                interval_seconds=60,
                resource_pool="collection_heavy",
                params={"name": "collection.second"},
            ),
            BaseDataSchedulerJob(
                name="realtime.quote",
                group="ashare-p2",
                interval_seconds=60,
                resource_pool="realtime",
                params={"name": "realtime.quote"},
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=collect_base_data,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        sleep_func=lambda _: None,
    )

    result = scheduler.run_loop(max_cycles=1)

    assert result["cycles"] == 1
    assert started[:2] == ["collection.slow", "realtime.quote"]
    assert overlaps == []


def test_scheduler_keeps_global_concurrency_limit_across_resource_pools() -> None:
    """不同资源池的任务仍受全局任务并发限制。"""

    started: list[str] = []
    overlaps: list[str] = []
    running = 0
    lock = threading.Lock()
    first_started = threading.Event()
    release_first = threading.Event()

    def collect_base_data(args: Namespace) -> dict[str, Any]:
        nonlocal running
        with lock:
            running += 1
            if running > 1:
                overlaps.append(args.name)
        started.append(args.name)
        if args.name == "realtime.quote":
            first_started.set()
            release_first.wait(timeout=1.5)
        with lock:
            running -= 1
        return {"status": "ok", "name": args.name}

    def sleep(_: float) -> None:
        if first_started.wait(timeout=1.5):
            release_first.set()

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        max_concurrent_jobs=1,
        loop_idle_seconds=0.01,
        resource_pools={
            "realtime": {"max_concurrent_jobs": 1},
            "analytics": {"max_concurrent_jobs": 1},
        },
        jobs=(
            BaseDataSchedulerJob(
                name="realtime.quote",
                group="ashare-p0",
                interval_seconds=60,
                resource_pool="realtime",
                params={"name": "realtime.quote"},
            ),
            BaseDataSchedulerJob(
                name="analytics.trigger",
                group="ashare-p1",
                interval_seconds=60,
                resource_pool="analytics",
                params={"name": "analytics.trigger"},
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=collect_base_data,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        sleep_func=sleep,
    )

    result = scheduler.run_loop(max_cycles=1)

    assert result["cycles"] == 1
    assert started == ["realtime.quote", "analytics.trigger"]
    assert overlaps == []


def test_scheduler_queues_due_jobs_by_priority() -> None:
    """同一轮到期任务应按 priority 降序入队。"""

    started: list[str] = []

    def collect_base_data(args: Namespace) -> dict[str, Any]:
        started.append(args.name)
        return {"status": "ok", "name": args.name}

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        max_concurrent_jobs=1,
        loop_idle_seconds=0.01,
        jobs=(
            BaseDataSchedulerJob(
                name="low",
                group="ashare-p0",
                interval_seconds=60,
                priority=100,
                params={"name": "low"},
            ),
            BaseDataSchedulerJob(
                name="high",
                group="ashare-p1",
                interval_seconds=60,
                priority=800,
                params={"name": "high"},
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=collect_base_data,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        sleep_func=lambda _: None,
    )

    result = scheduler.run_loop(max_cycles=1)

    assert result["cycles"] == 1
    assert started == ["high", "low"]


def test_scheduler_preserves_config_order_when_priority_ties() -> None:
    """同优先级任务应保持配置顺序，避免同一轮调度顺序抖动。"""

    started: list[str] = []

    def collect_base_data(args: Namespace) -> dict[str, Any]:
        started.append(args.name)
        return {"status": "ok", "name": args.name}

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        max_concurrent_jobs=1,
        loop_idle_seconds=0.01,
        jobs=(
            BaseDataSchedulerJob(
                name="first",
                group="ashare-p0",
                interval_seconds=60,
                priority=500,
                params={"name": "first"},
            ),
            BaseDataSchedulerJob(
                name="second",
                group="ashare-p1",
                interval_seconds=60,
                priority=500,
                params={"name": "second"},
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=collect_base_data,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        sleep_func=lambda _: None,
    )

    result = scheduler.run_loop(max_cycles=1)

    assert result["cycles"] == 1
    assert started == ["first", "second"]


def test_scheduler_reloads_resource_pools_before_selecting_next_job(
    tmp_path: Path,
) -> None:
    """loop 运行中修改资源池配置后，下一轮选 job 应读取新额度。"""

    started: list[str] = []
    collection_running = threading.Event()
    release_collection = threading.Event()
    config_file = tmp_path / "scheduler.json"

    config_file.write_text(
        json.dumps(
                {
                    "enabled": True,
                    "cache_backend": "null",
                    "loop_idle_seconds": 1,
                    "max_job_retries": 0,
                    "max_concurrent_jobs": 1,
                    "resource_pools": {
                        "collection_heavy": {"max_concurrent_jobs": 1},
                        "realtime": {"max_concurrent_jobs": 1},
                    },
                    "jobs": [
                        {
                            "name": "collection.slow",
                            "job_type": "data_quality_refresh",
                            "group": "analytics",
                            "enabled": True,
                            "interval_seconds": 60,
                            "resource_pool": "collection_heavy",
                            "params": {
                                "horizon": "collection.slow",
                                "market": "ashare",
                                "data_domains": ["bars"],
                            },
                        },
                        {
                            "name": "collection.second",
                            "job_type": "data_quality_refresh",
                            "group": "analytics",
                            "enabled": True,
                            "interval_seconds": 60,
                            "resource_pool": "collection_heavy",
                            "params": {
                                "horizon": "collection.second",
                                "market": "ashare",
                                "data_domains": ["bars"],
                            },
                        },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def write_hot_config() -> None:
        payload = {
            "enabled": True,
            "cache_backend": "null",
            "loop_idle_seconds": 1,
            "max_job_retries": 0,
            "max_concurrent_jobs": 2,
            "resource_pools": {
                "collection_heavy": {"max_concurrent_jobs": 2},
                "realtime": {"max_concurrent_jobs": 1},
            },
            "jobs": [
                {
                    "name": "collection.slow",
                    "job_type": "data_quality_refresh",
                    "group": "analytics",
                    "enabled": True,
                    "interval_seconds": 60,
                    "resource_pool": "collection_heavy",
                    "params": {
                        "horizon": "collection.slow",
                        "market": "ashare",
                        "data_domains": ["bars"],
                    },
                },
                {
                    "name": "collection.second",
                    "job_type": "data_quality_refresh",
                    "group": "analytics",
                    "enabled": True,
                    "interval_seconds": 60,
                    "resource_pool": "collection_heavy",
                    "params": {
                        "horizon": "collection.second",
                        "market": "ashare",
                        "data_domains": ["bars"],
                    },
                },
            ],
        }
        temp_file = config_file.with_suffix(".tmp")
        temp_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temp_file.replace(config_file)

    def run_data_quality_refresh(**kwargs: Any) -> dict[str, Any]:
        job_name = kwargs.get("horizon")
        started.append(str(job_name))
        if job_name == "collection.slow":
            collection_running.set()
            write_hot_config()
            release_collection.wait(timeout=1.5)
            collection_running.clear()
        elif job_name == "collection.second":
            assert collection_running.is_set()
            release_collection.set()
        return {"status": "ok", "name": job_name}

    config = replace(load_scheduler_config(config_file), job_timeout_seconds=0)
    scheduler = BaseDataScheduler(
        config,
        run_data_quality_refresh_func=run_data_quality_refresh,
        scheduler_config_file=config_file,
        sleep_func=lambda _: None,
    )

    result = scheduler.run_loop(max_cycles=1)

    assert result["cycles"] == 1
    assert started[:2] == ["collection.slow", "collection.second"]


def test_scheduler_loop_runs_market_universe_before_asset_dependents() -> None:
    """同一市场的资产池刷新应先于依赖资产池的采集任务执行，避免并发抢写资产主表。"""

    started: list[str] = []

    def collect_base_data(args: Namespace) -> dict[str, Any]:
        started.append(args.name)
        return {"status": "ok", "name": args.name}

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        max_concurrent_jobs=2,
        loop_idle_seconds=0.01,
        jobs=(
            BaseDataSchedulerJob(
                name="ashare.universe.all",
                group="ashare-p0",
                interval_seconds=3600,
                market="ashare",
                params={
                    "name": "ashare.universe.all",
                    "sync_task_type": "universe_refresh",
                },
            ),
            BaseDataSchedulerJob(
                name="ashare.fundamentals",
                group="ashare-p2",
                interval_seconds=3600,
                market="ashare",
                params={
                    "name": "ashare.fundamentals",
                    "sync_task_type": "fundamental_refresh",
                },
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=collect_base_data,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        sleep_func=lambda _: None,
    )

    result = scheduler.run_loop(max_cycles=1)

    assert result["cycles"] == 1
    assert started == ["ashare.universe.all"]


def test_scheduler_loop_defers_same_mutex_key_jobs_until_active_job_finishes() -> None:
    """同一互斥键的任务不能在已有运行任务时再次入队，避免新闻链路抢写 event_records。"""

    started: list[str] = []
    overlaps: list[str] = []
    news_started = threading.Event()
    release_news = threading.Event()
    news_running = threading.Event()

    def collect_base_data(args: Namespace) -> dict[str, Any]:
        started.append(args.name)
        if args.name == "ashare.events" and news_running.is_set():
            overlaps.append(args.name)
        if args.name == "ashare.news_articles":
            news_running.set()
            news_started.set()
            release_news.wait(timeout=1.5)
            news_running.clear()
        return {"status": "ok", "name": args.name}

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        max_concurrent_jobs=2,
        loop_idle_seconds=0.05,
        jobs=(
            BaseDataSchedulerJob(
                name="ashare.events",
                group="ashare-p1",
                interval_seconds=1,
                market="ashare",
                mutex_key="ashare.event_records",
                params={
                    "name": "ashare.events",
                    "sync_task_type": "event_refresh",
                },
            ),
            BaseDataSchedulerJob(
                name="ashare.news_articles",
                group="ashare-p1",
                interval_seconds=0,
                market="ashare",
                schedule_type="after_success",
                depends_on=("ashare.events",),
                mutex_key="ashare.event_records",
                params={
                    "name": "ashare.news_articles",
                    "sync_task_type": "event_article_enrichment",
                },
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=collect_base_data,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        sleep_func=lambda _: None,
    )

    try:
        result = scheduler.run_loop(max_cycles=3)
    finally:
        release_news.set()

    assert result["cycles"] == 3
    assert started.count("ashare.events") == 2
    assert started.count("ashare.news_articles") == 1
    assert overlaps == []


def test_scheduler_loop_records_failed_state_on_unexpected_error() -> None:
    """loop 主循环出现未预期异常时应写入失败结果，而不是留下陈旧 running 状态。"""

    def sleep(_: float) -> None:
        raise RuntimeError("loop sleep failed")

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        loop_idle_seconds=1,
        jobs=(
            BaseDataSchedulerJob(
                name="manual.only",
                group="ashare-p0",
                interval_seconds=0,
                schedule_type="manual",
            ),
        ),
    )
    scheduler = BaseDataScheduler(config, sleep_func=sleep)

    result = scheduler.run_loop(max_cycles=1)

    assert result["failed"] is True
    assert result["error_message"] == "loop sleep failed"


def test_scheduler_loop_triggers_after_success_dependents() -> None:
    """上游任务成功后，after_success 依赖任务应进入下一轮调度。"""

    started: list[str] = []

    def collect_base_data(args: Namespace) -> dict[str, Any]:
        started.append(args.name)
        return {"status": "ok", "name": args.name}

    def run_data_quality_refresh(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["market"] == "ashare"
        started.append("quality.ashare.after_close")
        return {"status": "available", "snapshot_count": 1}

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        max_concurrent_jobs=1,
        loop_idle_seconds=0.01,
        jobs=(
            BaseDataSchedulerJob(
                name="ashare.bars.1d.close_final",
                group="ashare-p0",
                interval_seconds=3600,
                market="ashare",
                params={
                    "name": "ashare.bars.1d.close_final",
                    "sync_task_type": "market_bars_close_final",
                },
            ),
            BaseDataSchedulerJob(
                name="quality.ashare.after_close",
                job_type="data_quality_refresh",
                group="analytics",
                interval_seconds=0,
                schedule_type="after_success",
                depends_on=("ashare.bars.1d.close_final",),
                market="ashare",
                params={"name": "quality.ashare.after_close", "market": "ashare"},
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=collect_base_data,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        run_data_quality_refresh_func=run_data_quality_refresh,
        sleep_func=lambda _: None,
    )

    result = scheduler.run_loop(max_cycles=2)

    assert result["cycles"] == 2
    assert started == ["ashare.bars.1d.close_final", "quality.ashare.after_close"]


def test_scheduler_loop_triggers_chained_after_success_dependents() -> None:
    """after_success 应支持收盘 K 线、质量刷新、推荐流水线的串联触发。"""

    started: list[str] = []

    def collect_base_data(args: Namespace) -> dict[str, Any]:
        started.append(args.name)
        return {"status": "ok", "name": args.name}

    def run_data_quality_refresh(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["market"] == "ashare"
        started.append("quality.ashare")
        return {"status": "available", "snapshot_count": 1}

    def run_recommendation_pipeline(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["universe_id"] == "universe:base:ashare:p0:all_a"
        started.append("analytics.recommendations.ashare.all_a")
        return {"status": "available", "recommendation_count": 3}

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        max_concurrent_jobs=1,
        loop_idle_seconds=0.01,
        jobs=(
            BaseDataSchedulerJob(
                name="ashare.bars.1d.close_final",
                group="ashare-p0",
                interval_seconds=3600,
                market="ashare",
                params={
                    "name": "ashare.bars.1d.close_final",
                    "sync_task_type": "market_bars_close_final",
                },
            ),
            BaseDataSchedulerJob(
                name="quality.ashare",
                job_type="data_quality_refresh",
                group="analytics",
                interval_seconds=0,
                schedule_type="after_success",
                depends_on=("ashare.bars.1d.close_final",),
                market="ashare",
                params={"name": "quality.ashare", "market": "ashare"},
            ),
            BaseDataSchedulerJob(
                name="analytics.recommendations.ashare.all_a",
                job_type="recommendation_pipeline",
                group="analytics",
                interval_seconds=0,
                schedule_type="after_success",
                depends_on=("quality.ashare",),
                market="ashare",
                params={
                    "name": "analytics.recommendations.ashare.all_a",
                    "universe_id": "universe:base:ashare:p0:all_a",
                },
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=collect_base_data,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        run_data_quality_refresh_func=run_data_quality_refresh,
        run_recommendation_pipeline_func=run_recommendation_pipeline,
        sleep_func=lambda _: None,
    )

    result = scheduler.run_loop(max_cycles=3)

    assert result["cycles"] == 3
    assert started == [
        "ashare.bars.1d.close_final",
        "quality.ashare",
        "analytics.recommendations.ashare.all_a",
    ]


def test_status_writes_from_multiple_scheduler_instances_do_not_share_temp_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多个调度器实例并发写同一状态文件时，不应抢同一个临时文件。"""

    status_file = tmp_path / "status.json"
    first_temp_written = threading.Event()
    allow_first_replace = threading.Event()
    original_write_text = Path.write_text

    def delayed_first_temp_write(path: Path, data: str, *args: Any, **kwargs: Any) -> int:
        result = original_write_text(path, data, *args, **kwargs)
        if path.name.startswith("status.json.") and path.name.endswith(".tmp") and "first" in data:
            first_temp_written.set()
            assert allow_first_replace.wait(timeout=2)
        return result

    monkeypatch.setattr(Path, "write_text", delayed_first_temp_write)

    config = BaseDataSchedulerConfig(job_timeout_seconds=0)
    first = BaseDataScheduler(config, status_file=status_file)
    second = BaseDataScheduler(config, status_file=status_file)
    errors: list[BaseException] = []

    def write_first() -> None:
        try:
            first.write_status(state="running", writer="first")
        except BaseException as exc:  # pragma: no cover - 失败时由断言展示
            errors.append(exc)

    thread = threading.Thread(target=write_first)
    thread.start()
    assert first_temp_written.wait(timeout=2)

    second.write_status(state="running", writer="second")
    allow_first_replace.set()
    thread.join(timeout=2)

    assert not errors
    assert status_file.exists()


def test_status_replace_retries_transient_os_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """状态文件替换遇到短暂占用时应重试，而不是立即让调度任务失败。"""

    source = tmp_path / "status.tmp"
    target = tmp_path / "status.json"
    source.write_text("{}", encoding="utf-8")
    attempts = 0
    sleeps: list[float] = []
    original_replace = Path.replace

    def flaky_replace(path: Path, target_path: Path) -> Path:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("文件被暂时占用")
        return original_replace(path, target_path)

    monkeypatch.setattr(Path, "replace", flaky_replace)

    replace_file_with_retry(source, target, sleep_func=sleeps.append, max_attempts=2)

    assert attempts == 2
    assert sleeps
    assert target.read_text(encoding="utf-8") == "{}"


def test_seconds_until_next_run_handles_empty_waiting_states() -> None:
    """所有任务都在运行或排队时，调度循环等待应退回 idle 秒数而不是崩溃。"""

    assert seconds_until_next_run([], idle_seconds=5) == 5.0


def test_collect_base_data_with_timeout_sets_spawn_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    """采集 payload 子进程应固定到项目 venv，避免 Windows spawn 跑到其他 Python。"""

    configured_executables: list[str] = []
    project_python = Path.cwd() / ".venv" / "Scripts" / "python.exe"

    class FakeQueue:
        def __init__(self, maxsize: int) -> None:
            self.maxsize = maxsize

        def get_nowait(self) -> dict[str, Any]:
            return {"ok": True, "result": {"status": "ok"}}

    class FakeProcess:
        exitcode = 0

        def __init__(self, *, target: Any, args: tuple[Any, ...]) -> None:
            self.target = target
            self.args = args

        def start(self) -> None:
            return None

        def is_alive(self) -> bool:
            return False

        def join(self, timeout: float | None = None) -> None:
            return None

    class FakeContext:
        def Queue(self, maxsize: int) -> FakeQueue:  # noqa: N802 - 模拟 multiprocessing API
            return FakeQueue(maxsize=maxsize)

        def Process(self, *, target: Any, args: tuple[Any, ...]) -> FakeProcess:  # noqa: N802
            return FakeProcess(target=target, args=args)

    monkeypatch.setattr(scheduler_module.multiprocessing, "get_context", lambda method: FakeContext())
    monkeypatch.setattr(
        scheduler_module.multiprocessing,
        "set_executable",
        lambda executable: configured_executables.append(str(executable)),
    )
    monkeypatch.setattr(scheduler_module.sys, "executable", r"C:\ProgramData\anaconda3\python.exe")

    result = collect_base_data_with_timeout(
        Namespace(),
        timeout_seconds=5,
        collect_base_data_func=lambda args: {"status": "ok"},
    )

    assert result == {"status": "ok"}
    assert configured_executables == [str(project_python)]


def test_direct_collect_base_data_configures_spawn_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """直接运行采集脚本时也应固定 spawn 解释器到当前虚拟环境。"""

    collect_base_data = import_collection_module()
    configured_executables: list[str] = []

    monkeypatch.setattr(
        collect_base_data.multiprocessing,
        "set_executable",
        lambda executable: configured_executables.append(str(executable)),
    )

    collect_base_data.configure_multiprocessing_spawn_executable()

    assert configured_executables == [sys.executable]


def test_collect_base_data_with_timeout_reads_queue_before_join_when_child_is_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """子进程已写入大结果但尚未退出时，父进程应先读队列再 join，避免 Windows Queue flush 死锁。"""

    events: list[str] = []

    class FakeQueue:
        def __init__(self, maxsize: int) -> None:
            self.maxsize = maxsize

        def get(self, timeout: float | None = None) -> dict[str, Any]:
            events.append("queue.get")
            return {"ok": True, "result": {"status": "ok", "rows": 872}}

        def get_nowait(self) -> dict[str, Any]:
            events.append("queue.get_nowait")
            return {"ok": True, "result": {"status": "fallback"}}

    class FakeProcess:
        exitcode = None

        def __init__(self, *, target: Any, args: tuple[Any, ...]) -> None:
            self.target = target
            self.args = args
            self.alive = True

        def start(self) -> None:
            events.append("process.start")

        def is_alive(self) -> bool:
            events.append("process.is_alive")
            return self.alive

        def join(self, timeout: float | None = None) -> None:
            events.append("process.join")
            self.alive = False
            self.exitcode = 0

        def terminate(self) -> None:
            events.append("process.terminate")
            self.alive = False

        def kill(self) -> None:
            events.append("process.kill")
            self.alive = False

    class FakeContext:
        def Queue(self, maxsize: int) -> FakeQueue:  # noqa: N802 - 模拟 multiprocessing API
            return FakeQueue(maxsize=maxsize)

        def Process(self, *, target: Any, args: tuple[Any, ...]) -> FakeProcess:  # noqa: N802
            return FakeProcess(target=target, args=args)

    monkeypatch.setattr(scheduler_module.multiprocessing, "get_context", lambda method: FakeContext())
    monkeypatch.setattr(scheduler_module.multiprocessing, "set_executable", lambda executable: None)

    result = collect_base_data_with_timeout(
        Namespace(),
        timeout_seconds=5,
        collect_base_data_func=lambda args: {"status": "ok"},
    )

    assert result == {"status": "ok", "rows": 872}
    assert events.index("queue.get") < events.index("process.join")
    assert "process.terminate" not in events
    assert "process.kill" not in events


def test_ashare_market_bars_backfill_registers_one_task_per_market_asset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股 K 线补采应按资产池批量登记任务，而不是固定只采 000001。"""

    collect_base_data = import_collection_module()

    calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append(
                {
                    "task": task,
                    "provider_key": provider_key,
                    "parameters": parameters,
                    "force": force,
                }
            )
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data,
        "batch_ashare_symbols",
        lambda session, limit, fallback_symbol, **kwargs: ["000001", "600519"],
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p0"],
        sync_task_type="market_bars_backfill",
        symbol_source="market_assets",
        limit=2,
        ashare_start="20260501",
        ashare_end="20260514",
    )

    collect_base_data.run_ashare_p0(object(), args, RecordingRuntime())

    ohlcv_calls = [item for item in calls if item["task"] == "ashare_p0_ohlcv"]
    assert [item["parameters"]["symbol"] for item in ohlcv_calls] == ["000001", "600519"]
    assert [item["provider_key"] for item in ohlcv_calls] == [
        "stock_zh_a_hist_tx:000001",
        "stock_zh_a_hist_tx:600519",
    ]


def test_ashare_market_bars_reload_batch_size_between_batches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A 股 K 线长任务应在批次边界读取最新批大小，让页面保存的配置热生效。"""

    collect_base_data = import_collection_module()
    scheduler_config_file = tmp_path / "base_data_scheduler.json"
    job_name = "ashare.bars.1d.bootstrap"

    def write_scheduler_batch_size(batch_size: int) -> None:
        scheduler_config_file.write_text(
            json.dumps(
                {
                    "schema_version": "data-sync-scheduler-v1",
                    "enabled": True,
                    "jobs": [
                        {
                            "name": job_name,
                            "group": "ashare-p0",
                            "enabled": True,
                            "interval_seconds": 3600,
                            "market": "ashare",
                            "params": {
                                "sync_task_type": "market_bars_full_history_backfill",
                                "batch_size": batch_size,
                            },
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    write_scheduler_batch_size(2)
    executed_batches: list[list[str]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    def fake_run_symbol_task_batch(symbols: list[str], **kwargs: Any) -> list[Any]:
        executed_batches.append(list(symbols))
        collect_symbol = kwargs["collect_symbol"]
        on_symbol_result = kwargs.get("on_symbol_result")
        results = []
        for index, symbol in enumerate(symbols):
            result = collect_symbol(symbol)
            if on_symbol_result is not None:
                on_symbol_result(symbol, result, index)
            results.append(result)
        if len(executed_batches) == 1:
            write_scheduler_batch_size(1)
        return results

    monkeypatch.setattr(
        collect_base_data,
        "batch_ashare_symbols",
        lambda session, limit, fallback_symbol, **kwargs: [
            "000001",
            "000002",
            "000003",
            "000004",
            "000005",
        ],
    )
    monkeypatch.setattr(
        collect_base_data,
        "should_refresh_asset_universe_before_incremental",
        lambda session, market: False,
    )
    monkeypatch.setattr(
        collect_base_data,
        "record_ashare_market_bar_watermark",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(collect_base_data, "run_symbol_task_batch", fake_run_symbol_task_batch)

    args = collect_base_data.default_collection_args(
        group=["ashare-p0"],
        sync_task_type="market_bars_full_history_backfill",
        symbol_source="market_assets",
        limit=5,
        batch_size=2,
    )
    args.progress_job_name = job_name
    args.runtime_scheduler_config_file = str(scheduler_config_file)

    collect_base_data.run_ashare_p0(object(), args, RecordingRuntime())

    assert executed_batches == [
        ["000001", "000002"],
        ["000003"],
        ["000004"],
        ["000005"],
    ]


def test_symbol_task_batch_logs_worker_start_and_finish(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """批量标的执行器应输出 worker 级开始和完成日志，便于定位卡住的股票。"""

    collect_base_data = import_collection_module()
    caplog.set_level(logging.INFO, logger=collect_base_data.__name__)

    results = collect_base_data.run_symbol_task_batch(
        ["000001"],
        max_workers=1,
        collect_symbol=lambda symbol: Namespace(
            task="ashare_p0_ohlcv",
            status="available",
            raw_record_id=None,
            item_count=1,
            error_message=None,
            payload={},
        ),
        stage_key="ashare_p0_ohlcv",
        batch_index=1,
        batch_count=1,
        batch_size=1,
        total_items=1,
    )

    assert len(results) == 1
    assert "标的采集开始 stage=ashare_p0_ohlcv symbol=000001" in caplog.text
    assert "标的采集完成 stage=ashare_p0_ohlcv symbol=000001" in caplog.text


def test_ashare_market_bars_reload_max_workers_between_batches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A 股 K 线长任务应在批次边界读取最新并发数，让当前运行任务可热调速。"""

    collect_base_data = import_collection_module()
    scheduler_config_file = tmp_path / "base_data_scheduler.json"
    job_name = "ashare.bars.1d.bootstrap"

    def write_scheduler_max_workers(max_workers: int) -> None:
        scheduler_config_file.write_text(
            json.dumps(
                {
                    "schema_version": "data-sync-scheduler-v1",
                    "enabled": True,
                    "jobs": [
                        {
                            "name": job_name,
                            "group": "ashare-p0",
                            "enabled": True,
                            "interval_seconds": 3600,
                            "market": "ashare",
                            "params": {
                                "sync_task_type": "market_bars_full_history_backfill",
                                "batch_size": 2,
                                "max_workers": max_workers,
                            },
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    write_scheduler_max_workers(2)
    observed_max_workers: list[int] = []

    def fake_run_symbol_task_batch(symbols: list[str], **kwargs: Any) -> list[Any]:
        observed_max_workers.append(kwargs["max_workers"])
        if len(observed_max_workers) == 1:
            write_scheduler_max_workers(4)
        return [
            Namespace(
                task=f"ashare_p0_ohlcv:{symbol}",
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )
            for symbol in symbols
        ]

    monkeypatch.setattr(
        collect_base_data,
        "batch_ashare_symbols",
        lambda session, limit, fallback_symbol, **kwargs: [
            "000001",
            "000002",
            "000003",
            "000004",
        ],
    )
    monkeypatch.setattr(
        collect_base_data,
        "should_refresh_asset_universe_before_incremental",
        lambda session, market: False,
    )
    monkeypatch.setattr(collect_base_data, "run_symbol_task_batch", fake_run_symbol_task_batch)

    args = collect_base_data.default_collection_args(
        group=["ashare-p0"],
        sync_task_type="market_bars_full_history_backfill",
        symbol_source="market_assets",
        limit=4,
        batch_size=2,
        max_workers=2,
    )
    args.progress_job_name = job_name
    args.runtime_scheduler_config_file = str(scheduler_config_file)

    collect_base_data.run_ashare_p0(
        object(),
        args,
        Namespace(run_task=lambda **kwargs: None),
        session_factory=object(),
    )

    assert observed_max_workers == [2, 4]


def test_rate_limited_collection_hot_reloads_runtime_rate_policies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """源级限频策略应在取源令牌前热读取运行期配置，让页面保存后当前任务生效。"""

    collect_base_data = import_collection_module()
    scheduler_config_file = tmp_path / "base_data_scheduler.json"
    job_name = "ashare.bars.1d.bootstrap"
    scheduler_config_file.write_text(
        json.dumps(
            {
                "schema_version": "data-sync-scheduler-v1",
                "enabled": True,
                "rate_policies": {
                    "eastmoney_kline": {
                        "max_concurrency": 1,
                        "min_interval_seconds": 1.5,
                    }
                },
                "jobs": [
                    {
                        "name": job_name,
                        "group": "ashare-p0",
                        "enabled": True,
                        "interval_seconds": 3600,
                        "market": "ashare",
                        "params": {
                            "sync_task_type": "market_bars_full_history_backfill",
                            "max_workers": 8,
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    updates: list[dict[str, Any]] = []

    class FakeLimiter:
        def update_policies(self, payload: dict[str, Any]) -> None:
            updates.append(payload)

        def acquire(self, source_key: str) -> Any:
            class _Context:
                def __enter__(self) -> None:
                    return None

                def __exit__(self, *_args: object) -> None:
                    return None

            return _Context()

        def record_success(self, source_key: str) -> None:
            return None

        def record_failure(self, source_key: str, error_message: str | None = None) -> None:
            return None

        def adaptive_snapshot(self, source_key: str) -> None:
            return None

    args = Namespace(
        runtime_scheduler_config_file=str(scheduler_config_file),
        progress_job_name=job_name,
        rate_policies={
            "eastmoney_kline": {
                "max_concurrency": 2,
                "min_interval_seconds": 0.5,
            }
        },
    )
    monkeypatch.setattr(collect_base_data, "SOURCE_RATE_LIMITER", FakeLimiter())
    monkeypatch.setattr(collect_base_data, "COLLECTION_RUNTIME_ARGS", args, raising=False)

    result = collect_base_data.run_rate_limited_collection("eastmoney_kline", lambda: "ok")

    assert result == "ok"
    assert updates == [
        {
            "eastmoney_kline": {
                "max_concurrency": 1,
                "min_interval_seconds": 1.5,
            }
        }
    ]


def test_ashare_market_bars_reload_limit_before_each_symbol(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A 股 K 线长任务应在提交每只股票前读取最新单次上限，避免整轮任务固定旧 limit。"""

    collect_base_data = import_collection_module()
    scheduler_config_file = tmp_path / "base_data_scheduler.json"
    job_name = "ashare.bars.1d"

    def write_scheduler_limit(limit: int) -> None:
        scheduler_config_file.write_text(
            json.dumps(
                {
                    "schema_version": "data-sync-scheduler-v1",
                    "enabled": True,
                    "jobs": [
                        {
                            "name": job_name,
                            "group": "ashare-p0",
                            "enabled": True,
                            "interval_seconds": 3600,
                            "limit": limit,
                            "market": "ashare",
                            "params": {
                                "sync_task_type": "market_bars_backfill",
                                "batch_size": 3,
                                "max_workers": 1,
                            },
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    write_scheduler_limit(2)
    observed_limits: list[int | None] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            if task == "ashare_p0_ohlcv":
                observed_limits.append(parameters["limit"])
                if len(observed_limits) == 1:
                    write_scheduler_limit(9)
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data,
        "batch_ashare_symbols",
        lambda session, limit, fallback_symbol, **kwargs: ["000001", "000002", "000003"],
    )
    monkeypatch.setattr(
        collect_base_data,
        "should_refresh_asset_universe_before_incremental",
        lambda session, market: False,
    )
    monkeypatch.setattr(
        collect_base_data,
        "record_ashare_market_bar_watermark",
        lambda *args, **kwargs: None,
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p0"],
        sync_task_type="market_bars_backfill",
        symbol_source="market_assets",
        limit=2,
        batch_size=3,
        max_workers=1,
    )
    args.progress_job_name = job_name
    args.runtime_scheduler_config_file = str(scheduler_config_file)

    collect_base_data.run_ashare_p0(object(), args, RecordingRuntime())

    assert observed_limits == [2, 9, 9]


def test_ashare_market_bars_recuts_sequential_batch_when_batch_size_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """顺序 K 线任务执行中修改批次大小时，应提前结束当前批次并用新批次大小继续。"""

    collect_base_data = import_collection_module()
    scheduler_config_file = tmp_path / "base_data_scheduler.json"
    job_name = "ashare.bars.1d"

    def write_scheduler_batch_size(batch_size: int) -> None:
        scheduler_config_file.write_text(
            json.dumps(
                {
                    "schema_version": "data-sync-scheduler-v1",
                    "enabled": True,
                    "jobs": [
                        {
                            "name": job_name,
                            "group": "ashare-p0",
                            "enabled": True,
                            "interval_seconds": 3600,
                            "limit": 2,
                            "market": "ashare",
                            "params": {
                                "sync_task_type": "market_bars_backfill",
                                "batch_size": batch_size,
                                "max_workers": 1,
                            },
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    write_scheduler_batch_size(3)
    observed_symbols: list[str] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            if task == "ashare_p0_ohlcv":
                observed_symbols.append(parameters["symbol"])
                if len(observed_symbols) == 1:
                    write_scheduler_batch_size(1)
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data,
        "batch_ashare_symbols",
        lambda session, limit, fallback_symbol, **kwargs: ["000001", "000002", "000003"],
    )
    monkeypatch.setattr(
        collect_base_data,
        "should_refresh_asset_universe_before_incremental",
        lambda session, market: False,
    )
    monkeypatch.setattr(
        collect_base_data,
        "record_ashare_market_bar_watermark",
        lambda *args, **kwargs: None,
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p0"],
        sync_task_type="market_bars_backfill",
        symbol_source="market_assets",
        limit=2,
        batch_size=3,
        max_workers=1,
    )
    args.progress_job_name = job_name
    args.runtime_scheduler_config_file = str(scheduler_config_file)

    results = collect_base_data.run_ashare_p0(object(), args, RecordingRuntime())
    ohlcv_payloads = [item.payload for item in results if item.task == "ashare_p0_ohlcv"]

    assert observed_symbols == ["000001", "000002", "000003"]
    assert [payload["batch_size"] for payload in ohlcv_payloads] == [3, 1, 1]


def test_ashare_market_bars_waits_while_runtime_control_paused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A 股 K 线长任务遇到暂停控制时，应在下一只股票提交前等待继续。"""

    collect_base_data = import_collection_module()
    scheduler_config_file = tmp_path / "base_data_scheduler.json"
    job_name = "ashare.bars.1d"

    def write_control(paused: bool) -> None:
        scheduler_config_file.write_text(
            json.dumps(
                {
                    "schema_version": "data-sync-scheduler-v1",
                    "enabled": True,
                    "jobs": [
                        {
                            "name": job_name,
                            "group": "ashare-p0",
                            "enabled": True,
                            "interval_seconds": 3600,
                            "market": "ashare",
                            "control": {"paused": paused},
                            "params": {
                                "sync_task_type": "market_bars_backfill",
                                "batch_size": 2,
                                "max_workers": 1,
                            },
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    write_control(False)
    submitted_symbols: list[str] = []
    sleep_calls: list[float] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            if task == "ashare_p0_ohlcv":
                submitted_symbols.append(parameters["symbol"])
                if len(submitted_symbols) == 1:
                    write_control(True)
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        write_control(False)

    monkeypatch.setattr(
        collect_base_data,
        "batch_ashare_symbols",
        lambda session, limit, fallback_symbol, **kwargs: ["000001", "000002"],
    )
    monkeypatch.setattr(
        collect_base_data,
        "should_refresh_asset_universe_before_incremental",
        lambda session, market: False,
    )
    monkeypatch.setattr(
        collect_base_data,
        "record_ashare_market_bar_watermark",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(collect_base_data, "time_sleep", fake_sleep)

    args = collect_base_data.default_collection_args(
        group=["ashare-p0"],
        sync_task_type="market_bars_backfill",
        symbol_source="market_assets",
        limit=2,
        batch_size=2,
        max_workers=1,
    )
    args.progress_job_name = job_name
    args.runtime_scheduler_config_file = str(scheduler_config_file)

    collect_base_data.run_ashare_p0(object(), args, RecordingRuntime())

    assert submitted_symbols == ["000001", "000002"]
    assert sleep_calls == [1.0]


def test_ashare_universe_refresh_fetches_complete_asset_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股 Universe 刷新应拉完整资产列表，不能被调度批大小截断。"""

    collect_base_data = import_collection_module()

    collect_limits: list[int | None] = []
    task_parameters: list[dict[str, Any]] = []

    def fake_collect_assets(self: Any, **kwargs: Any) -> Any:
        collect_limits.append(kwargs["limit"])
        return Namespace()

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            if task == "ashare_p0_assets":
                task_parameters.append(parameters)
                collect()
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(collect_base_data.AshareP0Collector, "collect_assets", fake_collect_assets)

    args = collect_base_data.default_collection_args(
        group=["ashare-p0"],
        sync_task_type="universe_refresh",
        limit=2,
        batch_size=2,
    )

    collect_base_data.run_ashare_p0(object(), args, RecordingRuntime())

    assert collect_limits == [None]
    assert task_parameters == [{"limit": None}]


def test_symbol_task_batch_runner_limits_concurrency_and_preserves_order() -> None:
    """按标的采集的批内执行器应限制并发，并保持结果顺序稳定。"""

    collect_base_data = import_collection_module()
    symbols = ["000001", "000002", "000003", "000004"]
    active_count = 0
    max_active_count = 0
    lock = threading.Lock()

    def collect_symbol(symbol: str) -> Namespace:
        nonlocal active_count, max_active_count
        with lock:
            active_count += 1
            max_active_count = max(max_active_count, active_count)
        time.sleep(0.05)
        with lock:
            active_count -= 1
        return Namespace(
            task=symbol,
            status="planned",
            raw_record_id=None,
            item_count=0,
            error_message=None,
            payload={},
        )

    results = collect_base_data.run_symbol_task_batch(
        symbols,
        max_workers=2,
        collect_symbol=collect_symbol,
    )

    assert [result.task for result in results] == symbols
    assert max_active_count == 2


def test_ashare_p1_list_refreshes_use_unbounded_source_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股 P1 列表型来源应完整刷新，不应把调度 batch_size 当作总量上限。"""

    collect_base_data = import_collection_module()

    calls: list[dict[str, Any]] = []

    class FakeProvider:
        def fetch_index_catalog(self, limit: int | None = None) -> Namespace:
            return Namespace(payload={"indexes": [{"code": "000300", "name": "沪深300"}]})

        def fetch_industry_names(self, limit: int | None = None) -> Namespace:
            return Namespace(payload={"names": []})

        def fetch_concept_names(self, limit: int | None = None) -> Namespace:
            return Namespace(payload={"names": []})

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append(
                {
                    "task": task,
                    "provider_key": provider_key,
                    "parameters": parameters,
                    "force": force,
                }
            )
            collect()
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data.AshareP1Collector,
        "__init__",
        lambda self, session: setattr(self, "sector_provider", FakeProvider()),
    )
    monkeypatch.setattr(
        collect_base_data.AshareP1Collector,
        "collect_flow_rank",
        lambda self, indicator, limit: calls.append(
            {"task": "collect_flow_rank", "indicator": indicator, "limit": limit}
        ),
    )
    monkeypatch.setattr(
        collect_base_data.AshareP1Collector,
        "collect_index_members",
        lambda self,
        index_code,
        index_name,
        universe_id,
        universe_name,
        strategy_context,
        limit: calls.append(
            {"task": "collect_index_members", "index_code": index_code, "limit": limit}
        ),
    )
    monkeypatch.setattr(
        collect_base_data.AshareP1Collector,
        "collect_industry_members",
        lambda self,
        industry_name,
        universe_id,
        universe_name,
        strategy_context,
        limit: calls.append(
            {"task": "collect_industry_members", "industry": industry_name, "limit": limit}
        ),
    )
    monkeypatch.setattr(
        collect_base_data.AshareP1Collector,
        "collect_concept_members",
        lambda self,
        concept_name,
        universe_id,
        universe_name,
        strategy_context,
        limit: calls.append(
            {"task": "collect_concept_members", "concept": concept_name, "limit": limit}
        ),
    )
    monkeypatch.setattr(
        collect_base_data.AshareP1Collector,
        "collect_stock_news",
        lambda self, symbol, asset_name, limit, enrich_articles=True: calls.append(
            {
                "task": "collect_stock_news",
                "symbol": symbol,
                "limit": limit,
                "enrich_articles": enrich_articles,
            }
        ),
    )
    monkeypatch.setattr(
        collect_base_data.AshareP1Collector,
        "collect_notice_reports",
        lambda self, symbol, date, limit: calls.append(
            {"task": "collect_notice_reports", "symbol": symbol, "limit": limit}
        ),
    )
    monkeypatch.setattr(
        collect_base_data,
        "should_refresh_asset_universe_before_incremental",
        lambda session, *, market, min_asset_count=None: False,
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p1"],
        sync_task_type="event_refresh",
        limit=2,
        batch_size=2,
    )

    collect_base_data.run_ashare_p1(object(), args, RecordingRuntime())

    event_call_limits = {
        item["task"]: item["limit"]
        for item in calls
        if item["task"] in {"collect_stock_news", "collect_notice_reports"}
    }
    assert event_call_limits == {
        "collect_stock_news": None,
        "collect_notice_reports": None,
    }

    calls.clear()
    args = collect_base_data.default_collection_args(
        group=["ashare-p1"],
        sync_task_type="universe_refresh",
        limit=2,
        batch_size=2,
        index_catalog_limit=0,
        industry_catalog_limit=0,
        concept_catalog_limit=0,
        catalog_member_limit=0,
    )

    collect_base_data.run_ashare_p1(object(), args, RecordingRuntime())

    flow_call = next(item for item in calls if item["task"] == "collect_flow_rank")
    flow_task = next(item for item in calls if item["task"] == "ashare_p1_flow_rank")
    assert flow_call["limit"] is None
    assert flow_task["parameters"]["limit"] is None


def test_ashare_capital_flow_records_rank_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """资金流榜单刷新完成后应写入榜单级水位，支持失败补跑和冷却判断。"""

    collect_base_data = import_collection_module()
    watermark_calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            return Namespace(
                task=task,
                status="available",
                raw_record_id="raw:flow",
                item_count=2,
                error_message=None,
                payload={"actual_source": "eastmoney"},
            )

    monkeypatch.setattr(
        collect_base_data,
        "record_ashare_capital_flow_watermark",
        lambda session, **kwargs: watermark_calls.append(kwargs),
        raising=False,
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p1"],
        sync_task_type="capital_flow_refresh",
        flow_window="今日",
    )

    collect_base_data.run_ashare_p1(object(), args, RecordingRuntime())

    assert len(watermark_calls) == 1
    assert watermark_calls[0]["indicator"] == "今日"
    assert watermark_calls[0]["result"].status == "available"


def test_ashare_northbound_flow_refresh_dispatches_to_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """北向资金任务应由 A 股 P1 入口分发到专用采集方法。"""

    collect_base_data = import_collection_module()
    calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append(
                {
                    "task": task,
                    "provider_key": provider_key,
                    "parameters": parameters,
                    "force": force,
                }
            )
            collect()
            return Namespace(
                task=task,
                status="available",
                raw_record_id="raw:northbound",
                item_count=1,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data.AshareP1Collector,
        "__init__",
        lambda self, session: None,
    )
    monkeypatch.setattr(
        collect_base_data.AshareP1Collector,
        "collect_northbound_flow",
        lambda self, symbol, limit: calls.append(
            {"task": "collect_northbound_flow", "symbol": symbol, "limit": limit}
        )
        or Namespace(
            task=f"collect_northbound_flow:{symbol}",
            status="available",
            raw_record_id=f"raw:northbound:{symbol}",
            item_count=1,
            error_message=None,
            payload={},
        ),
        raising=False,
    )
    monkeypatch.setattr(
        collect_base_data,
        "resolve_ashare_northbound_symbols",
        lambda session, args: ["000001", "600519"],
        raising=False,
    )
    monkeypatch.setattr(
        collect_base_data,
        "record_ashare_northbound_individual_watermark",
        lambda session, symbol, result: calls.append(
            {
                "task": "record_northbound_watermark",
                "symbol": symbol,
                "status": result.status,
            }
        ),
        raising=False,
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p1"],
        sync_task_type="northbound_flow_refresh",
        source_limit=3,
    )

    results = collect_base_data.run_ashare_p1(object(), args, RecordingRuntime())

    assert [result.task for result in results] == [
        "ashare_p1_northbound_flow",
        "ashare_p1_northbound_flow",
        "ashare_p1_northbound_flow",
    ]
    assert calls[0]["provider_key"] == "stock_hsgt_hist_em"
    assert calls[0]["parameters"] == {"symbol": "北向资金", "limit": 3}
    assert calls[1] == {"task": "collect_northbound_flow", "symbol": "北向资金", "limit": 3}
    assert calls[2]["provider_key"] == "stock_hsgt_individual_em"
    assert calls[2]["parameters"] == {"symbol": "000001", "limit": 3}
    assert calls[3] == {"task": "collect_northbound_flow", "symbol": "000001", "limit": 3}
    assert calls[4] == {
        "task": "record_northbound_watermark",
        "symbol": "000001",
        "status": "available",
    }
    assert calls[5]["provider_key"] == "stock_hsgt_individual_em"
    assert calls[5]["parameters"] == {"symbol": "600519", "limit": 3}
    assert calls[6] == {"task": "collect_northbound_flow", "symbol": "600519", "limit": 3}
    assert calls[7] == {
        "task": "record_northbound_watermark",
        "symbol": "600519",
        "status": "available",
    }


def test_batch_ashare_northbound_symbols_skips_failure_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """北向个股采集应跳过失败冷却期内的主板标的。"""

    collect_base_data = import_collection_module()
    now = datetime.now(tz=UTC)
    assets = [
        Namespace(asset_id="ashare:000001", symbol="000001", market="ashare", asset_type="stock"),
        Namespace(asset_id="ashare:600519", symbol="600519", market="ashare", asset_type="stock"),
    ]
    watermarks = {
        "ashare:000001": Namespace(
            status="error",
            next_retry_at=now + timedelta(minutes=10),
        )
    }

    class FakeAssetRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def find_by_market(self, market: str) -> list[Any]:
            assert market == "ashare"
            return assets

    monkeypatch.setattr(collect_base_data, "AssetRepository", FakeAssetRepository)
    monkeypatch.setattr(
        collect_base_data,
        "_fetch_data_sync_watermarks",
        lambda *args, **kwargs: watermarks,
    )

    symbols = collect_base_data.batch_ashare_northbound_symbols(
        object(),
        fallback_symbol="000001",
        now=now,
    )

    assert symbols == ["600519"]


def test_batch_ashare_northbound_symbols_prefers_missing_and_old_watermarks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """北向个股小批刷新应优先未采和最久未采的标的。"""

    collect_base_data = import_collection_module()
    now = datetime.now(tz=UTC)
    assets = [
        Namespace(asset_id="ashare:000001", symbol="000001", market="ashare", asset_type="stock"),
        Namespace(asset_id="ashare:600519", symbol="600519", market="ashare", asset_type="stock"),
        Namespace(asset_id="ashare:601398", symbol="601398", market="ashare", asset_type="stock"),
    ]
    watermarks = {
        "ashare:000001": Namespace(
            status="available",
            watermark_at=now - timedelta(days=1),
            last_success_at=now - timedelta(days=1),
            next_retry_at=None,
        ),
        "ashare:600519": Namespace(
            status="available",
            watermark_at=now - timedelta(days=8),
            last_success_at=now - timedelta(days=8),
            next_retry_at=None,
        ),
    }

    class FakeAssetRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def find_by_market(self, market: str) -> list[Any]:
            assert market == "ashare"
            return assets

    monkeypatch.setattr(collect_base_data, "AssetRepository", FakeAssetRepository)
    monkeypatch.setattr(
        collect_base_data,
        "_fetch_data_sync_watermarks",
        lambda *args, **kwargs: watermarks,
    )

    symbols = collect_base_data.batch_ashare_northbound_symbols(
        object(),
        fallback_symbol="000001",
        limit=2,
        now=now,
    )

    assert symbols == ["601398", "600519"]


def test_record_ashare_northbound_individual_watermark_uses_short_timeframe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """北向个股水位 timeframe 必须短于表结构限制。"""

    collect_base_data = import_collection_module()
    calls: list[dict[str, Any]] = []

    class FakeWatermarkRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def record_success(self, **kwargs: Any) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(
        collect_base_data,
        "DataSyncWatermarkRepository",
        FakeWatermarkRepository,
    )

    collect_base_data.record_ashare_northbound_individual_watermark(
        object(),
        symbol="000001",
        result=Namespace(status="available", item_count=1, task="northbound", payload={}),
    )

    assert calls[0]["timeframe"] == "northbound"
    assert len(calls[0]["timeframe"]) <= 16


def test_ashare_capital_flow_skips_when_failure_watermark_in_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """资金流榜单失败冷却未到时应跳过本轮请求，避免反复打不稳定来源。"""

    collect_base_data = import_collection_module()
    run_calls: list[str] = []
    next_retry_at = datetime.now(tz=UTC) + timedelta(minutes=10)

    class RecordingRuntime:
        def run_task(self, **kwargs: Any) -> Any:
            run_calls.append(kwargs["task"])
            raise AssertionError("失败冷却期内不应继续请求资金流数据源")

    monkeypatch.setattr(
        collect_base_data,
        "_fetch_data_sync_watermarks",
        lambda session, asset_ids, data_domain, provider, timeframe: {
            collect_base_data.ashare_capital_flow_watermark_asset_id("今日"): Namespace(
                status="error",
                next_retry_at=next_retry_at,
            )
        },
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p1"],
        sync_task_type="capital_flow_refresh",
        flow_window="今日",
    )

    results = collect_base_data.run_ashare_p1(object(), args, RecordingRuntime())

    assert run_calls == []
    assert results[0].status == "skipped"
    assert "失败冷却期" in results[0].error_message


def test_ashare_event_refresh_runs_stock_news_for_priority_assets_in_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股逐股新闻只覆盖重点资产，避免全市场逐股新闻拖慢事件主链路。"""

    collect_base_data = import_collection_module()

    calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append(
                {
                    "task": task,
                    "provider_key": provider_key,
                    "parameters": parameters,
                    "force": force,
                }
            )
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data,
        "resolve_ashare_priority_news_symbols",
        lambda session, args: ["000001", "600519", "002594"],
    )
    monkeypatch.setattr(
        collect_base_data,
        "should_refresh_asset_universe_before_incremental",
        lambda session, *, market, min_asset_count=None: False,
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p1"],
        sync_task_type="event_refresh",
        symbol_source="market_assets",
        limit=2,
        batch_size=2,
    )

    results = collect_base_data.run_ashare_p1(object(), args, RecordingRuntime())

    news_calls = [item for item in calls if item["task"] == "ashare_p1_stock_news"]
    assert [item["parameters"]["symbol"] for item in news_calls] == ["000001", "600519", "002594"]
    assert [item["parameters"]["limit"] for item in news_calls] == [None, None, None]
    assert [item["parameters"]["enrich_articles"] for item in news_calls] == [False, False, False]
    assert [
        item.payload.get("batch_index")
        for item in results
        if item.task == "ashare_p1_stock_news"
    ] == [
        1,
        1,
        2,
    ]
    notice_call = next(item for item in calls if item["task"] == "ashare_p1_notice_reports")
    assert notice_call["parameters"]["limit"] is None


def test_ashare_event_refresh_full_scope_uses_tradeable_market_assets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """盘后全量新闻应从可交易资产池取标的，不走盘中重点池解析。"""

    collect_base_data = import_collection_module()

    calls: list[dict[str, Any]] = []
    assets = [
        Namespace(asset_id="ashare:000001", symbol="000001", market="ashare", asset_type="stock"),
        Namespace(asset_id="ashare:600519", symbol="600519", market="ashare", asset_type="stock"),
        Namespace(asset_id="ashare:300750", symbol="300750", market="ashare", asset_type="stock"),
    ]

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append(
                {
                    "task": task,
                    "provider_key": provider_key,
                    "parameters": parameters,
                    "force": force,
                }
            )
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    class FakeAssetRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def find_by_market(self, market: str, *, only_tradable: bool = True) -> list[Any]:
            assert market == "ashare"
            return assets

    monkeypatch.setattr(collect_base_data, "AssetRepository", FakeAssetRepository)
    monkeypatch.setattr(
        collect_base_data,
        "resolve_ashare_priority_news_symbols",
        lambda session, args: (_ for _ in ()).throw(
            AssertionError("盘后全量新闻不应调用重点池解析器")
        ),
    )
    monkeypatch.setattr(
        collect_base_data,
        "should_refresh_asset_universe_before_incremental",
        lambda session, *, market, min_asset_count=None: False,
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p1"],
        sync_task_type="event_refresh",
        news_scope="full_tradeable",
        symbol_source="market_assets",
        batch_size=2,
        priority_symbol_limit=0,
    )

    collect_base_data.run_ashare_p1(object(), args, RecordingRuntime())

    news_calls = [item for item in calls if item["task"] == "ashare_p1_stock_news"]
    assert [item["parameters"]["symbol"] for item in news_calls] == ["000001", "600519"]


def test_ashare_news_retention_deletes_expired_article_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新闻保留后台任务应删除过期新闻/公告事件和证据整行。"""

    collect_base_data = import_collection_module()
    calls: list[dict[str, Any]] = []

    class FakeEventRepository:
        def __init__(self, session: Any) -> None:
            calls.append({"session": session})

        def delete_expired_article_events(self, *, cutoff: datetime) -> dict[str, int]:
            calls.append({"cutoff": cutoff})
            return {"event_records": 2, "evidence": 3, "total": 5}

    monkeypatch.setattr(collect_base_data, "EventRepository", FakeEventRepository, raising=False)
    monkeypatch.setattr(
        collect_base_data,
        "build_ashare_p1_default_tasks",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("新闻保留任务不应落入 P1 默认采集包")
        ),
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p1"],
        sync_task_type="event_article_retention",
        article_retention_days=90,
    )

    results = collect_base_data.run_ashare_p1(object(), args, object())

    assert results[0].task == "ashare_p1_news_retention"
    assert results[0].status == "available"
    assert results[0].item_count == 5
    assert results[0].payload["event_records"] == 2
    assert results[0].payload["evidence"] == 3
    assert datetime.now(tz=UTC) - calls[1]["cutoff"] >= timedelta(days=89)


def test_ashare_priority_news_symbols_use_event_priority_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """逐股新闻重点名单应优先来自事件重点池解析器。"""

    collect_base_data = import_collection_module()
    session = object()
    calls: list[dict[str, Any]] = []

    class FakeEventPriorityResolver:
        def __init__(self, resolver_session: Any) -> None:
            calls.append({"session": resolver_session})

        def resolve_ashare_symbols(self, *, limit: int) -> list[str]:
            calls.append({"limit": limit})
            return ["600519", "300750", "000001"]

    monkeypatch.setattr(collect_base_data, "EventPriorityResolver", FakeEventPriorityResolver)

    args = Namespace(priority_symbol_limit=2, ashare_symbol="002594")

    assert collect_base_data.resolve_ashare_priority_news_symbols(session, args) == [
        "600519",
        "000001",
    ]
    assert calls == [{"session": session}, {"limit": 2}]


def test_ashare_risk_sentiment_refreshes_use_unbounded_source_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股风险和情绪列表应完整刷新，不应只采一批样本。"""

    collect_base_data = import_collection_module()

    collected_limits: dict[str, int | None] = {}

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            collect()
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload=dict(parameters),
            )

    def record_limit(name: str):
        def inner(self: Any, *args: Any, **kwargs: Any) -> Any:
            collected_limits[name] = kwargs.get("limit")
            return Namespace()

        return inner

    for method_name in [
        "collect_stop_list",
        "collect_hot_rank",
        "collect_zt_pool",
        "collect_lhb_detail",
        "collect_block_trades",
        "collect_margin_sse",
        "collect_margin_szse",
    ]:
        monkeypatch.setattr(
            collect_base_data.AshareRiskSentimentCollector,
            method_name,
            record_limit(method_name),
        )

    args = collect_base_data.default_collection_args(
        group=["ashare-risk"],
        sync_task_type="risk_sentiment_refresh",
        limit=2,
        batch_size=2,
    )

    results = collect_base_data.run_ashare_risk(object(), args, RecordingRuntime())

    assert set(collected_limits) == {
        "collect_stop_list",
        "collect_hot_rank",
        "collect_zt_pool",
        "collect_lhb_detail",
        "collect_block_trades",
        "collect_margin_sse",
        "collect_margin_szse",
    }
    assert all(limit is None for limit in collected_limits.values())
    assert all(result.payload.get("limit") is None for result in results)


def test_ashare_risk_sentiment_records_independent_source_watermarks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """风险情绪每个子源都应独立写水位，避免一个源失败污染整组任务。"""

    collect_base_data = import_collection_module()
    watermark_calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            status = "error" if provider_key == "stock_hot_rank_em" else "available"
            return Namespace(
                task=task,
                status=status,
                raw_record_id=f"raw:{provider_key}" if status == "available" else None,
                item_count=3 if status == "available" else 0,
                error_message="hot rank timeout" if status == "error" else None,
                payload={"actual_source": f"akshare:{provider_key}"},
            )

    monkeypatch.setattr(
        collect_base_data,
        "record_ashare_risk_sentiment_watermark",
        lambda session, **kwargs: watermark_calls.append(kwargs),
        raising=False,
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-risk"],
        sync_task_type="risk_sentiment_refresh",
        limit=2,
        batch_size=2,
    )

    collect_base_data.run_ashare_risk(object(), args, RecordingRuntime())

    assert [
        (call["task"], call["provider"], call["result"].status)
        for call in watermark_calls
    ] == [
        ("ashare_risk_stop_list", "stock_zh_a_stop_em", "available"),
        ("ashare_sentiment_hot_rank", "stock_hot_rank_em", "error"),
        ("ashare_sentiment_zt_pool", "stock_zt_pool_em", "available"),
        ("ashare_risk_lhb_detail", "stock_lhb_detail_em", "available"),
        ("ashare_risk_block_trades", "stock_dzjy_mrmx", "available"),
        ("ashare_risk_margin_sse", "stock_margin_sse", "available"),
        ("ashare_risk_margin_szse", "stock_margin_szse", "available"),
    ]


def test_ashare_restricted_release_refresh_dispatches_to_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """限售解禁任务应由风险入口分发到专用采集方法。"""

    collect_base_data = import_collection_module()
    calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append(
                {
                    "task": task,
                    "provider_key": provider_key,
                    "parameters": parameters,
                    "force": force,
                }
            )
            collect()
            return Namespace(
                task=task,
                status="available",
                raw_record_id="raw:restricted",
                item_count=1,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data.AshareRiskSentimentCollector,
        "__init__",
        lambda self, session: None,
    )
    monkeypatch.setattr(
        collect_base_data.AshareRiskSentimentCollector,
        "collect_restricted_release",
        lambda self, **kwargs: calls.append(
            {"task": "collect_restricted_release", **kwargs}
        ),
        raising=False,
    )
    monkeypatch.setattr(
        collect_base_data,
        "record_ashare_risk_sentiment_watermark",
        lambda session, **kwargs: calls.append({"record_kind": "watermark", **kwargs}),
        raising=False,
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-risk"],
        sync_task_type="restricted_release_refresh",
        risk_start="20260601",
        risk_end="20260630",
        source_limit=5,
    )

    results = collect_base_data.run_ashare_risk(object(), args, RecordingRuntime())

    assert [result.task for result in results] == ["ashare_risk_restricted_release"]
    assert calls[0]["provider_key"] == "stock_restricted_release_detail_em"
    assert calls[0]["parameters"] == {
        "start_date": "20260601",
        "end_date": "20260630",
        "limit": 5,
        "risk_window_days": 30,
        "risk_ratio_threshold": "0.05",
    }
    assert calls[1] == {
        "task": "collect_restricted_release",
        "start_date": "20260601",
        "end_date": "20260630",
        "limit": 5,
        "risk_window_days": 30,
        "risk_ratio_threshold": Decimal("0.05"),
    }
    record_call = next(call for call in calls if call.get("record_kind") == "watermark")
    assert record_call["provider"] == "stock_restricted_release_detail_em"


def test_restricted_release_watermark_timeframe_fits_schema_limit() -> None:
    """限售解禁水位 timeframe 不应超过 data_sync_watermarks 的长度限制。"""

    collect_base_data = import_collection_module()

    timeframe = collect_base_data.ashare_risk_sentiment_watermark_timeframe(
        task="ashare_risk_restricted_release",
        provider="stock_restricted_release_detail_em",
        parameters={"start_date": "20260601", "end_date": "20260630"},
    )

    assert timeframe == "rr:20260630"
    assert len(timeframe) <= 16


def test_ashare_pledge_risk_refresh_dispatches_to_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """股权质押任务应由风险入口分发到专用采集方法。"""

    collect_base_data = import_collection_module()
    calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append(
                {
                    "task": task,
                    "provider_key": provider_key,
                    "parameters": parameters,
                    "force": force,
                }
            )
            collect()
            return Namespace(
                task=task,
                status="available",
                raw_record_id="raw:pledge",
                item_count=1,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data.AshareRiskSentimentCollector,
        "__init__",
        lambda self, session: None,
    )
    monkeypatch.setattr(
        collect_base_data.AshareRiskSentimentCollector,
        "collect_pledge_ratio",
        lambda self, **kwargs: calls.append({"task": "collect_pledge_ratio", **kwargs}),
        raising=False,
    )
    monkeypatch.setattr(
        collect_base_data,
        "record_ashare_risk_sentiment_watermark",
        lambda session, **kwargs: calls.append({"record_kind": "watermark", **kwargs}),
        raising=False,
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-risk"],
        sync_task_type="pledge_risk_refresh",
        risk_end="20260612",
        source_limit=5,
    )

    results = collect_base_data.run_ashare_risk(object(), args, RecordingRuntime())

    assert [result.task for result in results] == ["ashare_risk_pledge_ratio"]
    assert calls[0]["provider_key"] == "stock_gpzy_pledge_ratio_em"
    assert calls[0]["parameters"] == {
        "date": "20260612",
        "limit": 5,
        "risk_ratio_threshold": "0.30",
    }
    assert calls[1] == {
        "task": "collect_pledge_ratio",
        "date": "20260612",
        "limit": 5,
        "risk_ratio_threshold": Decimal("0.30"),
    }
    record_call = next(call for call in calls if call.get("record_kind") == "watermark")
    assert record_call["provider"] == "stock_gpzy_pledge_ratio_em"


def test_pledge_risk_watermark_timeframe_fits_schema_limit() -> None:
    """股权质押水位 timeframe 不应超过 data_sync_watermarks 的长度限制。"""

    collect_base_data = import_collection_module()

    timeframe = collect_base_data.ashare_risk_sentiment_watermark_timeframe(
        task="ashare_risk_pledge_ratio",
        provider="stock_gpzy_pledge_ratio_em",
        parameters={"risk_ratio_threshold": "0.30"},
    )

    assert timeframe == "pledge_ratio"
    assert len(timeframe) <= 16


def test_risk_sentiment_window_watermark_timeframes_fit_schema_limit() -> None:
    """风险情绪窗口型水位 timeframe 必须压缩到表结构长度内。"""

    collect_base_data = import_collection_module()

    cases = [
        (
            "ashare_risk_lhb_detail",
            "stock_lhb_detail_em",
            {"start_date": "20260501", "end_date": "20260514"},
        ),
        (
            "ashare_risk_block_trades",
            "stock_dzjy_mrmx",
            {"symbol": "A股", "start_date": "20260501", "end_date": "20260514"},
        ),
        (
            "ashare_risk_margin_sse",
            "stock_margin_sse",
            {"start_date": "20260501", "end_date": "20260514"},
        ),
        (
            "ashare_risk_margin_szse",
            "stock_margin_szse",
            {"date": "20260514"},
        ),
    ]

    timeframes = [
        collect_base_data.ashare_risk_sentiment_watermark_timeframe(
            task=task,
            provider=provider,
            parameters=parameters,
        )
        for task, provider, parameters in cases
    ]

    assert timeframes == [
        "lhb:260501-0514",
        "dzjy:260501-0514",
        "msse:260501-0514",
        "mszse:260514",
    ]
    assert all(len(timeframe) <= 16 for timeframe in timeframes)


def test_ashare_risk_sentiment_skips_only_source_in_failure_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单个风险源失败冷却未到时，只跳过该源，其他子阶段仍应继续执行。"""

    collect_base_data = import_collection_module()
    run_calls: list[str] = []
    watermark_calls: list[dict[str, Any]] = []
    next_retry_at = datetime.now(tz=UTC) + timedelta(minutes=10)

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            run_calls.append(provider_key)
            return Namespace(
                task=task,
                status="available",
                raw_record_id=f"raw:{provider_key}",
                item_count=1,
                error_message=None,
                payload={},
            )

    def fake_watermarks(
        session: Any,
        asset_ids: list[str],
        *,
        data_domain: str,
        provider: str,
        timeframe: str,
    ) -> dict[str, Any]:
        assert data_domain == collect_base_data.ASHARE_RISK_SENTIMENT_DATA_DOMAIN
        if provider == "stock_hot_rank_em":
            return {
                collect_base_data.ashare_risk_sentiment_watermark_asset_id(
                    "stock_hot_rank_em",
                    timeframe="hot_rank",
                ): Namespace(status="error", next_retry_at=next_retry_at)
            }
        return {}

    monkeypatch.setattr(collect_base_data, "_fetch_data_sync_watermarks", fake_watermarks)
    monkeypatch.setattr(
        collect_base_data,
        "record_ashare_risk_sentiment_watermark",
        lambda session, **kwargs: watermark_calls.append(kwargs),
        raising=False,
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-risk"],
        sync_task_type="risk_sentiment_refresh",
        risk_end="20260514",
    )

    results = collect_base_data.run_ashare_risk(object(), args, RecordingRuntime())

    assert "stock_hot_rank_em" not in run_calls
    assert len(run_calls) == 6
    hot_rank = next(result for result in results if result.task == "ashare_sentiment_hot_rank")
    assert hot_rank.status == "skipped"
    assert "失败冷却期" in hot_rank.error_message
    assert [call["provider"] for call in watermark_calls] == run_calls


def test_record_ashare_risk_sentiment_watermark_records_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """风险情绪源水位应记录成功和失败摘要，便于后续断点和手工重跑。"""

    collect_base_data = import_collection_module()
    calls: list[dict[str, Any]] = []

    class FakeWatermarkRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def record_success(self, **kwargs: Any) -> None:
            calls.append({"kind": "success", **kwargs})

        def record_failure(self, **kwargs: Any) -> None:
            calls.append({"kind": "failure", **kwargs})

    monkeypatch.setattr(collect_base_data, "DataSyncWatermarkRepository", FakeWatermarkRepository)

    collect_base_data.record_ashare_risk_sentiment_watermark(
        object(),
        task="ashare_risk_stop_list",
        provider="stock_zh_a_stop_em",
        timeframe="stop_list",
        result=Namespace(status="available", item_count=2, payload={"actual_source": "akshare"}),
    )
    collect_base_data.record_ashare_risk_sentiment_watermark(
        object(),
        task="ashare_sentiment_hot_rank",
        provider="stock_hot_rank_em",
        timeframe="hot_rank",
        result=Namespace(status="error", item_count=0, error_message="timeout", payload={}),
    )

    assert calls[0]["kind"] == "success"
    assert calls[0]["asset_id"] == "ashare:risk_sentiment:stock_zh_a_stop_em:stop_list"
    assert calls[0]["data_domain"] == collect_base_data.ASHARE_RISK_SENTIMENT_DATA_DOMAIN
    assert calls[0]["payload"]["item_count"] == 2
    assert calls[1]["kind"] == "failure"
    assert calls[1]["asset_id"] == "ashare:risk_sentiment:stock_hot_rank_em:hot_rank"
    assert calls[1]["error_message"] == "timeout"
    assert calls[1]["retry_after"] == timedelta(minutes=15)


def test_ashare_market_bars_backfill_refreshes_complete_universe_when_pool_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """库内没有 A 股资产时，按资产补采应先刷新完整全 A Universe。"""

    collect_base_data = import_collection_module()

    collect_limits: list[int | None] = []
    calls: list[str] = []

    class FakeAssetRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def find_by_market(self, market: str, *, only_tradable: bool = True) -> list[Any]:
            assert market == "ashare"
            return []

    def fake_collect_assets(self: Any, **kwargs: Any) -> Any:
        collect_limits.append(kwargs["limit"])
        return Namespace()

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append(task)
            if task == "ashare_p0_assets":
                collect()
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(collect_base_data, "AssetRepository", FakeAssetRepository)
    monkeypatch.setattr(collect_base_data.AshareP0Collector, "collect_assets", fake_collect_assets)

    args = collect_base_data.default_collection_args(
        group=["ashare-p0"],
        sync_task_type="market_bars_backfill",
        symbol_source="market_assets",
        limit=2,
        batch_size=2,
    )

    collect_base_data.run_ashare_p0(object(), args, RecordingRuntime())

    assert collect_limits == [None]
    assert calls[:2] == ["ashare_p0_calendar", "ashare_p0_assets"]


def test_ashare_market_bars_backfill_runs_all_market_assets_in_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股 K 线补采的一次调度应按批跑完整个待补资产池。"""

    collect_base_data = import_collection_module()

    calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append(
                {
                    "task": task,
                    "provider_key": provider_key,
                    "parameters": parameters,
                    "force": force,
                }
            )
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data,
        "batch_ashare_symbols",
        lambda session, limit, fallback_symbol, **kwargs: [
            "000001",
            "000002",
            "000003",
            "000004",
            "000005",
        ],
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p0"],
        sync_task_type="market_bars_backfill",
        symbol_source="market_assets",
        limit=2,
        batch_size=2,
        ashare_start="20260501",
        ashare_end="20260514",
    )

    results = collect_base_data.run_ashare_p0(object(), args, RecordingRuntime())

    ohlcv_calls = [item for item in calls if item["task"] == "ashare_p0_ohlcv"]
    assert [item["parameters"]["symbol"] for item in ohlcv_calls] == [
        "000001",
        "000002",
        "000003",
        "000004",
        "000005",
    ]
    assert [item["parameters"]["limit"] for item in ohlcv_calls] == [2, 2, 2, 2, 2]
    assert [
        item.payload.get("batch_index")
        for item in results
        if item.task == "ashare_p0_ohlcv"
    ] == [
        1,
        1,
        2,
        2,
        3,
    ]
    assert [
        item.payload.get("batch_count")
        for item in results
        if item.task == "ashare_p0_ohlcv"
    ] == [
        3,
        3,
        3,
        3,
        3,
    ]


def test_ashare_full_history_backfill_does_not_cap_single_symbol_bars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """10 年历史 K 线初始化任务不应把批次 limit 误传成单股 K 线条数上限。"""

    collect_base_data = import_collection_module()

    calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append(
                {
                    "task": task,
                    "provider_key": provider_key,
                    "parameters": parameters,
                    "force": force,
                }
            )
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data,
        "batch_ashare_symbols",
        lambda session, limit, fallback_symbol, **kwargs: ["000001", "600519"],
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p0"],
        sync_task_type="market_bars_full_history_backfill",
        symbol_source="market_assets",
        limit=2,
        batch_size=2,
        max_workers=1,
        ashare_start="20160605",
        ashare_end="20260605",
    )

    collect_base_data.run_ashare_p0(object(), args, RecordingRuntime())

    ohlcv_calls = [item for item in calls if item["task"] == "ashare_p0_ohlcv"]
    assert [item["parameters"]["symbol"] for item in ohlcv_calls] == ["000001", "600519"]
    assert [item["parameters"]["limit"] for item in ohlcv_calls] == [None, None]


def test_ashare_market_bars_uses_symbol_scoped_provider_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股 K 线批量补采的熔断应按标的隔离，避免少数股票断连导致整批跳过。"""

    collect_base_data = import_collection_module()

    calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append(
                {
                    "task": task,
                    "provider_key": provider_key,
                    "parameters": parameters,
                    "force": force,
                }
            )
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data,
        "batch_ashare_symbols",
        lambda session, limit, fallback_symbol, **kwargs: ["000001", "600519"],
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p0"],
        sync_task_type="market_bars_backfill",
        symbol_source="market_assets",
        limit=2,
    )

    collect_base_data.run_ashare_p0(object(), args, RecordingRuntime())

    ohlcv_calls = [item for item in calls if item["task"] == "ashare_p0_ohlcv"]
    assert [item["provider_key"] for item in ohlcv_calls] == [
        "stock_zh_a_hist_tx:000001",
        "stock_zh_a_hist_tx:600519",
    ]


def test_ashare_market_bars_skips_symbols_with_open_runtime_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股 K 线运行时熔断已打开的标的，不应继续进入年度窗口请求。"""

    collect_base_data = import_collection_module()
    calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append(
                {
                    "task": task,
                    "provider_key": provider_key,
                    "parameters": parameters,
                    "force": force,
                }
            )
            return Namespace(
                task=task,
                status="available",
                raw_record_id=None,
                item_count=1,
                error_message=None,
                payload={},
            )

        def get_provider_state(self, provider_key: str) -> dict[str, Any]:
            if provider_key == "stock_zh_a_hist_tx:001220":
                return {
                    "status": "open",
                    "opened_until": datetime(2026, 6, 4, 10, 15, tzinfo=UTC).isoformat(),
                }
            return {}

        def is_circuit_open(self, state: dict[str, Any]) -> bool:
            return state.get("status") == "open"

    monkeypatch.setattr(
        collect_base_data,
        "batch_ashare_symbols",
        lambda session, limit, fallback_symbol, **kwargs: ["001220", "600519"],
    )
    monkeypatch.setattr(
        collect_base_data,
        "should_refresh_asset_universe_before_incremental",
        lambda session, market: False,
    )
    monkeypatch.setattr(
        collect_base_data,
        "plan_ashare_market_bar_backfill_windows",
        lambda session, symbols, **kwargs: {
            "001220": [("20240101", "20241231"), ("20250101", "20251231")],
            "600519": [("20240101", "20241231")],
        },
    )
    monkeypatch.setattr(
        collect_base_data,
        "_fetch_data_sync_watermarks",
        lambda *args, **kwargs: {},
        raising=False,
    )
    monkeypatch.setattr(
        collect_base_data,
        "latest_ashare_trading_datetime",
        lambda session, value: value,
        raising=False,
    )
    monkeypatch.setattr(
        collect_base_data,
        "record_ashare_market_bar_watermark",
        lambda *args, **kwargs: None,
        raising=False,
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p0"],
        sync_task_type="market_bars_full_history_backfill",
        symbol_source="market_assets",
        limit=2,
        batch_size=2,
        ashare_start="20240101",
        ashare_end="20251231",
    )

    results = collect_base_data.run_ashare_p0(object(), args, RecordingRuntime())

    ohlcv_calls = [item for item in calls if item["task"] == "ashare_p0_ohlcv"]
    assert [item["parameters"]["symbol"] for item in ohlcv_calls] == ["600519"]
    skipped = [item for item in results if getattr(item, "payload", {}).get("symbol") == "001220"]
    assert skipped
    assert skipped[0].status == "skipped"
    assert skipped[0].error_message == "Provider 熔断冷却中，等待后续批次重跑。"


def test_batch_ashare_symbols_prefers_assets_with_less_market_bar_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股 K 线批次应优先补没有 K 线或 K 线较少的资产，避免反复覆盖代码靠前的股票。"""

    collect_base_data = import_collection_module()

    class FakeAssetRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def find_by_market(self, market: str, *, only_tradable: bool = True) -> list[Any]:
            assert market == "ashare"
            assert only_tradable is True
            return [
                Namespace(asset_id="ashare:000001", symbol="000001"),
                Namespace(asset_id="ashare:002594", symbol="002594"),
                Namespace(asset_id="ashare:300750", symbol="300750"),
                Namespace(asset_id="ashare:688363", symbol="688363"),
                Namespace(asset_id="ashare:873124", symbol="873124"),
                Namespace(asset_id="ashare:600519", symbol="600519"),
            ]

    monkeypatch.setattr(collect_base_data, "AssetRepository", FakeAssetRepository)
    monkeypatch.setattr(
        collect_base_data,
        "_fetch_ashare_bar_coverage",
        lambda session, asset_ids, timeframe: {
            "ashare:000001": (40, None),
            "ashare:002594": (8, None),
            "ashare:300750": (8, None),
        },
        raising=False,
    )

    symbols = collect_base_data.batch_ashare_symbols(
        object(),
        limit=2,
        fallback_symbol="000001",
    )

    assert symbols == ["600519", "002594"]


def test_batch_ashare_symbols_skips_failed_assets_until_retry_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股 K 线补采应跳过仍处于失败冷却期的标的，避免本轮反复请求同一不稳定源。"""

    collect_base_data = import_collection_module()
    now = datetime(2026, 6, 4, 10, 0, tzinfo=UTC)

    class FakeAssetRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def find_by_market(self, market: str, *, only_tradable: bool = True) -> list[Any]:
            assert market == "ashare"
            return [
                Namespace(asset_id="ashare:000001", symbol="000001"),
                Namespace(asset_id="ashare:002594", symbol="002594"),
                Namespace(asset_id="ashare:300750", symbol="300750"),
                Namespace(asset_id="ashare:600519", symbol="600519"),
            ]

    monkeypatch.setattr(collect_base_data, "AssetRepository", FakeAssetRepository)
    monkeypatch.setattr(
        collect_base_data,
        "_fetch_ashare_bar_coverage",
        lambda session, asset_ids, timeframe: {
            "ashare:000001": (0, None),
            "ashare:002594": (0, None),
            "ashare:300750": (0, None),
            "ashare:600519": (0, None),
        },
        raising=False,
    )
    monkeypatch.setattr(
        collect_base_data,
        "_fetch_data_sync_watermarks",
        lambda session, asset_ids, data_domain, provider, timeframe: {
            "ashare:000001": Namespace(status="error", next_retry_at=now + timedelta(minutes=10)),
            "ashare:002594": Namespace(status="error", next_retry_at=now - timedelta(minutes=1)),
        },
        raising=False,
    )

    symbols = collect_base_data.batch_ashare_symbols(
        object(),
        limit=None,
        fallback_symbol="000001",
        now=now,
    )

    assert symbols == ["002594", "600519"]


def test_batch_ashare_symbols_skips_assets_with_covered_requested_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全量历史 K 线任务应跳过数据库中已覆盖本次请求区间的标的，避免断点续跑时重跑。"""

    collect_base_data = import_collection_module()
    request_start = datetime(2016, 6, 1, tzinfo=UTC)
    request_end = datetime(2026, 6, 5, tzinfo=UTC)

    class FakeAssetRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def find_by_market(self, market: str, *, only_tradable: bool = True) -> list[Any]:
            assert market == "ashare"
            return [
                Namespace(asset_id="ashare:000001", symbol="000001"),
                Namespace(asset_id="ashare:002594", symbol="002594"),
                Namespace(asset_id="ashare:600519", symbol="600519"),
            ]

    monkeypatch.setattr(collect_base_data, "AssetRepository", FakeAssetRepository)
    monkeypatch.setattr(
        collect_base_data,
        "_fetch_ashare_bar_coverage",
        lambda session, asset_ids, timeframe: {
            "ashare:000001": (2426, request_start, request_end),
            "ashare:002594": (0, None, None),
            "ashare:600519": (1800, datetime(2018, 1, 1, tzinfo=UTC), request_end),
        },
        raising=False,
    )
    monkeypatch.setattr(
        collect_base_data,
        "_fetch_data_sync_watermarks",
        lambda session, asset_ids, data_domain, provider, timeframe: {},
        raising=False,
    )

    symbols = collect_base_data.batch_ashare_symbols(
        object(),
        fallback_symbol="000001",
        only_failed_or_stale=True,
        required_start_at=request_start,
        required_end_at=request_end,
    )

    assert symbols == ["002594", "600519"]


def test_fetch_ashare_bar_coverage_returns_earliest_and_latest() -> None:
    """A 股 K 线覆盖度查询必须返回最早和最新时间，断点续跑才能判断完整窗口。"""

    collect_base_data = import_collection_module()
    earliest = datetime(2016, 6, 8, tzinfo=UTC)
    latest = datetime(2026, 6, 5, tzinfo=UTC)

    class FakeSession:
        def execute(self, statement: Any) -> list[tuple[Any, ...]]:
            return [("ashare:000001", 2420, earliest, latest)]

    assert collect_base_data._fetch_ashare_bar_coverage(
        FakeSession(),
        ["ashare:000001"],
        timeframe="1d",
    ) == {"ashare:000001": (2420, earliest, latest)}


def test_fetch_ashare_bar_coverage_chunks_large_asset_sets() -> None:
    """A 股 K 线覆盖率查询应按资产分块，避免单条超大 IN 查询耗尽 PostgreSQL 共享内存。"""

    collect_base_data = import_collection_module()

    class FakeSession:
        def __init__(self) -> None:
            self.execute_count = 0

        def execute(self, statement: Any) -> list[tuple[Any, ...]]:
            self.execute_count += 1
            return [(f"ashare:chunk:{self.execute_count}", self.execute_count, None, None)]

    session = FakeSession()

    coverage = collect_base_data._fetch_ashare_bar_coverage(
        session,
        [f"ashare:{index:06d}" for index in range(1201)],
        timeframe="1d",
    )

    assert session.execute_count == 3
    assert coverage["ashare:chunk:1"] == (1, None, None)
    assert coverage["ashare:chunk:3"] == (3, None, None)


def test_batch_ashare_symbols_does_not_fallback_single_symbol_when_full_history_selection_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全量历史 K 线筛选失败时应让任务失败，而不是降级为单只 000001 后误报完成。"""

    collect_base_data = import_collection_module()

    class FakeAssetRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def find_by_market(self, market: str, *, only_tradable: bool = True) -> list[Any]:
            assert market == "ashare"
            assert only_tradable is True
            return [
                Namespace(asset_id="ashare:000001", symbol="000001"),
                Namespace(asset_id="ashare:600519", symbol="600519"),
            ]

    monkeypatch.setattr(collect_base_data, "AssetRepository", FakeAssetRepository)
    monkeypatch.setattr(
        collect_base_data,
        "_fetch_ashare_bar_coverage",
        lambda session, asset_ids, timeframe: (_ for _ in ()).throw(RuntimeError("coverage boom")),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="A 股 K 线补采标的筛选失败"):
        collect_base_data.batch_ashare_symbols(
            object(),
            fallback_symbol="000001",
            only_failed_or_stale=True,
        )


def test_batch_ashare_symbols_skips_assets_with_matching_success_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功水位已覆盖本次请求区间时，即使上市日晚于起始日也应视为可断点跳过。"""

    collect_base_data = import_collection_module()
    request_start = datetime(2016, 6, 1, tzinfo=UTC)
    request_end = datetime(2026, 6, 5, tzinfo=UTC)

    class FakeAssetRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def find_by_market(self, market: str, *, only_tradable: bool = True) -> list[Any]:
            assert market == "ashare"
            return [Namespace(asset_id="ashare:603507", symbol="603507")]

    monkeypatch.setattr(collect_base_data, "AssetRepository", FakeAssetRepository)
    monkeypatch.setattr(
        collect_base_data,
        "_fetch_ashare_bar_coverage",
        lambda session, asset_ids, timeframe: {
            "ashare:603507": (1600, datetime(2017, 1, 3, tzinfo=UTC), request_end)
        },
        raising=False,
    )
    monkeypatch.setattr(
        collect_base_data,
        "_fetch_data_sync_watermarks",
        lambda session, asset_ids, data_domain, provider, timeframe: {
            "ashare:603507": Namespace(
                status="available",
                next_retry_at=None,
                payload={
                    "sync_task_type": "market_bars_full_history_backfill",
                    "requested_start": "20160601",
                    "requested_end": "20260605",
                },
            )
        },
        raising=False,
    )

    symbols = collect_base_data.batch_ashare_symbols(
        object(),
        fallback_symbol="000001",
        only_failed_or_stale=True,
        required_start_at=request_start,
        required_end_at=request_end,
    )

    assert symbols == []


def test_resolve_ashare_collection_symbols_uses_resume_filter_for_full_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全量历史 K 线初始化应自动启用断点筛选，并把请求区间传给标的选择器。"""

    collect_base_data = import_collection_module()
    captured: dict[str, Any] = {}

    def fake_batch_ashare_symbols(session: Any, **kwargs: Any) -> list[str]:
        captured.update(kwargs)
        return ["000001"]

    monkeypatch.setattr(collect_base_data, "batch_ashare_symbols", fake_batch_ashare_symbols)

    args = collect_base_data.default_collection_args(
        group=["ashare-p0"],
        sync_task_type="market_bars_full_history_backfill",
        symbol_source="market_assets",
        ashare_start="20160601",
        ashare_end="20260605",
    )

    assert collect_base_data.resolve_ashare_collection_symbols(object(), args) == ["000001"]
    assert captured["only_failed_or_stale"] is True
    assert captured["required_start_at"] == datetime(2016, 6, 1, tzinfo=UTC)
    assert captured["required_end_at"] == datetime(2026, 6, 5, tzinfo=UTC)


def test_resolve_ashare_collection_symbols_uses_last_trading_day_for_full_history_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全量历史 K 线断点筛选应把自然日结束日校准到最后交易日，避免周末当天永远缺 K 线。"""

    collect_base_data = import_collection_module()
    captured: dict[str, Any] = {}

    def fake_batch_ashare_symbols(session: Any, **kwargs: Any) -> list[str]:
        captured.update(kwargs)
        return ["000001"]

    monkeypatch.setattr(collect_base_data, "batch_ashare_symbols", fake_batch_ashare_symbols)
    monkeypatch.setattr(
        collect_base_data,
        "latest_ashare_trading_datetime",
        lambda session, end_at: datetime(2026, 6, 5, tzinfo=UTC),
        raising=False,
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p0"],
        sync_task_type="market_bars_full_history_backfill",
        symbol_source="market_assets",
        ashare_start="20160608",
        ashare_end="20260606",
    )

    assert collect_base_data.resolve_ashare_collection_symbols(object(), args) == ["000001"]
    assert captured["required_end_at"] == datetime(2026, 6, 5, tzinfo=UTC)


def test_resolve_ashare_collection_symbols_uses_gap_filter_for_close_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """收盘最终日 K 应按最新交易日缺口筛选标的，避免每次手动执行全市场重跑。"""

    collect_base_data = import_collection_module()
    captured: dict[str, Any] = {}

    def fake_batch_ashare_symbols(session: Any, **kwargs: Any) -> list[str]:
        captured.update(kwargs)
        return ["000001"]

    monkeypatch.setattr(collect_base_data, "batch_ashare_symbols", fake_batch_ashare_symbols)
    monkeypatch.setattr(
        collect_base_data,
        "latest_ashare_trading_datetime",
        lambda session, end_at: datetime(2026, 6, 5, tzinfo=UTC),
        raising=False,
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p0"],
        sync_task_type="market_bars_close_final",
        symbol_source="market_assets",
        ashare_start="20251208",
        ashare_end="20260607",
        only_failed_or_stale=True,
    )

    assert collect_base_data.resolve_ashare_collection_symbols(object(), args) == ["000001"]
    assert captured["only_failed_or_stale"] is True
    assert captured["required_start_at"] == datetime(2025, 12, 8, tzinfo=UTC)
    assert captured["required_end_at"] == datetime(2026, 6, 5, tzinfo=UTC)


def test_ashare_kline_source_gate_uses_queue_workers_without_source_limiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股 K 线有效并发由任务队列 worker 控制，源优先级和降级保留在 provider 内部。"""

    collect_base_data = import_collection_module()

    def fail_if_called(source_key: str, collect: Any) -> Any:
        raise AssertionError(f"source limiter should not gate A-share K-line: {source_key}")

    monkeypatch.setattr(collect_base_data, "run_rate_limited_collection", fail_if_called)

    assert collect_base_data.ashare_kline_source_gate("eastmoney_kline", lambda: "ok") == "ok"


def test_record_ashare_market_bar_watermark_records_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股 K 线采集成功后应以库内最新 K 线时间更新采集水位。"""

    collect_base_data = import_collection_module()
    now = datetime(2026, 6, 4, 10, 0, tzinfo=UTC)
    latest_bar_at = datetime(2026, 6, 3, 15, 0, tzinfo=UTC)
    calls: list[dict[str, Any]] = []

    class FakeWatermarkRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def record_success(self, **kwargs: Any) -> None:
            calls.append({"method": "success", **kwargs})

        def record_failure(self, **kwargs: Any) -> None:
            calls.append({"method": "failure", **kwargs})

    monkeypatch.setattr(collect_base_data, "DataSyncWatermarkRepository", FakeWatermarkRepository)
    monkeypatch.setattr(
        collect_base_data,
        "_fetch_ashare_bar_coverage",
        lambda session, asset_ids, timeframe: {"ashare:600519": (117, latest_bar_at)},
        raising=False,
    )

    collect_base_data.record_ashare_market_bar_watermark(
        object(),
        symbol="600519",
        timeframe="1d",
        result=Namespace(status="available", item_count=117, error_message=None, payload={}),
        occurred_at=now,
    )

    assert calls == [
        {
            "method": "success",
            "asset_id": "ashare:600519",
            "symbol": "600519",
            "market": "ashare",
            "data_domain": "market_bars",
            "provider": "akshare:stock_zh_a_hist_tx",
            "timeframe": "1d",
            "watermark_at": latest_bar_at,
            "occurred_at": now,
            "payload": {"item_count": 117, "latest_bar_count": 117},
        }
    ]


def test_record_ashare_market_bar_watermark_keeps_requested_window_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功水位应保留本次请求窗口，供新股等实际起始日晚于 10 年起点的标的断点跳过。"""

    collect_base_data = import_collection_module()
    now = datetime(2026, 6, 4, 10, 0, tzinfo=UTC)
    earliest_bar_at = datetime(2018, 1, 3, tzinfo=UTC)
    latest_bar_at = datetime(2026, 6, 3, 15, 0, tzinfo=UTC)
    calls: list[dict[str, Any]] = []

    class FakeWatermarkRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def record_success(self, **kwargs: Any) -> None:
            calls.append({"method": "success", **kwargs})

        def record_failure(self, **kwargs: Any) -> None:
            calls.append({"method": "failure", **kwargs})

    monkeypatch.setattr(collect_base_data, "DataSyncWatermarkRepository", FakeWatermarkRepository)
    monkeypatch.setattr(
        collect_base_data,
        "_fetch_ashare_bar_coverage",
        lambda session, asset_ids, timeframe: {
            "ashare:603507": (1600, earliest_bar_at, latest_bar_at)
        },
        raising=False,
    )

    collect_base_data.record_ashare_market_bar_watermark(
        object(),
        symbol="603507",
        timeframe="1d",
        result=Namespace(
            status="available",
            item_count=1600,
            error_message=None,
            payload={
                "sync_task_type": "market_bars_full_history_backfill",
                "requested_start": "20160601",
                "requested_end": "20260605",
            },
        ),
        occurred_at=now,
    )

    assert calls[0]["payload"] == {
        "item_count": 1600,
        "latest_bar_count": 1600,
        "sync_task_type": "market_bars_full_history_backfill",
        "requested_start": "20160601",
        "requested_end": "20260605",
    }


def test_record_ashare_market_bar_watermark_records_network_failure_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股 K 线网络失败应写入失败水位和下次重试时间，而不是反复打同一源。"""

    collect_base_data = import_collection_module()
    now = datetime(2026, 6, 4, 10, 0, tzinfo=UTC)
    calls: list[dict[str, Any]] = []

    class FakeWatermarkRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def record_success(self, **kwargs: Any) -> None:
            calls.append({"method": "success", **kwargs})

        def record_failure(self, **kwargs: Any) -> None:
            calls.append({"method": "failure", **kwargs})

    monkeypatch.setattr(collect_base_data, "DataSyncWatermarkRepository", FakeWatermarkRepository)

    collect_base_data.record_ashare_market_bar_watermark(
        object(),
        symbol="301611",
        timeframe="1d",
        result=Namespace(
            status="error",
            item_count=0,
            error_message="Failed to perform, curl: (56) Connection closed abruptly",
            payload={},
        ),
        occurred_at=now,
    )

    assert calls == [
        {
            "method": "failure",
            "asset_id": "ashare:301611",
            "symbol": "301611",
            "market": "ashare",
            "data_domain": "market_bars",
            "provider": "akshare:stock_zh_a_hist_tx",
            "timeframe": "1d",
            "occurred_at": now,
            "retry_after": timedelta(minutes=15),
            "error_message": "Failed to perform, curl: (56) Connection closed abruptly",
            "payload": {"status": "error", "item_count": 0},
        }
    ]


def test_record_ashare_market_bar_watermark_can_skip_failure_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全量历史 K 线初始化失败只记录失败，不自动安排待重试时间。"""

    collect_base_data = import_collection_module()
    now = datetime(2026, 6, 4, 10, 0, tzinfo=UTC)
    calls: list[dict[str, Any]] = []

    class FakeWatermarkRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def record_success(self, **kwargs: Any) -> None:
            calls.append({"method": "success", **kwargs})

        def record_failure(self, **kwargs: Any) -> None:
            calls.append({"method": "failure", **kwargs})

    monkeypatch.setattr(collect_base_data, "DataSyncWatermarkRepository", FakeWatermarkRepository)

    collect_base_data.record_ashare_market_bar_watermark(
        object(),
        symbol="301611",
        timeframe="1d",
        result=Namespace(
            status="error",
            item_count=0,
            error_message="Failed to perform, curl: (56) Connection closed abruptly",
            payload={},
        ),
        occurred_at=now,
        schedule_retry=False,
    )

    assert calls == [
        {
            "method": "failure",
            "asset_id": "ashare:301611",
            "symbol": "301611",
            "market": "ashare",
            "data_domain": "market_bars",
            "provider": "akshare:stock_zh_a_hist_tx",
            "timeframe": "1d",
            "occurred_at": now,
            "retry_after": None,
            "error_message": "Failed to perform, curl: (56) Connection closed abruptly",
            "payload": {"status": "error", "item_count": 0},
        }
    ]


def test_attach_ashare_market_bar_retry_payload_can_skip_retry_metadata() -> None:
    """全量历史 K 线初始化失败日志保留分类，但不展示待重试倒计时。"""

    collect_base_data = import_collection_module()
    now = datetime(2026, 6, 4, 10, 0, tzinfo=UTC)

    result = collect_base_data.attach_ashare_market_bar_retry_payload(
        Namespace(
            status="error",
            item_count=0,
            error_message="Failed to perform, curl: (56) Connection closed abruptly",
            payload={},
        ),
        occurred_at=now,
        schedule_retry=False,
    )

    assert result.payload["provider_key"] == "akshare:stock_zh_a_hist_tx"
    assert result.payload["error_category"] == "network"
    assert "retry_after_seconds" not in result.payload
    assert "next_retry_at" not in result.payload


def test_record_ashare_market_bar_watermark_rolls_back_aborted_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """水位写入遇到数据库事务异常时，应回滚并跳过，不能继续打挂整轮 K 线任务。"""

    collect_base_data = import_collection_module()
    now = datetime(2026, 6, 4, 10, 0, tzinfo=UTC)

    class FakeSession:
        def __init__(self) -> None:
            self.rolled_back = False

        def rollback(self) -> None:
            self.rolled_back = True

    class FailingWatermarkRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def record_failure(self, **kwargs: Any) -> None:
            raise SQLAlchemyError("current transaction is aborted")

    session = FakeSession()
    monkeypatch.setattr(collect_base_data, "DataSyncWatermarkRepository", FailingWatermarkRepository)

    collect_base_data.record_ashare_market_bar_watermark(
        session,
        symbol="001208",
        timeframe="1d",
        result=Namespace(
            status="error",
            item_count=0,
            error_message="curl: (56) Connection closed abruptly",
            payload={},
        ),
        occurred_at=now,
    )

    assert session.rolled_back is True


def test_record_ashare_market_bar_watermark_uses_isolated_session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """调度提供 session_factory 时，K 线水位应使用独立短事务写入，避免污染主批次事务。"""

    collect_base_data = import_collection_module()
    now = datetime(2026, 6, 4, 10, 0, tzinfo=UTC)
    calls: list[dict[str, Any]] = []

    class MainSession:
        pass

    class WatermarkSession:
        def __init__(self) -> None:
            self.committed = False
            self.closed = False

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            raise AssertionError("成功路径不应回滚独立水位事务")

        def close(self) -> None:
            self.closed = True

    watermark_session = WatermarkSession()

    class FakeWatermarkRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def record_failure(self, **kwargs: Any) -> None:
            calls.append({"session": self.session, **kwargs})

    monkeypatch.setattr(collect_base_data, "DataSyncWatermarkRepository", FakeWatermarkRepository)

    collect_base_data.record_ashare_market_bar_watermark(
        MainSession(),
        symbol="001208",
        timeframe="1d",
        result=Namespace(
            status="error",
            item_count=0,
            error_message="curl: (56) Connection closed abruptly",
            payload={},
        ),
        occurred_at=now,
        session_factory=lambda: watermark_session,
    )

    assert calls[0]["session"] is watermark_session
    assert calls[0]["asset_id"] == "ashare:001208"
    assert watermark_session.committed is True
    assert watermark_session.closed is True


def test_ashare_market_bars_backfill_updates_watermarks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股 K 线批量补采应在每个标的完成后同步更新采集水位。"""

    collect_base_data = import_collection_module()
    latest_bar_at = datetime(2026, 6, 3, 15, 0, tzinfo=UTC)
    calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            if task != "ashare_p0_ohlcv":
                return Namespace(
                    task=task,
                    status="planned",
                    raw_record_id=None,
                    item_count=0,
                    error_message=None,
                    payload={},
                )
            if parameters["symbol"] == "600519":
                return Namespace(
                    task=task,
                    status="available",
                    raw_record_id="raw:600519",
                    item_count=117,
                    error_message=None,
                    payload={},
                )
            return Namespace(
                task=task,
                status="error",
                raw_record_id="raw:301611",
                item_count=0,
                error_message="curl: (56) Connection closed abruptly",
                payload={},
            )

    class FakeWatermarkRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def record_success(self, **kwargs: Any) -> None:
            calls.append({"method": "success", **kwargs})

        def record_failure(self, **kwargs: Any) -> None:
            calls.append({"method": "failure", **kwargs})

    monkeypatch.setattr(
        collect_base_data,
        "batch_ashare_symbols",
        lambda session, limit, fallback_symbol, **kwargs: ["600519", "301611"],
    )
    monkeypatch.setattr(
        collect_base_data,
        "should_refresh_asset_universe_before_incremental",
        lambda session, market: False,
    )
    monkeypatch.setattr(collect_base_data, "DataSyncWatermarkRepository", FakeWatermarkRepository)
    monkeypatch.setattr(
        collect_base_data,
        "_fetch_ashare_bar_coverage",
        lambda session, asset_ids, timeframe: {"ashare:600519": (117, latest_bar_at)},
        raising=False,
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p0"],
        sync_task_type="market_bars_backfill",
        symbol_source="market_assets",
        limit=2,
        batch_size=2,
    )

    collect_base_data.run_ashare_p0(object(), args, RecordingRuntime())

    assert [call["method"] for call in calls] == ["success", "failure"]
    assert calls[0]["asset_id"] == "ashare:600519"
    assert calls[0]["watermark_at"] == latest_bar_at
    assert calls[1]["asset_id"] == "ashare:301611"
    assert calls[1]["retry_after"] == timedelta(minutes=15)


def test_ashare_market_bars_backfill_uses_current_session_for_single_worker_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单 worker 写入仍在当前事务中，水位记录应复用当前 session 才能读到最新 K 线。"""

    collect_base_data = import_collection_module()
    watermark_calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            if task != "ashare_p0_ohlcv":
                return Namespace(
                    task=task,
                    status="planned",
                    raw_record_id=None,
                    item_count=0,
                    error_message=None,
                    payload={},
                )
            return Namespace(
                task=task,
                status="available",
                raw_record_id="raw:600519",
                item_count=117,
                error_message=None,
                payload={},
            )

    def fake_record_watermark(session: Any, **kwargs: Any) -> None:
        watermark_calls.append({"session": session, **kwargs})

    main_session = object()
    monkeypatch.setattr(
        collect_base_data,
        "batch_ashare_symbols",
        lambda session, limit, fallback_symbol, **kwargs: ["600519"],
    )
    monkeypatch.setattr(
        collect_base_data,
        "should_refresh_asset_universe_before_incremental",
        lambda session, market: False,
    )
    monkeypatch.setattr(
        collect_base_data,
        "record_ashare_market_bar_watermark",
        fake_record_watermark,
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p0"],
        sync_task_type="market_bars_backfill",
        symbol_source="market_assets",
        limit=1,
        batch_size=1,
        max_workers=1,
    )

    collect_base_data.run_ashare_p0(
        main_session,
        args,
        RecordingRuntime(),
        session_factory=lambda: object(),
    )

    assert watermark_calls[0]["session"] is main_session
    assert watermark_calls[0]["session_factory"] is None


def test_resolve_ashare_collection_symbols_keeps_full_backfill_universe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """market_assets 模式下 limit 表示单标的拉取条数，不应截断资产全集。"""

    collect_base_data = import_collection_module()

    class FakeAssetRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def find_by_market(self, market: str, *, only_tradable: bool = True) -> list[Any]:
            assert market == "ashare"
            return [
                Namespace(asset_id="ashare:110067", symbol="110067"),
                Namespace(asset_id="ashare:900001", symbol="900001"),
                Namespace(asset_id="ashare:159001", symbol="159001"),
            ] + [
                Namespace(asset_id=f"ashare:00000{index}", symbol=f"00000{index}")
                for index in range(1, 6)
            ]

    monkeypatch.setattr(collect_base_data, "AssetRepository", FakeAssetRepository)
    monkeypatch.setattr(
        collect_base_data,
        "_fetch_ashare_bar_coverage",
        lambda session, asset_ids, timeframe: {},
        raising=False,
    )
    args = collect_base_data.default_collection_args(
        sync_task_type="market_bars_backfill",
        symbol_source="market_assets",
        limit=2,
    )

    symbols = collect_base_data.resolve_ashare_collection_symbols(object(), args)

    assert symbols == ["000001", "000002", "000003", "000004", "000005"]


def test_ashare_market_bars_backfill_commits_after_each_symbol_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """长时间全市场补采应每批提交一次，避免超时后整轮写入回滚。"""

    collect_base_data = import_collection_module()

    class FakeSession:
        def __init__(self) -> None:
            self.commit_count = 0

        def commit(self) -> None:
            self.commit_count += 1

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data,
        "batch_ashare_symbols",
        lambda session, limit, fallback_symbol, **kwargs: [
            "000001",
            "000002",
            "000003",
            "000004",
            "000005",
        ],
    )
    monkeypatch.setattr(
        collect_base_data,
        "should_refresh_asset_universe_before_incremental",
        lambda session, market: False,
    )

    session = FakeSession()
    args = collect_base_data.default_collection_args(
        group=["ashare-p0"],
        sync_task_type="market_bars_backfill",
        symbol_source="market_assets",
        limit=2,
        batch_size=2,
    )

    collect_base_data.run_ashare_p0(session, args, RecordingRuntime())

    assert session.commit_count == 3


def test_ashare_fundamentals_run_all_market_assets_in_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股基本面和估值刷新应按完整资产池分批跑完。"""

    collect_base_data = import_collection_module()

    calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append({"task": task, "parameters": parameters})
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data,
        "batch_ashare_fundamental_symbols",
        lambda session, limit, fallback_symbol, **kwargs: [
            "000001",
            "000002",
            "000003",
            "000004",
            "000005",
        ],
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p2"],
        sync_task_type="fundamental_refresh",
        symbol_source="market_assets",
        limit=2,
        batch_size=2,
    )

    results = collect_base_data.run_ashare_p2(object(), args, RecordingRuntime())

    per_symbol_calls = [
        item
        for item in calls
        if item["task"] in {"ashare_p2_financial_indicators", "ashare_p2_valuation"}
    ]
    assert [item["parameters"]["symbol"] for item in per_symbol_calls] == [
        "000001",
        "000001",
        "000002",
        "000002",
        "000003",
        "000003",
        "000004",
        "000004",
        "000005",
        "000005",
    ]
    assert [
        item.payload.get("batch_index")
        for item in results
        if item.task == "ashare_p2_valuation"
    ] == [
        1,
        1,
        2,
        2,
        3,
    ]


def test_batch_ashare_fundamental_symbols_uses_watermarks_for_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """基本面增量刷新应跳过财务和估值都已成功且未过期的标的。"""

    collect_base_data = import_collection_module()
    stale_before = datetime(2026, 6, 1, tzinfo=UTC)

    class FakeAssetRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def find_by_market(self, market: str, *, only_tradable: bool = True) -> list[Any]:
            assert market == "ashare"
            return [
                Namespace(asset_id="ashare:000001", symbol="000001"),
                Namespace(asset_id="ashare:600519", symbol="600519"),
                Namespace(asset_id="ashare:002594", symbol="002594"),
            ]

    def fake_watermarks(
        session: Any,
        asset_ids: list[str],
        data_domain: str,
        provider: str,
        timeframe: str,
    ) -> dict[str, Any]:
        if data_domain == "fundamentals":
            return {
                "ashare:000001": Namespace(status="available", watermark_at=datetime(2026, 6, 4, tzinfo=UTC)),
                "ashare:600519": Namespace(status="available", watermark_at=datetime(2026, 6, 4, tzinfo=UTC)),
            }
        if data_domain == "valuation":
            return {
                "ashare:000001": Namespace(status="available", watermark_at=datetime(2026, 6, 4, tzinfo=UTC)),
                "ashare:600519": Namespace(status="error", next_retry_at=datetime(2026, 5, 31, tzinfo=UTC)),
            }
        return {}

    monkeypatch.setattr(collect_base_data, "AssetRepository", FakeAssetRepository)
    monkeypatch.setattr(collect_base_data, "_fetch_data_sync_watermarks", fake_watermarks)

    symbols = collect_base_data.batch_ashare_fundamental_symbols(
        object(),
        fallback_symbol="000001",
        only_failed_or_stale=True,
        stale_before=stale_before,
    )

    assert symbols == ["002594", "600519"]


def test_record_ashare_fundamental_watermark_records_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单股票财务/估值结果应写入独立水位，支持后续断点续跑和失败补跑。"""

    collect_base_data = import_collection_module()
    calls: list[dict[str, Any]] = []

    class FakeWatermarkRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def record_success(self, **kwargs: Any) -> None:
            calls.append({"kind": "success", **kwargs})

        def record_failure(self, **kwargs: Any) -> None:
            calls.append({"kind": "failure", **kwargs})

    monkeypatch.setattr(collect_base_data, "DataSyncWatermarkRepository", FakeWatermarkRepository)

    collect_base_data.record_ashare_fundamental_watermark(
        object(),
        symbol="000001",
        data_domain="fundamentals",
        provider="stock_financial_analysis_indicator_em",
        result=Namespace(status="available", item_count=3, payload={"actual_source": "akshare"}),
    )
    collect_base_data.record_ashare_fundamental_watermark(
        object(),
        symbol="000001",
        data_domain="valuation",
        provider="stock_value_em",
        result=Namespace(status="error", item_count=0, error_message="timeout", payload={}),
    )

    assert calls[0]["kind"] == "success"
    assert calls[0]["asset_id"] == "ashare:000001"
    assert calls[0]["data_domain"] == "fundamentals"
    assert calls[0]["payload"]["item_count"] == 3
    assert calls[1]["kind"] == "failure"
    assert calls[1]["data_domain"] == "valuation"
    assert calls[1]["error_message"] == "timeout"
    assert calls[1]["retry_after"] == timedelta(minutes=15)


def test_ashare_fundamentals_records_symbol_watermarks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """基本面批量任务应在单股票财务和估值完成后分别写入水位。"""

    collect_base_data = import_collection_module()
    watermark_calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            status = "error" if task == "ashare_p2_valuation" else "available"
            return Namespace(
                task=task,
                status=status,
                raw_record_id=None,
                item_count=1 if status == "available" else 0,
                error_message="valuation timeout" if status == "error" else None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data,
        "batch_ashare_fundamental_symbols",
        lambda session, limit, fallback_symbol, **kwargs: ["000001"],
    )
    monkeypatch.setattr(
        collect_base_data,
        "record_ashare_fundamental_watermark",
        lambda session, **kwargs: watermark_calls.append(kwargs),
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p2"],
        sync_task_type="fundamental_refresh",
        symbol_source="market_assets",
        batch_size=1,
    )

    collect_base_data.run_ashare_p2(object(), args, RecordingRuntime())

    assert [
        (call["symbol"], call["data_domain"], call["provider"], call["result"].status)
        for call in watermark_calls
    ] == [
        ("000001", "fundamentals", "stock_financial_analysis_indicator_em", "available"),
        ("000001", "valuation", "stock_value_em", "error"),
    ]


def test_ashare_fundamentals_does_not_fallback_to_single_symbol_when_universe_locked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """资产池刷新被锁住时，基本面任务应等待下一轮，而不是静默退化为只同步默认股票。"""

    collect_base_data = import_collection_module()

    calls: list[str] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append(task)
            if task == "ashare_p0_assets":
                return Namespace(
                    task=task,
                    status="locked",
                    raw_record_id=None,
                    item_count=0,
                    error_message="同参数采集任务正在运行",
                    payload={},
                )
            raise AssertionError(f"资产池未就绪时不应继续执行 {task}")

    monkeypatch.setattr(
        collect_base_data,
        "should_refresh_asset_universe_before_incremental",
        lambda session, market: True,
    )
    monkeypatch.setattr(
        collect_base_data,
        "batch_ashare_symbols",
        lambda session, limit, fallback_symbol, **kwargs: [fallback_symbol],
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p2"],
        sync_task_type="fundamental_refresh",
        symbol_source="market_assets",
        ashare_symbol="000001",
    )

    results = collect_base_data.run_ashare_p2(object(), args, RecordingRuntime())

    assert calls == ["ashare_p0_assets"]
    assert [result.task for result in results] == ["ashare_p0_assets"]
    assert results[0].status == "locked"


def test_crypto_market_bars_backfill_registers_one_task_per_market_asset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """数字货币 K 线补采应按市场资产批量注册任务，而不是固定只采 BTCUSDT。"""

    collect_base_data = import_collection_module()

    calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append(
                {
                    "task": task,
                    "provider_key": provider_key,
                    "parameters": parameters,
                    "force": force,
                }
            )
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data,
        "batch_crypto_symbols",
        lambda session, market, timeframe, limit, fallback_symbol: ["BTCUSDT", "ETHUSDT"],
        raising=False,
    )

    args = collect_base_data.default_collection_args(
        group=["crypto"],
        sync_task_type="market_bars_backfill",
        symbol_source="market_assets",
        crypto_market_type="spot",
        crypto_timeframe="1h",
        limit=2,
    )

    collect_base_data.run_crypto(object(), args, RecordingRuntime())

    ohlcv_calls = [item for item in calls if item["task"] == "crypto_ohlcv"]
    assert [item["parameters"]["symbol"] for item in ohlcv_calls] == ["BTCUSDT", "ETHUSDT"]
    assert [item["provider_key"] for item in ohlcv_calls] == [
        "ccxt_binance_fetch_ohlcv:spot:BTCUSDT",
        "ccxt_binance_fetch_ohlcv:spot:ETHUSDT",
    ]


def test_crypto_universe_refresh_fetches_complete_market_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Binance Universe 刷新应拉完整交易对列表，不能被调度批大小截断。"""

    collect_base_data = import_collection_module()

    collect_limits: list[int | None] = []
    task_parameters: list[dict[str, Any]] = []

    def fake_collect_markets(self: Any, **kwargs: Any) -> Any:
        collect_limits.append(kwargs["limit"])
        return Namespace()

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            if task == "crypto_markets":
                task_parameters.append(parameters)
                collect()
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data.CryptoDataCollector,
        "collect_markets",
        fake_collect_markets,
    )

    args = collect_base_data.default_collection_args(
        group=["crypto"],
        sync_task_type="universe_refresh",
        crypto_market_type="spot",
        limit=2,
        batch_size=2,
    )

    collect_base_data.run_crypto(object(), args, RecordingRuntime())

    assert collect_limits == [None]
    assert task_parameters == [{"market_type": "spot", "limit": None}]


def test_parse_collect_base_data_args_includes_crypto_lookback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """命令行入口应给数字货币日历任务提供 lookback，避免 universe_refresh 进入采集前崩溃。"""

    collect_base_data = import_collection_module()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "collect_base_data.py",
            "--group",
            "crypto",
            "--sync-task-type",
            "universe_refresh",
            "--crypto-market-type",
            "spot",
        ],
    )

    args = collect_base_data.parse_args()

    assert hasattr(args, "lookback")
    assert args.lookback is None


def test_resolve_crypto_collection_symbols_enables_recent_gap_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crypto 小时线任务应把 lookback 和单次条数传给缺口筛选，避免每轮全量轮转。"""

    collect_base_data = import_collection_module()
    captured: dict[str, Any] = {}

    def fake_batch_crypto_symbols(session: Any, **kwargs: Any) -> list[str]:
        captured.update(kwargs)
        return ["BTCUSDT"]

    monkeypatch.setattr(collect_base_data, "batch_crypto_symbols", fake_batch_crypto_symbols)

    args = collect_base_data.default_collection_args(
        group=["crypto"],
        sync_task_type="market_bars_backfill",
        symbol_source="market_assets",
        crypto_market_type="spot",
        crypto_timeframe="1h",
        crypto_symbol="BTCUSDT",
        lookback="168h",
        limit=150,
        only_failed_or_stale=True,
    )

    symbols = collect_base_data.resolve_crypto_collection_symbols(
        object(),
        args,
        market="crypto_spot",
    )

    assert symbols == ["BTCUSDT"]
    assert captured["only_failed_or_stale"] is True
    assert captured["min_bar_count"] == 150
    assert captured["stale_before"] is not None


def test_batch_crypto_symbols_skips_recently_covered_assets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crypto 小时线筛选应跳过最近窗口已覆盖且条数足够的交易对。"""

    collect_base_data = import_collection_module()

    assets = [
        Namespace(asset_id="crypto_spot:BTCUSDT", symbol="BTCUSDT"),
        Namespace(asset_id="crypto_spot:ETHUSDT", symbol="ETHUSDT"),
        Namespace(asset_id="crypto_spot:SOLUSDT", symbol="SOLUSDT"),
    ]

    class FakeAssetRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def find_by_market(self, market: str) -> list[Any]:
            assert market == "crypto_spot"
            return assets

    coverage = {
        "crypto_spot:BTCUSDT": (180, datetime(2026, 6, 14, 3, 0, tzinfo=UTC)),
        "crypto_spot:ETHUSDT": (20, datetime(2026, 6, 13, 3, 0, tzinfo=UTC)),
    }

    monkeypatch.setattr(collect_base_data, "AssetRepository", FakeAssetRepository)
    monkeypatch.setattr(
        collect_base_data,
        "_fetch_crypto_bar_coverage",
        lambda session, asset_ids, *, timeframe, market: coverage,
    )

    symbols = collect_base_data.batch_crypto_symbols(
        object(),
        market="crypto_spot",
        timeframe="1h",
        fallback_symbol="BTCUSDT",
        only_failed_or_stale=True,
        stale_before=datetime(2026, 6, 14, 0, 0, tzinfo=UTC),
        min_bar_count=150,
    )

    assert symbols == ["SOLUSDT", "ETHUSDT"]


def test_crypto_market_bars_backfill_runs_all_market_assets_in_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """数字货币 K 线补采的一次调度应按批跑完整个待补交易对集合。"""

    collect_base_data = import_collection_module()

    calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append(
                {
                    "task": task,
                    "provider_key": provider_key,
                    "parameters": parameters,
                    "force": force,
                }
            )
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data,
        "batch_crypto_symbols",
        lambda session, market, timeframe, limit, fallback_symbol: [
            "BTCUSDT",
            "ETHUSDT",
            "SOLUSDT",
            "BNBUSDT",
        ],
        raising=False,
    )

    args = collect_base_data.default_collection_args(
        group=["crypto"],
        sync_task_type="market_bars_backfill",
        symbol_source="market_assets",
        crypto_market_type="spot",
        crypto_timeframe="1h",
        limit=2,
        batch_size=2,
    )

    results = collect_base_data.run_crypto(object(), args, RecordingRuntime())

    ohlcv_calls = [item for item in calls if item["task"] == "crypto_ohlcv"]
    assert [item["parameters"]["symbol"] for item in ohlcv_calls] == [
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "BNBUSDT",
    ]
    assert [item["parameters"]["limit"] for item in ohlcv_calls] == [2, 2, 2, 2]
    assert [item.payload.get("batch_index") for item in results if item.task == "crypto_ohlcv"] == [
        1,
        1,
        2,
        2,
    ]
    assert [item.payload.get("batch_count") for item in results if item.task == "crypto_ohlcv"] == [
        2,
        2,
        2,
        2,
    ]


def test_crypto_market_bars_records_symbol_watermarks_and_skips_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crypto K 线应按交易对写水位，并只跳过仍在失败冷却期的交易对。"""

    collect_base_data = import_collection_module()
    run_calls: list[str] = []
    watermark_calls: list[dict[str, Any]] = []
    next_retry_at = datetime.now(tz=UTC) + timedelta(minutes=10)

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            if task == "crypto_ohlcv":
                run_calls.append(parameters["symbol"])
            return Namespace(
                task=task,
                status="available",
                raw_record_id=f"raw:{parameters.get('symbol', task)}",
                item_count=2,
                error_message=None,
                payload={},
            )

    def fake_watermarks(
        session: Any,
        asset_ids: list[str],
        *,
        data_domain: str,
        provider: str,
        timeframe: str,
    ) -> dict[str, Any]:
        assert data_domain == collect_base_data.CRYPTO_MARKET_BAR_DATA_DOMAIN
        assert provider == collect_base_data.CRYPTO_MARKET_BAR_PROVIDER
        assert timeframe == "1h"
        return {
            "crypto_spot:ETHUSDT": Namespace(status="error", next_retry_at=next_retry_at),
        }

    monkeypatch.setattr(
        collect_base_data,
        "batch_crypto_symbols",
        lambda session, market, timeframe, limit, fallback_symbol: ["BTCUSDT", "ETHUSDT"],
        raising=False,
    )
    monkeypatch.setattr(collect_base_data, "_fetch_data_sync_watermarks", fake_watermarks)
    monkeypatch.setattr(
        collect_base_data,
        "record_crypto_symbol_watermark",
        lambda session, **kwargs: watermark_calls.append(kwargs),
        raising=False,
    )

    args = collect_base_data.default_collection_args(
        group=["crypto"],
        sync_task_type="market_bars_backfill",
        symbol_source="market_assets",
        crypto_market_type="spot",
        crypto_timeframe="1h",
        batch_size=2,
    )

    results = collect_base_data.run_crypto(object(), args, RecordingRuntime())

    assert run_calls == ["BTCUSDT"]
    skipped = next(item for item in results if item.task == "crypto_ohlcv" and item.status == "skipped")
    assert skipped.payload["symbol"] == "ETHUSDT"
    assert "失败冷却期" in skipped.error_message
    assert [(call["symbol"], call["data_domain"], call["provider"]) for call in watermark_calls] == [
        ("BTCUSDT", collect_base_data.CRYPTO_MARKET_BAR_DATA_DOMAIN, collect_base_data.CRYPTO_MARKET_BAR_PROVIDER)
    ]


def test_crypto_derivatives_records_symbol_watermarks_and_uses_source_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crypto 衍生品快照应按交易对写水位，并通过 Binance native 源限流。"""

    collect_base_data = import_collection_module()
    source_gate_calls: list[str] = []
    watermark_calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            if task == "crypto_derivative_snapshot":
                collect()
            return Namespace(
                task=task,
                status="available",
                raw_record_id=f"raw:{parameters.get('symbol', task)}",
                item_count=1,
                error_message=None,
                payload={},
            )

    class FakeCollector:
        def __init__(self, session: Any) -> None:
            self.session = session

        def collect_derivative_snapshot(self, *, symbol: str) -> Any:
            return Namespace()

    monkeypatch.setattr(collect_base_data, "CryptoDataCollector", FakeCollector)
    monkeypatch.setattr(
        collect_base_data,
        "batch_crypto_derivative_symbols",
        lambda session, market, limit, fallback_symbol: ["BTCUSDT"],
        raising=False,
    )

    def fake_rate_limited(source_key: str, collect: Any) -> Any:
        source_gate_calls.append(source_key)
        return collect()

    monkeypatch.setattr(collect_base_data, "run_rate_limited_collection", fake_rate_limited)
    monkeypatch.setattr(
        collect_base_data,
        "record_crypto_symbol_watermark",
        lambda session, **kwargs: watermark_calls.append(kwargs),
        raising=False,
    )

    args = collect_base_data.default_collection_args(
        group=["crypto"],
        sync_task_type="derivative_refresh",
        symbol_source="market_assets",
        crypto_market_type="future",
        batch_size=1,
    )

    collect_base_data.run_crypto(object(), args, RecordingRuntime())

    assert source_gate_calls == ["binance_derivative_snapshot"]
    assert watermark_calls[0]["symbol"] == "BTCUSDT"
    assert watermark_calls[0]["market"] == "crypto_future"
    assert watermark_calls[0]["data_domain"] == collect_base_data.CRYPTO_DERIVATIVE_DATA_DOMAIN
    assert watermark_calls[0]["provider"] == collect_base_data.CRYPTO_DERIVATIVE_PROVIDER


def test_crypto_derivative_symbol_resolution_respects_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """衍生品快照按资产池展开时应遵守单次 limit，避免真实补采误跑全量。"""

    collect_base_data = import_collection_module()
    observed_limits: list[int | None] = []

    def fake_batch_symbols(
        session: Any,
        *,
        market: str,
        limit: int | None,
        fallback_symbol: str,
    ) -> list[str]:
        observed_limits.append(limit)
        return ["BTCUSDT", "ETHUSDT"]

    monkeypatch.setattr(collect_base_data, "batch_crypto_derivative_symbols", fake_batch_symbols)

    args = collect_base_data.default_collection_args(
        group=["crypto"],
        sync_task_type="derivative_refresh",
        symbol_source="market_assets",
        crypto_market_type="future",
        limit=2,
    )

    symbols = collect_base_data.resolve_crypto_derivative_collection_symbols(
        object(),
        args,
        market="crypto_future",
    )

    assert observed_limits == [2]
    assert symbols == ["BTCUSDT", "ETHUSDT"]


def test_record_crypto_symbol_watermark_records_success_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crypto 交易对水位应记录成功和失败，供 7x24 任务断点续跑。"""

    collect_base_data = import_collection_module()
    calls: list[dict[str, Any]] = []

    class FakeWatermarkRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def record_success(self, **kwargs: Any) -> None:
            calls.append({"kind": "success", **kwargs})

        def record_failure(self, **kwargs: Any) -> None:
            calls.append({"kind": "failure", **kwargs})

    monkeypatch.setattr(collect_base_data, "DataSyncWatermarkRepository", FakeWatermarkRepository)

    collect_base_data.record_crypto_symbol_watermark(
        object(),
        symbol="BTC/USDT",
        market="crypto_spot",
        data_domain="market_bars",
        provider="ccxt_binance_fetch_ohlcv",
        timeframe="1h",
        result=Namespace(status="available", item_count=2, payload={"actual_source": "ccxt"}),
    )
    collect_base_data.record_crypto_symbol_watermark(
        object(),
        symbol="ETHUSDT",
        market="crypto_future",
        data_domain="derivatives",
        provider="binance_derivative_snapshot",
        timeframe="future",
        result=Namespace(status="error", item_count=0, error_message="timeout", payload={}),
    )

    assert calls[0]["kind"] == "success"
    assert calls[0]["asset_id"] == "crypto_spot:BTCUSDT"
    assert calls[0]["timeframe"] == "1h"
    assert calls[0]["payload"]["item_count"] == 2
    assert calls[1]["kind"] == "failure"
    assert calls[1]["asset_id"] == "crypto_future:ETHUSDT"
    assert calls[1]["data_domain"] == "derivatives"
    assert calls[1]["retry_after"] == timedelta(minutes=15)


def test_crypto_derivative_refresh_runs_all_future_assets_in_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """合约衍生品快照应按完整合约资产池分批跑完。"""

    collect_base_data = import_collection_module()

    calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append(
                {
                    "task": task,
                    "provider_key": provider_key,
                    "parameters": parameters,
                }
            )
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data,
        "batch_crypto_derivative_symbols",
        lambda session, market, limit, fallback_symbol: [
            "BTCUSDT",
            "ETHUSDT",
            "SOLUSDT",
            "BNBUSDT",
        ],
        raising=False,
    )

    args = collect_base_data.default_collection_args(
        group=["crypto"],
        sync_task_type="derivative_refresh",
        symbol_source="market_assets",
        crypto_market_type="future",
        limit=2,
        batch_size=2,
    )

    results = collect_base_data.run_crypto(object(), args, RecordingRuntime())

    derivative_calls = [item for item in calls if item["task"] == "crypto_derivative_snapshot"]
    assert [item["parameters"]["symbol"] for item in derivative_calls] == [
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "BNBUSDT",
    ]
    assert [item["provider_key"] for item in derivative_calls] == [
        "binance_derivative_snapshot:future:BTCUSDT",
        "binance_derivative_snapshot:future:ETHUSDT",
        "binance_derivative_snapshot:future:SOLUSDT",
        "binance_derivative_snapshot:future:BNBUSDT",
    ]
    assert [
        item.payload.get("batch_index")
        for item in results
        if item.task == "crypto_derivative_snapshot"
    ] == [
        1,
        1,
        2,
        2,
    ]
