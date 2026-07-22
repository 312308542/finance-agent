"""finance-agent MCP 传输层启动契约测试。"""

from __future__ import annotations

from finance_agent.mcp_server import server


def test_mcp_arguments_default_to_stdio() -> None:
    """未指定传输协议时必须保持现有 stdio 行为。"""

    args = server.parse_args([])

    assert args.transport == "stdio"
    assert args.host == "127.0.0.1"
    assert args.port == 8000
    assert args.streamable_http_path == "/mcp"


def test_mcp_arguments_configure_streamable_http() -> None:
    """HTTP 模式必须能够显式配置监听地址、端口和 MCP 路径。"""

    args = server.parse_args(
        [
            "--transport",
            "streamable-http",
            "--host",
            "0.0.0.0",
            "--port",
            "8765",
            "--streamable-http-path",
            "/mcp",
        ]
    )

    assert args.transport == "streamable-http"
    assert args.host == "0.0.0.0"
    assert args.port == 8765
    assert args.streamable_http_path == "/mcp"


def test_main_passes_http_options_to_fastmcp(monkeypatch) -> None:
    """主入口必须把 HTTP 启动参数传给 FastMCP。"""

    captured: dict[str, object] = {}

    class FakeMcp:
        def run(self, transport: str) -> None:
            captured["transport"] = transport

    def fake_create_mcp_server(**kwargs):
        captured.update(kwargs)
        return FakeMcp()

    monkeypatch.setattr(server, "create_mcp_server", fake_create_mcp_server)

    server.main(
        [
            "--transport",
            "streamable-http",
            "--host",
            "0.0.0.0",
            "--port",
            "8765",
            "--streamable-http-path",
            "/mcp",
        ]
    )

    assert captured == {
        "host": "0.0.0.0",
        "port": 8765,
        "streamable_http_path": "/mcp",
        "transport": "streamable-http",
    }
