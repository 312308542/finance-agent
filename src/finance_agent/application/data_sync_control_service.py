"""Web 控制台使用的数据同步配置和调度器进程控制。"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from queue import Queue
from typing import Any
from uuid import uuid4

from finance_agent.cache import create_cache_client
from finance_agent.cache.null_cache import NullCacheClient
from finance_agent.data.sync_config import (
    DataSyncConfig,
    build_preset_config,
    export_scheduler_payload,
    load_data_sync_config,
    parse_data_sync_config,
    preview_data_sync_config,
    save_data_sync_config,
    validate_data_sync_config,
)
from finance_agent.scheduler import (
    load_scheduler_config,
    parse_scheduler_config,
    read_scheduler_health,
    write_scheduler_status_file,
)
from finance_agent.scheduler.base_data_progress import (
    BaseDataTaskProgressRecorder,
    build_progress_snapshot_response,
)
from finance_agent.scheduler.base_data_scheduler import parse_scheduler_resource_pools

JsonDict = dict[str, Any]
logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[3]
RUNTIME_DIR = ROOT_DIR / "runtime"
SCHEDULER_DIR = RUNTIME_DIR / "base_data_scheduler"
DEFAULT_DATA_SYNC_CONFIG = RUNTIME_DIR / "data_sync_config.json"
DEFAULT_SCHEDULER_CONFIG = SCHEDULER_DIR / "base_data_scheduler.json"
DEFAULT_STATUS_FILE = SCHEDULER_DIR / "status.json"
DEFAULT_EVENT_LOG_FILE = SCHEDULER_DIR / "events.jsonl"
DEFAULT_PROCESS_FILE = SCHEDULER_DIR / "process.json"
DEFAULT_PROCESS_LOG_FILE = SCHEDULER_DIR / "process.log"
DEFAULT_FAILED_RERUN_DIR = SCHEDULER_DIR / "failed_reruns"
MANUAL_RUN_START_LOCK_STALE_SECONDS = 300
MANUAL_RUN_PROGRESS_MAX_AGE_SECONDS = 300
PROCESS_TERMINATION_VERIFY_TIMEOUT_SECONDS = 5.0
PROCESS_TERMINATION_VERIFY_INTERVAL_SECONDS = 0.2
ASHARE_MARKET_BAR_RERUN_TASK_TYPES = {
    "market_bars_backfill",
    "market_bars_full_history_backfill",
    "market_bars_midday_partial",
    "market_bars_close_final",
    "market_bars_revision",
}
FUND_RERUN_TASK_TYPES = {
    "market_bars_full_history_backfill",
    "market_bars_close_final",
    "fund_nav_full_history_backfill",
    "fund_nav_daily",
}

_FAILED_RERUN_QUEUE: Queue[JsonDict] = Queue()
_FAILED_RERUN_LOCK = threading.Lock()
_FAILED_RERUN_WORKER: threading.Thread | None = None
_FAILED_RERUN_CURRENT: JsonDict | None = None
_FAILED_RERUN_HISTORY: list[JsonDict] = []
_ATTACHED_SCHEDULER_LOG_FILES: set[str] = set()
_ATTACHED_SCHEDULER_LOG_LOCK = threading.Lock()


def project_python_executable(root_dir: Path | None = None) -> str:
    """返回项目虚拟环境 Python，找不到时才回退当前解释器。"""

    project_root = root_dir or ROOT_DIR
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


class _SchedulerLogForwardDeduper:
    """对 stdout 和日志文件转发做轻量去重，避免同一行在后端控制台重复刷屏。"""

    def __init__(self, *, max_entries: int = 2000) -> None:
        self._max_entries = max_entries
        self._seen: set[str] = set()
        self._order: list[str] = []
        self._lock = threading.Lock()

    def should_forward(self, text: str) -> bool:
        """返回当前日志行是否需要继续转发。"""

        key = text.strip()
        if not key:
            return False
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            self._order.append(key)
            if len(self._order) > self._max_entries:
                stale = self._order.pop(0)
                self._seen.discard(stale)
            return True


def _log_forwarded_scheduler_line(
    text: str,
    *,
    deduper: _SchedulerLogForwardDeduper | None = None,
) -> None:
    """把调度器日志行转发到 Web 后端日志。"""

    if deduper is not None and not deduper.should_forward(text):
        return
    if text:
        print(f"[base-data-scheduler] {text}", flush=True)
        logger.info("[base-data-scheduler] %s", text)


def forward_scheduler_process_output(
    process: subprocess.Popen[str],
    *,
    deduper: _SchedulerLogForwardDeduper | None = None,
) -> None:
    """把调度子进程的控制台输出转发到 Web 后端日志。"""

    if process.stdout is None:
        return
    try:
        for line in process.stdout:
            text = line.rstrip()
            _log_forwarded_scheduler_line(text, deduper=deduper)
    except Exception as exc:  # pragma: no cover - 日志转发失败不应影响调度进程
        logger.warning("转发基础数据调度器日志失败：%s", exc)


def forward_scheduler_process_log_file(
    process: subprocess.Popen[str],
    process_log_file: Path,
    *,
    deduper: _SchedulerLogForwardDeduper | None = None,
    poll_interval_seconds: float = 0.5,
    start_at_end: bool = False,
) -> None:
    """tail 调度器日志文件，兜底转发超时保护子进程写入的采集明细。"""

    file_position = process_log_file.stat().st_size if start_at_end and process_log_file.exists() else 0
    try:
        while True:
            if process_log_file.exists():
                with process_log_file.open("r", encoding="utf-8", errors="replace") as file:
                    file.seek(file_position)
                    for line in file:
                        _log_forwarded_scheduler_line(line.rstrip(), deduper=deduper)
                    file_position = file.tell()
            if process.poll() is not None:
                break
            time.sleep(poll_interval_seconds)
    except Exception as exc:  # pragma: no cover - 文件日志兜底转发失败不应影响调度任务
        logger.warning("转发基础数据调度器文件日志失败：%s", exc)


class _ManualStatusFileProcess:
    """用手工任务状态文件适配已存在的调度任务，供日志 tail 线程判断何时退出。"""

    stdout = None

    def __init__(self, status_file: Path, *, pid: int) -> None:
        self.status_file = status_file
        self.pid = pid

    def poll(self) -> int | None:
        """返回 None 表示任务仍在运行，返回 0 表示可以停止 tail。"""

        try:
            payload = json.loads(self.status_file.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return None
        state = str(payload.get("state") or "").lower()
        last_job_status = str(payload.get("last_job_status") or "").lower()
        if state == "running" or last_job_status == "running":
            return None
        return 0


def attach_running_manual_scheduler_log_forwarders(
    run_dir: Path | None = None,
) -> int:
    """为后端重启前已经存在的 running 手工任务补挂日志文件转发线程。"""

    manual_run_dir = run_dir or (SCHEDULER_DIR / "manual_runs")
    if not manual_run_dir.exists():
        return 0
    attached_count = 0
    status_files = sorted(
        manual_run_dir.glob("*.status.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for status_file in status_files:
        try:
            payload = json.loads(status_file.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        state = str(payload.get("state") or "").lower()
        last_job_status = str(payload.get("last_job_status") or "").lower()
        if state != "running" and last_job_status != "running":
            continue
        try:
            pid = int(payload.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        process_log_value = str(payload.get("process_log_file") or "").strip()
        if not process_log_value:
            continue
        process_log_file = Path(process_log_value)
        log_key = str(process_log_file.resolve())
        with _ATTACHED_SCHEDULER_LOG_LOCK:
            if log_key in _ATTACHED_SCHEDULER_LOG_FILES:
                continue
            _ATTACHED_SCHEDULER_LOG_FILES.add(log_key)
        process = _ManualStatusFileProcess(status_file, pid=pid)
        log_deduper = _SchedulerLogForwardDeduper()
        threading.Thread(
            target=forward_scheduler_process_log_file,
            args=(process, process_log_file),
            kwargs={
                "deduper": log_deduper,
                "start_at_end": True,
            },
            name=f"base-data-adopted-file-log-forwarder-{process_log_file.stem}",
            daemon=True,
        ).start()
        attached_count += 1
        logger.info("已接管运行中手工任务日志文件 pid=%s log_file=%s", pid, process_log_file)
    return attached_count


class DataSyncControlService:
    """封装 Web 控制台对数据同步配置和本地调度器的操作。"""

    def read_config(self, *, config_file: Path = DEFAULT_DATA_SYNC_CONFIG) -> JsonDict:
        """读取已保存配置；不存在时返回默认全面配置。"""

        config = (
            load_data_sync_config(config_file)
            if config_file.exists()
            else build_preset_config()
        )
        return build_config_response(
            config=config,
            config_file=config_file,
            scheduler_config_file=DEFAULT_SCHEDULER_CONFIG,
        )

    def save_config(
        self,
        *,
        preset: str,
        markets: list[str] | None,
        enabled: bool,
        cache_backend: str,
        max_concurrent_jobs: int = 4,
        resource_pools: JsonDict | None = None,
        config_payload: JsonDict | None = None,
        config_file: Path = DEFAULT_DATA_SYNC_CONFIG,
        scheduler_config_file: Path = DEFAULT_SCHEDULER_CONFIG,
    ) -> JsonDict:
        """保存数据同步配置，并同步导出底层调度计划。"""

        if config_payload:
            config = parse_data_sync_config(config_payload)
        else:
            config = build_preset_config(preset, markets=markets or None)
        config = replace(
            config,
            enabled=enabled,
            cache_backend=cache_backend,
            max_concurrent_jobs=max_concurrent_jobs,
            resource_pools=(
                parse_scheduler_resource_pools(
                    resource_pools,
                    max_concurrent_jobs=max_concurrent_jobs,
                )
                if resource_pools is not None
                else config.resource_pools
            ),
        )
        validation = validate_data_sync_config(config)
        if not validation.valid:
            return {
                "status": "error",
                "message": "数据同步配置校验失败。",
                "data": build_config_response(
                    config=config,
                    config_file=config_file,
                    scheduler_config_file=scheduler_config_file,
                ),
            }

        save_data_sync_config(config, config_file)
        scheduler_payload = export_scheduler_payload(config)
        write_scheduler_payload(scheduler_config_file, scheduler_payload)
        return {
            "status": "ok",
            "data": build_config_response(
                config=config,
                config_file=config_file,
                scheduler_config_file=scheduler_config_file,
            ),
        }

    def read_scheduler_status(
        self,
        *,
        status_file: Path = DEFAULT_STATUS_FILE,
        process_file: Path = DEFAULT_PROCESS_FILE,
        max_age_seconds: int | None = None,
    ) -> JsonDict:
        """读取调度器健康状态和 Web 启动的进程状态。"""

        health = read_scheduler_health(status_file, max_age_seconds=max_age_seconds)
        process = read_process_metadata(process_file)
        if process:
            process["running"] = is_process_running(int(process["pid"]))
        else:
            process = {"running": False}
        status = "ok" if health.get("healthy") else health.get("status", "missing")
        return {"status": status, "health": health, "process": process}

    def read_scheduler_progress(
        self,
        *,
        event_limit: int = 80,
        cache: Any | None = None,
        cache_backend: str = "auto",
        scheduler_config_file: Path = DEFAULT_SCHEDULER_CONFIG,
        manual_run_dir: Path | None = None,
        redis_error_message: str | None = None,
    ) -> JsonDict:
        """读取 Redis 中的基础数据调度器运行态进度。"""

        attach_running_manual_scheduler_log_forwarders()
        cache_status = None
        cache_error = redis_error_message
        if cache is None:
            try:
                cache, _, cache_status = create_cache_client(backend=cache_backend)
                cache_backend = getattr(cache_status, "backend", cache_backend)
                cache_error = cache_error or getattr(cache_status, "error_message", None)
            except Exception as exc:
                cache = NullCacheClient()
                cache_backend = "null"
                cache_error = cache_error or str(exc)
        scheduler_config = None
        scheduler_jobs = ()
        try:
            scheduler_config = load_scheduler_config(scheduler_config_file)
            scheduler_jobs = scheduler_config.jobs
        except Exception as exc:
            logger.warning("读取调度器配置失败，进度页将不显示等待队列：%s", exc)
        if cache_backend != "redis":
            response = {
                "status": "degraded",
                "message": "Redis 不可用，无法读取实时进度。",
                "data": {
                    "cache_backend": cache_backend,
                    "tasks": [],
                    "waiting": [],
                },
            }
            merge_manual_run_progress(
                response,
                scheduler_jobs=scheduler_jobs,
                manual_run_dir=manual_run_dir or (SCHEDULER_DIR / "manual_runs"),
                event_limit=event_limit,
            )
            enrich_scheduler_progress_concurrency(response, scheduler_config=scheduler_config)
            return response
        if cache_status is not None and getattr(cache_status, "status", "available") != "available":
            response = {
                "status": "degraded",
                "message": "Redis 不可用，无法读取实时进度。",
                "data": {
                    "cache_backend": cache_backend,
                    "tasks": [],
                    "waiting": [],
                },
            }
            merge_manual_run_progress(
                response,
                scheduler_jobs=scheduler_jobs,
                manual_run_dir=manual_run_dir or (SCHEDULER_DIR / "manual_runs"),
                event_limit=event_limit,
            )
            enrich_scheduler_progress_concurrency(response, scheduler_config=scheduler_config)
            return response
        response = build_progress_snapshot_response(
            cache=cache,
            cache_backend=cache_backend,
            scheduler_jobs=scheduler_jobs,
            event_limit=event_limit,
            redis_error_message=cache_error,
        )
        merge_manual_run_progress(
            response,
            scheduler_jobs=scheduler_jobs,
            manual_run_dir=manual_run_dir or (SCHEDULER_DIR / "manual_runs"),
            event_limit=event_limit,
        )
        enrich_scheduler_progress_concurrency(response, scheduler_config=scheduler_config)
        return response

    def read_scheduler_jobs(
        self,
        *,
        data_sync_config_file: Path = DEFAULT_DATA_SYNC_CONFIG,
        scheduler_config_file: Path = DEFAULT_SCHEDULER_CONFIG,
    ) -> JsonDict:
        """读取可视化任务目录，供前端选择执行和编辑配置。"""

        payload = ensure_scheduler_payload(
            data_sync_config_file=data_sync_config_file,
            scheduler_config_file=scheduler_config_file,
        )
        config = parse_scheduler_config(payload)
        serialized_jobs = []
        for job in config.jobs:
            serialized_jobs.append(
                serialize_scheduler_job(job)
                | {"runtime_control": read_scheduler_job_control(payload, job.name)}
            )
        return {
            "status": "ok",
            "data": {
                "scheduler_config_file": str(scheduler_config_file),
                "config": {
                    "enabled": config.enabled,
                    "cache_backend": config.cache_backend,
                    "max_concurrent_jobs": config.max_concurrent_jobs,
                    "job_timeout_seconds": config.job_timeout_seconds,
                    "max_job_retries": config.max_job_retries,
                    "retry_backoff_seconds": config.retry_backoff_seconds,
                    "resource_pools": config.resource_pools,
                },
                "jobs": serialized_jobs,
            },
        }

    def update_scheduler_job(
        self,
        *,
        job_name: str,
        enabled: bool | None = None,
        interval_seconds: int | None = None,
        limit: int | None = None,
        batch_size: int | None = None,
        max_workers: int | None = None,
        schedule_type: str | None = None,
        run_at: list[str] | None = None,
        timezone: str | None = None,
        trading_day_policy: str | None = None,
        data_sync_config_file: Path = DEFAULT_DATA_SYNC_CONFIG,
        scheduler_config_file: Path = DEFAULT_SCHEDULER_CONFIG,
    ) -> JsonDict:
        """更新单个调度任务的运行时配置。"""

        payload = ensure_scheduler_payload(
            data_sync_config_file=data_sync_config_file,
            scheduler_config_file=scheduler_config_file,
        )
        job_payload = find_scheduler_job_payload(payload, job_name)
        if job_payload is None:
            return {"status": "error", "message": f"未找到调度任务：{job_name}", "data": {}}

        if enabled is not None:
            job_payload["enabled"] = bool(enabled)
        if interval_seconds is not None:
            job_payload["interval_seconds"] = max(0, int(interval_seconds))
        overrides: JsonDict = {}
        if limit is not None:
            job_payload["limit"] = max(1, int(limit))
            overrides["limit"] = job_payload["limit"]
        if schedule_type is not None:
            job_payload["schedule_type"] = str(schedule_type).strip()
        if run_at is not None:
            job_payload["run_at"] = [str(item).strip() for item in run_at if str(item).strip()]
        if timezone is not None:
            job_payload["timezone"] = str(timezone).strip() or "UTC"
        if trading_day_policy is not None:
            job_payload["trading_day_policy"] = str(trading_day_policy).strip()
        params = dict(job_payload.get("params") or {})
        if batch_size is not None:
            params["batch_size"] = max(1, int(batch_size))
            overrides["batch_size"] = params["batch_size"]
        if max_workers is not None:
            params["max_workers"] = max(1, int(max_workers))
            overrides["max_workers"] = params["max_workers"]
        job_payload["params"] = params
        update_scheduler_job_control(
            job_payload,
            last_action="config_updated",
            overrides=overrides,
        )

        parse_scheduler_config(payload)
        write_scheduler_payload(scheduler_config_file, payload)
        updated_config = parse_scheduler_config(payload)
        updated_job = next(job for job in updated_config.jobs if job.name == job_name)
        return {
            "status": "ok",
            "message": "任务配置已保存，正在运行的调度器会在下次启动或重载后使用新配置。",
            "data": {
                "scheduler_config_file": str(scheduler_config_file),
                "job": serialize_scheduler_job(updated_job),
                "runtime_control": read_scheduler_job_control(payload, job_name),
            },
        }

    def pause_scheduler_job(
        self,
        *,
        job_name: str,
        scheduler_config_file: Path = DEFAULT_SCHEDULER_CONFIG,
    ) -> JsonDict:
        """暂停运行中的调度任务；采集进程会在下一只标的提交前停住。"""

        payload = ensure_scheduler_payload(
            data_sync_config_file=DEFAULT_DATA_SYNC_CONFIG,
            scheduler_config_file=scheduler_config_file,
        )
        job_payload = find_scheduler_job_payload(payload, job_name)
        if job_payload is None:
            return {"status": "error", "message": f"未找到调度任务：{job_name}", "data": {}}
        update_scheduler_job_control(job_payload, paused=True, last_action="paused")
        parse_scheduler_config(payload)
        write_scheduler_payload(scheduler_config_file, payload)
        mark_scheduler_job_progress_paused(job_name=job_name)
        return {
            "status": "ok",
            "message": f"任务 {job_name} 已暂停；当前正在请求的标的会先完成，后续标的将等待继续。",
            "data": {
                "job_name": job_name,
                "runtime_control": read_scheduler_job_control(payload, job_name),
            },
        }

    def resume_scheduler_job(
        self,
        *,
        job_name: str,
        scheduler_config_file: Path = DEFAULT_SCHEDULER_CONFIG,
    ) -> JsonDict:
        """继续已暂停的调度任务。"""

        payload = ensure_scheduler_payload(
            data_sync_config_file=DEFAULT_DATA_SYNC_CONFIG,
            scheduler_config_file=scheduler_config_file,
        )
        job_payload = find_scheduler_job_payload(payload, job_name)
        if job_payload is None:
            return {"status": "error", "message": f"未找到调度任务：{job_name}", "data": {}}
        update_scheduler_job_control(job_payload, paused=False, last_action="resumed")
        parse_scheduler_config(payload)
        write_scheduler_payload(scheduler_config_file, payload)
        mark_scheduler_job_progress_resumed(job_name=job_name)
        return {
            "status": "ok",
            "message": f"任务 {job_name} 已继续；采集进程会从当前断点继续提交后续标的。",
            "data": {
                "job_name": job_name,
                "runtime_control": read_scheduler_job_control(payload, job_name),
            },
        }

    def run_scheduler_job(
        self,
        *,
        job_name: str,
        dry_run: bool = False,
        data_sync_config_file: Path = DEFAULT_DATA_SYNC_CONFIG,
        scheduler_config_file: Path = DEFAULT_SCHEDULER_CONFIG,
        manual_run_dir: Path | None = None,
        process_log_file: Path | None = None,
        job_param_overrides: JsonDict | None = None,
        wait_for_exit: bool = False,
    ) -> JsonDict:
        """使用只包含选中任务的临时配置启动一次 run-once 调度。"""

        payload = ensure_scheduler_payload(
            data_sync_config_file=data_sync_config_file,
            scheduler_config_file=scheduler_config_file,
        )
        job_payload = find_scheduler_job_payload(payload, job_name)
        if job_payload is None:
            return {"status": "error", "message": f"未找到调度任务：{job_name}", "data": {}}

        run_dir = manual_run_dir or (SCHEDULER_DIR / "manual_runs")
        run_dir.mkdir(parents=True, exist_ok=True)
        safe_job_name = safe_scheduler_job_name(job_name)
        running_job = find_running_manual_scheduler_job(
            run_dir,
            safe_job_name=safe_job_name,
            job_name=job_name,
        )
        if running_job is not None:
            return {
                "status": "error",
                "message": (
                    f"任务 {job_name} 已在运行中，PID={running_job['pid']}。"
                    "请等待当前任务完成后再启动，避免重复采集触发数据源限流或熔断。"
                ),
                "data": {
                    "dry_run": dry_run,
                    "writes_enabled": not dry_run,
                    "running_process": running_job,
                },
            }

        start_lock_file = manual_run_start_lock_file(run_dir, safe_job_name)
        starting_job = acquire_manual_run_start_lock(
            start_lock_file,
            job_name=job_name,
        )
        if starting_job is not None:
            return {
                "status": "error",
                "message": (
                    f"任务 {job_name} 正在启动中，PID={starting_job['pid']}。"
                    "请等待当前启动请求完成后再操作，避免重复采集触发数据源限流或熔断。"
                ),
                "data": {
                    "dry_run": dry_run,
                    "writes_enabled": not dry_run,
                    "running_process": starting_job,
                },
            }

        run_id = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        try:
            running_job = find_running_manual_scheduler_job(
                run_dir,
                safe_job_name=safe_job_name,
                job_name=job_name,
            )
            if running_job is not None:
                return {
                    "status": "error",
                    "message": (
                        f"任务 {job_name} 已在运行中，PID={running_job['pid']}。"
                        "请等待当前任务完成后再启动，避免重复采集触发数据源限流或熔断。"
                    ),
                    "data": {
                        "dry_run": dry_run,
                        "writes_enabled": not dry_run,
                        "running_process": running_job,
                    },
                }

            one_job_payload = deepcopy(payload)
            selected_job = deepcopy(job_payload)
            selected_job["enabled"] = True
            params = dict(selected_job.get("params") or {})
            if job_param_overrides:
                params.update(job_param_overrides)
            params["runtime_scheduler_config_file"] = str(scheduler_config_file)
            selected_job["params"] = params
            one_job_payload["enabled"] = True
            one_job_payload["max_concurrent_jobs"] = 1
            one_job_payload["jobs"] = [selected_job]
            parse_scheduler_config(one_job_payload)

            config_file = run_dir / f"{safe_job_name}_{run_id}.json"
            status_file = run_dir / f"{safe_job_name}_{run_id}.status.json"
            event_log_file = run_dir / f"{safe_job_name}_{run_id}.events.jsonl"
            log_file = process_log_file or (run_dir / f"{safe_job_name}_{run_id}.log")
            write_scheduler_payload(config_file, one_job_payload)

            command = [
                project_python_executable(),
                str(ROOT_DIR / "scripts" / "data" / "run_base_data_scheduler.py"),
                "--config",
                str(config_file),
                "--run-once",
                "--status-file",
                str(status_file),
                "--event-log-file",
                str(event_log_file),
                "--process-log-file",
                str(log_file),
            ]
            if dry_run:
                command.append("--dry-run")

            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            logger.info("启动单任务调度 job=%s dry_run=%s command=%s", job_name, dry_run, command)
            process = subprocess.Popen(
                command,
                cwd=ROOT_DIR,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
            started_at = datetime.now(tz=UTC).isoformat()
            write_manual_run_started_status(
                status_file,
                job_name=job_name,
                process=process,
                dry_run=dry_run,
                started_at=started_at,
                config_file=config_file,
                event_log_file=event_log_file,
                process_log_file=log_file,
            )
            log_deduper = _SchedulerLogForwardDeduper()
            threading.Thread(
                target=forward_scheduler_process_output,
                args=(process,),
                kwargs={"deduper": log_deduper},
                name=f"base-data-manual-{safe_job_name}-log-forwarder",
                daemon=True,
            ).start()
            threading.Thread(
                target=forward_scheduler_process_log_file,
                args=(process, log_file),
                kwargs={"deduper": log_deduper},
                name=f"base-data-manual-{safe_job_name}-file-log-forwarder",
                daemon=True,
            ).start()
            exit_code: int | None = None
            finished_at: str | None = None
            if wait_for_exit:
                exit_code = process.wait()
                finished_at = datetime.now(tz=UTC).isoformat()
            metadata = {
                "pid": process.pid,
                "command": command,
                "cwd": str(ROOT_DIR),
                "job_name": job_name,
                "dry_run": dry_run,
                "started_at": started_at,
                "config_file": str(config_file),
                "status_file": str(status_file),
                "event_log_file": str(event_log_file),
                "process_log_file": str(log_file),
            }
            if exit_code is not None:
                metadata["exit_code"] = exit_code
                metadata["finished_at"] = finished_at
            status = "ok" if exit_code in {None, 0} else "error"
            return {
                "status": status,
                "message": "已启动单任务预演。" if dry_run else "已启动单任务真实执行。",
                "data": {
                    "dry_run": dry_run,
                    "writes_enabled": not dry_run,
                    "process": metadata | {"running": not wait_for_exit},
                    "job": serialize_scheduler_job(parse_scheduler_config(one_job_payload).jobs[0]),
                },
            }
        finally:
            release_manual_run_start_lock(start_lock_file)

    def cancel_scheduler_job(
        self,
        *,
        job_name: str,
        manual_run_dir: Path | None = None,
        progress_recorder: Any | None = None,
    ) -> JsonDict:
        """取消由 Web 页面启动的单任务 run-once 进程。"""

        run_dir = manual_run_dir or (SCHEDULER_DIR / "manual_runs")
        safe_job_name = safe_scheduler_job_name(job_name)
        running_job = find_running_manual_scheduler_job(
            run_dir,
            safe_job_name=safe_job_name,
            job_name=job_name,
        )
        if running_job is None:
            return {
                "status": "ok",
                "message": f"没有找到正在运行的手工任务：{job_name}。",
                "data": {
                    "job_name": job_name,
                    "cancelled": False,
                },
            }

        terminated_pids: list[int] = []
        termination_errors: list[JsonDict] = []
        target_pids = cancel_target_pids(running_job)
        for target_pid in target_pids:
            if is_process_running(target_pid):
                try:
                    terminate_process(target_pid)
                except Exception as exc:  # noqa: BLE001 - 取消接口必须把失败原因返回给前端
                    termination_errors.append(
                        {"pid": target_pid, "error_message": str(exc)}
                    )
                    continue
                terminated_pids.append(target_pid)
        alive_pids = wait_for_processes_to_exit(
            target_pids,
            timeout_seconds=PROCESS_TERMINATION_VERIFY_TIMEOUT_SECONDS,
            interval_seconds=PROCESS_TERMINATION_VERIFY_INTERVAL_SECONDS,
        )
        if alive_pids or termination_errors:
            failed_process = dict(running_job)
            failed_process["running"] = True
            failed_process["terminated_pids"] = terminated_pids
            failed_process["alive_pids"] = alive_pids
            failed_process["termination_errors"] = termination_errors
            return {
                "status": "error",
                "message": (
                    f"任务 {job_name} 取消命令已发送，但仍有同步进程未退出："
                    f"{', '.join(str(pid) for pid in alive_pids) or '无'}。"
                ),
                "data": {
                    "job_name": job_name,
                    "cancelled": False,
                    "cancelled_process": failed_process,
                    "terminated_pids": terminated_pids,
                    "alive_pids": alive_pids,
                    "termination_errors": termination_errors,
                },
            }
        cancelled_process = dict(running_job)
        cancelled_process["running"] = False
        cancelled_process["cancelled_at"] = datetime.now(tz=UTC).isoformat()
        cancelled_process["terminated_pids"] = terminated_pids
        status_file = Path(str(running_job["status_file"]))
        write_cancelled_manual_status(
            status_file,
            job_name=job_name,
            process=cancelled_process,
        )
        mark_scheduler_job_progress_cancelled(
            job_name=job_name,
            progress_recorder=progress_recorder,
        )
        return {
            "status": "ok",
            "message": f"任务 {job_name} 已取消。",
            "data": {
                "job_name": job_name,
                "cancelled": True,
                "cancelled_process": cancelled_process,
                "status_file": str(status_file),
            },
        }

    def rerun_failed_scheduler_job(
        self,
        *,
        job_name: str,
        dry_run: bool = False,
        data_sync_config_file: Path = DEFAULT_DATA_SYNC_CONFIG,
        scheduler_config_file: Path = DEFAULT_SCHEDULER_CONFIG,
        failed_rerun_dir: Path | None = None,
        run_inline: bool = False,
    ) -> JsonDict:
        """把失败项重跑移交给后台串行队列，避免多个补跑任务同时打满上游数据源。"""

        payload = ensure_scheduler_payload(
            data_sync_config_file=data_sync_config_file,
            scheduler_config_file=scheduler_config_file,
        )
        job_payload = find_scheduler_job_payload(payload, job_name)
        if job_payload is None:
            return {"status": "error", "message": f"未找到调度任务：{job_name}", "data": {}}
        if str(job_payload.get("job_type") or "collection") != "collection":
            return {"status": "error", "message": "当前任务不是基础数据采集任务，暂不支持失败项重跑。", "data": {}}

        run_id = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        safe_job_name = safe_scheduler_job_name(job_name)
        request_id = f"failed-rerun:{safe_job_name}:{run_id}:{uuid4().hex[:8]}"
        request = {
            "request_id": request_id,
            "job_name": job_name,
            "dry_run": dry_run,
            "data_sync_config_file": str(data_sync_config_file),
            "scheduler_config_file": str(scheduler_config_file),
            "failed_rerun_dir": str(failed_rerun_dir or DEFAULT_FAILED_RERUN_DIR),
            "job_param_overrides": build_failed_rerun_job_param_overrides(job_payload),
            "queued_at": datetime.now(tz=UTC).isoformat(),
        }
        if run_inline:
            execution = execute_failed_rerun_request(request)
            return {
                "status": execution.get("status", "ok"),
                "message": "失败项重跑已执行完成。" if execution.get("status") == "ok" else "失败项重跑执行失败。",
                "data": {
                    "queue": failed_rerun_queue_snapshot(request_id=request_id) | {"mode": "inline"},
                    "request": request,
                    "execution": execution,
                },
            }

        enqueue_failed_rerun_request(request)
        return {
            "status": "ok",
            "message": "失败项重跑已加入后台串行队列，会在前一个补跑任务结束后依次执行。",
            "data": {
                "queue": failed_rerun_queue_snapshot(request_id=request_id),
                "request": request,
            },
        }

    def start_scheduler(
        self,
        *,
        dry_run: bool = False,
        max_cycles: int | None = None,
        data_sync_config_file: Path = DEFAULT_DATA_SYNC_CONFIG,
        config_file: Path = DEFAULT_SCHEDULER_CONFIG,
        status_file: Path = DEFAULT_STATUS_FILE,
        event_log_file: Path = DEFAULT_EVENT_LOG_FILE,
        process_file: Path = DEFAULT_PROCESS_FILE,
        process_log_file: Path = DEFAULT_PROCESS_LOG_FILE,
    ) -> JsonDict:
        """启动本地基础数据调度器进程。"""

        existing = read_process_metadata(process_file)
        if existing and is_process_running(int(existing["pid"])):
            dry_run = bool(existing.get("dry_run"))
            return {
                "status": "ok",
                "message": "基础数据调度器已在运行。",
                "data": {
                    "dry_run": dry_run,
                    "writes_enabled": not dry_run,
                    "process": existing | {"running": True},
                },
            }

        config = (
            load_data_sync_config(data_sync_config_file)
            if data_sync_config_file.exists()
            else build_preset_config()
        )
        scheduler_payload = export_scheduler_payload(config)
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(
            json.dumps(scheduler_payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        SCHEDULER_DIR.mkdir(parents=True, exist_ok=True)
        command = [
            project_python_executable(),
            str(ROOT_DIR / "scripts" / "data" / "run_base_data_scheduler.py"),
            "--config",
            str(config_file),
            "--loop",
            "--status-file",
            str(status_file),
            "--event-log-file",
            str(event_log_file),
            "--process-log-file",
            str(process_log_file),
        ]
        if dry_run:
            command.append("--dry-run")
        if max_cycles is not None:
            command.extend(["--max-cycles", str(max_cycles)])

        process_log_file.parent.mkdir(parents=True, exist_ok=True)
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        logger.info(
            "启动基础数据调度器进程 dry_run=%s max_cycles=%s command=%s",
            dry_run,
            max_cycles,
            command,
        )
        process = subprocess.Popen(
            command,
            cwd=ROOT_DIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        log_deduper = _SchedulerLogForwardDeduper()
        threading.Thread(
            target=forward_scheduler_process_output,
            args=(process,),
            kwargs={"deduper": log_deduper},
            name="base-data-scheduler-log-forwarder",
            daemon=True,
        ).start()
        threading.Thread(
            target=forward_scheduler_process_log_file,
            args=(process, process_log_file),
            kwargs={"deduper": log_deduper},
            name="base-data-scheduler-file-log-forwarder",
            daemon=True,
        ).start()
        metadata = {
            "pid": process.pid,
            "command": command,
            "cwd": str(ROOT_DIR),
            "dry_run": dry_run,
            "max_cycles": max_cycles,
            "started_at": datetime.now(tz=UTC).isoformat(),
            "config_file": str(config_file),
            "status_file": str(status_file),
            "event_log_file": str(event_log_file),
            "process_log_file": str(process_log_file),
        }
        write_process_metadata(process_file, metadata)
        message = (
            "已启动 1 轮预演调度：只生成执行计划，不会写入数据库。"
            if dry_run
            else "已启动真实数据同步：会调用采集器并写入数据库。"
        )
        return {
            "status": "ok",
            "message": message,
            "data": {
                "dry_run": dry_run,
                "writes_enabled": not dry_run,
                "process": metadata | {"running": True},
            },
        }

    def stop_scheduler(
        self,
        *,
        status_file: Path = DEFAULT_STATUS_FILE,
        process_file: Path = DEFAULT_PROCESS_FILE,
    ) -> JsonDict:
        """停止由 Web 控制台启动的基础数据调度器进程。"""

        process = read_process_metadata(process_file)
        if not process:
            return {"status": "ok", "message": "没有找到 Web 控制台启动的调度器进程。", "data": {}}

        pid = int(process["pid"])
        if is_process_running(pid):
            terminate_process(pid)
        process["running"] = False
        process["stopped_at"] = datetime.now(tz=UTC).isoformat()
        write_process_metadata(process_file, process)
        write_stopped_status(status_file, process)
        return {"status": "ok", "data": {"process": process}}


def build_config_response(
    *,
    config: DataSyncConfig,
    config_file: Path,
    scheduler_config_file: Path,
) -> JsonDict:
    """生成前端配置页需要的配置、预览和调度计划摘要。"""

    preview = preview_data_sync_config(config)
    validation = validate_data_sync_config(config)
    scheduler_payload = export_scheduler_payload(config)
    return {
        "config_file": str(config_file),
        "scheduler_config_file": str(scheduler_config_file),
        "config": config.to_dict(),
        "preview": preview,
        "validation": validation.to_dict(),
        "scheduler_payload": scheduler_payload,
        "jobs": scheduler_payload["jobs"],
    }


def ensure_scheduler_payload(
    *,
    data_sync_config_file: Path,
    scheduler_config_file: Path,
) -> JsonDict:
    """确保运行时调度配置存在，并返回可编辑 JSON。"""

    if scheduler_config_file.exists():
        payload = json.loads(scheduler_config_file.read_text(encoding="utf-8-sig"))
        parse_scheduler_config(payload)
        if should_merge_regenerable_scheduler_jobs(
            data_sync_config_file=data_sync_config_file,
            scheduler_config_file=scheduler_config_file,
        ):
            try:
                config = load_data_sync_config(data_sync_config_file)
                regenerated_payload = export_scheduler_payload(config)
            except Exception as exc:  # pragma: no cover - 旧配置仍应可读，迁移失败只降级
                logger.warning("补齐可再生成调度任务失败，继续使用现有运行时配置：%s", exc)
            else:
                if merge_regenerable_scheduler_jobs(payload, regenerated_payload):
                    parse_scheduler_config(payload)
                    write_scheduler_payload(scheduler_config_file, payload)
        return payload

    config = (
        load_data_sync_config(data_sync_config_file)
        if data_sync_config_file.exists()
        else build_preset_config()
    )
    payload = export_scheduler_payload(config)
    write_scheduler_payload(scheduler_config_file, payload)
    return payload


def should_merge_regenerable_scheduler_jobs(
    *,
    data_sync_config_file: Path,
    scheduler_config_file: Path,
) -> bool:
    """判断是否应把数据同步配置中新生成的任务补入运行时调度 JSON。"""

    if not data_sync_config_file.exists():
        return False
    try:
        default_scheduler = DEFAULT_SCHEDULER_CONFIG.resolve()
        scheduler_path = scheduler_config_file.resolve()
        data_config_parent = data_sync_config_file.resolve().parent
        scheduler_parent = scheduler_path.parent
    except OSError:
        return False
    if scheduler_path == default_scheduler:
        return True
    return data_config_parent == scheduler_parent or data_config_parent in scheduler_parent.parents


def merge_regenerable_scheduler_jobs(payload: JsonDict, regenerated_payload: JsonDict) -> bool:
    """只补齐可再生成任务和缺失依赖，保留现有任务的启停、批次和热控制配置。"""

    jobs = payload.get("jobs")
    regenerated_jobs = regenerated_payload.get("jobs")
    if not isinstance(jobs, list) or not isinstance(regenerated_jobs, list):
        return False
    changed = False
    for index, regenerated_job in enumerate(regenerated_jobs):
        if not isinstance(regenerated_job, dict):
            continue
        job_name = str(regenerated_job.get("name") or "")
        if not job_name:
            continue
        existing_job = find_scheduler_job_payload(payload, job_name)
        if existing_job is not None:
            changed = merge_scheduler_job_dependencies(existing_job, regenerated_job) or changed
            continue
        insert_at = next_existing_generated_job_index(
            jobs=jobs,
            regenerated_jobs=regenerated_jobs[index + 1 :],
        )
        jobs.insert(insert_at, deepcopy(regenerated_job))
        changed = True
    return changed


def merge_scheduler_job_dependencies(
    existing_job: JsonDict,
    regenerated_job: JsonDict,
) -> bool:
    """把新增任务带来的依赖补到已有任务上，不覆盖用户可编辑字段。"""

    regenerated_depends_on = regenerated_job.get("depends_on")
    if not isinstance(regenerated_depends_on, list) or not regenerated_depends_on:
        return False
    current_depends_on = existing_job.get("depends_on")
    if not isinstance(current_depends_on, list):
        current_depends_on = []
    merged_depends_on = [str(item) for item in current_depends_on]
    changed = False
    for dependency in regenerated_depends_on:
        dependency_name = str(dependency)
        if dependency_name not in merged_depends_on:
            merged_depends_on.append(dependency_name)
            changed = True
    if changed:
        existing_job["depends_on"] = merged_depends_on
    return changed


def next_existing_generated_job_index(
    *,
    jobs: list[Any],
    regenerated_jobs: list[Any],
) -> int:
    """返回下一个已存在再生成任务的位置，用于把缺失任务插回相近顺序。"""

    for regenerated_job in regenerated_jobs:
        if not isinstance(regenerated_job, dict):
            continue
        job_name = str(regenerated_job.get("name") or "")
        if not job_name:
            continue
        for index, job in enumerate(jobs):
            if isinstance(job, dict) and str(job.get("name") or "") == job_name:
                return index
    return len(jobs)


def write_scheduler_payload(path: Path, payload: JsonDict) -> None:
    """写入运行时调度配置。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temp_path.replace(path)


