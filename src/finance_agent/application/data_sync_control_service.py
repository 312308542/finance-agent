"""Web 控制台使用的数据同步配置和调度器进程控制。"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    read_scheduler_health,
    write_scheduler_status_file,
)
from finance_agent.scheduler.base_data_progress import build_progress_snapshot_response

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


def forward_scheduler_process_output(process: subprocess.Popen[str]) -> None:
    """把调度子进程的控制台输出转发到 Web 后端日志。"""

    if process.stdout is None:
        return
    try:
        for line in process.stdout:
            text = line.rstrip()
            if text:
                logger.info("[base-data-scheduler] %s", text)
    except Exception as exc:  # pragma: no cover - 日志转发失败不应影响调度进程
        logger.warning("转发基础数据调度器日志失败：%s", exc)


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
        scheduler_config_file.parent.mkdir(parents=True, exist_ok=True)
        scheduler_config_file.write_text(
            json.dumps(scheduler_payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
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
        redis_error_message: str | None = None,
    ) -> JsonDict:
        """读取 Redis 中的基础数据调度器运行态进度。"""

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
        scheduler_jobs = ()
        try:
            scheduler_config = load_scheduler_config(scheduler_config_file)
            scheduler_jobs = scheduler_config.jobs
        except Exception as exc:
            logger.warning("读取调度器配置失败，进度页将不显示等待队列：%s", exc)
        if cache_backend != "redis":
            return {
                "status": "degraded",
                "message": "Redis 不可用，无法读取实时进度。",
                "data": {
                    "cache_backend": cache_backend,
                    "tasks": [],
                    "waiting": [],
                },
            }
        if cache_status is not None and getattr(cache_status, "status", "available") != "available":
            return {
                "status": "degraded",
                "message": "Redis 不可用，无法读取实时进度。",
                "data": {
                    "cache_backend": cache_backend,
                    "tasks": [],
                    "waiting": [],
                },
            }
        return build_progress_snapshot_response(
            cache=cache,
            cache_backend=cache_backend,
            scheduler_jobs=scheduler_jobs,
            event_limit=event_limit,
            redis_error_message=cache_error,
        )

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
            sys.executable,
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
        threading.Thread(
            target=forward_scheduler_process_output,
            args=(process,),
            name="base-data-scheduler-log-forwarder",
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


def terminate_process(pid: int) -> None:
    """终止由 Web 控制台启动的调度器进程。"""

    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False)
        return
    os.kill(pid, signal.SIGTERM)


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
