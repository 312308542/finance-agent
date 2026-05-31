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
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from finance_agent.data.sync_config import (
    build_preset_config,
    export_scheduler_payload,
    load_data_sync_config,
)

JsonDict = dict[str, Any]
logger = logging.getLogger(__name__)

COLLECTION_GROUPS = {"all", "ashare-p0", "ashare-p1", "ashare-p2", "ashare-risk", "crypto"}
JOB_TYPES = {"collection", "recommendation_pipeline", "data_quality_refresh"}
DEFAULT_JOB_TIMEOUT_SECONDS = 3600
DEFAULT_HEALTH_STALE_SECONDS = 300
DEFAULT_MAX_CONCURRENT_JOBS = 4
STATUS_REPLACE_MAX_ATTEMPTS = 5
STATUS_REPLACE_RETRY_SECONDS = 0.05


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
    jobs: tuple[BaseDataSchedulerJob, ...] = ()


@dataclass
class ScheduledJobState:
    """运行期任务状态。"""

    job: BaseDataSchedulerJob
    next_run_at: datetime
    last_run_at: datetime | None = None
    last_summary: JsonDict | None = None
    running: bool = False
    queued: bool = False


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
    healthy = age_seconds <= stale_after and state not in {"failed", "stale"}
    if last_job_status == "failed":
        healthy = False
    status = "healthy" if healthy else "unhealthy"
    if age_seconds > stale_after:
        status = "stale"
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
        max_concurrent_jobs=as_positive_int(
            payload.get("max_concurrent_jobs", DEFAULT_MAX_CONCURRENT_JOBS),
            field_name="max_concurrent_jobs",
        ),
        jobs=jobs,
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
    group = as_job_group_choice(
        payload.get("group"),
        job_type=job_type,
        field_name=f"{name}.group",
    )
    interval_seconds = as_positive_int(
        payload.get("interval_seconds"),
        field_name=f"{name}.interval_seconds",
    )
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
    )


