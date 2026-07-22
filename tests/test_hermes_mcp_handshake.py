"""Hermes MCP 握手冒烟脚本的纯逻辑测试。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts.integration import smoke_mcp_handshake as smoke


def test_default_venv_python_supports_wsl_virtual_environment(tmp_path: Path) -> None:
    """WSL 项目应优先使用 .venv-wsl 中的解释器。"""

    wsl_python = tmp_path / ".venv-wsl" / "bin" / "python"
    wsl_python.parent.mkdir(parents=True)
    wsl_python.touch()

    assert smoke.default_venv_python(tmp_path) == wsl_python


@pytest.mark.skipif(os.name == "nt", reason="Windows 测试环境不保证可创建软链接")
def test_main_does_not_resolve_python_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """握手脚本不能解析虚拟环境 Python 软链接到基础解释器。"""

    captured: dict[str, Path] = {}

    async def fake_run_mcp_handshake(*, project_root: Path, python_executable: Path) -> dict[str, object]:
        captured["project_root"] = project_root
        captured["python_executable"] = python_executable
        return {"ok": True}

    monkeypatch.setattr(smoke, "run_mcp_handshake", fake_run_mcp_handshake)

    target_path = tmp_path / "base-python"
    target_path.touch()
    symlink_path = tmp_path / "venv-python"
    symlink_path.symlink_to(target_path)
    assert smoke.main(
        [
            "--project-root",
            str(tmp_path),
            "--python-executable",
            str(symlink_path),
        ]
    ) == 0

    assert captured["python_executable"] == symlink_path.absolute()


@pytest.mark.skipif(os.name == "nt", reason="Windows 测试环境不保证可创建软链接")
def test_absolute_python_executable_preserves_lexical_path(tmp_path: Path) -> None:
    """解释器路径绝对化不应跟随软链接。"""

    target_path = tmp_path / "base-python"
    target_path.touch()
    symlink_path = tmp_path / "venv-python"
    symlink_path.symlink_to(target_path)

    assert smoke.absolute_python_executable(symlink_path) == symlink_path.absolute()


def test_validate_mcp_tool_names_requires_core_entrypoints() -> None:
    """MCP Server 必须暴露 Hermes 调用金融内核所需的核心工具。"""

    with pytest.raises(AssertionError, match="run_workflow"):
        smoke.validate_mcp_tool_names({"list_workflows", "list_tools"})


def test_validate_interface_tool_payload_requires_read_only_tools() -> None:
    """MCP 的 list_tools 调用结果必须仍是只读事实工具清单。"""

    payload = {
        "status": "ok",
        "data": {
            "tools": [
                {
                    "name": "factor.get_asset_factor_context",
                    "description": "读取因子上下文。",
                    "read_only": True,
                }
            ]
        },
    }

    smoke.validate_interface_tool_payload(payload)


def test_validate_interface_tool_payload_allows_controlled_profile_write() -> None:
    """受控的投资画像写工具必须带人工复核和写入范围。"""

    payload = {
        "status": "ok",
        "data": {
            "tools": [
                {
                    "name": "profile.upsert",
                    "description": "写入投资画像",
                    "read_only": False,
                    "requires_review": True,
                    "write_scope": "investment_profile",
                }
            ]
        },
    }

    smoke.validate_interface_tool_payload(payload)


def test_validate_interface_tool_payload_rejects_write_tool() -> None:
    """交易或执行类工具不得出现在 Hermes 可见事实工具清单中。"""

    payload = {
        "status": "ok",
        "data": {
            "tools": [
                {
                    "name": "order.execute",
                    "description": "执行订单。",
                    "read_only": True,
                }
            ]
        },
    }

    with pytest.raises(AssertionError, match="不在允许"):
        smoke.validate_interface_tool_payload(payload)
