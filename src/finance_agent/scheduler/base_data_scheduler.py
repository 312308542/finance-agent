"""基础数据轻量调度器。

调度器只负责任务编排，不复制采集逻辑；实际采集仍复用
`scripts.data.collect_base_data` 中的分组入口。
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import multiprocessing
import os
import queue
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from finance_agent.cache import create_cache_client
from finance_agent.data.sync_config import (
    build_preset_config,
    export_scheduler_payload,
    load_data_sync_config,
)
from finance_agent.scheduler.admission import (
    AdmissionSnapshot,
    SchedulerAdmissionController,
)
from finance_agent.scheduler.base_data_progress import BaseDataTaskProgressRecorder
from finance_agent.scheduler.config_identity import (
    SchedulerConfigIdentity,
    load_config_identity,
)
from finance_agent.scheduler.persistent_scheduler_worker import PersistentSchedulerWorker
from finance_agent.scheduler.persistent_task_queue import PersistentTaskQueue, TaskClaim
from finance_agent.scheduler.planner import SchedulerPlanner
from finance_agent.scheduler.recovery_bridge import RecoverySchedulerMixin

JsonDict = dict[str, Any]
logger = logging.getLogger(__name__)

COLLECTION_GROUPS = {"all", "ashare-p0", "ashare-p1", "ashare-p2", "ashare-risk", "fund", "crypto"}
JOB_TYPES = {
    "collection",
    "recommendation_pipeline",
    "data_quality_refresh",
    "technical_screening_refresh",
    "structural_methodology_refresh",
    "decision_context_refresh",
    "trigger_evaluation",
    "agent_loop_consume",
    "high_risk_reviews",
    "reviews_due",
    "universe_merge",
    "universe_avoid_pool_rebuild",
    "backtest_run",
}
SCHEDULE_TYPES = {"interval", "daily_time", "trading_session", "manual", "after_success"}
DEPENDENCY_MODES = {"all_of", "any_of", "barrier"}
TRADING_DAY_POLICIES = {
    "any_day",
    "ashare",
    "trading_day_only",
    "non_trading_day_only",
    "previous_trading_day_required",
}
DEFAULT_JOB_TIMEOUT_SECONDS = 6 * 60 * 60
DEFAULT_HEALTH_STALE_SECONDS = 300
DEFAULT_MAX_CONCURRENT_JOBS = 4
DEFAULT_THREAD_POOL_MAX_WORKERS = 16
DEFAULT_JOB_PRIORITY = 100
DEFAULT_JOB_RESOURCE_POOL = "default"
STATUS_REPLACE_MAX_ATTEMPTS = 5
STATUS_REPLACE_RETRY_SECONDS = 0.05
PERSISTENT_TASK_RECOVERY_SWEEP_INTERVAL_SECONDS = 30.0


@dataclass(frozen=True)
class BaseDataSchedulerJob:
    """单个基础数据采集任务计划。"""

    name: str
    group: str | tuple[str, ...]
    interval_seconds: int
    job_type: str = "collection"
    enabled: bool = True
    limit: int | None = None
    market: str | None = None
    params: JsonDict = field(default_factory=dict)
    schedule_type: str = "interval"
    run_at: tuple[str, ...] = ()
    session_windows: tuple[str, ...] = ()
    timezone: str = "UTC"
    trading_day_policy: str = "any_day"
    depends_on: tuple[str, ...] = ()
    dependency_mode: str = "all_of"
    max_retries: int | None = None
    mutex_key: str | None = None
    priority: int = DEFAULT_JOB_PRIORITY
    resource_pool: str = DEFAULT_JOB_RESOURCE_POOL


@dataclass(frozen=True)
class BaseDataSchedulerConfig:
    """基础数据调度器配置。"""

    enabled: bool = True
    cache_backend: str = "auto"
    lock_ttl_seconds: int = 600
    circuit_failure_threshold: int = 3
    circuit_cooldown_seconds: int = 900
    force_provider: bool = False
    loop_idle_seconds: int = 5
    max_job_retries: int = 2
    retry_backoff_seconds: int = 30
    job_timeout_seconds: int = DEFAULT_JOB_TIMEOUT_SECONDS
    health_stale_seconds: int = DEFAULT_HEALTH_STALE_SECONDS
    max_concurrent_jobs: int = DEFAULT_MAX_CONCURRENT_JOBS
    resource_pools: JsonDict = field(default_factory=dict)
    rate_policies: JsonDict = field(default_factory=dict)
    jobs: tuple[BaseDataSchedulerJob, ...] = ()


@dataclass
class ScheduledJobState:
    """运行期任务状态。"""

    job: BaseDataSchedulerJob
    next_run_at: datetime | None
    last_run_at: datetime | None = None
    last_summary: JsonDict | None = None
    running: bool = False
    queued: bool = False
    last_success_generation: int | None = None
    last_triggered_dependency_generation: int | None = None
    pending_generation: int | None = None


def is_market_universe_job(job: BaseDataSchedulerJob) -> bool:
    """判断任务是否为某个市场的资产池刷新任务。"""

    return (
        job.job_type == "collection"
        and bool(job.market)
        and str(job.params.get("sync_task_type") or "").strip() == "universe_refresh"
    )


def filter_due_states_by_market_universe(
    due_states: list[ScheduledJobState],
    *,
    active_states: list[ScheduledJobState],
) -> tuple[list[ScheduledJobState], list[ScheduledJobState]]:
    """同一市场的资产池刷新优先执行，其它依赖资产池的任务等待下一轮。"""

    active_universe_markets = {
        str(state.job.market)
        for state in active_states
        if is_market_universe_job(state.job)
    }
    due_universe_markets = {
        str(state.job.market)
        for state in due_states
        if is_market_universe_job(state.job)
    }
    blocked_markets = active_universe_markets | due_universe_markets
    if not blocked_markets:
        return due_states, []

    runnable: list[ScheduledJobState] = []
    blocked: list[ScheduledJobState] = []
    for state in due_states:
        market = str(state.job.market or "")
        if market in blocked_markets and not is_market_universe_job(state.job):
            blocked.append(state)
        else:
            runnable.append(state)
    return runnable, blocked


def scheduler_job_mutex_key(job: BaseDataSchedulerJob) -> str | None:
    """返回任务互斥键；未配置时不参与互斥。"""

    mutex_key = str(job.mutex_key or "").strip()
    return mutex_key or None


def filter_due_states_by_mutex_key(
    due_states: list[ScheduledJobState],
    *,
    active_states: list[ScheduledJobState],
) -> tuple[list[ScheduledJobState], list[ScheduledJobState]]:
    """同一互斥键的任务串行执行，避免重复抓取或并发写同一资源。"""

    reserved_keys = {
        mutex_key
        for state in active_states
        if (mutex_key := scheduler_job_mutex_key(state.job)) is not None
    }
    if not reserved_keys and not any(scheduler_job_mutex_key(state.job) for state in due_states):
        return due_states, []

    runnable: list[ScheduledJobState] = []
    blocked: list[ScheduledJobState] = []
    for state in due_states:
        mutex_key = scheduler_job_mutex_key(state.job)
        if mutex_key is not None and mutex_key in reserved_keys:
            blocked.append(state)
            continue
        runnable.append(state)
        if mutex_key is not None:
            reserved_keys.add(mutex_key)
    return runnable, blocked


def scheduler_job_resource_pool(job: BaseDataSchedulerJob) -> str:
    """返回任务资源池；缺省任务归入 default 池。"""

    return str(job.resource_pool or DEFAULT_JOB_RESOURCE_POOL).strip() or DEFAULT_JOB_RESOURCE_POOL


def scheduler_resource_pool_limit(
    config: BaseDataSchedulerConfig,
    pool_name: str,
) -> int:
    """返回资源池并发额度；未知资源池按 default 池或全局并发兜底。"""

    pools = config.resource_pools or {}
    pool_payload = pools.get(pool_name) or pools.get(DEFAULT_JOB_RESOURCE_POOL) or {}
    if isinstance(pool_payload, Mapping):
        value = pool_payload.get("max_concurrent_jobs")
    else:
        value = None
    if value is None:
        return max(1, config.max_concurrent_jobs)
    return as_positive_int(value, field_name=f"resource_pools.{pool_name}.max_concurrent_jobs")


def scheduler_resource_pool_counts(states: list[ScheduledJobState]) -> dict[str, int]:
    """统计运行中任务占用的资源池槽位。"""

    counts: dict[str, int] = {}
    for state in states:
        pool_name = scheduler_job_resource_pool(state.job)
        counts[pool_name] = counts.get(pool_name, 0) + 1
    return counts


def next_run_at_for_job(job: BaseDataSchedulerJob, *, now: datetime) -> datetime | None:
    """根据任务调度类型计算下一次运行时间。"""

    normalized_now = now if now.tzinfo else now.replace(tzinfo=UTC)
    if job.schedule_type == "manual":
        return None
    if job.schedule_type == "after_success":
        return None
    if job.schedule_type == "interval":
        return normalized_now
    if job.schedule_type == "trading_session":
        return next_trading_session_run_at(job, now=normalized_now)
    if job.schedule_type == "daily_time":
        return next_calendar_run_at(
            job.run_at,
            now=normalized_now,
            timezone=job.timezone,
            trading_day_policy=job.trading_day_policy,
        )
    return normalized_now


def next_run_after_completion(job: BaseDataSchedulerJob, *, completed_at: datetime) -> datetime | None:
    """任务完成后计算后续运行时间。"""

    normalized_completed_at = completed_at if completed_at.tzinfo else completed_at.replace(tzinfo=UTC)
    if job.schedule_type == "interval":
        return normalized_completed_at + timedelta(seconds=job.interval_seconds)
    if job.schedule_type == "trading_session":
        return next_trading_session_run_at(
            job,
            now=normalized_completed_at + timedelta(seconds=job.interval_seconds),
        )
    if job.schedule_type == "daily_time":
        return next_run_at_for_job(job, now=normalized_completed_at)
    return None


def next_trading_session_run_at(
    job: BaseDataSchedulerJob,
    *,
    now: datetime,
) -> datetime | None:
    """计算交易窗口或收盘补跑时间中的下一次执行时刻。"""

    if not job.session_windows:
        raise ValueError(f"{job.name}.session_windows 不能为空")
    try:
        zone = ZoneInfo(job.timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"未知时区：{job.timezone}") from exc

    normalized_now = now if now.tzinfo else now.replace(tzinfo=UTC)
    local_now = normalized_now.astimezone(zone)
    windows = sorted(parse_session_window(value) for value in job.session_windows)
    fixed_times = sorted(parse_local_time(value) for value in job.run_at)

    for day_offset in range(0, 15):
        local_date = local_now.date() + timedelta(days=day_offset)
        if not schedule_date_allowed(local_date, job.trading_day_policy):
            continue

        if day_offset == 0:
            for window_start, window_end in windows:
                start_at = datetime.combine(local_date, window_start, tzinfo=zone)
                end_at = datetime.combine(local_date, window_end, tzinfo=zone)
                if start_at <= local_now <= end_at:
                    return local_now.astimezone(UTC)

        candidates: list[datetime] = []
        for window_start, _ in windows:
            candidate = datetime.combine(local_date, window_start, tzinfo=zone)
            if day_offset > 0 or candidate > local_now:
                candidates.append(candidate)
        for fixed_time in fixed_times:
            candidate = datetime.combine(local_date, fixed_time, tzinfo=zone)
            if day_offset > 0 or candidate >= local_now:
                candidates.append(candidate)
        if candidates:
            return min(candidates).astimezone(UTC)
    return None


def next_calendar_run_at(
    run_at: tuple[str, ...],
    *,
    now: datetime,
    timezone: str,
    trading_day_policy: str,
) -> datetime | None:
    """按本地时间列表计算下一次日历触发时间。"""

    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"未知时区：{timezone}") from exc
    local_now = now.astimezone(zone)
    times = sorted(parse_local_time(value) for value in run_at)
    for day_offset in range(0, 15):
        local_date = local_now.date() + timedelta(days=day_offset)
        if not schedule_date_allowed(local_date, trading_day_policy):
            continue
        for local_time in times:
            candidate = datetime.combine(local_date, local_time, tzinfo=zone)
            if candidate > local_now:
                return candidate.astimezone(UTC)
    return None


def parse_local_time(value: str) -> Any:
    """解析 HH:MM 或 HH:MM:SS 本地时间。"""

    parts = str(value).strip().split(":")
    if len(parts) not in {2, 3}:
        raise ValueError(f"run_at 时间格式应为 HH:MM 或 HH:MM:SS：{value}")
    hour, minute = int(parts[0]), int(parts[1])
    second = int(parts[2]) if len(parts) == 3 else 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise ValueError(f"run_at 时间超出范围：{value}")
    return datetime(2000, 1, 1, hour, minute, second).time()


def parse_session_window(value: str) -> tuple[Any, Any]:
    """解析并校验单个 HH:MM-HH:MM 交易时段窗口。"""

    raw_value = str(value).strip()
    start_value, separator, end_value = raw_value.partition("-")
    if not separator or not start_value.strip() or not end_value.strip():
        raise ValueError(f"交易时段窗口格式应为 HH:MM-HH:MM：{value}")
    start_time = parse_local_time(start_value)
    end_time = parse_local_time(end_value)
    if start_time >= end_time:
        raise ValueError(f"交易时段窗口开始时间必须早于结束时间：{value}")
    return start_time, end_time


def validate_session_windows(values: tuple[str, ...], *, field_name: str) -> None:
    """校验交易时段窗口非空、顺序有效且互不重叠。"""

    if not values:
        raise ValueError(f"{field_name} 不能为空")
    windows = sorted(parse_session_window(value) for value in values)
    for (_, previous_end), (current_start, _) in zip(windows, windows[1:], strict=False):
        if current_start < previous_end:
            raise ValueError(f"{field_name} 交易时段窗口不能重叠")


def schedule_date_allowed(local_date: Any, trading_day_policy: str) -> bool:
    """按交易日策略判断日期是否允许执行。"""

    is_weekday = local_date.weekday() < 5
    if trading_day_policy in {"any_day", "previous_trading_day_required"}:
        return True
    if trading_day_policy in {"ashare", "trading_day_only"}:
        return is_weekday
    if trading_day_policy == "non_trading_day_only":
        return not is_weekday
    return True


def trigger_after_success_dependents(
    states: list[ScheduledJobState],
    *,
    completed_job_name: str,
    triggered_at: datetime,
    completion_generation: int | None = None,
) -> None:
    """上游成功后按依赖模式唤醒 after_success 任务。"""

    for state in states:
        if state.job.schedule_type != "after_success":
            continue
        if completed_job_name not in state.job.depends_on:
            continue
        if state.running or state.queued:
            continue
        if (
            completion_generation is not None
            and state.last_triggered_dependency_generation == completion_generation
        ):
            continue
        if not dependency_is_satisfied(
            state.job,
            states,
            generation=completion_generation,
        ):
            continue
        state.next_run_at = triggered_at
        state.last_triggered_dependency_generation = completion_generation
        state.pending_generation = completion_generation


def dependency_is_satisfied(
    job: BaseDataSchedulerJob,
    states: Sequence[ScheduledJobState],
    *,
    generation: int | None = None,
) -> bool:
    """判断任务依赖是否满足。

    `all_of` 允许依赖分别在不同轮次成功；`any_of` 任一成功即可；`barrier`
    要求所有依赖在同一调度轮次成功，适合需要同一时间截面的并行任务。
    """

    if not job.depends_on:
        return True
    by_name = {state.job.name: state for state in states}
    dependency_states = [by_name.get(name) for name in job.depends_on]
    if any(state is None for state in dependency_states):
        return False
    resolved = [state for state in dependency_states if state is not None]
    generations = [state.last_success_generation for state in resolved]
    if job.dependency_mode == "any_of":
        return any(item is not None for item in generations)
    if job.dependency_mode == "barrier":
        if generation is not None:
            return all(item == generation for item in generations)
        # due-filter 在每轮扫描时没有单独的完成代次参数；当所有依赖已经
        # 记录同一代次时可以安全推断 barrier 已满足，避免任务永久阻塞。
        return bool(generations) and generations[0] is not None and all(
            item == generations[0] for item in generations
        )
    return all(item is not None for item in generations)


def scheduler_job_outcome_status(job: BaseDataSchedulerJob, summary: Any) -> str:
    """把需要硬质量门控的分析结果转换为调度终态。"""

    if job.job_type != "decision_context_refresh" or not isinstance(summary, Mapping):
        return "executed"
    quality = str(summary.get("quality_status") or summary.get("status") or "")
    return "executed" if quality == "available" else "blocked"


def filter_due_states_by_dependencies(
    due_states: list[ScheduledJobState],
    *,
    all_states: Sequence[ScheduledJobState],
    generation: int | None = None,
) -> tuple[list[ScheduledJobState], list[ScheduledJobState]]:
    """将依赖未满足的到期任务暂缓到下一轮。"""

    runnable: list[ScheduledJobState] = []
    blocked: list[ScheduledJobState] = []
    for state in due_states:
        effective_generation = (
            generation if generation is not None else state.pending_generation
        )
        if dependency_is_satisfied(
            state.job,
            all_states,
            generation=effective_generation,
        ):
            runnable.append(state)
        else:
            blocked.append(state)
    return runnable, blocked


def merge_scheduler_runtime_states(
    existing_states: Sequence[ScheduledJobState],
    new_config: BaseDataSchedulerConfig,
    *,
    now: datetime,
) -> list[ScheduledJobState]:
    """把热重载后的配置合并到运行态，不打断已运行任务。"""

    existing_by_name = {state.job.name: state for state in existing_states}
    merged: list[ScheduledJobState] = []
    for new_job in new_config.jobs:
        previous = existing_by_name.get(new_job.name)
        if previous is None:
            merged.append(
                ScheduledJobState(
                    job=new_job,
                    next_run_at=next_run_at_for_job(new_job, now=now),
                )
            )
            continue
        if previous.running:
            merged.append(previous)
            continue
        if not new_job.enabled:
            continue
        previous.job = new_job
        merged.append(previous)
    return merged


def default_scheduler_payload() -> JsonDict:
    """返回新一代全面数据同步默认调度计划。"""

    return export_scheduler_payload(build_preset_config())


def legacy_scheduler_payload() -> JsonDict:
    """返回旧版样例调度配置。

    该配置保留给本地样例采集和回归排查。正式数据同步默认使用
    `default_scheduler_payload()` 生成的全面计划。
    """

    return {
        "enabled": True,
        "cache_backend": "auto",
        "lock_ttl_seconds": 600,
        "circuit_failure_threshold": 3,
        "circuit_cooldown_seconds": 900,
        "force_provider": False,
        "loop_idle_seconds": 5,
        "max_job_retries": 2,
        "retry_backoff_seconds": 30,
        "job_timeout_seconds": DEFAULT_JOB_TIMEOUT_SECONDS,
        "health_stale_seconds": DEFAULT_HEALTH_STALE_SECONDS,
        "max_concurrent_jobs": DEFAULT_MAX_CONCURRENT_JOBS,
        "jobs": [
            {
                "name": "ashare-p0-assets-and-bars",
                "group": "ashare-p0",
                "enabled": True,
                "interval_seconds": 6 * 60 * 60,
                "limit": 20,
                "market": "ashare",
                "params": {
                    "ashare_symbol": "000001",
                    "ashare_name": "平安银行",
                    "ashare_start": "20260501",
                    "ashare_end": "20260514",
                    "ashare_timeframe": "1d",
                    "ashare_adjust": "qfq",
                },
            },
            {
                "name": "ashare-p1-sector-flow-news",
                "group": "ashare-p1",
                "enabled": True,
                "interval_seconds": 30 * 60,
                "limit": 10,
                "market": "ashare",
                "params": {
                    "industry": "银行",
                    "concept": "融资融券",
                    "flow_window": "5日",
                    "ashare_symbol": "000001",
                    "ashare_name": "平安银行",
                },
            },
            {
                "name": "ashare-p2-fundamental",
                "group": "ashare-p2",
                "enabled": True,
                "interval_seconds": 12 * 60 * 60,
                "limit": 10,
                "market": "ashare",
                "params": {
                    "ashare_symbol": "000001",
                    "ashare_name": "平安银行",
                    "report_date": "20250331",
                },
            },
            {
                "name": "ashare-risk-sentiment",
                "group": "ashare-risk",
                "enabled": True,
                "interval_seconds": 15 * 60,
                "limit": 10,
                "market": "ashare",
                "params": {
                    "risk_start": "20260501",
                    "risk_end": "20260514",
                    "risk_block_symbol": "A股",
                },
            },
            {
                "name": "crypto-spot-core",
                "group": "crypto",
                "enabled": True,
                "interval_seconds": 5 * 60,
                "limit": 20,
                "market": "crypto_spot",
                "params": {
                    "crypto_symbol": "BTCUSDT",
                    "crypto_timeframe": "1h",
                    "crypto_market_type": "spot",
                },
            },
        ],
    }


def default_data_sync_config_payload() -> JsonDict:
    """返回新的数据同步配置默认模板。"""

    return build_preset_config().to_dict()


def load_scheduler_config(path: str | Path | None = None) -> BaseDataSchedulerConfig:
    """从 JSON 文件读取调度器配置；未传路径时使用全面数据同步默认配置。"""

    if path is None:
        return parse_scheduler_config(default_scheduler_payload())

    config_path = Path(path)
    payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if is_data_sync_config_payload(payload):
        payload = export_scheduler_payload(load_data_sync_config(config_path))
    return parse_scheduler_config(payload)


def load_data_sync_scheduler_payload(path: str | Path | None = None) -> JsonDict:
    """加载新一代数据同步配置并导出底层调度器计划。"""

    config = load_data_sync_config(path)
    return export_scheduler_payload(config)


def is_data_sync_config_payload(payload: Any) -> bool:
    """判断 JSON 是否为新一代数据同步配置，而非已导出的调度计划。"""

    return (
        isinstance(payload, Mapping)
        and "markets" in payload
        and payload.get("schema_version") != "data-sync-scheduler-v1"
    )


def read_scheduler_health(
    status_file: str | Path,
    *,
    max_age_seconds: int | None = None,
) -> JsonDict:
    """读取调度器状态文件并返回健康检查摘要。"""

    path = Path(status_file)
    if not path.exists():
        return {
            "healthy": False,
            "status": "missing",
            "status_file": str(path),
            "message": "调度器状态文件不存在。",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        return {
            "healthy": False,
            "status": "invalid",
            "status_file": str(path),
            "message": f"调度器状态文件不是合法 JSON：{exc}",
        }

    updated_at = parse_datetime_or_none(payload.get("updated_at"))
    if updated_at is None:
        return {
            "healthy": False,
            "status": "invalid",
            "status_file": str(path),
            "payload": payload,
            "message": "调度器状态文件缺少 updated_at。",
        }

    age_seconds = max(0.0, (datetime.now(tz=UTC) - updated_at).total_seconds())
    stale_after = (
        max_age_seconds
        if max_age_seconds is not None
        else int(payload.get("health_stale_seconds") or 300)
    )
    state = str(payload.get("state") or "unknown")
    last_job_status = payload.get("last_job_status")
    terminal_states = {"stopped", "completed", "disabled", "interrupted", "cancelled"}
    healthy = age_seconds <= stale_after and state not in {"failed", "stale", *terminal_states}
    if last_job_status == "failed":
        healthy = False
    status = "healthy" if healthy else "unhealthy"
    if age_seconds > stale_after:
        status = "stale"
    elif state in terminal_states:
        status = state
    return {
        "healthy": healthy,
        "status": status,
        "status_file": str(path),
        "age_seconds": round(age_seconds, 3),
        "stale_after_seconds": stale_after,
        "state": state,
        "last_job": payload.get("last_job"),
        "last_job_status": last_job_status,
        "last_error": payload.get("last_error"),
        "payload": payload,
    }


def parse_scheduler_config(payload: Mapping[str, Any]) -> BaseDataSchedulerConfig:
    """解析并校验调度器配置。"""

    jobs_payload = payload.get("jobs")
    if not isinstance(jobs_payload, list):
        raise ValueError("调度配置必须包含 jobs 数组")

    jobs = tuple(parse_scheduler_job(item, index=index) for index, item in enumerate(jobs_payload))
    max_concurrent_jobs = as_positive_int(
        payload.get("max_concurrent_jobs", DEFAULT_MAX_CONCURRENT_JOBS),
        field_name="max_concurrent_jobs",
    )
    resource_pools = parse_scheduler_resource_pools(
        payload.get("resource_pools"),
        max_concurrent_jobs=max_concurrent_jobs,
    )
    warn_on_resource_pool_misconfiguration(
        jobs=jobs,
        resource_pools=resource_pools,
        max_concurrent_jobs=max_concurrent_jobs,
    )
    return BaseDataSchedulerConfig(
        enabled=as_bool(payload.get("enabled", True), field_name="enabled"),
        cache_backend=as_choice(
            payload.get("cache_backend", "auto"),
            choices={"auto", "redis", "null"},
            field_name="cache_backend",
        ),
        lock_ttl_seconds=as_positive_int(
            payload.get("lock_ttl_seconds", 600),
            field_name="lock_ttl_seconds",
        ),
        circuit_failure_threshold=as_positive_int(
            payload.get("circuit_failure_threshold", 3),
            field_name="circuit_failure_threshold",
        ),
        circuit_cooldown_seconds=as_positive_int(
            payload.get("circuit_cooldown_seconds", 900),
            field_name="circuit_cooldown_seconds",
        ),
        force_provider=as_bool(payload.get("force_provider", False), field_name="force_provider"),
        loop_idle_seconds=as_positive_int(
            payload.get("loop_idle_seconds", 5),
            field_name="loop_idle_seconds",
        ),
        max_job_retries=as_non_negative_int(
            payload.get("max_job_retries", 2),
            field_name="max_job_retries",
        ),
        retry_backoff_seconds=as_positive_int(
            payload.get("retry_backoff_seconds", 30),
            field_name="retry_backoff_seconds",
        ),
        job_timeout_seconds=as_positive_int(
            payload.get("job_timeout_seconds", DEFAULT_JOB_TIMEOUT_SECONDS),
            field_name="job_timeout_seconds",
        ),
        health_stale_seconds=as_positive_int(
            payload.get("health_stale_seconds", DEFAULT_HEALTH_STALE_SECONDS),
            field_name="health_stale_seconds",
        ),
        max_concurrent_jobs=max_concurrent_jobs,
        resource_pools=resource_pools,
        rate_policies=parse_json_object(payload.get("rate_policies"), field_name="rate_policies"),
        jobs=jobs,
    )


def parse_scheduler_resource_pools(value: Any, *, max_concurrent_jobs: int) -> JsonDict:
    """解析调度资源池配置；旧配置缺省时生成 default 池。"""

    if value is None:
        return {
            DEFAULT_JOB_RESOURCE_POOL: {
                "max_concurrent_jobs": max_concurrent_jobs,
                "description": "默认调度资源池",
            }
        }
    if not isinstance(value, Mapping):
        raise ValueError("resource_pools 必须是对象")
    parsed: JsonDict = {}
    for key, item in value.items():
        pool_name = str(key).strip()
        if not pool_name:
            raise ValueError("resource_pools 的资源池名称不能为空")
        if not isinstance(item, Mapping):
            raise ValueError(f"resource_pools.{pool_name} 必须是对象")
        pool_payload = dict(item)
        pool_payload["max_concurrent_jobs"] = as_positive_int(
            pool_payload.get("max_concurrent_jobs", 1),
            field_name=f"resource_pools.{pool_name}.max_concurrent_jobs",
        )
        parsed[pool_name] = pool_payload
    parsed.setdefault(
        DEFAULT_JOB_RESOURCE_POOL,
        {
            "max_concurrent_jobs": max_concurrent_jobs,
            "description": "默认调度资源池",
        },
    )
    return parsed


def warn_on_resource_pool_misconfiguration(
    *,
    jobs: Sequence[BaseDataSchedulerJob],
    resource_pools: Mapping[str, Any],
    max_concurrent_jobs: int,
) -> None:
    """对资源池配置中的常见易错点输出告警，但不阻断调度。

    两类告警：
    1. 任务引用了未在 ``resource_pools`` 声明的资源池名称（多为拼写错误）。
       运行期会兜底到 default 池或全局并发，但额度可能与预期不符。
    2. 各资源池额度之和超过全局 ``max_concurrent_jobs``。运行期全局闸门仍会
       压住实际并发，但部分资源池将永远拿不到声明的额度，配置含义会让人误解。
    """

    known_pools = set(resource_pools)
    referenced = {scheduler_job_resource_pool(job) for job in jobs}
    unknown = sorted(name for name in referenced if name not in known_pools)
    if unknown:
        logger.warning(
            "调度任务引用了未声明的资源池 %s，运行期将兜底到 default 池或全局并发，"
            "请确认是否为资源池名称拼写错误。",
            unknown,
        )

    pool_sum = 0
    for pool_name in known_pools:
        pool_payload = resource_pools.get(pool_name)
        if isinstance(pool_payload, Mapping):
            limit = pool_payload.get("max_concurrent_jobs")
            pool_sum += int(limit) if isinstance(limit, int) else max_concurrent_jobs
        else:
            pool_sum += max_concurrent_jobs
    if pool_sum > max_concurrent_jobs:
        logger.info(
            "资源池额度之和 %s 超过全局 max_concurrent_jobs %s，全局闸门仍会压住实际并发，"
            "部分资源池将无法同时达到各自声明的额度。",
            pool_sum,
            max_concurrent_jobs,
        )


def parse_scheduler_job(payload: Any, *, index: int) -> BaseDataSchedulerJob:
    """解析单个任务配置。"""

    if not isinstance(payload, Mapping):
        raise ValueError(f"jobs[{index}] 必须是对象")

    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError(f"jobs[{index}].name 不能为空")

    job_type = as_choice(
        payload.get("job_type", "collection"),
        choices=JOB_TYPES,
        field_name=f"{name}.job_type",
    )
    schedule_type = as_choice(
        payload.get("schedule_type", "interval"),
        choices=SCHEDULE_TYPES,
        field_name=f"{name}.schedule_type",
    )
    group = as_job_group_choice(
        payload.get("group"),
        job_type=job_type,
        field_name=f"{name}.group",
    )
    if schedule_type in {"interval", "trading_session"}:
        interval_seconds = as_positive_int(
            payload.get("interval_seconds"),
            field_name=f"{name}.interval_seconds",
        )
    else:
        default_interval = 24 * 60 * 60 if schedule_type == "daily_time" else 0
        interval_seconds = as_non_negative_int(
            payload.get("interval_seconds", default_interval),
            field_name=f"{name}.interval_seconds",
        )
    run_at = as_string_tuple(payload.get("run_at", ()), field_name=f"{name}.run_at")
    if schedule_type == "daily_time" and not run_at:
        raise ValueError(f"{name}.run_at 不能为空")
    session_windows = as_string_tuple(
        payload.get("session_windows", ()),
        field_name=f"{name}.session_windows",
    )
    if schedule_type == "trading_session":
        validate_session_windows(
            session_windows,
            field_name=f"{name}.session_windows",
        )
    timezone = str(payload.get("timezone") or "UTC").strip() or "UTC"
    trading_day_policy = as_choice(
        payload.get("trading_day_policy", "any_day"),
        choices=TRADING_DAY_POLICIES,
        field_name=f"{name}.trading_day_policy",
    )
    depends_on = as_string_tuple(payload.get("depends_on", ()), field_name=f"{name}.depends_on")
    if schedule_type == "after_success" and not depends_on:
        raise ValueError(f"{name}.depends_on 不能为空")
    dependency_mode = as_choice(
        payload.get("dependency_mode", "all_of"),
        choices=DEPENDENCY_MODES,
        field_name=f"{name}.dependency_mode",
    )
    mutex_key = str(payload.get("mutex_key") or "").strip() or None
    limit = payload.get("limit")
    if limit is not None:
        limit = as_positive_int(limit, field_name=f"{name}.limit")

    market = payload.get("market")
    params = dict(payload.get("params", {}) or {})
    if params is None:
        params = {}
    if not isinstance(params, Mapping):
        raise ValueError(f"{name}.params 必须是对象")
    if job_type == "collection":
        effective_group = as_group_choice(
            params.pop("group", group),
            field_name=f"{name}.params.group",
        )
    else:
        params.pop("group", None)
        effective_group = group

    return BaseDataSchedulerJob(
        name=name,
        group=effective_group,
        interval_seconds=interval_seconds,
        job_type=job_type,
        enabled=as_bool(payload.get("enabled", True), field_name=f"{name}.enabled"),
        limit=limit,
        market=str(market) if market is not None else None,
        params=dict(params),
        schedule_type=schedule_type,
        run_at=run_at,
        session_windows=session_windows,
        timezone=timezone,
        trading_day_policy=trading_day_policy,
        depends_on=depends_on,
        dependency_mode=dependency_mode,
        max_retries=(
            as_non_negative_int(payload.get("max_retries"), field_name=f"{name}.max_retries")
            if payload.get("max_retries") is not None
            else None
        ),
        mutex_key=mutex_key,
        priority=as_non_negative_int(
            payload.get("priority", DEFAULT_JOB_PRIORITY),
            field_name=f"{name}.priority",
        ),
        resource_pool=str(payload.get("resource_pool") or DEFAULT_JOB_RESOURCE_POOL).strip()
        or DEFAULT_JOB_RESOURCE_POOL,
    )


class BaseDataScheduler(RecoverySchedulerMixin):
    """按配置运行基础数据采集任务。"""

    def __init__(
        self,
        config: BaseDataSchedulerConfig,
        *,
        collect_base_data_func: Callable[[Any], JsonDict] | None = None,
        default_collection_args_func: Callable[..., Any] | None = None,
        run_recommendation_pipeline_func: Callable[..., JsonDict] | None = None,
        run_data_quality_refresh_func: Callable[..., JsonDict] | None = None,
        run_technical_screening_refresh_func: Callable[..., JsonDict] | None = None,
        run_structural_methodology_refresh_func: Callable[..., JsonDict] | None = None,
        run_decision_context_refresh_func: Callable[..., JsonDict] | None = None,
        run_trigger_evaluation_func: Callable[..., JsonDict] | None = None,
        run_agent_loop_consume_func: Callable[..., JsonDict] | None = None,
        run_high_risk_reviews_func: Callable[..., JsonDict] | None = None,
        run_reviews_due_func: Callable[..., JsonDict] | None = None,
        run_universe_merge_func: Callable[..., JsonDict] | None = None,
        run_avoid_pool_rebuild_func: Callable[..., JsonDict] | None = None,
        run_backtest_func: Callable[..., JsonDict] | None = None,
        sleep_func: Callable[[float], None] = time.sleep,
        status_file: str | Path | None = None,
        event_log_file: str | Path | None = None,
        scheduler_config_file: str | Path | None = None,
        service_name: str = "base_data_scheduler",
        persistent_task_queue_scope: Callable[
            [], AbstractContextManager[PersistentTaskQueue]
        ] | None = None,
    ) -> None:
        self.config = config
        self._collect_base_data = collect_base_data_func
        self._default_collection_args = default_collection_args_func
        self._run_recommendation_pipeline = run_recommendation_pipeline_func
        self._run_data_quality_refresh = run_data_quality_refresh_func
        self._run_technical_screening_refresh = run_technical_screening_refresh_func
        self._run_structural_methodology_refresh = run_structural_methodology_refresh_func
        self._run_decision_context_refresh = run_decision_context_refresh_func
        self._run_trigger_evaluation = run_trigger_evaluation_func
        self._run_agent_loop_consume = run_agent_loop_consume_func
        self._run_high_risk_reviews = run_high_risk_reviews_func
        self._run_reviews_due = run_reviews_due_func
        self._run_universe_merge = run_universe_merge_func
        self._run_avoid_pool_rebuild = run_avoid_pool_rebuild_func
        self._run_backtest = run_backtest_func
        self._uses_injected_collect_base_data = collect_base_data_func is not None
        self._sleep = sleep_func
        self.status_file = Path(status_file) if status_file else None
        self.event_log_file = Path(event_log_file) if event_log_file else None
        self.scheduler_config_file = Path(scheduler_config_file) if scheduler_config_file else None
        self.config_identity: SchedulerConfigIdentity | None = (
            load_config_identity(self.scheduler_config_file)
            if self.scheduler_config_file is not None
            else None
        )
        self.config_reload_error: str | None = None
        self.service_name = service_name
        self._persistent_task_queue_scope = persistent_task_queue_scope
        self._worker_id = f"{service_name}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
        # 停跑恢复补跑：在飞分区任务计数（受 max_concurrent_jobs 预算约束）。
        self._recovery_lock = threading.Lock()
        self._recovery_inflight_count = 0
        self.started_at: datetime | None = None
        self._status_lock = threading.Lock()
        self._event_lock = threading.Lock()
        self._progress = self._create_progress_recorder()

    def _create_progress_recorder(self) -> BaseDataTaskProgressRecorder:
        """创建进度记录器；Redis 不可用时自动降级为空实现。"""

        try:
            if self.config.cache_backend == "null":
                cache, _, _ = create_cache_client(backend="null")
                return BaseDataTaskProgressRecorder.from_cache_client(cache, cache_backend="null")
            backend = "auto" if self.config.cache_backend == "auto" else "redis"
            cache, _, cache_status = create_cache_client(backend=backend)
            if cache_status.backend != "redis":
                cache, _, _ = create_cache_client(backend="null")
                return BaseDataTaskProgressRecorder.from_cache_client(cache, cache_backend="null")
            return BaseDataTaskProgressRecorder.from_cache_client(cache, cache_backend="redis")
        except Exception:
            cache, _, _ = create_cache_client(backend="null")
            return BaseDataTaskProgressRecorder.from_cache_client(cache, cache_backend="null")

    def _persistent_task_idempotency_key(
        self,
        job: BaseDataSchedulerJob,
        *,
        scheduled_at: datetime,
    ) -> str:
        """构造单次调度运行的稳定幂等键。"""

        normalized_at = scheduled_at.astimezone(UTC)
        return f"scheduler:{job.name}:{normalized_at.isoformat()}"

    def _claim_persistent_task(
        self,
        job: BaseDataSchedulerJob,
        *,
        scheduled_at: datetime,
        dry_run: bool,
    ) -> TaskClaim | None:
        """将到期任务写入 PostgreSQL 并精确领取自己的租约。"""

        if self._persistent_task_queue_scope is None or dry_run:
            return None
        occurred_at = datetime.now(tz=UTC)
        idempotency_key = self._persistent_task_idempotency_key(
            job,
            scheduled_at=scheduled_at,
        )
        payload = {
            "job_name": job.name,
            "job_type": job.job_type,
            "market": job.market,
            "scheduled_at": scheduled_at.astimezone(UTC).isoformat(),
            "params": serialize_scheduler_value(job.params),
        }
        lease_seconds = max(60, int(self.config.job_timeout_seconds or 0) + 60)
        with self._persistent_task_queue_scope() as task_queue:
            recovered = task_queue.claim(
                worker_id=self._worker_id,
                lease_seconds=lease_seconds,
                job_name=job.name,
                now=occurred_at,
            )
            if recovered is not None:
                logger.info(
                    "接管恢复任务 job=%s task_id=%s attempts=%s",
                    job.name,
                    recovered.task_id,
                    recovered.attempts,
                )
                return recovered
            task_queue.enqueue(
                job_name=job.name,
                idempotency_key=idempotency_key,
                payload=payload,
                max_attempts=1,
                now=occurred_at,
            )
            return task_queue.claim(
                worker_id=self._worker_id,
                lease_seconds=lease_seconds,
                job_name=job.name,
                idempotency_key=idempotency_key,
                now=occurred_at,
            )

    def _recover_expired_persistent_tasks(self, *, force: bool = False) -> int:
        """周期释放过期租约，使启动后才到期的任务也能重新领取。"""

        if self._persistent_task_queue_scope is None:
            return 0
        now_monotonic = time.monotonic()
        last_sweep = float(
            getattr(self, "_persistent_recovery_last_sweep", 0.0) or 0.0
        )
        if (
            not force
            and last_sweep > 0
            and now_monotonic - last_sweep
            < PERSISTENT_TASK_RECOVERY_SWEEP_INTERVAL_SECONDS
        ):
            return 0
        self._persistent_recovery_last_sweep = now_monotonic
        with self._persistent_task_queue_scope() as task_queue:
            recovered_count = task_queue.recover_expired()
        if recovered_count:
            logger.warning("已恢复过期持久化任务 count=%s", recovered_count)
        return recovered_count

    def _recover_orphaned_persistent_tasks(self) -> int:
        """启动时回收同一 scheduler 前一实例留下的运行租约。"""

        if self._persistent_task_queue_scope is None:
            return 0
        recover = None
        worker_prefix = self._worker_id.rsplit(":", 2)[0]
        with self._persistent_task_queue_scope() as task_queue:
            recover = getattr(task_queue, "recover_orphaned", None)
            if not callable(recover):
                return 0
            recovered_count = int(
                recover(
                    worker_prefix=worker_prefix,
                    current_worker_id=self._worker_id,
                )
                or 0
            )
        if recovered_count:
            logger.warning("已回收前一 scheduler 实例遗留租约 count=%s", recovered_count)
        return recovered_count

    def _finish_persistent_task(
        self,
        claim: TaskClaim,
        *,
        succeeded: bool,
        error_message: str | None = None,
    ) -> None:
        """在独立事务中完成或失败回写持久化任务。"""

        if self._persistent_task_queue_scope is None:
            return
        with self._persistent_task_queue_scope() as task_queue:
            if succeeded:
                completed = task_queue.complete(
                    task_id=claim.task_id,
                    lease_token=claim.lease_token,
                )
                if not completed:
                    logger.warning(
                        "持久化任务完成回写未命中租约 task_id=%s worker_id=%s",
                        claim.task_id,
                        self._worker_id,
                    )
                return
            task_queue.fail(
                task_id=claim.task_id,
                lease_token=claim.lease_token,
                error_message=error_message or "scheduler_job_failed",
                retry_after=timedelta(seconds=max(0, self.config.retry_backoff_seconds)),
            )

    def _run_persistent_job(
        self,
        job: BaseDataSchedulerJob,
        *,
        claim: TaskClaim,
        dry_run: bool,
    ) -> JsonDict:
        """执行已领取任务，并将结果绑定到持久化租约。"""

        try:
            summary = self.run_job(job, dry_run=dry_run)
        except Exception as exc:
            self._finish_persistent_task(
                claim,
                succeeded=False,
                error_message=str(exc),
            )
            raise
        succeeded = summary.get("status") == "executed"
        self._finish_persistent_task(
            claim,
            succeeded=succeeded,
            error_message=str(summary.get("error_message") or "scheduler_job_failed"),
        )
        return summary | {
            "persistent_task_id": claim.task_id,
            "persistent_task_attempt": claim.attempts,
        }

    def plan(self) -> JsonDict:
        """生成当前调度计划，不触发采集。"""

        enabled_jobs = [job for job in self.config.jobs if job.enabled]
        now = datetime.now(tz=UTC)
        return {
            "generated_at": now.isoformat(),
            "enabled": self.config.enabled,
            "cache_backend": self.config.cache_backend,
            "loop_idle_seconds": self.config.loop_idle_seconds,
            "max_concurrent_jobs": self.config.max_concurrent_jobs,
            "jobs": [
                {
                    "name": job.name,
                    "job_type": job.job_type,
                    "group": job.group,
                    "enabled": job.enabled,
                    "interval_seconds": job.interval_seconds,
                    "limit": job.limit,
                    "market": job.market,
                    "params": job.params,
                    "will_run": self.config.enabled and job.enabled,
                }
                for job in self.config.jobs
            ],
            "enabled_job_count": len(enabled_jobs) if self.config.enabled else 0,
        }

    def run_once(self, *, dry_run: bool = False) -> JsonDict:
        """按配置执行一轮启用任务后退出。"""

        started_at = datetime.now(tz=UTC)
        self.started_at = started_at
        logger.info(
            "基础数据调度器启动 mode=run_once dry_run=%s enabled_jobs=%s",
            dry_run,
            len(self.enabled_jobs()),
        )
        self.emit_event("scheduler_start", mode="run_once", dry_run=dry_run)
        self.write_status(
            state="running",
            mode="run_once",
            dry_run=dry_run,
            started_at=started_at,
        )
        if not self.config.enabled:
            result = {
                "mode": "run_once",
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(tz=UTC).isoformat(),
                "enabled": False,
                "dry_run": dry_run,
                "jobs": [],
            }
            self.stop_scheduler(result=result, state="disabled")
            logger.info("基础数据调度器已禁用 mode=run_once")
            return result

        job_summaries = [self.run_job(job, dry_run=dry_run) for job in self.enabled_jobs()]
        result = {
            "mode": "run_once",
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(tz=UTC).isoformat(),
            "enabled": True,
            "dry_run": dry_run,
            "jobs": job_summaries,
        }
        self.stop_scheduler(result=result, state="completed")
        logger.info(
            "基础数据调度器完成 mode=run_once dry_run=%s job_count=%s",
            dry_run,
            len(job_summaries),
        )
        return result

    def _supports_persistent_loop(self, *, dry_run: bool) -> bool:
        """仅在队列提供完整持久调度 interface 时启用新闭环。"""

        if dry_run or self._persistent_task_queue_scope is None:
            return False
        with self._persistent_task_queue_scope() as task_queue:
            required_methods = (
                "list_tasks",
                "schedule",
                "set_admission",
                "coalesce_task",
                "claim_many",
            )
            return all(callable(getattr(task_queue, name, None)) for name in required_methods)

    def _persistent_recovery_blocked_domains(self, task_queue: Any) -> Mapping[str, Any]:
        """读取活动补跑批次的阻塞数据域，测试 adapter 可直接提供快照。"""

        provider = getattr(task_queue, "recovery_blocked_domains", None)
        if callable(provider):
            return provider()
        repository = getattr(task_queue, "repository", None)
        session = getattr(repository, "session", None)
        if session is None:
            return {}
        from finance_agent.data_recovery.gate import RecoveryGate
        from finance_agent.data_recovery.repository import RecoveryRepository

        return RecoveryGate(RecoveryRepository(session)).blocked_domains()

    def _admit_persistent_tasks(self, *, now: datetime) -> JsonDict:
        """重评估全部 planned/blocked 任务，并为本轮预留资源池和互斥键。"""

        if self._persistent_task_queue_scope is None:
            return {"admitted": 0, "blocked": 0}
        enabled_jobs = {job.name: job for job in self.enabled_jobs()}
        with self._persistent_task_queue_scope() as task_queue:
            running = task_queue.list_tasks(statuses=("running",), limit=10_000)
            candidates = task_queue.list_tasks(
                statuses=("scheduled", "blocked"),
                limit=10_000,
            )
            pool_running: dict[str, int] = {}
            active_mutex_keys: set[str] = set()
            for task in running:
                pool = str(getattr(task, "resource_pool", "default") or "default")
                pool_running[pool] = pool_running.get(pool, 0) + 1
                mutex_key = str(getattr(task, "mutex_key", None) or "").strip()
                if mutex_key:
                    active_mutex_keys.add(mutex_key)
            pool_names = {
                str(getattr(task, "resource_pool", "default") or "default")
                for task in candidates
            } | {job.resource_pool for job in enabled_jobs.values()}
            pool_limits = {
                pool: scheduler_resource_pool_limit(self.config, pool)
                for pool in pool_names
            }
            blocked_domains = self._persistent_recovery_blocked_domains(task_queue)
            controller = SchedulerAdmissionController()
            admitted = 0
            blocked = 0
            candidates.sort(
                key=lambda task: (
                    -int(getattr(task, "priority", DEFAULT_JOB_PRIORITY) or 0),
                    getattr(task, "scheduled_for", None) or now,
                    str(getattr(task, "task_id", "")),
                )
            )
            for task in candidates:
                job = enabled_jobs.get(str(task.job_name))
                if job is None:
                    decision_allowed = False
                    reason_code = "config_disabled"
                    reason_detail: JsonDict = {}
                    recheck_at = None
                else:
                    snapshot = AdmissionSnapshot(
                        now=now,
                        recovery_blocked_domains=blocked_domains,
                        active_mutex_keys=set(active_mutex_keys),
                        resource_pool_limits=pool_limits,
                        resource_pool_running=dict(pool_running),
                    )
                    decision = controller.evaluate(task, snapshot)
                    decision_allowed = decision.allowed
                    reason_code = decision.reason_code
                    reason_detail = decision.reason_detail
                    recheck_at = decision.recheck_at
                # 资源池持续满载时，blocked 任务可能连续多轮得到完全相同的准入结论。
                # 不重复写入相同状态和 Outbox 事件，避免大量积压任务拖慢补位循环。
                if (
                    not decision_allowed
                    and str(getattr(task, "status", "")) == "blocked"
                    and str(getattr(task, "blocked_reason", "") or "") == str(reason_code or "")
                    and dict(getattr(task, "blocked_detail", None) or {}) == dict(reason_detail or {})
                    and getattr(task, "blocked_until", None) == recheck_at
                ):
                    blocked += 1
                    continue
                changed = task_queue.set_admission(
                    task_id=task.task_id,
                    allowed=decision_allowed,
                    reason_code=reason_code,
                    reason_detail=reason_detail,
                    recheck_at=recheck_at,
                    now=now,
                )
                if not changed:
                    continue
                if decision_allowed:
                    admitted += 1
                    pool = str(getattr(task, "resource_pool", "default") or "default")
                    pool_running[pool] = pool_running.get(pool, 0) + 1
                    mutex_key = str(getattr(task, "mutex_key", None) or "").strip()
                    if mutex_key:
                        active_mutex_keys.add(mutex_key)
                else:
                    blocked += 1
            return {"admitted": admitted, "blocked": blocked}

    def _run_persistent_loop(
        self,
        *,
        started_at: datetime,
        max_cycles: int | None,
    ) -> JsonDict:
        """以 PostgreSQL 为唯一任务事实源运行 Planner/Admission/Worker 闭环。"""

        cycles = 0
        executed: list[JsonDict] = []
        running_jobs: list[str] = []
        inflight: dict[Future[Any], str] = {}
        scheduler_config_mtime_ns = (
            self.scheduler_config_file.stat().st_mtime_ns
            if self.scheduler_config_file and self.scheduler_config_file.exists()
            else None
        )
        lease_seconds = max(60, int(self.config.job_timeout_seconds or 0) + 60)

        def reload_runtime_config_if_changed() -> None:
            nonlocal scheduler_config_mtime_ns
            if self.scheduler_config_file is None or not self.scheduler_config_file.exists():
                return
            current_mtime_ns = self.scheduler_config_file.stat().st_mtime_ns
            if scheduler_config_mtime_ns == current_mtime_ns:
                return
            try:
                self.config = load_scheduler_config(self.scheduler_config_file)
                self.config_identity = load_config_identity(self.scheduler_config_file)
            except Exception as exc:  # noqa: BLE001 - 文件可能处于原子替换窗口
                self.config_reload_error = f"{type(exc).__name__}: {exc}"
                logger.warning("持久调度配置热重载失败，将在下一轮重试：%s", exc)
                return
            self.config_reload_error = None
            scheduler_config_mtime_ns = current_mtime_ns

        try:
            self._recover_orphaned_persistent_tasks()
            self._recover_expired_persistent_tasks(force=True)
            self._run_recovery_startup_scan()
            with ThreadPoolExecutor(
                max_workers=max(DEFAULT_THREAD_POOL_MAX_WORKERS, self.config.max_concurrent_jobs),
                thread_name_prefix="persistent-scheduler-job",
            ) as executor:
                while self.config.enabled:
                    completed_futures = [future for future in inflight if future.done()]
                    for future in completed_futures:
                        job_name = inflight.pop(future)
                        try:
                            executed.append(future.result())
                        except Exception as exc:  # noqa: BLE001 - 单任务异常不能拖住其他任务
                            executed.append(
                                {
                                    "job": job_name,
                                    "status": "failed",
                                    "error_message": str(exc),
                                    "finished_at": datetime.now(tz=UTC).isoformat(),
                                }
                            )
                            logger.exception(
                                "持久调度任务执行异常 job=%s，继续运行其他任务",
                                job_name,
                            )

                    reload_runtime_config_if_changed()
                    if not self.config.enabled:
                        break
                    now = datetime.now(tz=UTC)
                    self._recover_expired_persistent_tasks()
                    with self._persistent_task_queue_scope() as task_queue:
                        planning = SchedulerPlanner(
                            task_queue,
                            config_digest=(
                                self.config_identity.digest
                                if self.config_identity is not None
                                else None
                            ),
                        ).reconcile(now=now, config=self.config)
                    admission = self._admit_persistent_tasks(now=now)
                    self._claim_recovery_tasks(
                        executor,
                        free_slots=max(
                            0,
                            int(self.config.max_concurrent_jobs) - self._recovery_inflight(),
                        ),
                    )
                    worker = PersistentSchedulerWorker(
                        queue_scope=self._persistent_task_queue_scope,
                        jobs={job.name: job for job in self.enabled_jobs()},
                        execute_job=lambda job: self.run_job(job, dry_run=False),
                        worker_id=self._worker_id,
                        lease_seconds=lease_seconds,
                        retry_backoff_seconds=self.config.retry_backoff_seconds,
                        resource_pool_limits={
                            pool: scheduler_resource_pool_limit(self.config, pool)
                            for pool in {
                                job.resource_pool for job in self.enabled_jobs()
                            }
                        },
                    )
                    claims = worker.claim(
                        free_slots=max(
                            0,
                            int(self.config.max_concurrent_jobs)
                            - self._recovery_inflight()
                            - len(inflight),
                        ),
                        now=now,
                    )
                    for claim in claims:
                        future = executor.submit(worker.execute, claim, now=now)
                        inflight[future] = claim.job_name
                    running_jobs = list(inflight.values())
                    self.write_status(
                        state="running",
                        mode="loop",
                        dry_run=False,
                        started_at=started_at,
                        cycles=cycles,
                        running_jobs=running_jobs,
                        queued_jobs=[],
                        max_concurrent_jobs=self.config.max_concurrent_jobs,
                        planning=asdict(planning),
                        admission=admission,
                    )
                    cycles += 1

                    # 只等待一个完成事件，给刚释放的槽位尽快重新准入和领取，
                    # 避免长任务阻塞整个批次的调度补位。
                    if inflight:
                        done, _ = wait(
                            tuple(inflight),
                            timeout=max(0.01, float(self.config.loop_idle_seconds)),
                            return_when=FIRST_COMPLETED,
                        )
                        for future in done:
                            job_name = inflight.pop(future)
                            try:
                                executed.append(future.result())
                            except Exception as exc:  # noqa: BLE001 - 单任务异常隔离
                                executed.append(
                                    {
                                        "job": job_name,
                                        "status": "failed",
                                        "error_message": str(exc),
                                        "finished_at": datetime.now(tz=UTC).isoformat(),
                                    }
                                )
                                logger.exception(
                                    "持久调度任务执行异常 job=%s，继续运行其他任务",
                                    job_name,
                                )
                    running_jobs = list(inflight.values())
                    if max_cycles is not None and cycles >= max_cycles:
                        break
                    self._sleep(float(self.config.loop_idle_seconds))
        except KeyboardInterrupt:
            result = {
                "mode": "loop",
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(tz=UTC).isoformat(),
                "enabled": True,
                "dry_run": False,
                "interrupted": True,
                "cycles": cycles,
                "jobs": executed,
                "running_jobs": list(inflight.values()) or running_jobs,
                "queued_jobs": [],
            }
            self.stop_scheduler(result=result, state="interrupted")
            return result
        except Exception as exc:  # noqa: BLE001 - 主循环边界必须落盘失败状态
            result = {
                "mode": "loop",
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(tz=UTC).isoformat(),
                "enabled": True,
                "dry_run": False,
                "failed": True,
                "error_message": str(exc),
                "cycles": cycles,
                "jobs": executed,
                "running_jobs": list(inflight.values()) or running_jobs,
                "queued_jobs": [],
            }
            self.stop_scheduler(result=result, state="failed")
            logger.exception("持久调度循环异常退出 cycles=%s", cycles)
            return result

        result = {
            "mode": "loop",
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(tz=UTC).isoformat(),
            "enabled": True,
            "dry_run": False,
            "interrupted": False,
            "cycles": cycles,
            "jobs": executed,
        }
        self.stop_scheduler(result=result, state="completed")
        return result

    def run_loop(self, *, dry_run: bool = False, max_cycles: int | None = None) -> JsonDict:
        """进入轻量循环模式，按 interval_seconds 调度任务。"""

        started_at = datetime.now(tz=UTC)
        self.started_at = started_at
        logger.info(
            "基础数据调度器启动 mode=loop dry_run=%s enabled_jobs=%s max_cycles=%s "
            "max_concurrent_jobs=%s",
            dry_run,
            len(self.enabled_jobs()),
            max_cycles,
            self.config.max_concurrent_jobs,
        )
        self.emit_event("scheduler_start", mode="loop", dry_run=dry_run)
        self.write_status(
            state="running",
            mode="loop",
            dry_run=dry_run,
            started_at=started_at,
        )
        if not self.config.enabled:
            result = {
                "mode": "loop",
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(tz=UTC).isoformat(),
                "enabled": False,
                "dry_run": dry_run,
                "cycles": 0,
                "jobs": [],
            }
            self.stop_scheduler(result=result, state="disabled")
            logger.info("基础数据调度器已禁用 mode=loop")
            return result

        if self._supports_persistent_loop(dry_run=dry_run):
            return self._run_persistent_loop(
                started_at=started_at,
                max_cycles=max_cycles,
            )

        self._recover_expired_persistent_tasks(force=True)
        self._run_recovery_startup_scan()
        states = [
            ScheduledJobState(job=job, next_run_at=next_run_at_for_job(job, now=started_at))
            for job in self.enabled_jobs()
        ]
        cycles = 0
        executed: list[JsonDict] = []
        running: dict[Future[JsonDict], ScheduledJobState] = {}
        queued: list[ScheduledJobState] = []
        scheduler_config_mtime_ns = (
            self.scheduler_config_file.stat().st_mtime_ns
            if self.scheduler_config_file and self.scheduler_config_file.exists()
            else None
        )

        def running_job_names() -> list[str]:
            return [state.job.name for state in running.values()]

        def queued_job_names() -> list[str]:
            return [state.job.name for state in queued]

        def reload_runtime_config_if_changed() -> None:
            """调度循环中热重载资源池、全局并发和任务配置。"""

            nonlocal states, queued, scheduler_config_mtime_ns
            if self.scheduler_config_file is None or not self.scheduler_config_file.exists():
                return
            current_mtime_ns = self.scheduler_config_file.stat().st_mtime_ns
            if scheduler_config_mtime_ns == current_mtime_ns:
                return
            try:
                new_config = load_scheduler_config(self.scheduler_config_file)
                new_identity = load_config_identity(self.scheduler_config_file)
            except Exception as exc:  # noqa: BLE001 - 配置文件可能正处于保存替换窗口
                self.config_reload_error = f"{type(exc).__name__}: {exc}"
                logger.warning("调度配置热重载失败，将保留当前配置并在下一轮重试：%s", exc)
                return
            if not new_config.enabled:
                logger.info("调度配置已热重载且全局禁用，当前 loop 将等待现有任务自然结束。")
            states = merge_scheduler_runtime_states(
                states,
                new_config,
                now=datetime.now(tz=UTC),
            )
            queued = [state for state in states if state.queued]
            self.config = new_config
            self.config_identity = new_identity
            self.config_reload_error = None
            scheduler_config_mtime_ns = current_mtime_ns
            logger.info(
                "调度配置已热重载 max_concurrent_jobs=%s resource_pools=%s enabled_jobs=%s",
                self.config.max_concurrent_jobs,
                sorted(self.config.resource_pools),
                len(self.enabled_jobs()),
            )

        def fill_worker_slots(executor: ThreadPoolExecutor) -> None:
            self._claim_recovery_tasks(
                executor,
                free_slots=max(
                    0,
                    int(self.config.max_concurrent_jobs)
                    - len(running)
                    - len(queued)
                    - self._recovery_inflight(),
                ),
            )
            while queued and len(running) < self.config.max_concurrent_jobs:
                pool_counts = scheduler_resource_pool_counts(list(running.values()))
                selected_index: int | None = None
                for index, candidate in enumerate(queued):
                    pool_name = scheduler_job_resource_pool(candidate.job)
                    pool_limit = scheduler_resource_pool_limit(self.config, pool_name)
                    if pool_counts.get(pool_name, 0) < pool_limit:
                        selected_index = index
                        break
                if selected_index is None:
                    break
                state = queued.pop(selected_index)
                state.queued = False
                persistent_claim: TaskClaim | None = None
                if self._persistent_task_queue_scope is not None and not dry_run:
                    scheduled_at = state.next_run_at or datetime.now(tz=UTC)
                    persistent_claim = self._claim_persistent_task(
                        state.job,
                        scheduled_at=scheduled_at,
                        dry_run=dry_run,
                    )
                    if persistent_claim is None:
                        # 任务可能已经由另一个进程领取或完成；推进本地水位，避免
                        # 当前进程在同一个幂等键上忙等。
                        state.next_run_at = next_run_after_completion(
                            state.job,
                            completed_at=datetime.now(tz=UTC),
                        )
                        logger.info(
                            "持久化任务未领取，跳过本轮 job=%s scheduled_at=%s",
                            state.job.name,
                            scheduled_at.isoformat(),
                        )
                        continue
                state.running = True
                if persistent_claim is None:
                    future = executor.submit(self.run_job, state.job, dry_run=dry_run)
                else:
                    future = executor.submit(
                        self._run_persistent_job,
                        state.job,
                        claim=persistent_claim,
                        dry_run=dry_run,
                    )
                running[future] = state

        def write_loop_status() -> None:
            try:
                self.write_status(
                    state="running",
                    mode="loop",
                    dry_run=dry_run,
                    started_at=started_at,
                    cycles=cycles,
                    running_jobs=running_job_names(),
                    queued_jobs=queued_job_names(),
                    max_concurrent_jobs=self.config.max_concurrent_jobs,
                )
            except OSError as exc:
                # 状态文件被其他进程短暂占用时，不应终止调度主循环。
                logger.warning("调度器心跳状态写入失败，将在下一轮重试：%s", exc)
        try:
            with ThreadPoolExecutor(
                max_workers=max(DEFAULT_THREAD_POOL_MAX_WORKERS, self.config.max_concurrent_jobs),
                thread_name_prefix="base-data-job",
            ) as executor:
                while states:
                    completed = [
                        future
                        for future in running
                        if future.done()
                    ]
                    for future in completed:
                        state = running.pop(future)
                        state.running = False
                        state.queued = False
                        try:
                            summary = future.result()
                        except Exception as exc:  # pragma: no cover - run_job 自身会兜底
                            summary = {
                                "job": state.job.name,
                                "job_type": state.job.job_type,
                                "market": state.job.market,
                                "status": "failed",
                                "error_message": str(exc),
                                "finished_at": datetime.now(tz=UTC).isoformat(),
                            }
                        state.last_run_at = datetime.now(tz=UTC)
                        state.last_summary = summary
                        state.next_run_at = next_run_after_completion(
                            state.job,
                            completed_at=state.last_run_at,
                        )
                        if summary.get("status") == "executed":
                            success_generation = state.pending_generation or cycles
                            state.last_success_generation = success_generation
                            state.pending_generation = None
                            trigger_after_success_dependents(
                                states,
                                completed_job_name=state.job.name,
                                triggered_at=state.last_run_at,
                                completion_generation=success_generation,
                            )
                        executed.append(
                            summary
                            | {
                                "last_run_at": state.last_run_at.isoformat(),
                                "next_run_at": (
                                    state.next_run_at.isoformat()
                                    if state.next_run_at is not None
                                    else None
                                ),
                            }
                        )

                    reload_runtime_config_if_changed()
                    self._recover_expired_persistent_tasks()
                    fill_worker_slots(executor)

                    if max_cycles is not None and cycles >= max_cycles and not running:
                        break

                    if (
                        max_cycles is not None
                        and cycles >= max_cycles
                        and running
                    ):
                        done, _ = wait(
                            running.keys(),
                            timeout=max(0.1, float(self.config.loop_idle_seconds)),
                            return_when=FIRST_COMPLETED,
                        )
                        if not done:
                            write_loop_status()
                        continue

                    now = datetime.now(tz=UTC)
                    due_states = [
                        state
                        for state in states
                        if (
                            not state.running
                            and not state.queued
                            and state.next_run_at is not None
                            and state.next_run_at <= now
                        )
                    ]
                    due_states, dependency_blocked_states = filter_due_states_by_dependencies(
                        due_states,
                        all_states=states,
                    )
                    if dependency_blocked_states:
                        logger.info(
                            "任务依赖未满足，暂缓本轮调度 blocked_jobs=%s",
                            [state.job.name for state in dependency_blocked_states],
                        )
                    due_states, blocked_due_states = filter_due_states_by_market_universe(
                        due_states,
                        active_states=[*running.values(), *queued],
                    )
                    blocked_due_states.extend(dependency_blocked_states)
                    if blocked_due_states:
                        logger.info(
                            "同市场资产池刷新优先执行，暂缓依赖任务 blocked_jobs=%s active_jobs=%s",
                            [state.job.name for state in blocked_due_states],
                            [state.job.name for state in [*running.values(), *queued]],
                        )
                    due_states, mutex_blocked_states = filter_due_states_by_mutex_key(
                        due_states,
                        active_states=[*running.values(), *queued],
                    )
                    if mutex_blocked_states:
                        blocked_due_states.extend(mutex_blocked_states)
                        logger.info(
                            "同一互斥键任务正在运行或排队，暂缓本轮调度 blocked_jobs=%s active_jobs=%s",
                            [state.job.name for state in mutex_blocked_states],
                            [state.job.name for state in [*running.values(), *queued]],
                        )
                    due_states, recovery_blocked_pairs = self._filter_by_recovery_gate(
                        due_states
                    )
                    if recovery_blocked_pairs:
                        blocked_due_states.extend(
                            state for state, _decision in recovery_blocked_pairs
                        )
                        logger.info(
                            "补跑门控拦截派生任务 blocked_jobs=%s reason=blocked_by_recovery",
                            [state.job.name for state, _decision in recovery_blocked_pairs],
                        )
                    if blocked_due_states and not due_states:
                        write_loop_status()
                        if running or queued:
                            wait(
                                running.keys(),
                                timeout=max(0.1, float(self.config.loop_idle_seconds)),
                                return_when=FIRST_COMPLETED,
                            )
                        else:
                            self._sleep(float(self.config.loop_idle_seconds))
                        continue
                    if not due_states:
                        write_loop_status()
                        waiting_states = [
                            state
                            for state in states
                            if not state.running and not state.queued
                        ]
                        if running or queued:
                            wait(
                                running.keys(),
                                timeout=seconds_until_next_run(
                                    waiting_states,
                                    self.config.loop_idle_seconds,
                                ),
                                return_when=FIRST_COMPLETED,
                            )
                        else:
                            self._sleep(
                                seconds_until_next_run(states, self.config.loop_idle_seconds)
                            )
                        continue

                    cycles += 1
                    due_states = sorted(
                        due_states,
                        key=lambda state: state.job.priority,
                        reverse=True,
                    )
                    for state in due_states:
                        state.queued = True
                        queued.append(state)
                        logger.info(
                            "调度任务入队 job=%s job_type=%s market=%s queued_jobs=%s "
                            "running_jobs=%s",
                            state.job.name,
                            state.job.job_type,
                            state.job.market,
                            queued_job_names(),
                            running_job_names(),
                        )
                    reload_runtime_config_if_changed()
                    fill_worker_slots(executor)
                    write_loop_status()
        except KeyboardInterrupt:
            result = {
                "mode": "loop",
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(tz=UTC).isoformat(),
                "enabled": True,
                "dry_run": dry_run,
                "interrupted": True,
                "cycles": cycles,
                "jobs": executed,
                "running_jobs": running_job_names(),
                "queued_jobs": queued_job_names(),
            }
            self.stop_scheduler(result=result, state="interrupted")
            logger.warning(
                "基础数据调度器被中断 mode=loop cycles=%s running_jobs=%s queued_jobs=%s",
                cycles,
                running_job_names(),
                queued_job_names(),
            )
            return result
        except Exception as exc:  # noqa: BLE001 - 调度器主循环边界需要落盘失败状态
            result = {
                "mode": "loop",
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(tz=UTC).isoformat(),
                "enabled": True,
                "dry_run": dry_run,
                "failed": True,
                "error_message": str(exc),
                "cycles": cycles,
                "jobs": executed,
                "running_jobs": running_job_names(),
                "queued_jobs": queued_job_names(),
            }
            self.stop_scheduler(result=result, state="failed")
            logger.exception(
                "基础数据调度器异常退出 mode=loop cycles=%s running_jobs=%s queued_jobs=%s",
                cycles,
                running_job_names(),
                queued_job_names(),
            )
            return result

        result = {
            "mode": "loop",
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(tz=UTC).isoformat(),
            "enabled": True,
            "dry_run": dry_run,
            "interrupted": False,
            "cycles": cycles,
            "jobs": executed,
        }
        self.stop_scheduler(result=result, state="completed")
        logger.info(
            "基础数据调度器完成 mode=loop dry_run=%s cycles=%s job_count=%s",
            dry_run,
            cycles,
            len(executed),
        )
        return result

    def run_job(self, job: BaseDataSchedulerJob, *, dry_run: bool = False) -> JsonDict:
        """执行单个启用任务。"""

        started_at = datetime.now(tz=UTC)
        logger.info(
            "调度任务开始 job=%s job_type=%s market=%s dry_run=%s",
            job.name,
            job.job_type,
            job.market,
            dry_run,
        )
        planned = {
            "job": job.name,
            "job_type": job.job_type,
            "group": job.group,
            "market": job.market,
            "interval_seconds": job.interval_seconds,
            "started_at": started_at.isoformat(),
        }
        args: Any = None
        progress: BaseDataTaskProgressRecorder | None = None
        progress_run_id: str | None = None
        if job.job_type == "collection":
            args = self.build_collection_args(job)
            planned["collection_args"] = vars(args)
        elif job.job_type == "recommendation_pipeline":
            planned["recommendation_args"] = self.build_recommendation_pipeline_kwargs(job)
        elif job.job_type == "data_quality_refresh":
            planned["data_quality_args"] = self.build_data_quality_refresh_kwargs(job)
        elif job.job_type == "technical_screening_refresh":
            planned["technical_screening_args"] = self.build_technical_screening_refresh_kwargs(job)
        elif job.job_type == "structural_methodology_refresh":
            planned["structural_methodology_args"] = self.build_structural_methodology_refresh_kwargs(job)
        elif job.job_type == "decision_context_refresh":
            planned["decision_context_args"] = self.build_decision_context_refresh_kwargs(job)
        elif job.job_type == "trigger_evaluation":
            planned["trigger_evaluation_args"] = self.build_trigger_evaluation_kwargs(job)
        elif job.job_type == "agent_loop_consume":
            planned["agent_loop_consume_args"] = self.build_agent_loop_consume_kwargs(job)
        elif job.job_type == "high_risk_reviews":
            planned["high_risk_review_args"] = self.build_high_risk_review_kwargs(job)
        elif job.job_type == "reviews_due":
            planned["reviews_due_args"] = self.build_reviews_due_kwargs(job)
        elif job.job_type == "universe_merge":
            planned["universe_merge_args"] = self.build_universe_merge_kwargs(job)
        elif job.job_type == "universe_avoid_pool_rebuild":
            planned["avoid_pool_rebuild_args"] = self.build_avoid_pool_rebuild_kwargs(job)
        elif job.job_type == "backtest_run":
            planned["backtest_args"] = self.build_backtest_kwargs(job)
        else:
            raise ValueError(f"不支持的调度任务类型：{job.job_type}")

        if dry_run:
            summary = planned | {"dry_run": True, "status": "planned", "attempt_count": 0}
            self.emit_event("job_planned", job=job.name, market=job.market)
            self.write_status(
                state="running",
                last_job=job.name,
                last_job_status="planned",
                last_job_at=datetime.now(tz=UTC),
            )
            logger.info(
                "调度任务预演完成 job=%s job_type=%s market=%s",
                job.name,
                job.job_type,
                job.market,
            )
            return summary

        if job.job_type == "collection":
            try:
                progress = self._progress
                progress_run_id = progress.job_started(
                    job_name=job.name,
                    title=str(job.params.get("title") or job.name),
                    market=job.market,
                    task_type=str(job.params.get("sync_task_type") or job.job_type),
                    interval_seconds=job.interval_seconds,
                    max_workers=collection_progress_max_workers(
                        args,
                        fallback=self.config.max_concurrent_jobs,
                    ),
                )
                args.progress_job_name = job.name
                args.progress_run_id = progress_run_id
                args.progress_ttl_seconds = max(job.interval_seconds + 1800, 900)
                args.progress_cache_backend = progress.cache_backend
            except Exception as exc:
                progress = None
                progress_run_id = None
                logger.warning("初始化调度任务进度失败 job=%s error=%s", job.name, exc)

        max_retries = self.config.max_job_retries if job.max_retries is None else job.max_retries
        max_attempts = max_retries + 1
        self.emit_event("job_start", job=job.name, market=job.market, max_attempts=max_attempts)
        self.write_status(
            state="running",
            last_job=job.name,
            last_job_status="running",
            last_job_at=started_at,
            last_error=None,
        )
        last_error: str | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(
                    "调度任务执行中 job=%s attempt=%s/%s",
                    job.name,
                    attempt,
                    max_attempts,
                )
                summary = self.execute_job(job, args=args)
            except Exception as exc:
                last_error = str(exc)
                is_timeout = isinstance(exc, JobTimeoutError)
                logger.exception(
                    "调度任务失败 job=%s attempt=%s/%s timeout=%s error=%s",
                    job.name,
                    attempt,
                    max_attempts,
                    is_timeout,
                    last_error,
                )
                self.emit_event(
                    "job_timeout" if is_timeout else "job_error",
                    job=job.name,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    error_message=last_error,
                )
                if attempt < max_attempts and not is_timeout:
                    logger.warning(
                        "调度任务准备重试 job=%s next_attempt=%s backoff_seconds=%s",
                        job.name,
                        attempt + 1,
                        self.config.retry_backoff_seconds,
                    )
                    self.emit_event(
                        "job_retry",
                        job=job.name,
                        attempt=attempt,
                        next_attempt=attempt + 1,
                        retry_backoff_seconds=self.config.retry_backoff_seconds,
                    )
                    self._sleep(float(self.config.retry_backoff_seconds))
                    continue
                failed = planned | {
                    "dry_run": False,
                    "status": "failed",
                    "attempt_count": attempt,
                    "error_message": last_error,
                    "finished_at": datetime.now(tz=UTC).isoformat(),
                }
                self.write_status(
                    state="running",
                    last_job=job.name,
                    last_job_status="failed",
                    last_job_at=datetime.now(tz=UTC),
                    last_error=last_error,
                )
                logger.error(
                    "调度任务最终失败 job=%s attempts=%s error=%s",
                    job.name,
                    attempt,
                    last_error,
                )
                if progress is not None:
                    progress.job_failed(
                        job_name=job.name,
                        run_id=progress_run_id,
                        error_message=last_error or "unknown error",
                        summary=failed,
                    )
                return failed

            outcome_status = scheduler_job_outcome_status(job, summary)
            executed = planned | {
                "dry_run": False,
                "status": outcome_status,
                "attempt_count": attempt,
                "summary": summary,
                "finished_at": datetime.now(tz=UTC).isoformat(),
            }
            self.emit_event(
                "job_success" if outcome_status == "executed" else "job_blocked",
                job=job.name,
                attempt=attempt,
                summary=compact_collection_summary(summary),
            )
            self.write_status(
                state="running",
                last_job=job.name,
                last_job_status=outcome_status,
                last_job_at=datetime.now(tz=UTC),
                last_error=None,
            )
            logger.info(
                "调度任务完成 job=%s job_type=%s market=%s attempt=%s status=%s summary=%s",
                job.name,
                job.job_type,
                job.market,
                attempt,
                outcome_status,
                compact_collection_summary(summary),
            )
            if progress is not None:
                progress.job_completed(
                    job_name=job.name,
                    run_id=progress_run_id,
                    status="completed",
                    summary=summary if isinstance(summary, dict) else {},
                )
            return executed

        raise RuntimeError("任务重试循环出现不可达状态")

    def enabled_jobs(self) -> tuple[BaseDataSchedulerJob, ...]:
        """返回全局和任务开关都启用的任务。"""

        if not self.config.enabled:
            return ()
        return tuple(job for job in self.config.jobs if job.enabled)

    def execute_job(self, job: BaseDataSchedulerJob, *, args: Any) -> JsonDict:
        """按任务类型分派到采集或采集后分析执行器。"""

        if job.job_type == "collection":
            return self.collect_base_data(args, job_name=job.name)
        if job.job_type == "recommendation_pipeline":
            kwargs = self.build_recommendation_pipeline_kwargs(job)
            return self.run_recommendation_pipeline(**kwargs)
        if job.job_type == "data_quality_refresh":
            kwargs = self.build_data_quality_refresh_kwargs(job)
            return self.run_data_quality_refresh(**kwargs)
        if job.job_type == "technical_screening_refresh":
            kwargs = self.build_technical_screening_refresh_kwargs(job)
            return self.run_technical_screening_refresh(**kwargs)
        if job.job_type == "structural_methodology_refresh":
            kwargs = self.build_structural_methodology_refresh_kwargs(job)
            return self.run_structural_methodology_refresh(**kwargs)
        if job.job_type == "decision_context_refresh":
            kwargs = self.build_decision_context_refresh_kwargs(job)
            return self.run_decision_context_refresh(**kwargs)
        if job.job_type == "trigger_evaluation":
            kwargs = self.build_trigger_evaluation_kwargs(job)
            return self.run_trigger_evaluation(**kwargs)
        if job.job_type == "agent_loop_consume":
            kwargs = self.build_agent_loop_consume_kwargs(job)
            return self.run_agent_loop_consume(**kwargs)
        if job.job_type == "high_risk_reviews":
            kwargs = self.build_high_risk_review_kwargs(job)
            return self.run_high_risk_reviews(**kwargs)
        if job.job_type == "reviews_due":
            kwargs = self.build_reviews_due_kwargs(job)
            return self.run_reviews_due(**kwargs)
        if job.job_type == "universe_merge":
            kwargs = self.build_universe_merge_kwargs(job)
            return self.run_universe_merge(**kwargs)
        if job.job_type == "universe_avoid_pool_rebuild":
            kwargs = self.build_avoid_pool_rebuild_kwargs(job)
            return self.run_avoid_pool_rebuild(**kwargs)
        if job.job_type == "backtest_run":
            kwargs = self.build_backtest_kwargs(job)
            return self.run_backtest(**kwargs)
        raise ValueError(f"不支持的调度任务类型：{job.job_type}")

    def build_collection_args(self, job: BaseDataSchedulerJob) -> Any:
        """把任务配置转换为 collect_base_data 可接受的参数对象。"""

        if job.job_type != "collection":
            raise ValueError(f"{job.name} 不是基础采集任务，不能生成采集参数")
        if isinstance(job.group, tuple):
            group_value: list[str] = list(job.group)
        else:
            group_value = [job.group]
        overrides = {
            "group": group_value,
            "cache_backend": self.config.cache_backend,
            "lock_ttl_seconds": self.config.lock_ttl_seconds,
            "circuit_failure_threshold": self.config.circuit_failure_threshold,
            "circuit_cooldown_seconds": self.config.circuit_cooldown_seconds,
            "force_provider": self.config.force_provider,
            "rate_policies": self.config.rate_policies,
        }
        if self.scheduler_config_file is not None:
            overrides["runtime_scheduler_config_file"] = str(self.scheduler_config_file)
        if job.limit is not None:
            overrides["limit"] = job.limit
        elif (
            job.market == "fund"
            and str(job.params.get("sync_task_type") or "")
            in {"market_bars_full_history_backfill", "fund_nav_full_history_backfill"}
        ):
            overrides["limit"] = None
        overrides.update(job.params)
        if (
            job.market == "ashare"
            and str(overrides.get("sync_task_type") or "") == "risk_sentiment_refresh"
            and (not overrides.get("risk_start") or not overrides.get("risk_end"))
        ):
            risk_dates = build_ashare_lookback_date_overrides("14d")
            overrides.setdefault("risk_start", risk_dates["ashare_start"])
            overrides.setdefault("risk_end", risk_dates["ashare_end"])
        if (
            job.market == "ashare"
            and str(overrides.get("sync_task_type") or "")
            in {
                "market_bars_backfill",
                "market_bars_full_history_backfill",
                "market_bars_midday_partial",
                "market_bars_close_final",
                "market_bars_revision",
            }
            and overrides.get("lookback")
        ):
            overrides.update(build_ashare_lookback_date_overrides(str(overrides["lookback"])))
        elif (
            job.market == "fund"
            and str(overrides.get("sync_task_type") or "")
            in {
                "market_bars_full_history_backfill",
                "market_bars_close_final",
                "fund_nav_full_history_backfill",
                "fund_nav_daily",
            }
            and overrides.get("lookback")
        ):
            fund_sync_task_type = str(overrides.get("sync_task_type") or "")
            overrides.update(
                build_fund_lookback_date_overrides(
                    str(overrides["lookback"]),
                    completed_only=fund_sync_task_type
                    in {
                        "market_bars_full_history_backfill",
                        "fund_nav_full_history_backfill",
                    },
                )
            )
        elif (
            job.market == "ashare"
            and str(overrides.get("sync_task_type") or "")
            == "market_bars_full_history_backfill"
            and not overrides.get("ashare_end")
        ):
            overrides["ashare_end"] = datetime.now(tz=UTC).strftime("%Y%m%d")
        return self.default_collection_args(**overrides)

    def build_recommendation_pipeline_kwargs(self, job: BaseDataSchedulerJob) -> JsonDict:
        """把推荐流水线任务配置转换为候选池流水线参数。"""

        if job.job_type != "recommendation_pipeline":
            raise ValueError(f"{job.name} 不是推荐流水线任务")
        universe_id = str(job.params.get("universe_id") or "").strip()
        if not universe_id:
            raise ValueError(f"{job.name}.params.universe_id 不能为空")
        params: JsonDict = {
            "universe_id": universe_id,
            "strategy": str(job.params.get("strategy") or "balanced_swing_v1"),
            "horizon": str(job.params.get("horizon") or "swing"),
            "limit": job.limit or int(job.params.get("limit") or 20),
        }
        for key in (
            "timeframe",
            "source",
            "candidate_source",
            "technical_screening_strategy",
            "strategy_id",
            "avoid_universe_id",
        ):
            value = job.params.get(key)
            if value is not None:
                params[key] = value
        strategy_ids = job.params.get("strategy_ids")
        if strategy_ids is not None:
            if not isinstance(strategy_ids, list | tuple):
                raise ValueError(f"{job.name}.params.strategy_ids 必须是列表")
            params["strategy_ids"] = [str(item) for item in strategy_ids]
        for key in ("window", "min_bars", "min_available_factor_groups"):
            value = job.params.get(key)
            if value is not None:
                params[key] = int(value)
        for key in ("min_indicator_coverage_ratio", "min_factor_coverage_ratio"):
            value = job.params.get(key)
            if value is not None:
                params[key] = float(value)
        value = job.params.get("auto_sync_watchlist")
        if value is not None:
            params["auto_sync_watchlist"] = bool(value)
        value = job.params.get("observation_enabled")
        if value is not None:
            params["observation_enabled"] = bool(value)
        value = job.params.get("round_trip_cost")
        if value is not None:
            params["round_trip_cost"] = float(value)
        for key in ("owner_id", "watchlist_id"):
            value = job.params.get(key)
            if value is not None:
                params[key] = str(value)
        value = job.params.get("recommendation_intake_limit")
        if value is not None:
            params["recommendation_intake_limit"] = int(value)
        return params

    def build_decision_context_refresh_kwargs(self, job: BaseDataSchedulerJob) -> JsonDict:
        """把收盘决策上下文任务配置转换为刷新参数。"""

        if job.job_type != "decision_context_refresh":
            raise ValueError(f"{job.name} 不是决策上下文任务")
        context_type = str(job.params.get("context_type") or "").strip()
        if context_type not in {"market", "sector"}:
            raise ValueError(f"{job.name}.params.context_type 必须是 market 或 sector")
        market = str(job.params.get("market") or job.market or "").strip()
        universe_id = str(job.params.get("universe_id") or "").strip()
        if not market or not universe_id:
            raise ValueError(f"{job.name} 缺少 market 或 universe_id")
        return {
            "context_type": context_type,
            "market": market,
            "universe_id": universe_id,
            "lookback_bars": int(job.params.get("lookback_bars") or 61),
        }

    def build_data_quality_refresh_kwargs(self, job: BaseDataSchedulerJob) -> JsonDict:
        """把数据质量任务配置转换为刷新服务参数。"""

        if job.job_type != "data_quality_refresh":
            raise ValueError(f"{job.name} 不是数据质量刷新任务")
        market = str(job.params.get("market") or job.market or "").strip()
        if not market:
            raise ValueError(f"{job.name}.params.market 不能为空")
        domains = job.params.get("data_domains")
        data_domains = (
            [str(item) for item in domains if str(item).strip()]
            if isinstance(domains, list | tuple)
            else []
        )
        params: JsonDict = {
            "market": market,
            "timeframe": str(job.params.get("timeframe") or ("1d" if market == "ashare" else "1h")),
            "limit": job.limit or int(job.params.get("limit") or 200),
            "min_bars": int(job.params.get("min_bars") or (60 if market == "ashare" else 120)),
            "stale_after_seconds": int(job.params.get("stale_after_seconds") or 24 * 60 * 60),
        }
        if data_domains:
            params["data_domains"] = data_domains
        value = job.params.get("horizon")
        if value is not None:
            params["horizon"] = str(value)
        return params

    def build_technical_screening_refresh_kwargs(self, job: BaseDataSchedulerJob) -> JsonDict:
        """把技术初筛任务配置转换为刷新服务参数。"""

        if job.job_type != "technical_screening_refresh":
            raise ValueError(f"{job.name} 不是技术初筛刷新任务")
        market = str(job.params.get("market") or job.market or "").strip()
        if not market:
            raise ValueError(f"{job.name}.params.market 不能为空")
        params: JsonDict = {
            "market": market,
            "universe_id": str(
                job.params.get("universe_id")
                or (
                    "universe:technical:ashare:main_board"
                    if market == "ashare"
                    else f"universe:technical:{market}"
                )
            ),
            "timeframe": str(job.params.get("timeframe") or "1d"),
            "limit": job.limit or int(job.params.get("limit") or 200),
            "min_bars": int(job.params.get("min_bars") or 250),
            "ttl_days": int(job.params.get("ttl_days") or 3),
        }
        value = job.params.get("source")
        if value is not None:
            params["source"] = str(value)
        return params

    def build_structural_methodology_refresh_kwargs(self, job: BaseDataSchedulerJob) -> JsonDict:
        """把结构方法论任务配置转换为刷新服务参数。"""

        if job.job_type != "structural_methodology_refresh":
            raise ValueError(f"{job.name} 不是结构方法论刷新任务")
        market = str(job.params.get("market") or job.market or "").strip()
        if not market:
            raise ValueError(f"{job.name}.params.market 不能为空")

        def parse_string_list(value: Any) -> list[str]:
            if isinstance(value, list | tuple):
                return [str(item).strip() for item in value if str(item).strip()]
            if value is None:
                return []
            return [str(value).strip()] if str(value).strip() else []

        params: JsonDict = {
            "market": market,
            "timeframe": str(job.params.get("timeframe") or "1d"),
            "engines": parse_string_list(
                job.params.get("engines")
                or ["swings", "smc", "harmonic", "elliott", "ichimoku"]
            ),
            "lookback_bars": int(job.params.get("lookback_bars") or 250),
        }
        timeframes = parse_string_list(job.params.get("timeframes"))
        if timeframes:
            params["timeframes"] = timeframes
        if job.limit is not None:
            params["limit"] = int(job.limit)
        for key in (
            "universe_ids",
            "source",
            "swing_window",
            "harmonic_tolerance",
            "harmonic_max_bars_since_d",
            "fvg_min_atr_ratio",
            "fvg_include_mitigated",
            "elliott_confidence_threshold",
            "min_bars_per_wave",
        ):
            value = job.params.get(key)
            if value is None:
                continue
            if key == "universe_ids":
                values = parse_string_list(value)
                if values:
                    params[key] = values
            elif key in {"swing_window", "harmonic_max_bars_since_d", "min_bars_per_wave"}:
                params[key] = int(value)
            elif key == "fvg_include_mitigated":
                params[key] = bool(value)
            elif key in {
                "harmonic_tolerance",
                "fvg_min_atr_ratio",
                "elliott_confidence_threshold",
            }:
                params[key] = float(value)
            else:
                params[key] = str(value)
        return params

    def build_trigger_evaluation_kwargs(self, job: BaseDataSchedulerJob) -> JsonDict:
        """把触发评估任务配置转换为触发服务参数。"""

        if job.job_type != "trigger_evaluation":
            raise ValueError(f"{job.name} 不是触发评估任务")
        groups = job.params.get("trigger_groups")
        trigger_groups = (
            [str(item) for item in groups if str(item).strip()]
            if isinstance(groups, list | tuple)
            else []
        )
        params: JsonDict = {
            "owner_id": str(job.params.get("owner_id") or "default-owner"),
            "dispatch": as_bool(
                job.params.get("dispatch", True),
                field_name=f"{job.name}.params.dispatch",
            ),
            "max_events_per_run": int(job.params.get("max_events_per_run") or job.limit or 50),
        }
        if trigger_groups:
            params["trigger_groups"] = trigger_groups
        for key in (
            "portfolio_id",
            "watchlist_id",
            "recommendation_run_id",
            "horizon",
            "timeframe",
            "intraday_sharp_drop_threshold",
            "intraday_volume_surge_multiplier",
            "agent_runtime",
        ):
            value = job.params.get(key)
            if value is not None:
                params[key] = str(value)
        for key in ("since_minutes", "cooldown_minutes", "recommendation_limit"):
            value = job.params.get(key)
            if value is not None:
                params[key] = int(value)
        value = job.params.get("intraday_quote_window_minutes")
        if value is not None:
            params["intraday_quote_window_minutes"] = int(value)
        return params

    def build_agent_loop_consume_kwargs(self, job: BaseDataSchedulerJob) -> JsonDict:
        """把 Agent 事件消费任务配置转换为内部 Agent Loop 参数。"""

        if job.job_type != "agent_loop_consume":
            raise ValueError(f"{job.name} 不是 Agent 事件消费任务")
        return {
            "owner_id": str(job.params.get("owner_id") or "default-owner"),
            "limit": int(job.params.get("limit") or job.limit or 10),
            "use_model_planner": as_bool(
                job.params.get("use_model_planner", True),
                field_name=f"{job.name}.params.use_model_planner",
            ),
        }

    def build_high_risk_review_kwargs(self, job: BaseDataSchedulerJob) -> JsonDict:
        """把高风险复核任务配置转换为复核服务参数。"""

        if job.job_type != "high_risk_reviews":
            raise ValueError(f"{job.name} 不是高风险复核任务")
        return {
            "owner_id": str(job.params.get("owner_id") or "default-owner"),
            "limit": int(job.params.get("limit") or job.limit or 10),
        }

    def build_reviews_due_kwargs(self, job: BaseDataSchedulerJob) -> JsonDict:
        """把到期复盘任务配置转换为人工操作闭环服务参数。"""

        if job.job_type != "reviews_due":
            raise ValueError(f"{job.name} 不是到期复盘任务")
        return {
            "owner_id": str(job.params.get("owner_id") or "default-owner"),
            "limit": int(job.params.get("limit") or job.limit or 20),
        }

    def build_universe_merge_kwargs(self, job: BaseDataSchedulerJob) -> JsonDict:
        """把候选池合并任务配置转换为数据生产服务参数。"""

        if job.job_type != "universe_merge":
            raise ValueError(f"{job.name} 不是候选池合并任务")
        target_universe_id = str(job.params.get("target_universe_id") or "").strip()
        if not target_universe_id:
            raise ValueError(f"{job.name}.params.target_universe_id 不能为空")
        name = str(job.params.get("name") or "").strip()
        if not name:
            raise ValueError(f"{job.name}.params.name 不能为空")
        source_universe_ids = parse_string_list(job.params.get("source_universe_ids"))
        if not source_universe_ids:
            raise ValueError(f"{job.name}.params.source_universe_ids 不能为空")
        source_weights = parse_float_mapping(job.params.get("source_weights"))
        return {
            "target_universe_id": target_universe_id,
            "name": name,
            "source_universe_ids": source_universe_ids,
            "source_weights": source_weights,
            "strategy_context": str(
                job.params.get("strategy_context") or "recommendation_universe_merge"
            ),
        }

    def build_avoid_pool_rebuild_kwargs(self, job: BaseDataSchedulerJob) -> JsonDict:
        """把回避池重建任务配置转换为数据生产服务参数。"""

        if job.job_type != "universe_avoid_pool_rebuild":
            raise ValueError(f"{job.name} 不是回避池重建任务")
        universe_id = str(job.params.get("universe_id") or "").strip()
        if not universe_id:
            raise ValueError(f"{job.name}.params.universe_id 不能为空")
        name = str(job.params.get("name") or "").strip()
        if not name:
            raise ValueError(f"{job.name}.params.name 不能为空")
        market = str(job.params.get("market") or job.market or "").strip()
        if not market:
            raise ValueError(f"{job.name}.params.market 不能为空")
        return {
            "universe_id": universe_id,
            "name": name,
            "market": market,
            "strategy_context": str(job.params.get("strategy_context") or "avoid_pool"),
        }

    def build_backtest_kwargs(self, job: BaseDataSchedulerJob) -> JsonDict:
        """把回测任务配置转换为回测运行参数。"""

        if job.job_type != "backtest_run":
            raise ValueError(f"{job.name} 不是回测任务")
        market = str(job.params.get("market") or job.market or "").strip()
        if not market:
            raise ValueError(f"{job.name}.params.market 不能为空")
        universe_id = str(job.params.get("universe_id") or "").strip()
        if not universe_id:
            raise ValueError(f"{job.name}.params.universe_id 不能为空")
        strategy_id = str(job.params.get("strategy_id") or "").strip()
        if not strategy_id:
            raise ValueError(f"{job.name}.params.strategy_id 不能为空")
        return {
            "market": market,
            "strategy": str(job.params.get("strategy") or "factor_score_topn"),
            "universe_id": universe_id,
            "strategy_id": strategy_id,
            "years": int(job.params.get("years") or 5),
            "score_mode": str(job.params.get("score_mode") or "replayed"),
            "topn": int(job.params.get("topn") or job.limit or 20),
            "rebalance": str(job.params.get("rebalance") or "once"),
            "timeframe": str(job.params.get("timeframe") or "1d"),
        }

    def run_recommendation_pipeline(self, **kwargs: Any) -> JsonDict:
        """执行候选池推荐流水线。"""

        if self._run_recommendation_pipeline is not None:
            return self._run_recommendation_pipeline(**kwargs)

        auto_sync_watchlist = bool(kwargs.pop("auto_sync_watchlist", False))
        owner_id = str(kwargs.get("owner_id", "") or "").strip()
        watchlist_id = str(kwargs.pop("watchlist_id", "") or "").strip()
        recommendation_intake_limit = int(kwargs.pop("recommendation_intake_limit", 0) or 0)

        from finance_agent.pipelines.recommendation import UniverseRecommendationPipeline
        from finance_agent.storage.db import create_session_factory, session_scope

        session_factory = create_session_factory()
        with session_scope(session_factory) as session:
            result = UniverseRecommendationPipeline(session).run_for_universe(**kwargs)
            payload = asdict(result)
            payload["errors"] = list(result.errors)
            if auto_sync_watchlist:
                payload["watchlist_intake"] = self.sync_recommendations_to_default_watchlist(
                    session=session,
                    owner_id=owner_id,
                    watchlist_id=watchlist_id,
                    market=result.market,
                    recommendation_run_id=result.recommendation_run_id,
                    recommendation_status=result.status,
                    limit=recommendation_intake_limit or int(kwargs.get("limit") or 20),
                )
        return payload

    def run_decision_context_refresh(self, **kwargs: Any) -> JsonDict:
        """刷新收盘市场状态或热门板块快照。"""

        if self._run_decision_context_refresh is not None:
            return self._run_decision_context_refresh(**kwargs)

        from finance_agent.application.closing_decision_context_service import (
            ClosingDecisionContextService,
        )
        from finance_agent.storage.db import create_session_factory, session_scope

        session_factory = create_session_factory()
        with session_scope(session_factory) as session:
            return ClosingDecisionContextService(session).refresh(**kwargs)

    def run_universe_merge(self, **kwargs: Any) -> JsonDict:
        """执行候选池合并任务。"""

        if self._run_universe_merge is not None:
            return self._run_universe_merge(**kwargs)

        from finance_agent.application.data_production_service import ProductionUniverseService
        from finance_agent.storage.db import create_session_factory, session_scope

        session_factory = create_session_factory()
        with session_scope(session_factory) as session:
            plans = ProductionUniverseService(session).merge_universes(**kwargs)
        return {
            "status": "available",
            "target_universe_id": kwargs["target_universe_id"],
            "member_count": len(plans),
            "source_universe_ids": list(kwargs["source_universe_ids"]),
        }

    def run_avoid_pool_rebuild(self, **kwargs: Any) -> JsonDict:
        """执行回避池重建任务。"""

        if self._run_avoid_pool_rebuild is not None:
            return self._run_avoid_pool_rebuild(**kwargs)

        from finance_agent.application.data_production_service import ProductionUniverseService
        from finance_agent.storage.db import create_session_factory, session_scope

        session_factory = create_session_factory()
        with session_scope(session_factory) as session:
            plans = ProductionUniverseService(session).rebuild_avoid_pool(**kwargs)
        return {
            "status": "available",
            "universe_id": kwargs["universe_id"],
            "market": kwargs["market"],
            "member_count": len(plans),
        }

    def run_backtest(self, **kwargs: Any) -> JsonDict:
        """执行轻量回测任务。"""

        if self._run_backtest is not None:
            return self._run_backtest(**kwargs)

        from finance_agent.storage.db import create_session_factory, session_scope

        strategy = str(kwargs.get("strategy") or "factor_score_topn")
        if strategy == "strategy_validation_gate":
            from finance_agent.research.validation_gate import StrategyValidationGate
            from finance_agent.storage.repositories import StrategyObservationRepository

            session_factory = create_session_factory()
            with session_scope(session_factory) as session:
                state = StrategyObservationRepository(session).get_trial_state(
                    str(kwargs["strategy_id"])
                )
                decision = StrategyValidationGate().evaluate_forward(
                    state=state or {"state": "research"},
                    outcomes=(getattr(state, "forward_metrics", {}) if state else {}),
                )
            return {
                "status": "available",
                "strategy_id": kwargs["strategy_id"],
                "state": decision.next_state,
                "allowed": decision.allowed,
                "reason_codes": list(decision.reason_codes),
            }
        if strategy in {"strategy_forward_settlement", "strategy_walk_forward_v2"}:
            from datetime import UTC

            from finance_agent.research.strategy_walk_forward_runner import (
                run_strategy_walk_forward,
            )

            session_factory = create_session_factory()
            end_at = kwargs.get("end_at") or datetime.now(tz=UTC)
            start_at = kwargs.get("start_at") or (
                end_at - timedelta(days=365 * int(kwargs.get("years") or 5))
            )
            with session_scope(session_factory) as session:
                result = run_strategy_walk_forward(
                    session,
                    strategy_id=str(kwargs["strategy_id"]),
                    universe_id=str(
                        kwargs.get("universe_id") or "universe:merged:ashare:recommendation"
                    ),
                    start_at=start_at,
                    end_at=end_at,
                    topn=int(kwargs.get("topn") or 20),
                    dry_run=bool(kwargs.get("dry_run", False)),
                    asset_limit=kwargs.get("asset_limit"),
                )
            return result
        from finance_agent.backtesting.runner import run_factor_score_topn_backtest

        session_factory = create_session_factory()
        with session_scope(session_factory) as session:
            return run_factor_score_topn_backtest(session, **kwargs)

    def sync_recommendations_to_default_watchlist(
        self,
        *,
        session: Any,
        owner_id: str,
        watchlist_id: str,
        market: str,
        recommendation_run_id: str | None,
        recommendation_status: str,
        limit: int,
    ) -> JsonDict:
        """推荐流水线成功后，把非回避推荐同步进系统研究跟踪池。"""

        if recommendation_status != "available" or not recommendation_run_id:
            return {
                "status": "skipped",
                "reason": "recommendation_unavailable",
                "recommendation_status": recommendation_status,
            }
        if not owner_id or not watchlist_id:
            return {"status": "skipped", "reason": "missing_owner_or_watchlist"}

        from finance_agent.agents.personal_assistant import PersonalFinanceAgentService

        agent = PersonalFinanceAgentService(session)
        as_of = datetime.now(tz=UTC)
        agent.watchlists.upsert_watchlist(
            watchlist_id=watchlist_id,
            owner_id=owner_id,
            name=default_watchlist_name(market),
            market=market,
            purpose="system_research_pool",
            status="active",
            payload={
                "source": "base_data_scheduler",
                "recommendation_run_id": recommendation_run_id,
                "description": "接收调度器推荐流水线产生的非回避候选，供后续 Agent 复核。",
            },
        )
        summary = agent.sync_recommendations_to_watchlist(
            owner_id=owner_id,
            recommendation_run_id=recommendation_run_id,
            watchlist_id=watchlist_id,
            as_of=as_of,
            limit=limit,
        )
        return asdict(summary) | {"status": "executed"}

    def run_data_quality_refresh(self, **kwargs: Any) -> JsonDict:
        """刷新数据质量快照。"""

        if self._run_data_quality_refresh is not None:
            return self._run_data_quality_refresh(**kwargs)

        from finance_agent.application.data_quality_service import DataQualityService
        from finance_agent.storage.db import create_session_factory, session_scope

        session_factory = create_session_factory()
        with session_scope(session_factory) as session:
            return DataQualityService(session).refresh_quality_snapshots(**kwargs)

    def run_technical_screening_refresh(self, **kwargs: Any) -> JsonDict:
        """刷新技术初筛池。"""

        if self._run_technical_screening_refresh is not None:
            return self._run_technical_screening_refresh(**kwargs)

        from finance_agent.application.technical_screening_service import (
            TechnicalScreeningService,
        )
        from finance_agent.storage.db import create_session_factory, session_scope

        market = str(kwargs.pop("market"))
        universe_id = str(kwargs.pop("universe_id"))
        timeframe = str(kwargs.pop("timeframe", "1d"))
        limit = int(kwargs.pop("limit", 200))
        min_bars = int(kwargs.pop("min_bars", 250))
        ttl_days = int(kwargs.pop("ttl_days", 3))
        source = kwargs.pop("source", None)
        session_factory = create_session_factory()
        with session_scope(session_factory) as session:
            service = TechnicalScreeningService(session)
            if market == "ashare":
                result = service.screen_ashare(
                    limit=limit,
                    timeframe=timeframe,
                    source=source,
                    min_bars=min_bars,
                    persist=True,
                )
            elif market == "fund":
                result = service.screen_funds(
                    limit=limit,
                    timeframe=timeframe,
                    source=source,
                    min_bars=min_bars,
                    persist=True,
                )
            else:
                raise ValueError(f"暂不支持 {market} 技术初筛刷新")

        return {
            "status": result.status,
            "screening_id": result.screening_id,
            "universe_id": universe_id or result.universe_id,
            "market": result.market,
            "strategy": result.strategy,
            "candidate_count": result.candidate_count,
            "accepted_count": result.accepted_count,
            "rejected_count": result.rejected_count,
            "skipped_count": result.skipped_count,
            "rule_hits": result.rule_hits,
            "ttl_days": ttl_days,
        }

    def run_structural_methodology_refresh(self, **kwargs: Any) -> JsonDict:
        """刷新结构方法论证据快照。"""

        if self._run_structural_methodology_refresh is not None:
            return self._run_structural_methodology_refresh(**kwargs)

        from finance_agent.application.structural_methodology_service import (
            StructuralMethodologyRefreshService,
        )
        from finance_agent.storage.db import create_session_factory, session_scope

        session_factory = create_session_factory()
        with session_scope(session_factory) as session:
            return StructuralMethodologyRefreshService(session).refresh(**kwargs)

    def run_trigger_evaluation(self, **kwargs: Any) -> JsonDict:
        """执行触发评估并按需派发到 Agent 唤醒队列。"""

        if self._run_trigger_evaluation is not None:
            return self._run_trigger_evaluation(**kwargs)

        from decimal import Decimal

        from finance_agent.storage.db import create_session_factory, session_scope
        from finance_agent.triggers import TriggerEvaluationRequest, TriggerService

        owner_id = str(kwargs.pop("owner_id"))
        dispatch = as_bool(kwargs.pop("dispatch", True), field_name="dispatch")
        agent_runtime = str(kwargs.pop("agent_runtime", "hermes_agent"))
        max_events_per_run = int(kwargs.pop("max_events_per_run", 50))
        cooldown_minutes = int(kwargs.pop("cooldown_minutes", 15))
        trigger_groups = tuple(str(item) for item in kwargs.pop("trigger_groups", []))
        request = TriggerEvaluationRequest(
            owner_id=owner_id,
            as_of=datetime.now(tz=UTC),
            portfolio_id=kwargs.pop("portfolio_id", None),
            watchlist_id=kwargs.pop("watchlist_id", None),
            recommendation_run_id=kwargs.pop("recommendation_run_id", None),
            horizon=str(kwargs.pop("horizon", "swing")),
            timeframe=str(kwargs.pop("timeframe", "1d")),
            since_minutes=int(kwargs.pop("since_minutes", 60)),
            cooldown_minutes=cooldown_minutes,
            recommendation_limit=int(kwargs.pop("recommendation_limit", 20)),
            drawdown_threshold=Decimal(str(kwargs.pop("drawdown_threshold", "0.050000"))),
            trigger_groups=trigger_groups,
            intraday_quote_window_minutes=int(kwargs.pop("intraday_quote_window_minutes", 30)),
            intraday_sharp_drop_threshold=Decimal(
                str(kwargs.pop("intraday_sharp_drop_threshold", "-0.040000"))
            ),
            intraday_volume_surge_multiplier=Decimal(
                str(kwargs.pop("intraday_volume_surge_multiplier", "3.000000"))
            ),
            intraday_price_change_threshold=Decimal(
                str(kwargs.pop("intraday_price_change_threshold", "0.020000"))
            ),
        )
        session_factory = create_session_factory()
        with session_scope(session_factory) as session:
            service = TriggerService(session)
            evaluation = service.evaluate(request)
            from finance_agent.triggers.webhook import HermesWebhookPublisher

            publisher = HermesWebhookPublisher.from_environment()
            dispatch_result = (
                service.dispatch_pending(
                    owner_id=owner_id,
                    limit=max(max_events_per_run, 1),
                    as_of=request.as_of,
                    agent_runtime=agent_runtime,
                    publisher=publisher.publish if publisher else None,
                )
                if dispatch
                else None
            )

        created_count = len(evaluation.created_events)
        suppressed_count = len(evaluation.suppressed_dedup_keys)
        payload: JsonDict = {
            "status": "available",
            "owner_id": owner_id,
            "created_count": created_count,
            "suppressed_count": suppressed_count,
            "skipped_count": 0,
            "cooldown_count": suppressed_count,
            "max_events_per_run": max_events_per_run,
            "trigger_groups": list(trigger_groups),
            "dispatch": dispatch,
            "agent_runtime": agent_runtime,
            "evaluation": evaluation.to_dict(),
        }
        if dispatch_result is not None:
            payload["dispatched_count"] = len(dispatch_result.dispatched_events)
            payload["skipped_count"] = len(dispatch_result.skipped_events)
            payload["failed_count"] = len(dispatch_result.failed_events)
            payload["dispatch_result"] = dispatch_result.to_dict()
        else:
            payload["dispatched_count"] = 0
        return payload

    def run_agent_loop_consume(self, **kwargs: Any) -> JsonDict:
        """消费已派发的 Agent 唤醒事件。"""

        if self._run_agent_loop_consume is not None:
            return self._run_agent_loop_consume(**kwargs)

        from finance_agent.agents.loop import InternalFinanceAgentLoopRunner
        from finance_agent.storage.db import create_session_factory, session_scope

        owner_id = str(kwargs.get("owner_id") or "default-owner")
        limit = int(kwargs.get("limit") or 10)
        use_model_planner = as_bool(
            kwargs.get("use_model_planner", True),
            field_name="use_model_planner",
        )
        session_factory = create_session_factory()
        with session_scope(session_factory) as session:
            result = InternalFinanceAgentLoopRunner(session).run_once(
                owner_id=owner_id,
                limit=limit,
                use_graph=use_model_planner,
            )
        payload = result.to_dict()
        payload.update(
            {
                "status": "available",
                "owner_id": owner_id,
                "limit": limit,
                "consumed": (
                    payload["processed_count"]
                    + payload["skipped_count"]
                    + payload["failed_count"]
                ),
                "succeeded": payload["processed_count"],
                "failed": payload["failed_count"],
                "fallback_used": not use_model_planner,
            }
        )
        return payload

    def run_high_risk_reviews(self, **kwargs: Any) -> JsonDict:
        """执行待处理高风险模型复核事件。"""

        if self._run_high_risk_reviews is not None:
            return self._run_high_risk_reviews(**kwargs)

        from finance_agent.agents.runtime.high_risk_review_service import HighRiskReviewService
        from finance_agent.storage.db import create_session_factory, session_scope

        owner_id = str(kwargs.get("owner_id") or "default-owner")
        limit = int(kwargs.get("limit") or 10)
        session_factory = create_session_factory()
        with session_scope(session_factory) as session:
            payload = HighRiskReviewService(session=session).run_pending_reviews(
                owner_id=owner_id,
                limit=limit,
            )
        payload.update(
            {
                "status": "available",
                "owner_id": owner_id,
                "limit": limit,
            }
        )
        return payload

    def run_reviews_due(self, **kwargs: Any) -> JsonDict:
        """执行到期的人工操作闭环复盘任务。"""

        if self._run_reviews_due is not None:
            return self._run_reviews_due(**kwargs)

        from finance_agent.application.action_loop_service import ActionLoopService
        from finance_agent.storage.db import create_session_factory, session_scope

        owner_id = str(kwargs.get("owner_id") or "default-owner")
        limit = int(kwargs.get("limit") or 20)
        session_factory = create_session_factory()
        with session_scope(session_factory) as session:
            payload = ActionLoopService(session).run_due_reviews(
                owner_id=owner_id,
                limit=limit,
            )
        payload.update(
            {
                "status": "available",
                "owner_id": owner_id,
                "limit": limit,
            }
        )
        return payload

    def collect_base_data(self, args: Any, *, job_name: str | None = None) -> JsonDict:
        """延迟导入采集入口，避免模块加载阶段触碰脚本依赖。"""

        timeout_seconds = self.config.job_timeout_seconds
        if timeout_seconds > 0:
            heartbeat_interval_seconds = max(1, min(60, self.config.health_stale_seconds // 2))
            return collect_base_data_with_timeout(
                args,
                timeout_seconds=timeout_seconds,
                heartbeat_interval_seconds=heartbeat_interval_seconds,
                heartbeat=lambda: self.write_status(
                    state="running",
                    last_job=job_name,
                    last_job_status="running",
                    last_job_at=datetime.now(tz=UTC),
                    last_error=None,
                ),
                collect_base_data_func=(
                    self._collect_base_data
                    if self._uses_injected_collect_base_data
                    else None
                ),
            )
        if self._collect_base_data is None:
            module = import_collection_module()
            self._collect_base_data = module.collect_base_data
        return self._collect_base_data(args)

    def default_collection_args(self, **overrides: Any) -> Any:
        """延迟导入采集参数工厂。"""

        if self._default_collection_args is None:
            module = import_collection_module()
            self._default_collection_args = module.default_collection_args
        return self._default_collection_args(**overrides)

    def stop_scheduler(self, *, result: JsonDict, state: str) -> None:
        """写入停止状态和停止事件。"""

        finished_at = datetime.now(tz=UTC)
        last_job = None
        last_job_status = None
        last_error = None
        if result.get("jobs"):
            last = result["jobs"][-1]
            last_job = last.get("job")
            last_job_status = last.get("status")
            last_error = last.get("error_message")
        self.write_status(
            state=state,
            mode=result.get("mode"),
            dry_run=result.get("dry_run"),
            started_at=parse_datetime_or_none(result.get("started_at")) or self.started_at,
            finished_at=finished_at,
            cycles=result.get("cycles"),
            last_job=last_job,
            last_job_status=last_job_status,
            last_error=last_error,
        )
        self.emit_event(
            "scheduler_stop",
            state=state,
            mode=result.get("mode"),
            dry_run=result.get("dry_run"),
            cycles=result.get("cycles"),
            job_count=len(result.get("jobs", [])),
        )

    def write_status(self, **payload: Any) -> None:
        """写入调度器健康状态文件。"""

        if self.status_file is None:
            return
        with self._status_lock:
            now = datetime.now(tz=UTC)
            status = {
                "service": self.service_name,
                "pid": os.getpid(),
                "updated_at": now.isoformat(),
                "state": payload.get("state", "running"),
                "enabled": self.config.enabled,
                "job_count": len(self.config.jobs),
                "enabled_job_count": len(self.enabled_jobs()),
                "health_stale_seconds": self.config.health_stale_seconds,
                "config_reload_error": self.config_reload_error,
            }
            if self.config_identity is not None:
                status.update(self.config_identity.to_status_dict())
            if self.status_file.exists():
                try:
                    previous_status = json.loads(self.status_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    previous_status = {}
                if isinstance(previous_status, dict):
                    for key, value in previous_status.items():
                        status.setdefault(key, value)
                    status["updated_at"] = now.isoformat()
                    status["pid"] = os.getpid()
                    status["service"] = self.service_name
                    status["enabled"] = self.config.enabled
                    status["job_count"] = len(self.config.jobs)
                    status["enabled_job_count"] = len(self.enabled_jobs())
                    status["health_stale_seconds"] = self.config.health_stale_seconds
                    status["config_reload_error"] = self.config_reload_error
                    if self.config_identity is not None:
                        status.update(self.config_identity.to_status_dict())
            if status.get("state") == "running":
                status.pop("stopped_process", None)
            for key, value in payload.items():
                status[key] = serialize_scheduler_value(value)
            write_scheduler_status_file(
                self.status_file,
                status,
                sleep_func=self._sleep,
            )

    def emit_event(self, event: str, **payload: Any) -> None:
        """写入结构化 JSONL 事件日志。"""

        if self.event_log_file is None:
            return
        record = {
            "service": self.service_name,
            "event": event,
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "pid": os.getpid(),
        }
        for key, value in payload.items():
            record[key] = serialize_scheduler_value(value)
        with self._event_lock:
            self.event_log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.event_log_file.open("a", encoding="utf-8") as file:
                file.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str))
                file.write("\n")


def import_collection_module() -> Any:
    """兼容脚本目录和包路径两种运行方式导入采集入口。"""

    try:
        return importlib.import_module("scripts.data.collect_base_data")
    except ModuleNotFoundError as exc:
        if not str(exc.name).startswith("scripts"):
            raise
        try:
            return importlib.import_module("collect_base_data")
        except ModuleNotFoundError:
            script_path = (
                Path(__file__).resolve().parents[3]
                / "scripts"
                / "data"
                / "collect_base_data.py"
            )
            spec = importlib.util.spec_from_file_location(
                "finance_agent_collect_base_data",
                script_path,
            )
            if spec is None or spec.loader is None:
                raise ModuleNotFoundError("无法定位 scripts/data/collect_base_data.py") from None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module


def build_ashare_lookback_date_overrides(lookback: str, *, now: datetime | None = None) -> JsonDict:
    """把 A 股 lookback 配置转换为采集脚本需要的 YYYYMMDD 起止日期。"""

    current = now or datetime.now(tz=UTC)
    end_date = current.date()
    start_date = end_date - timedelta(days=parse_lookback_days(lookback, default_days=30))
    return {
        "ashare_start": start_date.strftime("%Y%m%d"),
        "ashare_end": end_date.strftime("%Y%m%d"),
    }


def build_fund_lookback_date_overrides(
    lookback: str,
    *,
    now: datetime | None = None,
    completed_only: bool = False,
) -> JsonDict:
    """把基金 lookback 配置转换为采集脚本复用的 YYYYMMDD 起止日期。"""

    current = now or datetime.now(tz=UTC)
    end_date = current.date()
    if completed_only:
        end_date -= timedelta(days=1)
    start_date = end_date - timedelta(days=parse_lookback_days(lookback, default_days=30))
    return {
        "ashare_start": start_date.strftime("%Y%m%d"),
        "ashare_end": end_date.strftime("%Y%m%d"),
    }


def parse_lookback_days(value: str | None, *, default_days: int) -> int:
    """把 30d / 72h 这类 lookback 字符串转换为天数。"""

    if not value:
        return default_days
    text = str(value).strip().lower()
    try:
        if text.endswith("y"):
            return max(1, int(text[:-1]) * 365)
        if text.endswith("h"):
            return max(1, int(text[:-1]) // 24)
        if text.endswith("d"):
            return max(1, int(text[:-1]))
        return max(1, int(text))
    except ValueError:
        return default_days


def scheduler_python_executable(root_dir: Path | None = None) -> str:
    """返回调度器派生子进程时应使用的项目 Python。"""

    project_root = root_dir or Path(__file__).resolve().parents[3]
    candidates = (
        [
            project_root / ".venv" / "Scripts" / "python.exe",
            project_root / ".venv" / "bin" / "python",
        ]
        if os.name == "nt"
        else [
            project_root / ".venv" / "bin" / "python",
            project_root / ".venv" / "Scripts" / "python.exe",
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


class JobTimeoutError(TimeoutError):
    """单个采集任务超过调度器允许时间。"""


def collect_base_data_with_timeout(
    args: Any,
    *,
    timeout_seconds: int,
    heartbeat_interval_seconds: int = DEFAULT_HEALTH_STALE_SECONDS // 2,
    heartbeat: Callable[[], None] | None = None,
    collect_base_data_func: Callable[[Any], JsonDict] | None = None,
) -> JsonDict:
    """在子进程中执行采集，超时后终止子进程。"""

    # Windows venv 启动器可能让 sys.executable 指到底层 Anaconda，这里显式固定到项目 venv。
    multiprocessing.set_executable(scheduler_python_executable())
    context = multiprocessing.get_context("spawn")
    result_queue: multiprocessing.Queue[JsonDict] = context.Queue(maxsize=1)
    if collect_base_data_func is None:
        target = collect_base_data_payload_child
        process_args = (vars(args), result_queue)
    else:
        target = collect_base_data_callable_child
        process_args = (collect_base_data_func, args, result_queue)

    process = context.Process(target=target, args=process_args)
    process.start()
    deadline = time.monotonic() + timeout_seconds
    next_heartbeat_at = time.monotonic() + max(1, heartbeat_interval_seconds)
    message: JsonDict | None = None
    while process.is_alive():
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            break
        wait_seconds = min(1.0, remaining_seconds)
        try:
            message = result_queue.get(timeout=wait_seconds)
            break
        except queue.Empty:
            pass
        if heartbeat is not None and time.monotonic() >= next_heartbeat_at:
            heartbeat()
            next_heartbeat_at = time.monotonic() + max(1, heartbeat_interval_seconds)
    if message is not None:
        # 先读取结果再等待子进程退出，避免 Windows 队列写大结果时互等。
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)
            if process.is_alive():
                process.kill()
                process.join(5)
    if process.is_alive():
        process.terminate()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
        raise JobTimeoutError(f"采集任务超过 {timeout_seconds} 秒未完成，已终止子进程")

    if message is None:
        try:
            message = result_queue.get_nowait()
        except queue.Empty as exc:
            raise RuntimeError(f"采集子进程未返回结果，exitcode={process.exitcode}") from exc

    if message.get("ok"):
        result = message.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("采集子进程返回了非对象结果")
        return result
    error_message = str(message.get("error_message") or "采集子进程执行失败")
    child_traceback = message.get("traceback")
    if child_traceback:
        error_message = f"{error_message}\n{child_traceback}"
    raise RuntimeError(error_message)


def collect_base_data_payload_child(
    args_payload: JsonDict,
    result_queue: multiprocessing.Queue[JsonDict],
) -> None:
    """子进程入口：重新导入采集脚本并执行。"""

    try:
        module = import_collection_module()
        args = module.default_collection_args(**args_payload)
        result_queue.put({"ok": True, "result": module.collect_base_data(args)})
    except BaseException as exc:  # noqa: BLE001 - 子进程边界需要捕获并返回全部异常
        result_queue.put(
            {
                "ok": False,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "traceback": traceback.format_exc(),
            }
        )


def collect_base_data_callable_child(
    collect_base_data_func: Callable[[Any], JsonDict],
    args: Any,
    result_queue: multiprocessing.Queue[JsonDict],
) -> None:
    """子进程入口：执行测试或嵌入场景传入的采集函数。"""

    try:
        result_queue.put({"ok": True, "result": collect_base_data_func(args)})
    except BaseException as exc:  # noqa: BLE001 - 子进程边界需要捕获并返回全部异常
        result_queue.put(
            {
                "ok": False,
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "traceback": traceback.format_exc(),
            }
        )


def write_scheduler_status_file(
    status_file: Path,
    status: JsonDict,
    *,
    sleep_func: Callable[[float], None] = time.sleep,
    max_attempts: int = STATUS_REPLACE_MAX_ATTEMPTS,
) -> None:
    """用唯一临时文件原子写入调度状态，并对 Windows 文件占用做短暂重试。"""

    status_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file = status_file.with_name(
        f"{status_file.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    temp_file.write_text(
        json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    try:
        replace_file_with_retry(
            temp_file,
            status_file,
            sleep_func=sleep_func,
            max_attempts=max_attempts,
        )
    finally:
        try:
            temp_file.unlink(missing_ok=True)
        except OSError:
            logger.warning("清理调度状态临时文件失败 path=%s", temp_file, exc_info=True)


def replace_file_with_retry(
    source: Path,
    target: Path,
    *,
    sleep_func: Callable[[float], None] = time.sleep,
    max_attempts: int = STATUS_REPLACE_MAX_ATTEMPTS,
) -> None:
    """替换文件，遇到 Windows 短暂占用时重试。"""

    attempts = max(1, max_attempts)
    for attempt in range(1, attempts + 1):
        try:
            source.replace(target)
            return
        except OSError:
            if attempt >= attempts:
                raise
            sleep_func(STATUS_REPLACE_RETRY_SECONDS * attempt)


def seconds_until_next_run(states: list[ScheduledJobState], idle_seconds: int) -> float:
    """计算下一次循环休眠秒数。"""

    next_run_values = [state.next_run_at for state in states if state.next_run_at is not None]
    if not next_run_values:
        return float(idle_seconds)
    now = datetime.now(tz=UTC)
    next_run_at = min(next_run_values)
    wait_seconds = max(0.0, (next_run_at - now).total_seconds())
    return min(float(idle_seconds), wait_seconds) if wait_seconds else 0.0


def as_bool(value: Any, *, field_name: str) -> bool:
    """解析布尔配置值。"""

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on"}:
            return True
        if normalized in {"false", "0", "no", "n", "off"}:
            return False
    raise ValueError(f"{field_name} 必须是布尔值")


def as_positive_int(value: Any, *, field_name: str) -> int:
    """解析正整数配置值。"""

    if isinstance(value, bool):
        raise ValueError(f"{field_name} 必须是正整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是正整数") from exc
    if parsed <= 0:
        raise ValueError(f"{field_name} 必须大于 0")
    return parsed


def as_non_negative_int(value: Any, *, field_name: str) -> int:
    """解析非负整数配置值。"""

    if isinstance(value, bool):
        raise ValueError(f"{field_name} 必须是非负整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是非负整数") from exc
    if parsed < 0:
        raise ValueError(f"{field_name} 必须大于等于 0")
    return parsed


def as_choice(value: Any, *, choices: set[str], field_name: str) -> str:
    """解析枚举配置值。"""

    parsed = str(value or "").strip()
    if parsed not in choices:
        allowed = ", ".join(sorted(choices))
        raise ValueError(f"{field_name} 必须是以下取值之一: {allowed}")
    return parsed


def as_string_tuple(value: Any, *, field_name: str) -> tuple[str, ...]:
    """解析字符串或字符串数组。"""

    if value is None or value == "":
        return ()
    if isinstance(value, str):
        parsed = (value.strip(),)
    elif isinstance(value, list | tuple):
        parsed = tuple(str(item).strip() for item in value if str(item).strip())
    else:
        raise ValueError(f"{field_name} 必须是字符串或字符串数组")
    if any(not item for item in parsed):
        raise ValueError(f"{field_name} 不能为空字符串")
    return parsed


def parse_json_object(value: Any, *, field_name: str) -> JsonDict:
    """解析 JSON 对象配置。"""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} 必须是对象")
    return dict(value)


def parse_string_list(value: Any) -> list[str]:
    """解析逗号分隔字符串或字符串数组。"""

    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list | tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ValueError("配置值必须是字符串或字符串数组")


def parse_float_mapping(value: Any) -> dict[str, float]:
    """解析字符串到浮点数的 JSON 对象。"""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("source_weights 必须是对象")
    return {str(key): float(item) for key, item in value.items()}


def as_group_choice(value: Any, *, field_name: str) -> str | tuple[str, ...]:
    """解析采集分组，支持单分组和多分组。"""

    if isinstance(value, list | tuple):
        groups = tuple(str(item).strip() for item in value if str(item).strip())
        if not groups:
            raise ValueError(f"{field_name} 不能为空")
        for group in groups:
            as_choice(group, choices=COLLECTION_GROUPS, field_name=field_name)
        return groups
    return as_choice(value, choices=COLLECTION_GROUPS, field_name=field_name)


def as_job_group_choice(
    value: Any,
    *,
    job_type: str,
    field_name: str,
) -> str | tuple[str, ...]:
    """按任务类型解析调度分组。"""

    if job_type in {
        "recommendation_pipeline",
        "data_quality_refresh",
        "technical_screening_refresh",
        "structural_methodology_refresh",
        "decision_context_refresh",
        "trigger_evaluation",
        "high_risk_reviews",
        "reviews_due",
        "universe_merge",
        "universe_avoid_pool_rebuild",
        "backtest_run",
    }:
        return as_choice(value, choices={"analytics"}, field_name=field_name)
    if job_type == "agent_loop_consume":
        return as_choice(value, choices={"agent"}, field_name=field_name)
    return as_group_choice(value, field_name=field_name)


def default_watchlist_name(market: str) -> str:
    """返回调度器默认观察池名称。"""

    return {
        "ashare": "A 股系统研究跟踪池",
        "crypto_spot": "数字货币现货推荐观察池",
        "crypto_future": "数字货币合约推荐观察池",
    }.get(market, f"{market} 推荐观察池")


def parse_datetime_or_none(value: Any) -> datetime | None:
    """解析 ISO 时间字符串，无法解析时返回空。"""

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def serialize_scheduler_value(value: Any) -> Any:
    """把调度器状态中的值转换为 JSON 友好格式。"""

    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return {str(key): serialize_scheduler_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [serialize_scheduler_value(item) for item in value]
    return value


def compact_collection_summary(summary: JsonDict) -> JsonDict:
    """压缩采集摘要，避免事件日志写入过大的 Provider 响应。"""

    keys = (
        "status",
        "started_at",
        "finished_at",
        "total_tasks",
        "available",
        "failed",
        "skipped",
        "groups",
        "sync_task_type",
        "mode",
    )
    return {key: summary.get(key) for key in keys if key in summary}


def collection_progress_max_workers(args: Any, *, fallback: int) -> int:
    """读取采集任务内部的按标的并发数，用于任务监控进度展示。"""

    raw_value = getattr(args, "max_workers", None)
    if raw_value in {None, ""}:
        raw_value = fallback
    try:
        return max(1, int(raw_value))
    except (TypeError, ValueError):
        return max(1, int(fallback))
