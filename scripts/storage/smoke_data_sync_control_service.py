"""验证 Web 数据同步控制服务的启动反馈语义。"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from finance_agent.application import data_sync_control_service as control_module
from finance_agent.application.data_sync_control_service import DataSyncControlService


class FakeProcess:
    """避免 smoke 测试真的启动后台调度器。"""

    pid = 24680


def main() -> None:
    """检查 dry-run 和真实同步启动响应是否能明确区分写库行为。"""

    original_popen = control_module.subprocess.Popen
    control_module.subprocess.Popen = lambda *args, **kwargs: FakeProcess()
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            service = DataSyncControlService()
            dry_run = service.start_scheduler(
                dry_run=True,
                max_cycles=1,
                config_file=temp_path / "scheduler.json",
                status_file=temp_path / "status.json",
                event_log_file=temp_path / "events.jsonl",
                process_file=temp_path / "dry-process.json",
                process_log_file=temp_path / "dry-process.log",
            )
            assert_start_response(
                dry_run,
                expected_dry_run=True,
                expected_writes_enabled=False,
                expected_command_flag="--dry-run",
            )
            if "不会写入数据库" not in dry_run.get("message", ""):
                raise AssertionError(f"dry-run 启动提示必须明确不会写库：{dry_run}")

            real_run = service.start_scheduler(
                dry_run=False,
                max_cycles=1,
                config_file=temp_path / "scheduler.json",
                status_file=temp_path / "status.json",
                event_log_file=temp_path / "events.jsonl",
                process_file=temp_path / "real-process.json",
                process_log_file=temp_path / "real-process.log",
            )
            assert_start_response(
                real_run,
                expected_dry_run=False,
                expected_writes_enabled=True,
                expected_command_flag=None,
            )
            if "会调用采集器并写入数据库" not in real_run.get("message", ""):
                raise AssertionError(f"真实同步启动提示必须明确会写库：{real_run}")
    finally:
        control_module.subprocess.Popen = original_popen

    print({"status": "ok", "checked": ["dry_run", "real_run"]})


def assert_start_response(
    response: dict[str, Any],
    *,
    expected_dry_run: bool,
    expected_writes_enabled: bool,
    expected_command_flag: str | None,
) -> None:
    """断言调度器启动响应中的关键语义字段。"""

    data = response.get("data") or {}
    process = data.get("process") or {}
    command = process.get("command") or []
    if response.get("status") != "ok":
        raise AssertionError(f"启动响应必须成功：{response}")
    if data.get("dry_run") is not expected_dry_run:
        raise AssertionError(f"响应必须回显 dry_run：{response}")
    if data.get("writes_enabled") is not expected_writes_enabled:
        raise AssertionError(f"响应必须声明是否写库：{response}")
    if process.get("dry_run") is not expected_dry_run:
        raise AssertionError(f"进程元数据必须回显 dry_run：{response}")
    if expected_command_flag and expected_command_flag not in command:
        raise AssertionError(f"dry-run 命令必须带 {expected_command_flag}：{command}")
    if expected_command_flag is None and "--dry-run" in command:
        raise AssertionError(f"真实同步命令不能带 --dry-run：{command}")


if __name__ == "__main__":
    main()
