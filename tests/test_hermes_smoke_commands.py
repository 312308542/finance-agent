"""Hermes skill 命令冒烟脚本的纯逻辑测试。"""

from __future__ import annotations

import pytest

from scripts.integration import smoke_hermes_skill_commands as smoke


def test_build_direct_cli_command_uses_project_venv() -> None:
    """direct 模式应使用项目 venv Python 调用 CLI 模块。"""

    command = smoke.build_direct_cli_command(
        python_executable=r"D:\Code\aiAgents\finance-agent\.venv\Scripts\python.exe",
        cli_args=("workflows", "list"),
    )

    assert command == [
        r"D:\Code\aiAgents\finance-agent\.venv\Scripts\python.exe",
        "-m",
        "finance_agent.cli",
        "workflows",
        "list",
    ]


def test_validate_workflows_requires_all_expected_workflows() -> None:
    """Workflow 清单必须固定包含 Hermes skill 允许的 6 个入口。"""

    payload = {
        "status": "ok",
        "data": {
            "workflows": [
                {"workflow_type": workflow_type}
                for workflow_type in smoke.EXPECTED_WORKFLOWS
                if workflow_type != "daily_review"
            ]
        },
    }

    with pytest.raises(AssertionError, match="daily_review"):
        smoke.validate_workflow_list(payload)


def test_validate_tools_requires_read_only_metadata() -> None:
    """工具清单必须显式标识只读权限。"""

    payload = {
        "status": "ok",
        "data": {
            "tools": [
                {
                    "name": "factor.get_asset_factor_context",
                    "description": "读取因子上下文。",
                }
            ]
        },
    }

    with pytest.raises(AssertionError, match="read_only"):
        smoke.validate_tool_list(payload)


def test_validate_model_config_rejects_plain_secret() -> None:
    """模型配置输出不得泄漏真实 API key 形态字符串。"""

    with pytest.raises(AssertionError, match="疑似真实 API key"):
        smoke.validate_model_config_text('{"api_key": "sk-1234567890abcdefghijklmnopqrstuvwxyz"}')