def merge_manual_run_progress(
    response: JsonDict,
    *,
    scheduler_jobs: list[Any] | tuple[Any, ...],
    manual_run_dir: Path,
    event_limit: int,
) -> None:
    """把手工 run-once 状态文件合并到进度响应，覆盖无 Redis 快照的 analytics 任务。"""

    data = response.get("data")
    if not isinstance(data, dict):
        return
    tasks = data.get("tasks")
    waiting = data.get("waiting")
    if not isinstance(tasks, list) or not isinstance(waiting, list):
        return
    existing_jobs = {
        str(task.get("job_name") or "")
        for task in tasks
        if isinstance(task, dict) and str(task.get("job_name") or "")
    }
    manual_tasks = read_recent_manual_run_tasks(
        manual_run_dir=manual_run_dir,
        scheduler_jobs=scheduler_jobs,
        event_limit=event_limit,
    )
    for task in manual_tasks:
        job_name = str(task.get("job_name") or "")
        if not job_name or job_name in existing_jobs:
            continue
        tasks.append(task)
        existing_jobs.add(job_name)
    data["waiting"] = [
        item
        for item in waiting
        if not isinstance(item, dict) or str(item.get("job_name") or "") not in existing_jobs
    ]
    metrics = data.get("metrics")
    if isinstance(metrics, dict):
        metrics["running_count"] = sum(
            1 for task in tasks if isinstance(task, dict) and task.get("status") == "running"
        )
        metrics["failed_count"] = sum(
            1 for task in tasks if isinstance(task, dict) and task.get("status") == "failed"
        )
        metrics["completed_recent_count"] = sum(
            1 for task in tasks if isinstance(task, dict) and task.get("status") == "completed"
        )
        metrics["waiting_count"] = len(data["waiting"])


