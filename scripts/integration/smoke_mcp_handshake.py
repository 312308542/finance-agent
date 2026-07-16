"""验证 finance-agent MCP Server 可被 Hermes 等客户端握手和枚举工具。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable

JsonDict = dict[str, Any]

REQUIRED_MCP_TOOLS = {
    "list_workflows",
    "run_workflow",
    "get_workflow_run",
    "get_report",
    "list_tools",
    "call_tool",
    "evaluate_triggers",
    "dispatch_triggers",
    "run_triggers_once",
    "graph_health",
    "memory_recall_asset_context",
    "memory_get_asset_timeline",
}
ALLOWED_INTERFACE_TOOL_PREFIXES = (
    "portfolio.",
    "watchlist.",
    "factor.",
    "signal_risk.",
    "memory.",
    "graph.",
    "data_quality.",
    "recommendation.",
    "workflow.",
)
FORBIDDEN_INTERFACE_TOOL_KEYWORDS = ("order", "trade", "execute", "broker", "place")


def project_root_from_script() -> Path:
    """推断仓库根目录。"""

    return Path(__file__).resolve().parents[2]


def default_venv_python(project_root: Path) -> Path:
    """返回项目 venv Python。"""

    windows_python = project_root / ".venv" / "Scripts" / "python.exe"
    if windows_python.exists():
        return windows_python
    return project_root / ".venv" / "bin" / "python"


def validate_mcp_tool_names(tool_names: set[str]) -> None:
    """校验 MCP wrapper 工具清单。"""

    missing = REQUIRED_MCP_TOOLS - tool_names
    if missing:
        raise AssertionError(f"MCP Server 缺少工具：{sorted(missing)}")


def validate_interface_tool_payload(payload: JsonDict) -> None:
    """校验 MCP list_tools wrapper 返回的事实工具清单。"""

    if payload.get("status") != "ok":
        raise AssertionError(f"MCP list_tools 返回状态不是 ok：{payload}")
    tools = payload.get("data", {}).get("tools", [])
    if not tools:
        raise AssertionError("MCP list_tools 必须返回至少一个事实工具。")
    for tool in tools:
        name = str(tool.get("name") or "")
        if tool.get("read_only") is not True:
            raise AssertionError(f"{name} 缺少 read_only=true 元数据。")
        if not name.startswith(ALLOWED_INTERFACE_TOOL_PREFIXES):
            raise AssertionError(f"{name} 不在允许的只读工具命名空间内。")
        if any(keyword in name.lower() for keyword in FORBIDDEN_INTERFACE_TOOL_KEYWORDS):
            raise AssertionError(f"{name} 疑似交易/执行类工具，不应暴露给 Hermes。")


def extract_json_from_call_tool_result(result: Any) -> JsonDict:
    """从 MCP call_tool 返回对象中提取 JSON。"""

    content_items = getattr(result, "content", None) or []
    texts: list[str] = []
    for item in content_items:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            texts.append(text)
    if not texts:
        raise AssertionError(f"MCP call_tool 未返回文本内容：{result}")
    for text in texts:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise AssertionError(f"MCP call_tool 文本不是 JSON 对象：{texts}")


async def run_mcp_handshake(*, project_root: Path, python_executable: Path) -> dict[str, Any]:
    """启动 MCP Server 子进程并完成 initialize/list_tools。"""

    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:
        raise RuntimeError(
            "缺少 MCP Python SDK。请先安装项目依赖或执行："
            '.\\.venv\\Scripts\\python.exe -m pip install "mcp[cli]"'
        ) from exc

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    server = StdioServerParameters(
        command=str(python_executable),
        args=["-m", "finance_agent.mcp_server"],
        cwd=str(project_root),
        env=env,
    )
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools_result = await session.list_tools()
            tool_names = {tool.name for tool in tools_result.tools}
            validate_mcp_tool_names(tool_names)
            interface_tools_result = await session.call_tool("list_tools", {})
            interface_payload = extract_json_from_call_tool_result(interface_tools_result)
            validate_interface_tool_payload(interface_payload)
            return {
                "mcp_tool_count": len(tool_names),
                "interface_tool_count": len(interface_payload["data"]["tools"]),
                "required_tools_present": True,
            }


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""

    project_root = project_root_from_script()
    parser = argparse.ArgumentParser(description="验证 finance-agent MCP Server 握手。")
    parser.add_argument("--project-root", default=str(project_root))
    parser.add_argument("--python-executable", default=str(default_venv_python(project_root)))
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    """脚本入口。"""

    args = parse_args(argv)
    try:
        result = asyncio.run(
            run_mcp_handshake(
                project_root=Path(args.project_root).resolve(),
                python_executable=Path(args.python_executable).resolve(),
            )
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", "data": result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
