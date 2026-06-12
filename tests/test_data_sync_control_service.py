import json
import logging
import os
from pathlib import Path
from typing import Any

from finance_agent.application import data_sync_control_service as control_service
from finance_agent.application.data_sync_control_service import (
    DataSyncControlService,
    _ATTACHED_SCHEDULER_LOG_FILES,
    _SchedulerLogForwardDeduper,
    attach_running_manual_scheduler_log_forwarders,
    forward_scheduler_process_log_file,
    read_scheduler_job_control,
    write_stopped_status,
)
from finance_agent.data.sync_config import build_preset_config, save_data_sync_config


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
        def __init__(self, *, target: Any, args: tuple[Any, ...], kwargs: dict[str, Any], name: str, daemon: bool) -> None:
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


def test_start_scheduler_refreshes_existing_scheduler_config(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """启动调度器前应刷新已存在的旧 scheduler JSON，确保新增 analytics job 生效。"""

    data_sync_config_file = tmp_path / "data_sync_config.json"
    scheduler_config_file = tmp_path / "base_data_scheduler.json"
    status_file = tmp_path / "status.json"
    event_log_file = tmp_path / "events.jsonl"
    process_file = tmp_path / "process.json"
    process_log_file = tmp_path / "process.log"
    save_data_sync_config(build_preset_config("personal-comprehensive"), data_sync_config_file)
    scheduler_config_file.write_text(
        json.dumps(
            {
                "schema_version": "data-sync-scheduler-v1",
                "enabled": True,
                "jobs": [],
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

    result = DataSyncControlService().start_scheduler(
        dry_run=True,
        max_cycles=1,
        config_file=scheduler_config_file,
        data_sync_config_file=data_sync_config_file,
        status_file=status_file,
        event_log_file=event_log_file,
        process_file=process_file,
        process_log_file=process_log_file,
    )

    saved_payload = json.loads(scheduler_config_file.read_text(encoding="utf-8"))
    analytics_jobs = [
        job for job in saved_payload["jobs"] if job.get("job_type") == "recommendation_pipeline"
    ]
    assert result["status"] == "ok"
    assert "--process-log-file" in popen_call["command"]
    assert str(process_log_file) in popen_call["command"]
    assert popen_call["kwargs"]["stdout"] is not None
    assert popen_call["kwargs"]["stderr"] is not None
    assert {job["params"]["universe_id"] for job in analytics_jobs} == {
        "universe:merged:ashare:recommendation",
        "universe:base:crypto:spot:binance",
        "universe:base:crypto:future:binance",
    }


def test_start_scheduler_prefers_project_venv_python(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """启动后台调度器时应优先使用项目 .venv，避免采集子进程跑到 Anaconda。"""

    data_sync_config_file = tmp_path / "data_sync_config.json"
    scheduler_config_file = tmp_path / "base_data_scheduler.json"
    status_file = tmp_path / "status.json"
    event_log_file = tmp_path / "events.jsonl"
    process_file = tmp_path / "process.json"
    process_log_file = tmp_path / "process.log"
    venv_python = tmp_path / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    save_data_sync_config(build_preset_config("personal-comprehensive"), data_sync_config_file)

    popen_call: dict[str, Any] = {}

    def fake_popen(command: list[str], **kwargs: Any) -> _FakeProcess:
        popen_call["command"] = command
        popen_call["kwargs"] = kwargs
        return _FakeProcess()

    monkeypatch.setattr(control_service, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(control_service.sys, "executable", "C:\\ProgramData\\anaconda3\\python.exe")
    monkeypatch.setattr(control_service.subprocess, "Popen", fake_popen)

    result = DataSyncControlService().start_scheduler(
        dry_run=True,
        max_cycles=1,
        config_file=scheduler_config_file,
        data_sync_config_file=data_sync_config_file,
        status_file=status_file,
        event_log_file=event_log_file,
        process_file=process_file,
        process_log_file=process_log_file,
    )

    assert result["status"] == "ok"
    assert popen_call["command"][0] == str(venv_python)


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
