"""验证 MCP Server 模块可以按可选依赖方式加载。"""

from __future__ import annotations

import importlib.util

from finance_agent.mcp_server.server import create_mcp_server


def main() -> None:
    """执行 MCP Server 冒烟。

    当前仓库把 MCP 作为正式工具入口，但本地环境可能还没有安装 Python MCP SDK。
    未安装时必须给出清晰错误；安装后必须能创建 server 实例。
    """

    has_mcp = importlib.util.find_spec("mcp") is not None
    if has_mcp:
        server = create_mcp_server()
        if server is None:
            raise AssertionError("MCP SDK 已安装时必须能创建 server。")
        print({"mcp_installed": True, "server_created": True})
        return

    try:
        create_mcp_server()
    except RuntimeError as exc:
        message = str(exc)
        if "缺少 MCP Python SDK" not in message:
            raise AssertionError("缺少 MCP SDK 时必须返回清晰中文提示。") from exc
        print({"mcp_installed": False, "message": message})
        return
    raise AssertionError("缺少 MCP SDK 时不应成功创建 server。")


if __name__ == "__main__":
    main()
