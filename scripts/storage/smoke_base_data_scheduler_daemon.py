"""验证基础数据调度器的常驻运行能力。

覆盖能力：
- 失败任务按配置重试。
- 调度器写入结构化 JSONL 事件日志。
- 调度器写入健康状态文件。
- 健康检查能读取最新状态。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from argparse import Namespace
from pathlib import Path
from typing import Any

from finance_agent.scheduler import (
    BaseDataScheduler,
    BaseDataSchedulerConfig,
    BaseDataSchedulerJob,
    collect_base_data_with_timeout,
    read_scheduler_health,
)


def slow_collect(_: Any) -> dict[str, Any]:
    """模拟卡住的采集函数。"""

    time.sleep(5)
    return {"status": "unexpected"}


def heartbeat_collect(_: Any) -> dict[str, Any]:
    """模拟能正常完成但耗时超过一次心跳窗口的采集函数。"""

    time.sleep(2.2)
    return {"status": "ok"}


def main() -> None:
    """执行常驻调度器 smoke。"""

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        status_file = temp_path / "scheduler-status.json"
        log_file = temp_path / "scheduler-events.jsonl"
        attempts: list[str] = []
        sleeps: list[float] = []

        def collect(_: Any) -> dict[str, Any]:
            attempts.append("run")
            if len(attempts) < 3:
                raise RuntimeError(f"模拟采集失败 {len(attempts)}")
            return {
                "status": "ok",
                "available": 1,
                "failed": 0,
                "skipped": 0,
                "total_tasks": 1,
            }

        config = BaseDataSchedulerConfig(
            jobs=(
                BaseDataSchedulerJob(
                    name="daemon-smoke",
                    group="ashare-p0",
                    interval_seconds=1,
                    limit=1,
                    market="ashare",
                ),
            ),
            max_job_retries=2,
            retry_backoff_seconds=1,
            job_timeout_seconds=0,
        )
        scheduler = BaseDataScheduler(
            config,
            collect_base_data_func=collect,
            default_collection_args_func=lambda **kwargs: type("Args", (), kwargs)(),
            sleep_func=lambda seconds: sleeps.append(seconds),
            status_file=status_file,
            event_log_file=log_file,
        )

        result = scheduler.run_once()
        job = result["jobs"][0]
        if job["status"] != "executed":
            raise AssertionError(f"任务最终必须执行成功：{job}")
        if job["attempt_count"] != 3 or len(attempts) != 3:
            raise AssertionError(f"任务必须重试到第三次成功：{job}")
        if sleeps != [1, 1]:
            raise AssertionError(f"失败重试必须按退避时间休眠：{sleeps}")

        status_payload = json.loads(status_file.read_text(encoding="utf-8"))
        if status_payload["state"] != "completed":
            raise AssertionError(f"状态文件必须记录 completed：{status_payload}")
        if status_payload["last_job_status"] != "executed":
            raise AssertionError(f"状态文件必须记录最后任务成功：{status_payload}")

        events = [
            json.loads(line)
            for line in log_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        event_types = [event["event"] for event in events]
        for expected in (
            "scheduler_start",
            "job_start",
            "job_retry",
            "job_success",
            "scheduler_stop",
        ):
            if expected not in event_types:
                raise AssertionError(f"事件日志缺少 {expected}：{event_types}")

        health = read_scheduler_health(status_file, max_age_seconds=60)
        if not health["healthy"] or health["status"] != "healthy":
            raise AssertionError(f"健康检查必须通过：{health}")

        cli_health = subprocess.run(
            [
                sys.executable,
                "scripts/data/run_base_data_scheduler.py",
                "--health-check",
                "--status-file",
                str(status_file),
                "--health-max-age-seconds",
                "60",
            ],
            check=True,
            capture_output=True,
            encoding="utf-8",
            text=True,
        )
        cli_payload = json.loads(cli_health.stdout)
        if not cli_payload["healthy"]:
            raise AssertionError(f"CLI 健康检查必须通过：{cli_payload}")

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        status_file = temp_path / "timeout-status.json"
        log_file = temp_path / "timeout-events.jsonl"
        timeout_config = BaseDataSchedulerConfig(
            jobs=(
                BaseDataSchedulerJob(
                    name="timeout-smoke",
                    group="ashare-p2",
                    interval_seconds=1,
                    limit=1,
                    market="ashare",
                ),
            ),
            max_job_retries=2,
            retry_backoff_seconds=1,
            job_timeout_seconds=1,
        )
        timeout_scheduler = BaseDataScheduler(
            timeout_config,
            collect_base_data_func=slow_collect,
            default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
            sleep_func=lambda _: None,
            status_file=status_file,
            event_log_file=log_file,
        )
        timeout_result = timeout_scheduler.run_once()
        timeout_job = timeout_result["jobs"][0]
        if timeout_job["status"] != "failed":
            raise AssertionError(f"超时任务必须失败返回：{timeout_job}")
        if timeout_job["attempt_count"] != 1:
            raise AssertionError(f"超时任务不应继续重试拖慢调度：{timeout_job}")
        if "超过 1 秒" not in timeout_job.get("error_message", ""):
            raise AssertionError(f"超时错误信息不符合预期：{timeout_job}")

        timeout_status = json.loads(status_file.read_text(encoding="utf-8"))
        if timeout_status["last_job_status"] != "failed":
            raise AssertionError(f"状态文件必须记录超时失败：{timeout_status}")

        timeout_events = [
            json.loads(line)
            for line in log_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        timeout_event_types = [event["event"] for event in timeout_events]
        if "job_timeout" not in timeout_event_types:
            raise AssertionError(f"事件日志必须记录 job_timeout：{timeout_event_types}")

        heartbeats: list[float] = []
        heartbeat_result = collect_base_data_with_timeout(
            Namespace(group=["ashare-p2"]),
            timeout_seconds=15,
            heartbeat_interval_seconds=1,
            heartbeat=lambda: heartbeats.append(time.monotonic()),
            collect_base_data_func=heartbeat_collect,
        )
        if heartbeat_result["status"] != "ok":
            raise AssertionError(f"心跳采集必须正常返回：{heartbeat_result}")
        if not heartbeats:
            raise AssertionError("长采集任务等待期间必须执行健康心跳")

        merge_status_file = temp_path / "merge-status.json"
        merge_scheduler = BaseDataScheduler(
            config,
            collect_base_data_func=heartbeat_collect,
            default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
            status_file=merge_status_file,
        )
        merge_scheduler.write_status(state="running", mode="loop", dry_run=False)
        merge_scheduler.write_status(
            state="running",
            last_job="heartbeat-job",
            last_job_status="running",
        )
        merged_status = json.loads(merge_status_file.read_text(encoding="utf-8"))
        if merged_status.get("mode") != "loop" or merged_status.get("dry_run") is not False:
            raise AssertionError(f"心跳刷新必须保留调度上下文：{merged_status}")

    print(
        {
            "status": "ok",
            "attempt_count": len(attempts),
            "event_count": len(events),
            "health": health["status"],
            "cli_health": cli_payload["status"],
            "timeout_status": timeout_job["status"],
            "heartbeat_count": len(heartbeats),
        }
    )


if __name__ == "__main__":
    main()
