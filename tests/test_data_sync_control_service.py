import json
import logging
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from finance_agent.application import data_sync_control_service as control_service
from finance_agent.application.data_sync_control_service import (
    _ATTACHED_SCHEDULER_LOG_FILES,
    DataSyncControlService,
    _SchedulerLogForwardDeduper,
    attach_running_manual_scheduler_log_forwarders,
    ensure_scheduler_payload,
    forward_scheduler_process_log_file,
    merge_persistent_scheduler_progress,
    read_scheduler_job_control,
    write_stopped_status,
)
from finance_agent.data.sync_config import build_preset_config, save_data_sync_config
from finance_agent.scheduler.base_data_scheduler import read_scheduler_health


class _FakeProcess:
    pid = 4242
    stdout: list[str] = []

    def __init__(self) -> None:
        self.wait_called = False

    def wait(self) -> int:
        self.wait_called = True
        return 0


class _FinishedProcess:
    pid = 4343

    def poll(self) -> int:
        return 0


def test_explicit_runtime_paths_drive_default_scheduler_status(tmp_path: Path) -> None:
    """安装到 site-packages 后，显式目录仍应驱动默认调度器状态读取。"""

    project_root = tmp_path / "image-root"
    runtime_dir = tmp_path / "shared-runtime"
    status_file = runtime_dir / "base_data_scheduler" / "status.json"
    status_file.parent.mkdir(parents=True)
    status_file.write_text(
        json.dumps(
            {
                "service": "base_data_scheduler",
                "state": "running",
                "updated_at": datetime.now(UTC).isoformat(),
                "health_stale_seconds": 300,
            }
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "FINANCE_AGENT_PROJECT_ROOT": str(project_root),
            "FINANCE_AGENT_RUNTIME_DIR": str(runtime_dir),
        }
    )
    source_root = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = os.pathsep.join(
        value
        for value in (str(source_root), env.get("PYTHONPATH", ""))
        if value
    )
    script = """
import json
from finance_agent.application.data_sync_control_service import (
    DEFAULT_STATUS_FILE,
    ROOT_DIR,
    RUNTIME_DIR,
    DataSyncControlService,
)

print(json.dumps({
    "root_dir": str(ROOT_DIR),
    "runtime_dir": str(RUNTIME_DIR),
    "default_status_file": str(DEFAULT_STATUS_FILE),
    "scheduler_status": DataSyncControlService().read_scheduler_status(),
}))
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["root_dir"] == str(project_root.resolve())
    assert payload["runtime_dir"] == str(runtime_dir.resolve())
    assert payload["default_status_file"] == str(status_file.resolve())
    assert payload["scheduler_status"]["status"] == "ok"
    assert payload["scheduler_status"]["health"]["status_file"] == str(status_file.resolve())


def test_scheduler_status_keeps_postgresql_tasks_but_marks_missing_heartbeat_degraded(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """PostgreSQL 任务可读不等于 scheduler 存活，心跳缺失必须降级。"""

    class FakeSnapshot:
        status_counts = {
            "scheduled": 1,
            "blocked": 1,
            "pending": 2,
            "running": 1,
            "completed": 3,
            "failed": 0,
            "cancelled": 0,
        }
        running_jobs = [{"job_name": "running.job"}]
        waiting_jobs = [{"job_name": "pending.job"}]
        scheduler_config_digest = "scheduler-digest"
        api_config_digest = "api-digest"
        config_drift = True

        def to_dict(self) -> dict[str, Any]:
            return {
                "source": "postgresql",
                "status_counts": self.status_counts,
                "tasks": self.running_jobs + self.waiting_jobs,
                "running_jobs": self.running_jobs,
                "waiting": self.waiting_jobs,
                "scheduler_config_digest": self.scheduler_config_digest,
                "api_config_digest": self.api_config_digest,
                "config_drift": self.config_drift,
                "metrics": {"max_concurrent_jobs": 4, "expired_lease_count": 0},
            }

    class FakeReporter:
        def snapshot(self, **kwargs: Any) -> FakeSnapshot:
            return FakeSnapshot()

    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.SchedulerRuntimeReporter.from_session",
        lambda session: FakeReporter(),
    )

    result = DataSyncControlService().read_scheduler_status(
        session=object(),
        status_file=tmp_path / "missing.json",
        scheduler_config_file=tmp_path / "missing-config.json",
    )

    assert result["status"] == "degraded"
    assert result["health"]["status"] == "missing"
    assert result["health"]["healthy"] is False
    assert result["health"]["source"] == "status_file"
    assert result["health"]["task_source"] == "postgresql"
    assert result["health"]["database_status"] == "available"
    assert result["runtime"]["status_counts"]["pending"] == 2
    assert result["runtime"]["config_drift"] is True
    assert result["process"]["running"] is False


def test_scheduler_health_marks_terminal_state_not_running(tmp_path: Path) -> None:
    """新鲜但已停止的状态文件不能被误判为调度器存活。"""

    status_file = tmp_path / "status.json"
    status_file.write_text(
        json.dumps(
            {
                "service": "base_data_scheduler",
                "state": "stopped",
                "updated_at": datetime.now(UTC).isoformat(),
                "health_stale_seconds": 300,
            }
        ),
        encoding="utf-8",
    )

    health = read_scheduler_health(status_file)

    assert health["healthy"] is False
    assert health["status"] == "stopped"


def test_scheduler_status_marks_expired_lease_unhealthy_with_fresh_heartbeat(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """存在过期运行租约时，不能仅凭新鲜状态文件返回 healthy。"""

    status_file = tmp_path / "status.json"
    status_file.write_text(
        json.dumps(
            {
                "service": "base_data_scheduler",
                "state": "running",
                "updated_at": datetime.now(UTC).isoformat(),
                "health_stale_seconds": 300,
            }
        ),
        encoding="utf-8",
    )

    class FakeSnapshot:
        running_jobs = [{"job_name": "stale.running"}]
        waiting_jobs: list[dict[str, Any]] = []
        scheduler_config_digest = "digest"
        api_config_digest = "digest"
        config_drift = False

        def to_dict(self) -> dict[str, Any]:
            return {
                "source": "postgresql",
                "generated_at": datetime.now(UTC).isoformat(),
                "running_jobs": self.running_jobs,
                "waiting": [],
                "resource_pools": {},
                "metrics": {"max_concurrent_jobs": 4, "expired_lease_count": 1},
            }

    class FakeReporter:
        def snapshot(self, **kwargs: Any) -> FakeSnapshot:
            return FakeSnapshot()

    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.SchedulerRuntimeReporter.from_session",
        lambda session: FakeReporter(),
    )

    result = DataSyncControlService().read_scheduler_status(
        session=object(),
        status_file=status_file,
        scheduler_config_file=tmp_path / "missing-config.json",
    )

    assert result["status"] == "degraded"
    assert result["health"]["status"] == "unhealthy"
    assert result["health"]["healthy"] is False
    assert "过期任务租约" in result["health"]["message"]


def test_scheduler_progress_database_failure_drops_redis_task_facts(monkeypatch) -> None:
    """数据库不可用时 Redis 只能保留基础遥测，不能继续充当任务事实源。"""

    class BrokenReporter:
        def snapshot(self, **kwargs: Any) -> None:
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.SchedulerRuntimeReporter.from_session",
        lambda session: BrokenReporter(),
    )
    response = {
        "status": "ok",
        "data": {
            "cache_backend": "redis",
            "tasks": [{"job_name": "redis.only", "status": "running"}],
            "waiting": [{"job_name": "redis.waiting"}],
            "running_jobs": [{"job_name": "redis.only"}],
            "source_rate_states": [{"source": "akshare", "state": "closed"}],
        },
    }

    result = merge_persistent_scheduler_progress(
        response,
        session=object(),
        scheduler_config=None,
        scheduler_config_file=Path("missing.json"),
    )

    assert result["status"] == "degraded"
    assert result["data"]["source"] == "unavailable"
    assert result["data"]["database_status"] == "error"
    assert result["data"]["tasks"] == []
    assert result["data"]["waiting"] == []
    assert result["data"]["running_jobs"] == []
    assert result["data"]["source_rate_states"] == [
        {"source": "akshare", "state": "closed"}
    ]


def test_scheduler_status_marks_database_failure_and_falls_back_to_status_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """数据库异常时允许读状态文件，但必须显式暴露降级来源和错误。"""

    status_file = tmp_path / "status.json"
    status_file.write_text(
        json.dumps(
            {
                "service": "base_data_scheduler",
                "state": "running",
                "updated_at": datetime.now(UTC).isoformat(),
                "health_stale_seconds": 300,
            }
        ),
        encoding="utf-8",
    )

    class BrokenReporter:
        def snapshot(self, **kwargs: Any) -> None:
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.SchedulerRuntimeReporter.from_session",
        lambda session: BrokenReporter(),
    )

    result = DataSyncControlService().read_scheduler_status(
        session=object(),
        status_file=status_file,
    )

    assert result["status"] == "degraded"
    assert result["health"]["source"] == "status_file"
    assert result["health"]["database_status"] == "error"
    assert "database unavailable" in result["health"]["database_error"]


def test_forward_scheduler_process_log_file_emits_file_lines(
    tmp_path: Path,
    caplog,
    capsys,
) -> None:
    """调度器日志文件中的采集明细应转发到后端标准日志。"""

    log_file = tmp_path / "process.log"
    log_file.write_text("采集任务开始 symbol=600519\n采集任务完成 symbol=600519\n", encoding="utf-8")

    caplog.set_level(logging.INFO, logger="finance_agent.application.data_sync_control_service")

    forward_scheduler_process_log_file(
        _FinishedProcess(),  # type: ignore[arg-type]
        log_file,
        deduper=_SchedulerLogForwardDeduper(),
        poll_interval_seconds=0.001,
    )

    assert "[base-data-scheduler] 采集任务开始 symbol=600519" in caplog.text
    assert "[base-data-scheduler] 采集任务完成 symbol=600519" in caplog.text
    captured = capsys.readouterr()
    assert "[base-data-scheduler] 采集任务开始 symbol=600519" in captured.out
    assert "[base-data-scheduler] 采集任务完成 symbol=600519" in captured.out


def test_scheduler_log_forward_deduper_skips_duplicate_lines() -> None:
    """stdout 和文件日志同时出现同一行时，只转发一次。"""

    deduper = _SchedulerLogForwardDeduper()

    assert deduper.should_forward("same line") is True
    assert deduper.should_forward("same line") is False


def test_attach_running_manual_scheduler_log_forwarders_adopts_existing_task(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """后端重启后，进度读取应能为仍在运行的手工任务补挂日志 tail 线程。"""

    manual_dir = tmp_path / "manual"
    manual_dir.mkdir()
    log_file = manual_dir / "ashare_bars.log"
    log_file.write_text("历史日志不应在接管时刷屏\n", encoding="utf-8")
    status_file = manual_dir / "ashare_bars_1d_bootstrap_20260607T000000Z.status.json"
    status_file.write_text(
        json.dumps(
            {
                "state": "running",
                "last_job_status": "running",
                "last_job": "ashare.bars.1d.bootstrap",
                "pid": 12345,
                "process_log_file": str(log_file),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    started_threads: list[dict[str, Any]] = []
    _ATTACHED_SCHEDULER_LOG_FILES.clear()

    class FakeThread:
        def __init__(
            self,
            *,
            target: Any,
            args: tuple[Any, ...],
            kwargs: dict[str, Any],
            name: str,
            daemon: bool,
        ) -> None:
            self.target = target
            self.args = args
            self.kwargs = kwargs
            self.name = name
            self.daemon = daemon

        def start(self) -> None:
            started_threads.append(
                {
                    "target": self.target,
                    "args": self.args,
                    "kwargs": self.kwargs,
                    "name": self.name,
                    "daemon": self.daemon,
                }
            )

    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.is_process_running",
        lambda pid: pid == 12345,
    )
    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.threading.Thread",
        FakeThread,
    )

    assert attach_running_manual_scheduler_log_forwarders(manual_dir) == 1
    assert attach_running_manual_scheduler_log_forwarders(manual_dir) == 0
    assert len(started_threads) == 1
    assert started_threads[0]["target"] is forward_scheduler_process_log_file
    assert started_threads[0]["args"][1] == log_file
    assert started_threads[0]["kwargs"]["start_at_end"] is True


def test_read_scheduler_progress_includes_recent_manual_analytics_job(
    tmp_path: Path,
) -> None:
    """手工 analytics 任务没有 Redis 逐标的进度时，也应在任务监控中展示最近运行状态。"""

    scheduler_config_file = tmp_path / "base_data_scheduler.json"
    manual_dir = tmp_path / "manual_runs"
    manual_dir.mkdir()
    job_name = "analytics.universe.rebuild_avoid_pool.ashare"
    scheduler_config_file.write_text(
        json.dumps(
            {
                "schema_version": "data-sync-scheduler-v1",
                "enabled": True,
                "cache_backend": "redis",
                "jobs": [
                    {
                        "name": job_name,
                        "job_type": "universe_avoid_pool_rebuild",
                        "group": "analytics",
                        "enabled": True,
                        "interval_seconds": 0,
                        "schedule_type": "after_success",
                        "depends_on": ["ashare.risk_sentiment"],
                        "market": "ashare",
                        "params": {
                            "sync_task_type": "analytics.universe.rebuild_avoid_pool",
                            "name": "A 股系统回避池",
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    safe_job_name = "analytics_universe_rebuild_avoid_pool_ashare"
    status_file = manual_dir / f"{safe_job_name}_20260614T193417Z.status.json"
    event_log_file = manual_dir / f"{safe_job_name}_20260614T193417Z.events.jsonl"
    process_log_file = manual_dir / f"{safe_job_name}_20260614T193417Z.log"
    event_log_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "job_start",
                        "job": job_name,
                        "timestamp": "2026-06-14T19:34:24+00:00",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "event": "job_success",
                        "job": job_name,
                        "summary": {"status": "available"},
                        "timestamp": "2026-06-14T19:34:37+00:00",
                    },
                    ensure_ascii=False,
                ),
            ]
        ),
        encoding="utf-8",
    )
    process_log_file.write_text(
        "调度任务开始 job=analytics.universe.rebuild_avoid_pool.ashare\n"
        "调度任务完成 job=analytics.universe.rebuild_avoid_pool.ashare status=executed\n",
        encoding="utf-8",
    )
    status_file.write_text(
        json.dumps(
            {
                "service": "base_data_scheduler",
                "state": "completed",
                "mode": "run_once",
                "job_name": job_name,
                "last_job": job_name,
                "last_job_status": "executed",
                "started_at": "2026-06-14T19:34:24+00:00",
                "updated_at": "2026-06-14T19:34:37+00:00",
                "finished_at": "2026-06-14T19:34:37+00:00",
                "event_log_file": str(event_log_file),
                "process_log_file": str(process_log_file),
                "pid": 66924,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = DataSyncControlService().read_scheduler_progress(
        event_limit=20,
        cache=control_service.NullCacheClient(),
        cache_backend="redis",
        scheduler_config_file=scheduler_config_file,
        manual_run_dir=manual_dir,
    )

    assert result["status"] == "ok"
    tasks = {task["job_name"]: task for task in result["data"]["tasks"]}
    assert job_name in tasks
    task = tasks[job_name]
    assert task["status"] == "completed"
    assert task["title"] == "A 股系统回避池"
    assert task["summary"]["total_items"] == 1
    assert task["summary"]["completed_items"] == 1
    assert task["summary"]["progress_ratio"] == 1.0
    assert [event["event_type"] for event in task["recent_events"]] == [
        "job_start",
        "job_success",
    ]
    waiting_names = {item["job_name"] for item in result["data"]["waiting"]}
    assert job_name not in waiting_names


def test_read_scheduler_progress_includes_manual_job_when_redis_degraded(
    tmp_path: Path,
) -> None:
    """Redis 降级时仍应合并手工任务状态，避免页面点击后看不到运行结果。"""

    scheduler_config_file = tmp_path / "base_data_scheduler.json"
    manual_dir = tmp_path / "manual_runs"
    manual_dir.mkdir()
    job_name = "analytics.universe.rebuild_avoid_pool.ashare"
    scheduler_config_file.write_text(
        json.dumps(
            {
                "schema_version": "data-sync-scheduler-v1",
                "enabled": True,
                "cache_backend": "redis",
                "jobs": [
                    {
                        "name": job_name,
                        "job_type": "universe_avoid_pool_rebuild",
                        "group": "analytics",
                        "enabled": True,
                        "interval_seconds": 0,
                        "schedule_type": "manual",
                        "market": "ashare",
                        "params": {
                            "sync_task_type": "analytics.universe.rebuild_avoid_pool",
                            "name": "A 股回避池重建",
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    safe_job_name = "analytics_universe_rebuild_avoid_pool_ashare"
    event_log_file = manual_dir / f"{safe_job_name}_20260615T010203Z.events.jsonl"
    process_log_file = manual_dir / f"{safe_job_name}_20260615T010203Z.log"
    status_file = manual_dir / f"{safe_job_name}_20260615T010203Z.status.json"
    event_log_file.write_text(
        json.dumps(
            {
                "event": "job_start",
                "job": job_name,
                "timestamp": "2026-06-15T01:02:03+00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    process_log_file.write_text(
        "调度任务开始 job=analytics.universe.rebuild_avoid_pool.ashare\n",
        encoding="utf-8",
    )
    status_file.write_text(
        json.dumps(
            {
                "service": "base_data_scheduler",
                "state": "running",
                "mode": "run_once",
                "job_name": job_name,
                "last_job": job_name,
                "last_job_status": "running",
                "started_at": "2026-06-15T01:02:03+00:00",
                "updated_at": "2026-06-15T01:02:03+00:00",
                "event_log_file": str(event_log_file),
                "process_log_file": str(process_log_file),
                "pid": 12345,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = DataSyncControlService().read_scheduler_progress(
        event_limit=20,
        cache=control_service.NullCacheClient(),
        cache_backend="null",
        scheduler_config_file=scheduler_config_file,
        manual_run_dir=manual_dir,
    )

    assert result["status"] == "degraded"
    tasks = {task["job_name"]: task for task in result["data"]["tasks"]}
    assert tasks[job_name]["status"] == "running"
    assert tasks[job_name]["title"] == "A 股回避池重建"
    waiting_names = {item["job_name"] for item in result["data"]["waiting"]}
    assert job_name not in waiting_names


def test_start_scheduler_reports_docker_manager_without_spawning_local_process(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Web 启动接口只能报告 Docker 调度器，不能再次拉起 Windows Python 进程。"""

    status_file = tmp_path / "status.json"
    updated_at = datetime.now(tz=UTC).isoformat()
    status_file.write_text(
        json.dumps(
            {
                "state": "running",
                "updated_at": updated_at,
                "last_job_status": "executed",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def fail_popen(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Docker 模式不应调用本机 subprocess.Popen")

    monkeypatch.setattr(control_service.subprocess, "Popen", fail_popen)

    result = DataSyncControlService().start_scheduler(status_file=status_file)

    assert result["status"] == "ok"
    assert result["data"]["managed_by"] == "docker-compose"
    assert result["data"]["service"] == "finance-agent-scheduler"
    assert result["data"]["running"] is True


def test_stop_scheduler_does_not_terminate_docker_service(tmp_path: Path) -> None:
    """Web 停止接口不能把 Docker 调度器误当成本地进程终止。"""

    status_file = tmp_path / "status.json"
    status_file.write_text(
        json.dumps(
            {
                "state": "running",
                "updated_at": datetime.now(tz=UTC).isoformat(),
                "last_job_status": "executed",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = DataSyncControlService().stop_scheduler(status_file=status_file)

    assert result["status"] == "ok"
    assert result["data"]["managed_by"] == "docker-compose"
    assert result["data"]["service"] == "finance-agent-scheduler"
    assert result["data"]["running"] is True


def test_read_scheduler_status_ignores_stale_windows_process_metadata(tmp_path: Path) -> None:
    """状态接口不能因旧 Windows PID 元数据把 Docker 服务显示为未启动。"""

    status_file = tmp_path / "status.json"
    process_file = tmp_path / "process.json"
    updated_at = datetime.now(tz=UTC).isoformat()
    status_file.write_text(
        json.dumps(
            {
                "state": "running",
                "updated_at": updated_at,
                "last_job_status": "executed",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    process_file.write_text(json.dumps({"pid": 54652}), encoding="utf-8")

    result = DataSyncControlService().read_scheduler_status(
        status_file=status_file,
        process_file=process_file,
    )

    assert result["process"] == {
        "managed_by": "docker-compose",
        "service": "finance-agent-scheduler",
        "running": True,
        "state": "running",
        "updated_at": updated_at,
    }


def test_save_config_persists_scheduler_concurrency(tmp_path: Path) -> None:
    """保存前端配置时，应把后台并发数同步到调度器配置。"""

    data_sync_config_file = tmp_path / "data_sync_config.json"
    scheduler_config_file = tmp_path / "base_data_scheduler.json"

    result = DataSyncControlService().save_config(
        preset="personal-comprehensive",
        markets=["ashare", "crypto_spot"],
        enabled=True,
        cache_backend="redis",
        max_concurrent_jobs=6,
        config_file=data_sync_config_file,
        scheduler_config_file=scheduler_config_file,
    )

    saved_config = json.loads(data_sync_config_file.read_text(encoding="utf-8"))
    scheduler_payload = json.loads(scheduler_config_file.read_text(encoding="utf-8"))

    assert result["status"] == "ok"
    assert saved_config["max_concurrent_jobs"] == 6
    assert scheduler_payload["max_concurrent_jobs"] == 6
    assert result["data"]["config"]["max_concurrent_jobs"] == 6
    assert result["data"]["scheduler_payload"]["max_concurrent_jobs"] == 6


def test_save_config_persists_scheduler_resource_pools(tmp_path: Path) -> None:
    """保存前端配置时，应把资源池额度同步到数据配置和调度器配置。"""

    data_sync_config_file = tmp_path / "data_sync_config.json"
    scheduler_config_file = tmp_path / "base_data_scheduler.json"
    resource_pools = {
        "realtime": {"max_concurrent_jobs": 2, "description": "盘中轻量任务"},
        "collection_heavy": {"max_concurrent_jobs": 3, "description": "外部重采集"},
        "default": {"max_concurrent_jobs": 4, "description": "默认兜底"},
    }

    result = DataSyncControlService().save_config(
        preset="personal-comprehensive",
        markets=["ashare", "fund"],
        enabled=True,
        cache_backend="redis",
        max_concurrent_jobs=6,
        resource_pools=resource_pools,
        config_file=data_sync_config_file,
        scheduler_config_file=scheduler_config_file,
    )

    saved_config = json.loads(data_sync_config_file.read_text(encoding="utf-8"))
    scheduler_payload = json.loads(scheduler_config_file.read_text(encoding="utf-8"))

    assert result["status"] == "ok"
    assert saved_config["resource_pools"]["collection_heavy"]["max_concurrent_jobs"] == 3
    assert scheduler_payload["resource_pools"]["collection_heavy"]["max_concurrent_jobs"] == 3
    assert result["data"]["config"]["resource_pools"]["realtime"]["max_concurrent_jobs"] == 2
    assert result["data"]["scheduler_payload"]["resource_pools"]["default"][
        "max_concurrent_jobs"
    ] == 4


def test_ensure_scheduler_payload_syncs_runtime_concurrency_limits(tmp_path: Path) -> None:
    """调度配置存在时，也必须同步数据同步配置中的全局和资源池并发闸门。"""

    data_config_file = tmp_path / "data_sync_config.json"
    scheduler_config_file = tmp_path / "base_data_scheduler.json"
    resource_pools = {
        "analytics": {"max_concurrent_jobs": 3, "description": "分析计算"},
        "collection_heavy": {"max_concurrent_jobs": 3, "description": "外部采集"},
        "default": {"max_concurrent_jobs": 5, "description": "默认兜底"},
    }
    config = replace(
        build_preset_config("personal-ashare"),
        max_concurrent_jobs=10,
        resource_pools=resource_pools,
    )
    save_data_sync_config(config, data_config_file)

    stale_payload = control_service.export_scheduler_payload(
        replace(config, max_concurrent_jobs=4, resource_pools={
            "analytics": {"max_concurrent_jobs": 1},
            "collection_heavy": {"max_concurrent_jobs": 2},
            "default": {"max_concurrent_jobs": 4},
        })
    )
    scheduler_config_file.write_text(
        json.dumps(stale_payload, ensure_ascii=False),
        encoding="utf-8",
    )

    payload = ensure_scheduler_payload(
        data_sync_config_file=data_config_file,
        scheduler_config_file=scheduler_config_file,
    )

    assert payload["max_concurrent_jobs"] == 10
    assert payload["resource_pools"]["analytics"]["max_concurrent_jobs"] == 3
    assert payload["resource_pools"]["collection_heavy"]["max_concurrent_jobs"] == 3
    persisted = json.loads(scheduler_config_file.read_text(encoding="utf-8"))
    assert persisted["max_concurrent_jobs"] == 10
    assert persisted["resource_pools"]["default"]["max_concurrent_jobs"] == 5


def test_read_scheduler_jobs_returns_visual_task_catalog(tmp_path: Path) -> None:
    """前端应能读取可执行、可配置的调度任务目录。"""

    scheduler_config_file = tmp_path / "base_data_scheduler.json"
    scheduler_config_file.write_text(
        json.dumps(
            {
                "schema_version": "data-sync-scheduler-v1",
                "enabled": True,
                "cache_backend": "redis",
                "max_concurrent_jobs": 3,
                "jobs": [
                    {
                        "name": "ashare.events",
                        "group": "ashare-p1",
                        "enabled": True,
                        "interval_seconds": 300,
                        "limit": 20,
                        "market": "ashare",
                        "params": {
                            "sync_task_type": "event_refresh",
                            "batch_size": 20,
                            "max_workers": 1,
                        },
                    },
                    {
                        "name": "ashare.bars.1d",
                        "group": "ashare-p0",
                        "enabled": True,
                        "interval_seconds": 3600,
                        "limit": 200,
                        "market": "ashare",
                        "params": {
                            "sync_task_type": "market_bars_backfill",
                            "batch_size": 200,
                            "max_workers": 4,
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = DataSyncControlService().read_scheduler_jobs(
        scheduler_config_file=scheduler_config_file,
    )

    assert result["status"] == "ok"
    assert result["data"]["config"]["max_concurrent_jobs"] == 3
    assert result["data"]["jobs"] == [
        {
            "name": "ashare.events",
            "job_type": "collection",
            "group": "ashare-p1",
            "enabled": True,
            "interval_seconds": 300,
            "limit": 20,
            "market": "ashare",
            "schedule_type": "interval",
            "run_at": [],
            "timezone": "UTC",
            "trading_day_policy": "any_day",
            "depends_on": [],
            "priority": 100,
            "resource_pool": "default",
            "params": {
                "sync_task_type": "event_refresh",
                "batch_size": 20,
                "max_workers": 1,
            },
            "runtime_control": {
                "paused": False,
                "last_action": "",
                "updated_at": None,
                "overrides": {},
            },
        },
        {
            "name": "ashare.bars.1d",
            "job_type": "collection",
            "group": "ashare-p0",
            "enabled": True,
            "interval_seconds": 3600,
            "limit": 200,
            "market": "ashare",
            "schedule_type": "interval",
            "run_at": [],
            "timezone": "UTC",
            "trading_day_policy": "any_day",
            "depends_on": [],
            "priority": 100,
            "resource_pool": "default",
            "params": {
                "sync_task_type": "market_bars_backfill",
                "batch_size": 200,
                "max_workers": 4,
            },
            "runtime_control": {
                "paused": False,
                "last_action": "",
                "updated_at": None,
                "overrides": {},
            },
        }
    ]


def test_read_scheduler_jobs_exposes_priority_and_resource_pool(tmp_path: Path) -> None:
    """任务目录应暴露优先级和资源池，便于前端解释调度顺序。"""

    scheduler_config_file = tmp_path / "base_data_scheduler.json"
    scheduler_config_file.write_text(
        json.dumps(
            {
                "schema_version": "data-sync-scheduler-v1",
                "enabled": True,
                "cache_backend": "redis",
                "max_concurrent_jobs": 3,
                "resource_pools": {
                    "realtime": {"max_concurrent_jobs": 1},
                    "default": {"max_concurrent_jobs": 3},
                },
                "jobs": [
                    {
                        "name": "ashare.realtime_quotes",
                        "group": "ashare-p0",
                        "enabled": True,
                        "interval_seconds": 300,
                        "priority": 800,
                        "resource_pool": "realtime",
                        "market": "ashare",
                        "params": {"sync_task_type": "realtime_quote_refresh"},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = DataSyncControlService().read_scheduler_jobs(
        scheduler_config_file=scheduler_config_file,
    )

    assert result["status"] == "ok"
    assert result["data"]["config"]["resource_pools"]["realtime"]["max_concurrent_jobs"] == 1
    assert result["data"]["jobs"][0]["priority"] == 800
    assert result["data"]["jobs"][0]["resource_pool"] == "realtime"


def test_scheduler_progress_exposes_global_and_pool_concurrency(tmp_path: Path) -> None:
    """任务进度接口应输出全局并发和资源池摘要，但 Redis 降级时不虚构等待队列。"""

    scheduler_config_file = tmp_path / "base_data_scheduler.json"
    scheduler_config_file.write_text(
        json.dumps(
            {
                "schema_version": "data-sync-scheduler-v1",
                "enabled": True,
                "cache_backend": "null",
                "max_concurrent_jobs": 4,
                "resource_pools": {
                    "collection_heavy": {"max_concurrent_jobs": 2},
                    "realtime": {"max_concurrent_jobs": 1},
                    "default": {"max_concurrent_jobs": 4},
                },
                "jobs": [
                    {
                        "name": "ashare.capital_flow",
                        "group": "ashare-p1",
                        "enabled": True,
                        "interval_seconds": 1800,
                        "resource_pool": "collection_heavy",
                        "priority": 600,
                        "market": "ashare",
                        "params": {"sync_task_type": "capital_flow_refresh"},
                    },
                    {
                        "name": "ashare.realtime_quotes",
                        "group": "ashare-p0",
                        "enabled": True,
                        "interval_seconds": 300,
                        "resource_pool": "realtime",
                        "priority": 800,
                        "market": "ashare",
                        "params": {"sync_task_type": "realtime_quote_refresh"},
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = DataSyncControlService().read_scheduler_progress(
        cache=control_service.NullCacheClient(),
        cache_backend="null",
        scheduler_config_file=scheduler_config_file,
    )

    assert result["status"] == "degraded"
    assert result["data"]["global_concurrency"] == {"running": 0, "limit": 4}
    assert result["data"]["resource_pools"]["collection_heavy"] == {
        "running": 0,
        "queued": 0,
        "limit": 2,
    }
    assert result["data"]["resource_pools"]["realtime"] == {
        "running": 0,
        "queued": 0,
        "limit": 1,
    }


def test_read_scheduler_jobs_merges_missing_regenerable_jobs(tmp_path: Path) -> None:
    """旧版运行时调度 JSON 应自动补入可从数据同步配置再生成的新任务。"""

    data_sync_config_file = tmp_path / "data_sync_config.json"
    scheduler_config_file = tmp_path / "base_data_scheduler.json"
    save_data_sync_config(build_preset_config("personal-comprehensive"), data_sync_config_file)
    scheduler_config_file.write_text(
        json.dumps(
            {
                "schema_version": "data-sync-scheduler-v1",
                "enabled": True,
                "cache_backend": "redis",
                "max_concurrent_jobs": 3,
                "jobs": [
                    {
                        "name": "analytics.recommendations.ashare.all_a",
                        "job_type": "recommendation_pipeline",
                        "group": "analytics",
                        "enabled": False,
                        "interval_seconds": 3600,
                        "market": "ashare",
                        "params": {
                            "sync_task_type": "analytics.recommendations",
                            "universe_id": "universe:merged:ashare:recommendation",
                        },
                    }
                ],
                "processing": {},
                "notes": ["旧版运行时调度文件"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = DataSyncControlService().read_scheduler_jobs(
        data_sync_config_file=data_sync_config_file,
        scheduler_config_file=scheduler_config_file,
    )

    saved_payload = json.loads(scheduler_config_file.read_text(encoding="utf-8"))
    jobs = {job["name"]: job for job in saved_payload["jobs"]}
    returned_jobs = {job["name"]: job for job in result["data"]["jobs"]}
    assert result["status"] == "ok"
    assert jobs["analytics.recommendations.ashare.all_a"]["enabled"] is False
    assert "analytics.universe.merge.ashare.recommendation" in jobs
    assert "analytics.universe.merge.ashare.recommendation" in returned_jobs
    assert jobs["analytics.universe.merge.ashare.recommendation"]["params"]["name"] == "A 股推荐合并候选池"
    assert jobs["analytics.recommendations.ashare.all_a"]["depends_on"] == [
        "analytics.snapshot.ashare.close",
        "analytics.sector.ashare.daily",
        "analytics.structural.ashare.daily",
        "analytics.strategy.validation_gate",
    ]


def test_update_scheduler_job_persists_editable_fields(tmp_path: Path) -> None:
    """前端保存单任务配置后，应更新运行时调度配置文件。"""

    scheduler_config_file = tmp_path / "base_data_scheduler.json"
    scheduler_config_file.write_text(
        json.dumps(
            {
                "schema_version": "data-sync-scheduler-v1",
                "enabled": True,
                "cache_backend": "redis",
                "jobs": [
                    {
                        "name": "ashare.events",
                        "group": "ashare-p1",
                        "enabled": True,
                        "interval_seconds": 300,
                        "limit": 20,
                        "market": "ashare",
                        "params": {
                            "sync_task_type": "event_refresh",
                            "batch_size": 20,
                            "max_workers": 1,
                        },
                    },
                    {
                        "name": "ashare.bars.1d",
                        "group": "ashare-p0",
                        "enabled": True,
                        "interval_seconds": 3600,
                        "limit": 200,
                        "market": "ashare",
                        "params": {
                            "sync_task_type": "market_bars_backfill",
                            "batch_size": 200,
                            "max_workers": 4,
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = DataSyncControlService().update_scheduler_job(
        job_name="ashare.events",
        enabled=False,
        interval_seconds=900,
        limit=12,
        batch_size=6,
        max_workers=2,
        scheduler_config_file=scheduler_config_file,
    )

    saved_payload = json.loads(scheduler_config_file.read_text(encoding="utf-8"))
    saved_job = saved_payload["jobs"][0]
    untouched_job = saved_payload["jobs"][1]
    assert result["status"] == "ok"
    assert result["data"]["job"]["enabled"] is False
    assert saved_job["enabled"] is False
    assert saved_job["interval_seconds"] == 900
    assert saved_job["limit"] == 12
    assert saved_job["params"]["batch_size"] == 6
    assert saved_job["params"]["max_workers"] == 2
    assert untouched_job["name"] == "ashare.bars.1d"
    assert untouched_job["interval_seconds"] == 3600
    assert untouched_job["limit"] == 200
    assert untouched_job["params"]["batch_size"] == 200
    assert untouched_job["params"]["max_workers"] == 4


def test_update_scheduler_job_exposes_runtime_control_snapshot(tmp_path: Path) -> None:
    """保存单任务配置后，响应里应带回运行期控制状态，便于前端展示热加载结果。"""

    scheduler_config_file = tmp_path / "base_data_scheduler.json"
    scheduler_config_file.write_text(
        json.dumps(
            {
                "schema_version": "data-sync-scheduler-v1",
                "enabled": True,
                "jobs": [
                    {
                        "name": "ashare.bars.1d.bootstrap",
                        "group": "ashare-p0",
                        "enabled": True,
                        "interval_seconds": 3600,
                        "limit": 200,
                        "market": "ashare",
                        "params": {
                            "sync_task_type": "market_bars_full_history_backfill",
                            "batch_size": 40,
                            "max_workers": 2,
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = DataSyncControlService().update_scheduler_job(
        job_name="ashare.bars.1d.bootstrap",
        limit=80,
        batch_size=20,
        max_workers=3,
        scheduler_config_file=scheduler_config_file,
    )

    saved_payload = json.loads(scheduler_config_file.read_text(encoding="utf-8"))
    control = read_scheduler_job_control(saved_payload, "ashare.bars.1d.bootstrap")
    assert result["status"] == "ok"
    assert result["data"]["runtime_control"]["paused"] is False
    assert result["data"]["runtime_control"]["last_action"] == "config_updated"
    assert result["data"]["runtime_control"]["overrides"] == {
        "limit": 80,
        "batch_size": 20,
        "max_workers": 3,
    }
    assert control["overrides"]["batch_size"] == 20


def test_pause_and_resume_scheduler_job_persist_runtime_control(tmp_path: Path) -> None:
    """暂停/继续应写入调度 JSON 的运行期控制区，采集子进程可热读取。"""

    scheduler_config_file = tmp_path / "base_data_scheduler.json"
    scheduler_config_file.write_text(
        json.dumps(
            {
                "schema_version": "data-sync-scheduler-v1",
                "enabled": True,
                "jobs": [
                    {
                        "name": "ashare.bars.1d.bootstrap",
                        "group": "ashare-p0",
                        "enabled": True,
                        "interval_seconds": 3600,
                        "market": "ashare",
                        "params": {
                            "sync_task_type": "market_bars_full_history_backfill",
                            "batch_size": 40,
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    paused = DataSyncControlService().pause_scheduler_job(
        job_name="ashare.bars.1d.bootstrap",
        scheduler_config_file=scheduler_config_file,
    )
    paused_payload = json.loads(scheduler_config_file.read_text(encoding="utf-8"))
    resumed = DataSyncControlService().resume_scheduler_job(
        job_name="ashare.bars.1d.bootstrap",
        scheduler_config_file=scheduler_config_file,
    )
    resumed_payload = json.loads(scheduler_config_file.read_text(encoding="utf-8"))

    assert paused["status"] == "ok"
    assert paused["data"]["runtime_control"]["paused"] is True
    assert read_scheduler_job_control(paused_payload, "ashare.bars.1d.bootstrap")["paused"] is True
    assert resumed["status"] == "ok"
    assert resumed["data"]["runtime_control"]["paused"] is False
    assert read_scheduler_job_control(resumed_payload, "ashare.bars.1d.bootstrap")["last_action"] == "resumed"


def test_run_scheduler_job_starts_one_off_process_with_single_job_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """选择单个任务执行时，应生成只包含该任务的 run-once 临时配置。"""

    scheduler_config_file = tmp_path / "base_data_scheduler.json"
    manual_dir = tmp_path / "manual"
    process_log_file = tmp_path / "manual.log"
    scheduler_config_file.write_text(
        json.dumps(
            {
                "schema_version": "data-sync-scheduler-v1",
                "enabled": True,
                "cache_backend": "redis",
                "max_concurrent_jobs": 4,
                "jobs": [
                    {
                        "name": "ashare.events",
                        "group": "ashare-p1",
                        "enabled": False,
                        "interval_seconds": 300,
                        "market": "ashare",
                        "params": {"sync_task_type": "event_refresh"},
                    },
                    {
                        "name": "ashare.realtime_quotes",
                        "group": "ashare-p0",
                        "enabled": True,
                        "interval_seconds": 300,
                        "market": "ashare",
                        "params": {"sync_task_type": "realtime_quote_refresh"},
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    popen_call: dict[str, Any] = {}

    def fake_popen(command: list[str], **kwargs: Any) -> _FakeProcess:
        popen_call["command"] = command
        popen_call["kwargs"] = kwargs
        return _FakeProcess()

    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.subprocess.Popen",
        fake_popen,
    )

    result = DataSyncControlService().run_scheduler_job(
        job_name="ashare.events",
        dry_run=True,
        scheduler_config_file=scheduler_config_file,
        manual_run_dir=manual_dir,
        process_log_file=process_log_file,
    )

    command = popen_call["command"]
    config_path = Path(command[command.index("--config") + 1])
    manual_payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert result["status"] == "ok"
    assert "--run-once" in command
    assert "--loop" not in command
    assert "--dry-run" in command
    assert manual_payload["max_concurrent_jobs"] == 1
    assert [job["name"] for job in manual_payload["jobs"]] == ["ashare.events"]
    assert manual_payload["jobs"][0]["enabled"] is True
    assert (
        manual_payload["jobs"][0]["params"]["runtime_scheduler_config_file"]
        == str(scheduler_config_file)
    )


def test_run_scheduler_job_prefers_project_venv_python(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """页面手工执行任务时应优先使用项目 .venv Python，避免外层调度进程跑偏。"""

    scheduler_config_file = tmp_path / "base_data_scheduler.json"
    manual_dir = tmp_path / "manual"
    process_log_file = tmp_path / "manual.log"
    venv_python = tmp_path / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    scheduler_config_file.write_text(
        json.dumps(
            {
                "schema_version": "data-sync-scheduler-v1",
                "enabled": True,
                "jobs": [
                    {
                        "name": "ashare.bars.1d.bootstrap",
                        "group": "ashare-p0",
                        "enabled": True,
                        "interval_seconds": 3600,
                        "market": "ashare",
                        "params": {"sync_task_type": "market_bars_full_history_backfill"},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    popen_call: dict[str, Any] = {}

    def fake_popen(command: list[str], **kwargs: Any) -> _FakeProcess:
        popen_call["command"] = command
        popen_call["kwargs"] = kwargs
        return _FakeProcess()

    monkeypatch.setattr(control_service, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(control_service.sys, "executable", "C:\\ProgramData\\anaconda3\\python.exe")
    monkeypatch.setattr(control_service.subprocess, "Popen", fake_popen)

    result = DataSyncControlService().run_scheduler_job(
        job_name="ashare.bars.1d.bootstrap",
        dry_run=True,
        scheduler_config_file=scheduler_config_file,
        manual_run_dir=manual_dir,
        process_log_file=process_log_file,
    )

    assert result["status"] == "ok"
    assert popen_call["command"][0] == str(venv_python)


def test_run_scheduler_job_rejects_duplicate_running_manual_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """同一个手动任务仍在运行时，不应再次启动一个重复采集进程。"""

    scheduler_config_file = tmp_path / "base_data_scheduler.json"
    manual_dir = tmp_path / "manual"
    manual_dir.mkdir()
    scheduler_config_file.write_text(
        json.dumps(
            {
                "schema_version": "data-sync-scheduler-v1",
                "enabled": True,
                "cache_backend": "redis",
                "jobs": [
                    {
                        "name": "ashare.events",
                        "group": "ashare-p1",
                        "enabled": False,
                        "interval_seconds": 300,
                        "market": "ashare",
                        "params": {"sync_task_type": "event_refresh"},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (manual_dir / "ashare_events_20260604T000000Z.status.json").write_text(
        json.dumps(
            {
                "state": "running",
                "last_job": "ashare.events",
                "last_job_status": "running",
                "pid": 1234,
                "started_at": "2026-06-04T00:00:00+00:00",
                "updated_at": "2026-06-04T00:01:00+00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.is_process_running",
        lambda pid: pid == 1234,
    )
    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.find_related_process_pids",
        lambda pid: [],
    )

    def fail_popen(*args: Any, **kwargs: Any) -> _FakeProcess:
        raise AssertionError("重复运行的手动任务不应启动新进程")

    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.subprocess.Popen",
        fail_popen,
    )

    result = DataSyncControlService().run_scheduler_job(
        job_name="ashare.events",
        dry_run=False,
        scheduler_config_file=scheduler_config_file,
        manual_run_dir=manual_dir,
    )

    assert result["status"] == "error"
    assert "已在运行中" in result["message"]
    assert result["data"]["running_process"]["pid"] == 1234


def test_run_scheduler_job_rejects_duplicate_while_first_starting(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """首个手工任务尚未写入 status 时，启动锁也应阻止同名任务重复启动。"""

    scheduler_config_file = tmp_path / "base_data_scheduler.json"
    manual_dir = tmp_path / "manual"
    manual_dir.mkdir()
    scheduler_config_file.write_text(
        json.dumps(
            {
                "schema_version": "data-sync-scheduler-v1",
                "enabled": True,
                "cache_backend": "redis",
                "jobs": [
                    {
                        "name": "ashare.events",
                        "group": "ashare-p1",
                        "enabled": False,
                        "interval_seconds": 300,
                        "market": "ashare",
                        "params": {"sync_task_type": "event_refresh"},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    popen_count = 0
    nested_result: dict[str, Any] = {}

    def fake_popen(command: list[str], **kwargs: Any) -> _FakeProcess:
        nonlocal popen_count
        _ = command, kwargs
        popen_count += 1
        if popen_count == 1:
            nested_result["result"] = DataSyncControlService().run_scheduler_job(
                job_name="ashare.events",
                dry_run=False,
                scheduler_config_file=scheduler_config_file,
                manual_run_dir=manual_dir,
            )
        return _FakeProcess()

    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.subprocess.Popen",
        fake_popen,
    )

    result = DataSyncControlService().run_scheduler_job(
        job_name="ashare.events",
        dry_run=False,
        scheduler_config_file=scheduler_config_file,
        manual_run_dir=manual_dir,
    )

    status_files = list(manual_dir.glob("ashare_events_*.status.json"))
    assert result["status"] == "ok"
    assert popen_count == 1
    assert nested_result["result"]["status"] == "error"
    assert "正在启动中" in nested_result["result"]["message"]
    assert not list(manual_dir.glob("ashare_events.start.lock"))
    assert len(status_files) == 1
    saved_status = json.loads(status_files[0].read_text(encoding="utf-8"))
    assert saved_status["state"] == "running"
    assert saved_status["last_job"] == "ashare.events"
    assert saved_status["last_job_status"] == "running"
    assert saved_status["pid"] == _FakeProcess.pid


def test_cancel_scheduler_job_terminates_running_manual_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """取消单任务时，应终止正在运行的手工调度进程，并把状态文件写为 cancelled。"""

    manual_dir = tmp_path / "manual"
    manual_dir.mkdir()
    status_file = manual_dir / "ashare_events_20260604T000000Z.status.json"
    status_file.write_text(
        json.dumps(
            {
                "service": "base_data_scheduler",
                "state": "running",
                "mode": "run_once",
                "last_job": "ashare.events",
                "last_job_status": "running",
                "pid": 1234,
                "started_at": "2026-06-04T00:00:00+00:00",
                "updated_at": "2026-06-04T00:01:00+00:00",
                "dry_run": False,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    terminated_pids: list[int] = []
    cancelled_progress_jobs: list[tuple[str, str]] = []
    alive_pids = {1234}

    class FakeProgressRecorder:
        def job_cancelled(self, *, job_name: str, error_message: str) -> None:
            cancelled_progress_jobs.append((job_name, error_message))

    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.is_process_running",
        lambda pid: pid in alive_pids,
    )
    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.find_related_process_pids",
        lambda pid: [],
    )
    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.terminate_process",
        lambda pid: (terminated_pids.append(pid), alive_pids.discard(pid)),
    )

    result = DataSyncControlService().cancel_scheduler_job(
        job_name="ashare.events",
        manual_run_dir=manual_dir,
        progress_recorder=FakeProgressRecorder(),
    )

    saved_status = json.loads(status_file.read_text(encoding="utf-8"))
    assert result["status"] == "ok"
    assert terminated_pids == [1234]
    assert cancelled_progress_jobs == [("ashare.events", "用户在 Web 页面取消了任务。")]
    assert result["data"]["cancelled_process"]["pid"] == 1234
    assert result["data"]["status_file"] == str(status_file)
    assert saved_status["state"] == "cancelled"
    assert saved_status["last_job_status"] == "cancelled"
    assert saved_status["cancelled_process"]["pid"] == 1234
    assert saved_status["dry_run"] is False


def test_cancel_scheduler_job_is_noop_without_running_manual_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """没有正在运行的手工任务时，取消接口应安全返回，不应误杀其他进程。"""

    manual_dir = tmp_path / "manual"
    manual_dir.mkdir()
    status_file = manual_dir / "ashare_events_20260604T000000Z.status.json"
    status_file.write_text(
        json.dumps(
            {
                "state": "completed",
                "last_job": "ashare.events",
                "last_job_status": "ok",
                "pid": 1234,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.terminate_process",
        lambda pid: (_ for _ in ()).throw(AssertionError("没有运行任务时不应终止进程")),
    )

    result = DataSyncControlService().cancel_scheduler_job(
        job_name="ashare.events",
        manual_run_dir=manual_dir,
    )

    assert result["status"] == "ok"
    assert result["data"]["cancelled"] is False
    assert "没有找到正在运行的手工任务" in result["message"]


def test_cancel_scheduler_job_terminates_child_when_parent_pid_exited(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """父调度进程已退出但采集子进程仍在运行时，也应能取消该手工任务。"""

    manual_dir = tmp_path / "manual"
    manual_dir.mkdir()
    status_file = manual_dir / "ashare_bars_1d_bootstrap_20260605T023325Z.status.json"
    status_file.write_text(
        json.dumps(
            {
                "service": "base_data_scheduler",
                "state": "running",
                "mode": "run_once",
                "last_job": "ashare.bars.1d.bootstrap",
                "last_job_status": "running",
                "pid": 90832,
                "started_at": "2026-06-05T02:33:33+00:00",
                "updated_at": "2026-06-05T03:42:23+00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    terminated_pids: list[int] = []
    alive_pids = {87828}

    class FakeProgressRecorder:
        def job_cancelled(self, *, job_name: str, error_message: str) -> None:
            _ = job_name, error_message

    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.is_process_running",
        lambda pid: pid in alive_pids,
    )
    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.find_related_process_pids",
        lambda pid: [87828] if pid == 90832 else [],
    )
    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.terminate_process",
        lambda pid: (terminated_pids.append(pid), alive_pids.discard(pid)),
    )

    result = DataSyncControlService().cancel_scheduler_job(
        job_name="ashare.bars.1d.bootstrap",
        manual_run_dir=manual_dir,
        progress_recorder=FakeProgressRecorder(),
    )

    saved_status = json.loads(status_file.read_text(encoding="utf-8"))
    assert result["status"] == "ok"
    assert terminated_pids == [87828]
    assert result["data"]["cancelled"] is True
    assert result["data"]["cancelled_process"]["pid"] == 90832
    assert result["data"]["cancelled_process"]["related_pids"] == [87828]
    assert saved_status["state"] == "cancelled"


def test_cancel_scheduler_job_reports_error_when_process_survives_termination(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """取消命令发出后进程仍存活时，不应把任务误标记为已取消。"""

    manual_dir = tmp_path / "manual"
    manual_dir.mkdir()
    status_file = manual_dir / "ashare_bars_1d_bootstrap_20260605T023325Z.status.json"
    status_file.write_text(
        json.dumps(
            {
                "service": "base_data_scheduler",
                "state": "running",
                "mode": "run_once",
                "last_job": "ashare.bars.1d.bootstrap",
                "last_job_status": "running",
                "pid": 90832,
                "started_at": "2026-06-05T02:33:33+00:00",
                "updated_at": "2026-06-05T03:42:23+00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    terminated_pids: list[int] = []
    cancelled_progress_jobs: list[str] = []

    class FakeProgressRecorder:
        def job_cancelled(self, *, job_name: str, error_message: str) -> None:
            _ = error_message
            cancelled_progress_jobs.append(job_name)

    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.is_process_running",
        lambda pid: pid == 90832,
    )
    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.find_related_process_pids",
        lambda pid: [],
    )
    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.PROCESS_TERMINATION_VERIFY_TIMEOUT_SECONDS",
        0,
    )
    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.terminate_process",
        lambda pid: terminated_pids.append(pid),
    )

    result = DataSyncControlService().cancel_scheduler_job(
        job_name="ashare.bars.1d.bootstrap",
        manual_run_dir=manual_dir,
        progress_recorder=FakeProgressRecorder(),
    )

    saved_status = json.loads(status_file.read_text(encoding="utf-8"))
    assert result["status"] == "error"
    assert result["data"]["cancelled"] is False
    assert result["data"]["alive_pids"] == [90832]
    assert terminated_pids == [90832]
    assert cancelled_progress_jobs == []
    assert saved_status["state"] == "running"
    assert saved_status["last_job_status"] == "running"


def test_run_scheduler_job_ignores_stale_manual_status_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """状态文件显示 running 但 PID 已不存在时，应允许重新启动任务。"""

    scheduler_config_file = tmp_path / "base_data_scheduler.json"
    manual_dir = tmp_path / "manual"
    manual_dir.mkdir()
    scheduler_config_file.write_text(
        json.dumps(
            {
                "schema_version": "data-sync-scheduler-v1",
                "enabled": True,
                "cache_backend": "redis",
                "jobs": [
                    {
                        "name": "ashare.events",
                        "group": "ashare-p1",
                        "enabled": False,
                        "interval_seconds": 300,
                        "market": "ashare",
                        "params": {"sync_task_type": "event_refresh"},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (manual_dir / "ashare_events_20260604T000000Z.status.json").write_text(
        json.dumps(
            {
                "state": "running",
                "last_job": "ashare.events",
                "last_job_status": "running",
                "pid": 1234,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    popen_call: dict[str, Any] = {}

    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.is_process_running",
        lambda pid: False,
    )
    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.find_related_process_pids",
        lambda pid: [],
    )

    def fake_popen(command: list[str], **kwargs: Any) -> _FakeProcess:
        popen_call["command"] = command
        popen_call["kwargs"] = kwargs
        return _FakeProcess()

    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.subprocess.Popen",
        fake_popen,
    )

    result = DataSyncControlService().run_scheduler_job(
        job_name="ashare.events",
        dry_run=True,
        scheduler_config_file=scheduler_config_file,
        manual_run_dir=manual_dir,
    )

    assert result["status"] == "ok"
    assert "--run-once" in popen_call["command"]


def test_rerun_failed_scheduler_job_runs_failed_items_with_serial_overrides(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """失败项重跑应进入单任务、单并发、失败/过期项优先的 run-once 配置。"""

    scheduler_config_file = tmp_path / "base_data_scheduler.json"
    manual_dir = tmp_path / "failed-rerun"
    scheduler_config_file.write_text(
        json.dumps(
            {
                "schema_version": "data-sync-scheduler-v1",
                "enabled": True,
                "cache_backend": "redis",
                "max_concurrent_jobs": 4,
                "jobs": [
                    {
                        "name": "ashare.bars.1d.bootstrap",
                        "group": "ashare-p0",
                        "enabled": False,
                        "interval_seconds": 0,
                        "limit": 200,
                        "market": "ashare",
                        "params": {
                            "sync_task_type": "market_bars_full_history_backfill",
                            "symbol_source": "market_assets",
                            "batch_size": 200,
                            "max_workers": 4,
                        },
                        "schedule_type": "manual",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    fake_process = _FakeProcess()
    popen_call: dict[str, Any] = {}

    def fake_popen(command: list[str], **kwargs: Any) -> _FakeProcess:
        popen_call["command"] = command
        popen_call["kwargs"] = kwargs
        return fake_process

    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.subprocess.Popen",
        fake_popen,
    )

    result = DataSyncControlService().rerun_failed_scheduler_job(
        job_name="ashare.bars.1d.bootstrap",
        dry_run=False,
        scheduler_config_file=scheduler_config_file,
        failed_rerun_dir=manual_dir,
        run_inline=True,
    )

    command = popen_call["command"]
    config_path = Path(command[command.index("--config") + 1])
    rerun_payload = json.loads(config_path.read_text(encoding="utf-8"))
    selected_job = rerun_payload["jobs"][0]
    assert result["status"] == "ok"
    assert result["data"]["queue"]["mode"] == "inline"
    assert fake_process.wait_called is True
    assert "--run-once" in command
    assert rerun_payload["max_concurrent_jobs"] == 1
    assert selected_job["name"] == "ashare.bars.1d.bootstrap"
    assert selected_job["enabled"] is True
    assert selected_job["params"]["only_failed_or_stale"] is True
    assert selected_job["params"]["max_workers"] == 1


def test_write_stopped_status_uses_atomic_scheduler_status_writer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """主动停止调度器时，也应使用安全状态文件写入路径。"""

    calls: list[tuple[Path, dict[str, Any]]] = []

    def fake_write_scheduler_status_file(status_file: Path, payload: dict[str, Any]) -> None:
        calls.append((status_file, payload))
        status_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.write_scheduler_status_file",
        fake_write_scheduler_status_file,
    )

    status_file = tmp_path / "status.json"
    process = {"pid": 1234, "running": False}

    write_stopped_status(status_file, process)

    assert calls
    assert calls[0][0] == status_file
    assert calls[0][1]["state"] == "stopped"
    assert calls[0][1]["stopped_process"] == process


def test_rerun_failed_fund_scheduler_job_uses_serial_failed_only_overrides(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """基金失败项重跑也应强制串行，并优先只跑失败或过期标的。"""

    scheduler_config_file = tmp_path / "base_data_scheduler.json"
    manual_dir = tmp_path / "failed-rerun-fund"
    scheduler_config_file.write_text(
        json.dumps(
            {
                "schema_version": "data-sync-scheduler-v1",
                "enabled": True,
                "cache_backend": "redis",
                "max_concurrent_jobs": 4,
                "jobs": [
                    {
                        "name": "fund.etf.bars.1d.bootstrap",
                        "group": "fund",
                        "enabled": False,
                        "interval_seconds": 0,
                        "limit": 50,
                        "market": "fund",
                        "params": {
                            "sync_task_type": "market_bars_full_history_backfill",
                            "fund_asset_type": "etf",
                            "symbol_source": "market_assets",
                            "batch_size": 50,
                            "max_workers": 2,
                        },
                        "schedule_type": "manual",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    fake_process = _FakeProcess()
    popen_call: dict[str, Any] = {}

    def fake_popen(command: list[str], **kwargs: Any) -> _FakeProcess:
        popen_call["command"] = command
        popen_call["kwargs"] = kwargs
        return fake_process

    monkeypatch.setattr(
        "finance_agent.application.data_sync_control_service.subprocess.Popen",
        fake_popen,
    )

    result = DataSyncControlService().rerun_failed_scheduler_job(
        job_name="fund.etf.bars.1d.bootstrap",
        dry_run=False,
        scheduler_config_file=scheduler_config_file,
        failed_rerun_dir=manual_dir,
        run_inline=True,
    )

    command = popen_call["command"]
    config_path = Path(command[command.index("--config") + 1])
    rerun_payload = json.loads(config_path.read_text(encoding="utf-8"))
    selected_job = rerun_payload["jobs"][0]
    assert result["status"] == "ok"
    assert selected_job["params"]["only_failed_or_stale"] is True
    assert selected_job["params"]["max_workers"] == 1
    assert rerun_payload["max_concurrent_jobs"] == 1
