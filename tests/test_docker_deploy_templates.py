from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKER_DEPLOY = ROOT / "deploy" / "docker"


def read_template(name: str) -> str:
    return (DOCKER_DEPLOY / name).read_text(encoding="utf-8")


def test_scheduler_dockerfile_installs_project_and_talib_dependencies() -> None:
    """调度器 Dockerfile 应安装项目包和 TA-Lib 相关系统依赖。"""

    content = read_template("Dockerfile.scheduler")

    assert "FROM python:3.11-slim" in content
    assert "build-essential" in content
    assert "ta-lib" in content.lower() or "libta-lib" in content.lower()
    assert "pip install --no-cache-dir" in content
    assert "pip install --no-cache-dir ." in content
    assert "scripts/data/run_base_data_scheduler.py" in content
    assert "--loop" in content
    assert "--status-file" in content
    assert "--event-log-file" in content


def test_scheduler_compose_mounts_runtime_and_uses_external_services() -> None:
    """compose 片段应挂载 runtime，并通过环境变量连接既有 Postgres/Redis。"""

    content = read_template("compose.scheduler.yml")

    assert "finance-agent-scheduler" in content
    assert "Dockerfile.scheduler" in content
    assert "runtime:/app/runtime" in content
    assert "FINANCE_AGENT_DATABASE_URL" in content
    assert "FINANCE_AGENT_REDIS_URL" in content
    assert "postgresql+psycopg://" in content
    assert "redis://redis:6379/0" in content
    assert "restart: unless-stopped" in content
    assert "scripts/data/run_base_data_scheduler.py" in content
    assert "finance-agent-gotdx-gateway" in content
    assert "Dockerfile.gotdx-gateway" in content
    assert "condition: service_healthy" in content
    assert "FINANCE_AGENT_GOTDX_GATEWAY_URL" in content


def test_gotdx_gateway_dockerfile_builds_standalone_go_binary() -> None:
    content = read_template("Dockerfile.gotdx-gateway")

    assert "FROM golang:1.26-alpine" in content
    assert "go build" in content
    assert "gotdx-gateway" in content
    assert "TDX_GATEWAY_ADDR=0.0.0.0:8790" in content


def test_dockerignore_excludes_heavy_and_sensitive_paths() -> None:
    """Docker 构建上下文应排除本地环境、运行数据和敏感配置。"""

    content = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    for expected in [
        ".git",
        ".venv",
        ".ai",
        ".gitnexus",
        ".idea",
        "runtime",
        "artifacts",
        "build",
        "dist",
        "apps/agent-office/node_modules",
        "apps/agent-office/dist",
        ".env",
        "__pycache__",
    ]:
        assert expected in content


def test_root_postgres_compose_raises_local_connection_limit() -> None:
    """本地 TimescaleDB 应提高连接数，以适配 API、调度器和 MCP 并发。"""

    content = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "max_connections=300" in content


def test_root_compose_includes_all_runtime_services() -> None:
    """根 Compose 应作为四项运行服务的一键启动入口。"""

    content = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert content.count("restart: unless-stopped") == 5
    assert "finance-agent-gotdx-gateway" in content
    assert "finance-agent-scheduler" in content
    assert "Dockerfile.gotdx-gateway" in content
    assert "Dockerfile.scheduler" in content
    assert "context: ." in content
    assert "FINANCE_AGENT_DOCKER_DATABASE_URL" in content
    assert "FINANCE_AGENT_DOCKER_REDIS_URL" in content
    assert "@postgres:5432/finance_agent" in content
    assert "redis://redis:6379/0" in content
    assert "postgres:\n        condition: service_healthy" in content
    assert "redis:\n        condition: service_healthy" in content
    assert "finance-agent-gotdx-gateway:\n        condition: service_healthy" in content
