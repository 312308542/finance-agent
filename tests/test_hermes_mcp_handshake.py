"""Hermes MCP 握手冒烟脚本的纯逻辑测试。"""

from __future__ import annotations

import pytest

from scripts.integration import smoke_mcp_handshake as smoke


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