def read_recent_manual_run_tasks(
    *,
    manual_run_dir: Path,
    scheduler_jobs: list[Any] | tuple[Any, ...],
    event_limit: int,
) -> list[JsonDict]:
    """读取每个手工任务最近一次运行状态，转换为任务监控可展示结构。"""

    if not manual_run_dir.exists():
        return []
    now = datetime.now(tz=UTC)
    job_by_name = {str(getattr(job, "name", "") or ""): job for job in scheduler_jobs}
    latest_by_job: dict[str, tuple[float, JsonDict]] = {}
    for status_file in manual_run_dir.glob("*.status.json"):
        try:
            if now.timestamp() - status_file.stat().st_mtime > MANUAL_RUN_PROGRESS_MAX_AGE_SECONDS:
                continue
        except OSError:
            continue
        try:
            payload = json.loads(status_file.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        job_name = str(payload.get("job_name") or payload.get("last_job") or "").strip()
        if not job_name:
            continue
        job = job_by_name.get(job_name)
        if job is None:
            continue
        task = build_manual_run_task_view(
            status_payload=payload,
            status_file=status_file,
            scheduler_job=job,
            event_limit=event_limit,
        )
        current = latest_by_job.get(job_name)
        modified_time = status_file.stat().st_mtime
        if current is None or modified_time > current[0]:
            latest_by_job[job_name] = (modified_time, task)
    return [
        task
        for _, task in sorted(
            latest_by_job.values(),
            key=lambda item: str(item[1].get("updated_at") or ""),
            reverse=True,
        )
    ]


def build_manual_run_task_view(
    *,
    status_payload: JsonDict,
    status_file: Path,
    scheduler_job: Any,
    event_limit: int,
) -> JsonDict:
    """把单个手工任务状态文件转成任务监控项。"""

    job_name = str(status_payload.get("job_name") or status_payload.get("last_job") or "")
    state = str(status_payload.get("state") or "").lower()
    last_job_status = str(status_payload.get("last_job_status") or "").lower()
    status = manual_run_task_status(state=state, last_job_status=last_job_status)
    started_at = str(status_payload.get("started_at") or "")
    updated_at = str(status_payload.get("updated_at") or status_payload.get("finished_at") or started_at)
    finished_at = str(status_payload.get("finished_at") or "")
    title = manual_run_task_title(scheduler_job=scheduler_job, job_name=job_name)
    total_items, completed_items, running_items, failed_items = manual_run_summary_counts(status)
    duration_seconds = manual_run_duration_seconds(started_at=started_at, updated_at=updated_at)
    process_log_file = (
        Path(str(status_payload.get("process_log_file") or ""))
        if status_payload.get("process_log_file")
        else None
    )
    event_log_file = (
        Path(str(status_payload.get("event_log_file") or ""))
        if status_payload.get("event_log_file")
        else None
    )
    events = read_manual_run_events(
        event_log_file=event_log_file,
        process_log_file=process_log_file,
        job_name=job_name,
        run_id=status_file.stem,
        limit=event_limit,
    )
    return {
        "job_name": job_name,
        "run_id": status_file.stem,
        "title": title,
        "market": getattr(scheduler_job, "market", None),
        "task_type": str((getattr(scheduler_job, "params", {}) or {}).get("sync_task_type") or ""),
        "status": status,
        "interval_seconds": int(getattr(scheduler_job, "interval_seconds", 0) or 0),
        "started_at": started_at,
        "updated_at": updated_at,
        "finished_at": finished_at,
        "batch_index": None,
        "batch_count": None,
        "batch_size": int((getattr(scheduler_job, "params", {}) or {}).get("batch_size") or 0),
        "max_workers": int((getattr(scheduler_job, "params", {}) or {}).get("max_workers") or 0),
        "throughput_per_minute": 0.0,
        "summary": {
            "total_items": total_items,
            "completed_items": completed_items,
            "running_items": running_items,
            "failed_items": failed_items,
            "retry_items": 0,
            "remaining_items": running_items,
            "progress_ratio": 1.0 if status in {"completed", "failed"} else 0.0,
        },
        "stages": [
            {
                "stage_key": "manual_run",
                "title": "手工任务执行",
                "status": status,
                "total_items": total_items,
                "completed_items": completed_items,
                "failed_items": failed_items,
                "running_items": running_items,
                "progress_ratio": 1.0 if status in {"completed", "failed"} else 0.0,
                "updated_at": updated_at,
            }
        ],
        "recent_events": events,
        "metrics": {
            "duration_seconds": duration_seconds,
            "error_rate": 1.0 if status == "failed" else 0.0,
            "max_workers": int((getattr(scheduler_job, "params", {}) or {}).get("max_workers") or 0),
            "throughput_per_minute": 0.0,
            "node": "local",
            "cache_backend": "status_file",
        },
        "error_message": str(status_payload.get("last_error") or status_payload.get("last_error_message") or ""),
    }


def manual_run_task_status(*, state: str, last_job_status: str) -> str:
    """把调度器状态字段转换为任务监控状态。"""

    if state in {"running", "starting"} or last_job_status in {"running", "starting"}:
        return "running"
    if state in {"cancelled", "failed", "error"} or last_job_status in {"failed", "error", "cancelled"}:
        return "failed"
    if state == "completed" or last_job_status in {"executed", "ok", "success"}:
        return "completed"
    return "unknown"


def manual_run_summary_counts(status: str) -> tuple[int, int, int, int]:
    """为非逐标的手工任务生成 1 个逻辑步骤的汇总计数。"""

    if status == "running":
        return 1, 0, 1, 0
    if status == "failed":
        return 1, 0, 0, 1
    if status == "completed":
        return 1, 1, 0, 0
    return 1, 0, 0, 0


def manual_run_task_title(*, scheduler_job: Any, job_name: str) -> str:
    """优先使用任务参数中的中文名称作为手工任务标题。"""

    params = getattr(scheduler_job, "params", {}) or {}
    return str(
        params.get("name")
        or params.get("title")
        or getattr(scheduler_job, "title", None)
        or job_name
    )


def manual_run_duration_seconds(*, started_at: str, updated_at: str) -> int:
    """计算手工任务运行时长。"""

    started = parse_iso_datetime_or_none(started_at)
    updated = parse_iso_datetime_or_none(updated_at)
    if started is None or updated is None:
        return 0
    return max(0, int((updated - started).total_seconds()))


def parse_iso_datetime_or_none(value: Any) -> datetime | None:
    """解析 ISO 时间字符串，无法解析时返回 None。"""

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def read_manual_run_events(
    *,
    event_log_file: Path | None,
    process_log_file: Path | None,
    job_name: str,
    run_id: str,
    limit: int,
) -> list[JsonDict]:
    """读取手工任务事件，优先使用 JSONL 事件，缺失时用进程日志兜底。"""

    events = read_manual_run_json_events(
        event_log_file=event_log_file,
        job_name=job_name,
        run_id=run_id,
        limit=limit,
    )
    if events:
        return events
    return read_manual_run_log_events(
        process_log_file=process_log_file,
        job_name=job_name,
        run_id=run_id,
        limit=limit,
    )


def read_manual_run_json_events(
    *,
    event_log_file: Path | None,
    job_name: str,
    run_id: str,
    limit: int,
) -> list[JsonDict]:
    """读取手工任务 JSONL 事件。"""

    if event_log_file is None or not event_log_file.exists():
        return []
    events: list[JsonDict] = []
    try:
        lines = event_log_file.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return []
    for line in lines[-max(1, limit) :]:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        events.append(
            {
                "event_type": str(item.get("event") or "manual_run_event"),
                "job_name": str(item.get("job") or job_name),
                "run_id": run_id,
                "created_at": str(item.get("timestamp") or ""),
                "status": manual_event_status(str(item.get("event") or "")),
                "error_message": str(item.get("error_message") or item.get("error") or ""),
                "item_count": int((item.get("summary") or {}).get("item_count") or 0)
                if isinstance(item.get("summary"), dict)
                else 0,
            }
        )
    return events


def read_manual_run_log_events(
    *,
    process_log_file: Path | None,
    job_name: str,
    run_id: str,
    limit: int,
) -> list[JsonDict]:
    """把手工任务进程日志转换为可点击查看的最近事件。"""

    if process_log_file is None or not process_log_file.exists():
        return []
    try:
        lines = [line.strip() for line in process_log_file.read_text(encoding="utf-8", errors="replace").splitlines()]
    except OSError:
        return []
    events: list[JsonDict] = []
    for line in lines[-max(1, limit) :]:
        if not line:
            continue
        events.append(
            {
                "event_type": "manual_run_log",
                "job_name": job_name,
                "run_id": run_id,
                "created_at": "",
                "status": "failed" if "失败" in line or "ERROR" in line else "completed",
                "error_message": line if "失败" in line or "ERROR" in line else "",
                "message": line,
            }
        )
    return events


def manual_event_status(event_type: str) -> str:
    """把手工任务事件类型转换为前端事件状态。"""

    normalized = event_type.lower()
    if "fail" in normalized or "error" in normalized:
        return "failed"
    if "start" in normalized:
        return "running"
    return "completed"


def find_scheduler_job_payload(payload: JsonDict, job_name: str) -> JsonDict | None:
    """从调度配置 JSON 中查找任务对象。"""

    normalized_name = str(job_name).strip()
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        return None
    for job in jobs:
        if isinstance(job, dict) and str(job.get("name") or "").strip() == normalized_name:
            return job
    return None


def read_scheduler_job_control(payload: JsonDict, job_name: str) -> JsonDict:
    """读取单任务运行期控制状态，缺省时返回可直接展示的默认值。"""

    job_payload = find_scheduler_job_payload(payload, job_name)
    control = job_payload.get("control") if isinstance(job_payload, dict) else {}
    if not isinstance(control, dict):
        control = {}
    overrides = control.get("overrides") if isinstance(control.get("overrides"), dict) else {}
    return {
        "paused": bool(control.get("paused", False)),
        "last_action": str(control.get("last_action") or ""),
        "updated_at": control.get("updated_at"),
        "overrides": dict(overrides),
    }


def update_scheduler_job_control(
    job_payload: JsonDict,
    *,
    paused: bool | None = None,
    last_action: str,
    overrides: JsonDict | None = None,
) -> JsonDict:
    """更新单任务运行期控制区；该区由长任务采集进程热读取。"""

    existing = job_payload.get("control") if isinstance(job_payload.get("control"), dict) else {}
    current = dict(existing or {})
    if paused is not None:
        current["paused"] = bool(paused)
    else:
        current.setdefault("paused", False)
    if overrides:
        merged_overrides = dict(current.get("overrides") or {})
        merged_overrides.update(overrides)
        current["overrides"] = merged_overrides
    else:
        current.setdefault("overrides", dict(current.get("overrides") or {}))
    current["last_action"] = last_action
    current["updated_at"] = datetime.now(tz=UTC).isoformat()
    job_payload["control"] = current
    return current


def enrich_scheduler_progress_concurrency(
    response: JsonDict,
    *,
    scheduler_config: Any | None,
) -> None:
    """为进度响应补充全局并发和资源池占用摘要。"""

    data = response.setdefault("data", {})
    if scheduler_config is None:
        data.setdefault("global_concurrency", {"running": 0, "limit": 0})
        data.setdefault("resource_pools", {})
        return
    tasks = data.get("tasks") if isinstance(data.get("tasks"), list) else []
    waiting = data.get("waiting") if isinstance(data.get("waiting"), list) else []
    running_job_names = {
        str(task.get("job_name") or "")
        for task in tasks
        if isinstance(task, dict) and task.get("status") == "running"
    }
    waiting_job_names = {
        str(item.get("job_name") or "")
        for item in waiting
        if isinstance(item, dict)
    }
    jobs_by_name = {str(job.name): job for job in scheduler_config.jobs}
    data["global_concurrency"] = {
        "running": len(running_job_names),
        "limit": int(getattr(scheduler_config, "max_concurrent_jobs", 0) or 0),
    }
    pool_summary: JsonDict = {}
    resource_pools = dict(getattr(scheduler_config, "resource_pools", {}) or {})
    for pool_name, pool_payload in resource_pools.items():
        limit = 0
        if isinstance(pool_payload, dict):
            limit = int(pool_payload.get("max_concurrent_jobs") or 0)
        pool_summary[str(pool_name)] = {"running": 0, "queued": 0, "limit": limit}
    for job_name in running_job_names:
        job = jobs_by_name.get(job_name)
        pool_name = str(getattr(job, "resource_pool", "default") or "default") if job else "default"
        pool_summary.setdefault(pool_name, {"running": 0, "queued": 0, "limit": 0})
        pool_summary[pool_name]["running"] += 1
    for job_name in waiting_job_names:
        job = jobs_by_name.get(job_name)
        pool_name = str(getattr(job, "resource_pool", "default") or "default") if job else "default"
        pool_summary.setdefault(pool_name, {"running": 0, "queued": 0, "limit": 0})
        pool_summary[pool_name]["queued"] += 1
    data["resource_pools"] = pool_summary


def serialize_scheduler_job(job: Any) -> JsonDict:
    """把调度任务 dataclass 转为前端可编辑结构。"""

    return {
        "name": job.name,
        "job_type": job.job_type,
        "group": list(job.group) if isinstance(job.group, tuple) else job.group,
        "enabled": job.enabled,
        "interval_seconds": job.interval_seconds,
        "limit": job.limit,
        "market": job.market,
        "schedule_type": job.schedule_type,
        "run_at": list(job.run_at),
        "timezone": job.timezone,
        "trading_day_policy": job.trading_day_policy,
        "depends_on": list(job.depends_on),
        "priority": job.priority,
        "resource_pool": job.resource_pool,
        "params": job.params,
    }


def safe_scheduler_job_name(job_name: str) -> str:
    """生成可用于临时文件名的调度任务名。"""

    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in job_name)


