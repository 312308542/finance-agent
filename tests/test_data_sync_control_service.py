import json
from pathlib import Path
from typing import Any

from finance_agent.application.data_sync_control_service import (
    DataSyncControlService,
    write_stopped_status,
)
from finance_agent.data.sync_config import build_preset_config, save_data_sync_config


class _FakeProcess:
    pid = 4242


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
    assert "stdout" not in popen_call["kwargs"]
    assert "stderr" not in popen_call["kwargs"]
    assert {job["params"]["universe_id"] for job in analytics_jobs} == {
        "universe:base:ashare:p0:all_a",
        "universe:base:crypto:spot:binance",
        "universe:base:crypto:future:binance",
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
