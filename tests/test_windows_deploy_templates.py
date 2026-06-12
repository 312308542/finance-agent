from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WINDOWS_DEPLOY = ROOT / "deploy" / "windows"


def read_template(name: str) -> str:
    return (WINDOWS_DEPLOY / name).read_text(encoding="utf-8")


def test_windows_scheduler_task_template_registers_daemon_with_logs() -> None:
    """Windows 调度器模板应注册常驻基础数据调度任务并保留状态文件和事件日志。"""

    content = read_template("register_scheduler_task.ps1")

    assert "Register-ScheduledTask" in content
    assert "New-ScheduledTaskTrigger -AtStartup" in content
    assert "scripts\\data\\run_base_data_scheduler.py" in content
    assert "--loop" in content
    assert "--status-file" in content
    assert "--event-log-file" in content
    assert "New-ScheduledTaskSettingsSet" in content
    assert "-RestartCount" in content
    assert "SupportsShouldProcess" in content


def test_windows_api_task_template_binds_localhost_by_default() -> None:
    """Windows API 模板应默认仅绑定 127.0.0.1，避免无认证 API 直接暴露。"""

    content = read_template("register_api_task.ps1")

    assert "Register-ScheduledTask" in content
    assert "-m uvicorn finance_agent.api.app:app" in content
    assert "--app-dir src" in content
    assert '[string]$BindHost = "127.0.0.1"' in content
    assert "--host $BindHost" in content
    assert "[int]$Port = 8000" in content
    assert "--port $Port" in content
    assert "当前 API 无认证层" in content
    assert "SupportsShouldProcess" in content


def test_windows_unregister_template_removes_both_tasks() -> None:
    """卸载模板应能移除调度器和 API 两个计划任务。"""

    content = read_template("unregister_tasks.ps1")

    assert "Unregister-ScheduledTask" in content
    assert "FinanceAgent-BaseDataScheduler" in content
    assert "FinanceAgent-Api" in content
    assert "SupportsShouldProcess" in content


def test_windows_deploy_readme_documents_install_status_logs_and_upgrade() -> None:
    """Windows 部署 README 应覆盖安装、状态查看、日志位置、升级和安全边界。"""

    content = read_template("README.md")

    for expected in (
        "安装",
        "查看状态",
        "日志位置",
        "升级流程",
        "127.0.0.1",
        "runtime/base_data_scheduler/status.json",
        "runtime/base_data_scheduler/events.jsonl",
        "alembic upgrade head",
        "当前 API 无认证层",
    ):
        assert expected in content
