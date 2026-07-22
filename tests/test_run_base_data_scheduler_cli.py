"""基础数据调度器 CLI 的回归测试。"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]


def _load_scheduler_script():
    path = ROOT_DIR / "scripts" / "data" / "run_base_data_scheduler.py"
    spec = importlib.util.spec_from_file_location("run_base_data_scheduler_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_run_base_data_scheduler_only_runs_named_job(tmp_path: Path) -> None:
    """`--only` 应显式启用并只执行指定的手动任务。"""

    config_path = tmp_path / "scheduler.json"
    config_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "cache_backend": "null",
                "jobs": [
                    {
                        "name": "ashare.universe.all",
                        "job_type": "collection",
                        "group": "ashare-p0",
                        "interval_seconds": 3600,
                        "market": "ashare",
                        "limit": 1,
                        "params": {"group": ["ashare-p0"]},
                    },
                    {
                        "name": "analytics.structural.ashare.daily",
                        "enabled": False,
                        "job_type": "structural_methodology_refresh",
                        "group": "analytics",
                        "interval_seconds": 3600,
                        "market": "ashare",
                        "schedule_type": "manual",
                        "params": {"scope": "technical_screening_pool"},
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT_DIR / "scripts" / "data" / "run_base_data_scheduler.py"),
            "--config",
            str(config_path),
            "--run-once",
            "--dry-run",
            "--only",
            "analytics.structural.ashare.daily",
            "--status-file",
            str(tmp_path / "status.json"),
            "--event-log-file",
            str(tmp_path / "events.jsonl"),
            "--log-level",
            "CRITICAL",
        ],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(result.stdout)
    assert [job["job"] for job in payload["jobs"]] == ["analytics.structural.ashare.daily"]
    assert payload["jobs"][0]["status"] == "planned"


def test_build_gotdx_gateway_context_maps_cli_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _load_scheduler_script()
    captured: list[object] = []

    class _Supervisor:
        def __init__(self, config: object) -> None:
            captured.append(config)

    import finance_agent.runtime as runtime

    monkeypatch.setattr(runtime, "GotdxGatewaySupervisor", _Supervisor)
    args = SimpleNamespace(
        manage_gotdx_gateway=True,
        gotdx_gateway_command="gotdx-gateway --read-timeout 3",
        gotdx_gateway_url="http://127.0.0.1:18790",
        gotdx_gateway_working_dir=str(tmp_path),
        gotdx_gateway_log_file="runtime/test-gotdx.log",
        gotdx_gateway_startup_timeout_seconds=9.0,
        gotdx_gateway_monitor_interval_seconds=2.0,
        gotdx_gateway_max_restarts=4,
    )

    context = module.build_gotdx_gateway_context(args)

    assert isinstance(context, _Supervisor)
    config = captured[0]
    assert config.command == ("gotdx-gateway", "--read-timeout", "3")
    assert config.base_url == "http://127.0.0.1:18790"
    assert config.working_dir == tmp_path
    assert config.log_file == ROOT_DIR / "runtime" / "test-gotdx.log"
    assert config.max_restart_attempts == 4