def manual_run_start_lock_file(run_dir: Path, safe_job_name: str) -> Path:
    """返回手工任务启动阶段使用的原子锁文件。"""

    return run_dir / f"{safe_job_name}.start.lock"


def acquire_manual_run_start_lock(lock_file: Path, *, job_name: str) -> JsonDict | None:
    """尝试获取手工任务启动锁；返回已有锁信息表示获取失败。"""

    lock_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "job_name": job_name,
        "state": "starting",
        "last_job_status": "starting",
        "pid": os.getpid(),
        "created_at": datetime.now(tz=UTC).isoformat(),
        "lock_file": str(lock_file),
    }
    while True:
        try:
            fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if is_manual_run_start_lock_stale(lock_file):
                try:
                    lock_file.unlink()
                except FileNotFoundError:
                    pass
                continue
            return read_manual_run_start_lock(lock_file, fallback=payload)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
        return None


def read_manual_run_start_lock(lock_file: Path, *, fallback: JsonDict) -> JsonDict:
    """读取启动锁信息，锁文件损坏时返回可展示的兜底信息。"""

    try:
        payload = json.loads(lock_file.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    result = fallback | payload
    result.setdefault("state", "starting")
    result.setdefault("last_job_status", "starting")
    result.setdefault("lock_file", str(lock_file))
    result["pid"] = parse_process_id(result.get("pid")) or os.getpid()
    return result


def is_manual_run_start_lock_stale(lock_file: Path) -> bool:
    """判断启动锁是否陈旧；用于清理后端异常退出遗留的锁。"""

    try:
        age_seconds = datetime.now(tz=UTC).timestamp() - lock_file.stat().st_mtime
    except OSError:
        return False
    return age_seconds > MANUAL_RUN_START_LOCK_STALE_SECONDS


def release_manual_run_start_lock(lock_file: Path) -> None:
    """释放手工任务启动锁。"""

    try:
        lock_file.unlink()
    except FileNotFoundError:
        return


def write_manual_run_started_status(
    status_file: Path,
    *,
    job_name: str,
    process: Any,
    dry_run: bool,
    started_at: str,
    config_file: Path,
    event_log_file: Path,
    process_log_file: Path,
) -> None:
    """在调度器子进程写入心跳前，先写入运行占位，关闭重复启动窗口。"""

    write_scheduler_status_file(
        status_file,
        {
            "service": "base_data_scheduler",
            "state": "running",
            "mode": "run_once",
            "dry_run": dry_run,
            "enabled": True,
            "job_count": 1,
            "enabled_job_count": 1,
            "pid": int(process.pid),
            "job_name": job_name,
            "last_job": job_name,
            "last_job_status": "running",
            "last_job_at": started_at,
            "started_at": started_at,
            "updated_at": started_at,
            "config_file": str(config_file),
            "event_log_file": str(event_log_file),
            "process_log_file": str(process_log_file),
        },
    )


def find_running_manual_scheduler_job(
    run_dir: Path,
    *,
    safe_job_name: str,
    job_name: str,
) -> JsonDict | None:
    """查找同名手动任务是否仍在运行，避免重复启动同一采集任务。"""

    if not run_dir.exists():
        return None
    status_files = sorted(
        run_dir.glob(f"{safe_job_name}_*.status.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for status_file in status_files:
        try:
            payload = json.loads(status_file.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue

        status_job_name = str(payload.get("last_job") or payload.get("job_name") or "").strip()
        if status_job_name and status_job_name != job_name:
            continue
        state = str(payload.get("state") or "").lower()
        last_job_status = str(payload.get("last_job_status") or "").lower()
        if state != "running" and last_job_status != "running":
            continue
        try:
            pid = int(payload.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        root_running = is_process_running(pid)
        related_pids = find_related_process_pids(pid)
        running_pids = []
        if root_running:
            running_pids.append(pid)
        running_pids.extend(
            related_pid for related_pid in related_pids if is_process_running(related_pid)
        )
        if not running_pids:
            continue

        return {
            "pid": pid,
            "job_name": job_name,
            "state": state,
            "last_job_status": last_job_status,
            "started_at": payload.get("started_at"),
            "updated_at": payload.get("updated_at"),
            "status_file": str(status_file),
            "related_pids": related_pids,
            "running_pids": unique_ints(running_pids),
        }
    return None


def build_failed_rerun_job_param_overrides(job_payload: JsonDict) -> JsonDict:
    """为失败项重跑生成低并发、可补漏的采集参数覆盖。"""

    params = job_payload.get("params") if isinstance(job_payload.get("params"), dict) else {}
    overrides: JsonDict = {"max_workers": 1}
    sync_task_type = str(params.get("sync_task_type") or "").strip()
    market = str(job_payload.get("market") or "").strip()
    if market == "ashare" and sync_task_type in ASHARE_MARKET_BAR_RERUN_TASK_TYPES:
        overrides["only_failed_or_stale"] = True
    if market == "fund" and sync_task_type in FUND_RERUN_TASK_TYPES:
        overrides["only_failed_or_stale"] = True
    return overrides


def enqueue_failed_rerun_request(request: JsonDict) -> None:
    """把失败项重跑请求加入全局串行队列，并确保后台 worker 已启动。"""

    _FAILED_RERUN_QUEUE.put(request)
    ensure_failed_rerun_worker()


def ensure_failed_rerun_worker() -> None:
    """启动单 worker 队列；worker 常驻等待，保证失败项补跑串行执行。"""

    global _FAILED_RERUN_WORKER
    with _FAILED_RERUN_LOCK:
        if _FAILED_RERUN_WORKER is not None and _FAILED_RERUN_WORKER.is_alive():
            return
        _FAILED_RERUN_WORKER = threading.Thread(
            target=failed_rerun_worker_loop,
            name="base-data-failed-rerun-worker",
            daemon=True,
        )
        _FAILED_RERUN_WORKER.start()


def failed_rerun_worker_loop() -> None:
    """串行消费失败项重跑请求。"""

    global _FAILED_RERUN_CURRENT
    while True:
        request = _FAILED_RERUN_QUEUE.get()
        started_at = datetime.now(tz=UTC).isoformat()
        with _FAILED_RERUN_LOCK:
            _FAILED_RERUN_CURRENT = request | {"started_at": started_at}
        try:
            result = execute_failed_rerun_request(request)
            status = result.get("status", "ok")
        except Exception as exc:  # pragma: no cover - worker 兜底，避免队列线程退出
            status = "error"
            result = {"status": "error", "message": str(exc)[:400], "data": {}}
            logger.exception("失败项重跑 worker 执行失败 job=%s", request.get("job_name"))
        finished = {
            **request,
            "status": status,
            "started_at": started_at,
            "finished_at": datetime.now(tz=UTC).isoformat(),
            "result": result,
        }
        with _FAILED_RERUN_LOCK:
            _FAILED_RERUN_CURRENT = None
            _FAILED_RERUN_HISTORY.append(finished)
            del _FAILED_RERUN_HISTORY[:-20]
        _FAILED_RERUN_QUEUE.task_done()


def execute_failed_rerun_request(request: JsonDict) -> JsonDict:
    """执行单个失败项重跑请求；由后台 worker 调用并等待子进程退出。"""

    return DataSyncControlService().run_scheduler_job(
        job_name=str(request["job_name"]),
        dry_run=bool(request.get("dry_run")),
        data_sync_config_file=Path(str(request["data_sync_config_file"])),
        scheduler_config_file=Path(str(request["scheduler_config_file"])),
        manual_run_dir=Path(str(request["failed_rerun_dir"])),
        job_param_overrides=dict(request.get("job_param_overrides") or {}),
        wait_for_exit=True,
    )


def failed_rerun_queue_snapshot(*, request_id: str | None = None) -> JsonDict:
    """返回失败项重跑队列的轻量状态，供 API 响应展示。"""

    with _FAILED_RERUN_LOCK:
        current = deepcopy(_FAILED_RERUN_CURRENT)
        recent = deepcopy(_FAILED_RERUN_HISTORY[-5:])
    return {
        "mode": "queued",
        "request_id": request_id,
        "queued_count": _FAILED_RERUN_QUEUE.qsize(),
        "running_count": 1 if current else 0,
        "running": current,
        "recent": recent,
    }


def read_process_metadata(process_file: Path) -> JsonDict | None:
    """读取 Web 控制台记录的调度器进程元数据。"""

    if not process_file.exists():
        return None
    try:
        payload = json.loads(process_file.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not payload.get("pid"):
        return None
    return payload


def write_process_metadata(process_file: Path, payload: JsonDict) -> None:
    """保存 Web 控制台启动的调度器进程元数据。"""

    process_file.parent.mkdir(parents=True, exist_ok=True)
    process_file.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def is_process_running(pid: int) -> bool:
    """判断进程是否仍在运行。"""

    if pid <= 0:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def find_related_process_pids(pid: int) -> list[int]:
    """查找给定父 PID 派生的仍存活子进程，覆盖 Windows multiprocessing orphan 场景。"""

    if pid <= 0:
        return []
    if os.name != "nt":
        return []

    script = (
        "$ErrorActionPreference='Stop';"
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,CommandLine | "
        "ConvertTo-Json -Compress -Depth 3"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    processes = payload if isinstance(payload, list) else [payload]
    process_rows: list[JsonDict] = [row for row in processes if isinstance(row, dict)]
    parent_map: dict[int, list[int]] = {}
    related: set[int] = set()
    for row in process_rows:
        process_id = parse_process_id(row.get("ProcessId"))
        parent_id = parse_process_id(row.get("ParentProcessId"))
        if process_id is None:
            continue
        if parent_id is not None:
            parent_map.setdefault(parent_id, []).append(process_id)
        command_line = str(row.get("CommandLine") or "")
        if f"parent_pid={pid}" in command_line:
            related.add(process_id)

    frontier = [pid]
    while frontier:
        parent_id = frontier.pop()
        for child_pid in parent_map.get(parent_id, []):
            if child_pid in related:
                continue
            related.add(child_pid)
            frontier.append(child_pid)
    return unique_ints(related)


def parse_process_id(value: Any) -> int | None:
    """把系统进程查询结果里的 PID 字段安全转换为整数。"""

    try:
        process_id = int(value)
    except (TypeError, ValueError):
        return None
    return process_id if process_id > 0 else None


def unique_ints(values: Any) -> list[int]:
    """按原顺序去重整数 PID。"""

    result: list[int] = []
    seen: set[int] = set()
    for value in values or []:
        try:
            item = int(value)
        except (TypeError, ValueError):
            continue
        if item <= 0 or item in seen:
            continue
        result.append(item)
        seen.add(item)
    return result


def cancel_target_pids(running_job: JsonDict) -> list[int]:
    """根据运行信息生成需要终止的 PID 列表。"""

    pid = parse_process_id(running_job.get("pid"))
    root_pids = [pid] if pid is not None else []
    running_pids = unique_ints(running_job.get("running_pids"))
    related_pids = unique_ints(running_job.get("related_pids"))
    return unique_ints([*root_pids, *running_pids, *related_pids])


def wait_for_processes_to_exit(
    pids: list[int],
    *,
    timeout_seconds: float = PROCESS_TERMINATION_VERIFY_TIMEOUT_SECONDS,
    interval_seconds: float = PROCESS_TERMINATION_VERIFY_INTERVAL_SECONDS,
) -> list[int]:
    """等待进程退出；超时后返回仍存活的 PID。"""

    target_pids = unique_ints(pids)
    if not target_pids:
        return []
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        alive_pids = [pid for pid in target_pids if is_process_running(pid)]
        if not alive_pids:
            return []
        if time.monotonic() >= deadline:
            return alive_pids
        sleep_seconds = min(max(interval_seconds, 0.01), max(deadline - time.monotonic(), 0.01))
        time.sleep(sleep_seconds)


def terminate_process(pid: int) -> None:
    """终止由 Web 控制台启动的调度器进程。"""

    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False)
        return
    os.kill(pid, signal.SIGTERM)


def mark_scheduler_job_progress_cancelled(
    *,
    job_name: str,
    progress_recorder: Any | None = None,
) -> None:
    """同步更新 Redis 任务进度，避免已取消任务在监控页继续显示运行中。"""

    try:
        recorder = progress_recorder or BaseDataTaskProgressRecorder.create(backend="auto")
        recorder.job_cancelled(job_name=job_name, error_message="用户在 Web 页面取消了任务。")
    except Exception as exc:  # pragma: no cover - 进度展示失败不应阻塞终止进程
        logger.warning("同步取消任务进度失败 job=%s error=%s", job_name, exc)


def mark_scheduler_job_progress_paused(
    *,
    job_name: str,
    progress_recorder: Any | None = None,
) -> None:
    """同步更新 Redis 进度，把任务标记为用户暂停。"""

    try:
        recorder = progress_recorder or BaseDataTaskProgressRecorder.create(backend="auto")
        recorder.job_paused(job_name=job_name, message="用户在 Web 页面暂停了任务。")
    except Exception as exc:  # pragma: no cover - 进度展示失败不应影响控制状态
        logger.warning("同步暂停任务进度失败 job=%s error=%s", job_name, exc)


def mark_scheduler_job_progress_resumed(
    *,
    job_name: str,
    progress_recorder: Any | None = None,
) -> None:
    """同步更新 Redis 进度，把任务从暂停恢复为运行中。"""

    try:
        recorder = progress_recorder or BaseDataTaskProgressRecorder.create(backend="auto")
        recorder.job_resumed(job_name=job_name, message="用户在 Web 页面继续了任务。")
    except Exception as exc:  # pragma: no cover - 进度展示失败不应影响控制状态
        logger.warning("同步继续任务进度失败 job=%s error=%s", job_name, exc)


def write_cancelled_manual_status(status_file: Path, *, job_name: str, process: JsonDict) -> None:
    """把被取消的手工 run-once 任务状态写回状态文件，供前端任务监控读取。"""

    try:
        payload = json.loads(status_file.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    now = datetime.now(tz=UTC).isoformat()
    payload.update(
        {
            "service": payload.get("service") or "base_data_scheduler",
            "state": "cancelled",
            "mode": payload.get("mode") or "run_once",
            "pid": process.get("pid"),
            "job_name": job_name,
            "last_job": job_name,
            "last_job_status": "cancelled",
            "last_error": None,
            "cancelled_at": now,
            "updated_at": now,
            "health_stale_seconds": payload.get("health_stale_seconds") or 300,
            "cancelled_process": process,
        }
    )
    write_scheduler_status_file(status_file, payload)


def write_stopped_status(status_file: Path, process: JsonDict) -> None:
    """在主动停止调度器后写入可被前端读取的状态。"""

    payload = {
        "service": "base_data_scheduler",
        "state": "stopped",
        "mode": "loop",
        "updated_at": datetime.now(tz=UTC).isoformat(),
        "health_stale_seconds": 300,
        "last_job": None,
        "last_job_status": "stopped",
        "last_error": None,
        "stopped_process": process,
    }
    write_scheduler_status_file(status_file, payload)
