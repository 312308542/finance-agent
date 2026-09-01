"""调度器配置身份与常驻入口约束测试。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from finance_agent.scheduler.base_data_scheduler import (
    BaseDataScheduler,
    BaseDataSchedulerConfig,
    BaseDataSchedulerJob,
)
from finance_agent.scheduler.config_identity import load_config_identity

ROOT_DIR = Path(__file__).resolve().parents[1]


def _load_scheduler_script():
    path = ROOT_DIR / "scripts" / "data" / "run_base_data_scheduler.py"
    spec = importlib.util.spec_from_file_location("scheduler_config_identity_cli", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_config_identity_uses_absolute_path_sha256_and_version(tmp_path: Path) -> None:
    """身份必须绑定实际文件内容，而不是调用方提供的相对路径。"""

    config_path = tmp_path / "scheduler.json"
    config_path.write_text(
        json.dumps({"schema_version": "scheduler-v7", "jobs": []}),
        encoding="utf-8",
    )

    identity = load_config_identity(config_path)

    assert identity.path == str(config_path.resolve())
    assert len(identity.digest) == 64
    assert identity.version == "scheduler-v7"
    assert identity.mtime_ns == config_path.stat().st_mtime_ns


def test_loop_requires_explicit_config_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """常驻 loop 不允许静默使用内置配置。"""

    module = _load_scheduler_script()
    monkeypatch.setattr(sys, "argv", ["run_base_data_scheduler.py", "--loop"])

    with pytest.raises(SystemExit) as exc_info:
        module.parse_args()

    assert exc_info.value.code == 2


def test_scheduler_status_reports_loaded_config_identity(tmp_path: Path) -> None:
    """状态文件应报告 scheduler 实际加载的配置身份。"""

    config_path = tmp_path / "scheduler.json"
    config_path.write_text(
        json.dumps({"schema_version": "scheduler-v1", "jobs": []}),
        encoding="utf-8",
    )
    status_path = tmp_path / "status.json"
    scheduler = BaseDataScheduler(
        BaseDataSchedulerConfig(jobs=()),
        scheduler_config_file=config_path,
        status_file=status_path,
    )

    scheduler.write_status(state="running")

    status = json.loads(status_path.read_text(encoding="utf-8"))
    identity = load_config_identity(config_path)
    assert status["config_path"] == identity.path
    assert status["config_digest"] == identity.digest
    assert status["config_version"] == identity.version
    assert status["config_loaded_at"]
    assert status["config_mtime_ns"] == identity.mtime_ns
    assert status["config_reload_error"] is None


def test_failed_hot_reload_keeps_last_config_identity(tmp_path: Path) -> None:
    """热重载失败时继续报告最后成功版本，并暴露错误。"""

    config_path = tmp_path / "scheduler.json"
    config_path.write_text(
        json.dumps({"schema_version": "scheduler-v1", "jobs": []}),
        encoding="utf-8",
    )

    def invalidate_config(_args: object) -> dict[str, str]:
        config_path.write_text("{invalid", encoding="utf-8")
        return {"status": "ok"}

    scheduler = BaseDataScheduler(
        BaseDataSchedulerConfig(
            job_timeout_seconds=0,
            loop_idle_seconds=0.01,
            jobs=(
                BaseDataSchedulerJob(
                    name="test.reload",
                    group="test",
                    interval_seconds=60,
                    params={"name": "test.reload"},
                ),
            ),
        ),
        scheduler_config_file=config_path,
        collect_base_data_func=invalidate_config,
        default_collection_args_func=lambda **kwargs: type("Args", (), kwargs)(),
        sleep_func=lambda _seconds: None,
    )
    original_identity = scheduler.config_identity

    scheduler.run_loop(max_cycles=1)

    assert scheduler.config_identity == original_identity
    assert "JSON" in str(scheduler.config_reload_error)


def test_root_compose_passes_scheduler_config_path() -> None:
    """根 Compose 的 scheduler 必须显式加载共享 runtime 配置。"""

    content = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    expected = (
        "${FINANCE_AGENT_DOCKER_RUNTIME_DIR:-/app/runtime}/"
        "base_data_scheduler/base_data_scheduler.json"
    )
    scheduler_section = content.split("  finance-agent-scheduler:", 1)[1].split(
        "  finance-agent-mcp:", 1
    )[0]

    assert "- --config" in scheduler_section
    assert f"- {expected}" in scheduler_section