class BaseDataScheduler:
    """按配置运行基础数据采集任务。"""

    def __init__(
        self,
        config: BaseDataSchedulerConfig,
        *,
        collect_base_data_func: Callable[[Any], JsonDict] | None = None,
        default_collection_args_func: Callable[..., Any] | None = None,
        run_recommendation_pipeline_func: Callable[..., JsonDict] | None = None,
        run_data_quality_refresh_func: Callable[..., JsonDict] | None = None,
        sleep_func: Callable[[float], None] = time.sleep,
        status_file: str | Path | None = None,
        event_log_file: str | Path | None = None,
        service_name: str = "base_data_scheduler",
    ) -> None:
        self.config = config
        self._collect_base_data = collect_base_data_func
        self._default_collection_args = default_collection_args_func
        self._run_recommendation_pipeline = run_recommendation_pipeline_func
        self._run_data_quality_refresh = run_data_quality_refresh_func
        self._uses_injected_collect_base_data = collect_base_data_func is not None
        self._sleep = sleep_func
        self.status_file = Path(status_file) if status_file else None
        self.event_log_file = Path(event_log_file) if event_log_file else None
        self.service_name = service_name
        self.started_at: datetime | None = None
        self._status_lock = threading.Lock()
        self._event_lock = threading.Lock()

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

        states = [ScheduledJobState(job=job, next_run_at=started_at) for job in self.enabled_jobs()]
        cycles = 0
        executed: list[JsonDict] = []
        running: dict[Future[JsonDict], ScheduledJobState] = {}
        queued: list[ScheduledJobState] = []

        def running_job_names() -> list[str]:
            return [state.job.name for state in running.values()]

        def queued_job_names() -> list[str]:
            return [state.job.name for state in queued]

        def fill_worker_slots(executor: ThreadPoolExecutor) -> None:
            while queued and len(running) < self.config.max_concurrent_jobs:
                state = queued.pop(0)
                state.queued = False
                state.running = True
                running[executor.submit(self.run_job, state.job, dry_run=dry_run)] = state

        def write_loop_status() -> None:
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
        try:
            with ThreadPoolExecutor(
                max_workers=max(1, self.config.max_concurrent_jobs),
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
                        state.next_run_at = state.last_run_at + timedelta(
                            seconds=state.job.interval_seconds,
                        )
                        executed.append(
                            summary
                            | {
                                "last_run_at": state.last_run_at.isoformat(),
                                "next_run_at": state.next_run_at.isoformat(),
                            }
                        )

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
                        if not state.running and not state.queued and state.next_run_at <= now
                    ]
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
        if job.job_type == "collection":
            args = self.build_collection_args(job)
            planned["collection_args"] = vars(args)
        elif job.job_type == "recommendation_pipeline":
            planned["recommendation_args"] = self.build_recommendation_pipeline_kwargs(job)
        elif job.job_type == "data_quality_refresh":
            planned["data_quality_args"] = self.build_data_quality_refresh_kwargs(job)
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

        max_attempts = self.config.max_job_retries + 1
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
                return failed

            executed = planned | {
                "dry_run": False,
                "status": "executed",
                "attempt_count": attempt,
                "summary": summary,
                "finished_at": datetime.now(tz=UTC).isoformat(),
            }
            self.emit_event(
                "job_success",
                job=job.name,
                attempt=attempt,
                summary=compact_collection_summary(summary),
            )
            self.write_status(
                state="running",
                last_job=job.name,
                last_job_status="executed",
                last_job_at=datetime.now(tz=UTC),
                last_error=None,
            )
            logger.info(
                "调度任务完成 job=%s job_type=%s market=%s attempt=%s status=executed summary=%s",
                job.name,
                job.job_type,
                job.market,
                attempt,
                compact_collection_summary(summary),
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
        }
        if job.limit is not None:
            overrides["limit"] = job.limit
        overrides.update(job.params)
        if (
            job.market == "ashare"
            and str(overrides.get("sync_task_type") or "") == "market_bars_backfill"
            and overrides.get("lookback")
        ):
            overrides.update(build_ashare_lookback_date_overrides(str(overrides["lookback"])))
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
        for key in ("timeframe", "source"):
            value = job.params.get(key)
            if value is not None:
                params[key] = value
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
        for key in ("owner_id", "watchlist_id"):
            value = job.params.get(key)
            if value is not None:
                params[key] = str(value)
        value = job.params.get("recommendation_intake_limit")
        if value is not None:
            params["recommendation_intake_limit"] = int(value)
        return params

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

    def run_recommendation_pipeline(self, **kwargs: Any) -> JsonDict:
        """执行候选池推荐流水线。"""

        if self._run_recommendation_pipeline is not None:
            return self._run_recommendation_pipeline(**kwargs)

        auto_sync_watchlist = bool(kwargs.pop("auto_sync_watchlist", False))
        owner_id = str(kwargs.pop("owner_id", "") or "").strip()
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
        """推荐流水线成功后，把非回避推荐同步进默认观察池。"""

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
            purpose="接收调度器推荐流水线产生的非回避候选，供后续 Agent 复核。",
            status="active",
            payload={
                "source": "base_data_scheduler",
                "recommendation_run_id": recommendation_run_id,
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
            }
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


def parse_lookback_days(value: str | None, *, default_days: int) -> int:
    """把 30d / 72h 这类 lookback 字符串转换为天数。"""

    if not value:
        return default_days
    text = str(value).strip().lower()
    try:
        if text.endswith("h"):
            return max(1, int(text[:-1]) // 24)
        if text.endswith("d"):
            return max(1, int(text[:-1]))
        return max(1, int(text))
    except ValueError:
        return default_days


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
    while process.is_alive():
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            break
        wait_seconds = min(1.0, remaining_seconds)
        process.join(wait_seconds)
        if heartbeat is not None and time.monotonic() >= next_heartbeat_at:
            heartbeat()
            next_heartbeat_at = time.monotonic() + max(1, heartbeat_interval_seconds)
    if process.is_alive():
        process.terminate()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
        raise JobTimeoutError(f"采集任务超过 {timeout_seconds} 秒未完成，已终止子进程")

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

    if not states:
        return float(idle_seconds)
    now = datetime.now(tz=UTC)
    next_run_at = min(state.next_run_at for state in states)
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

    if job_type in {"recommendation_pipeline", "data_quality_refresh"}:
        return as_choice(value, choices={"analytics"}, field_name=field_name)
    return as_group_choice(value, field_name=field_name)


def default_watchlist_name(market: str) -> str:
    """返回调度器默认观察池名称。"""

    return {
        "ashare": "A 股推荐观察池",
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
