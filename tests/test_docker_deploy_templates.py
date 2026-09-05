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
    assert "ARG PIP_INDEX_URL=https://pypi.org/simple" in content
    assert "pip install --no-cache-dir" in content
    assert "pip install --no-cache-dir ." in content
    assert "scripts/data/run_base_data_scheduler.py" in content
    assert "--loop" in content
    assert "--status-file" in content
    assert "--event-log-file" in content
    assert "$FINANCE_AGENT_RUNTIME_DIR/base_data_scheduler/status.json" in content
    assert "$FINANCE_AGENT_RUNTIME_DIR/base_data_scheduler/events.jsonl" in content


def test_backend_dockerfile_runs_fastapi_with_native_talib() -> None:
    """后端镜像应安装完整项目依赖并默认运行 FastAPI。"""

    content = read_template("Dockerfile.backend")

    assert "FROM python:3.11-slim" in content
    assert "build-essential" in content
    assert "ta-lib" in content.lower()
    assert "ARG PIP_INDEX_URL=https://pypi.org/simple" in content
    assert "pip install --no-cache-dir ." in content
    assert "COPY config ./config" in content
    assert "finance_agent.api.app:app" in content
    assert 'EXPOSE 8000' in content
    assert 'HEALTHCHECK' in content


def test_docker_runtime_paths_are_explicit_and_shared() -> None:
    """镜像和 Compose 应显式约定项目目录及共享运行目录。"""

    backend_dockerfile_content = read_template("Dockerfile.backend")
    scheduler_dockerfile_content = read_template("Dockerfile.scheduler")
    compose_content = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    for dockerfile_content in (backend_dockerfile_content, scheduler_dockerfile_content):
        assert "FINANCE_AGENT_PROJECT_ROOT=/app" in dockerfile_content
        assert "FINANCE_AGENT_RUNTIME_DIR=/app/runtime" in dockerfile_content
    assert compose_content.count("FINANCE_AGENT_PROJECT_ROOT: /app") == 4
    assert "FINANCE_AGENT_DOCKER_PROJECT_ROOT" not in compose_content
    runtime_path_expression = "${FINANCE_AGENT_DOCKER_RUNTIME_DIR:-/app/runtime}"
    assert compose_content.count(
        f"FINANCE_AGENT_RUNTIME_DIR: {runtime_path_expression}"
    ) == 4
    assert compose_content.count(f"target: {runtime_path_expression}") == 4
    assert f"- {runtime_path_expression}/base_data_scheduler/status.json" in compose_content
    assert f"- {runtime_path_expression}/base_data_scheduler/events.jsonl" in compose_content


def test_frontend_dockerfile_builds_spa_and_nginx_proxies_api() -> None:
    """前端镜像应使用多阶段构建，并通过 Nginx 提供同源 API。"""

    dockerfile_content = read_template("Dockerfile.frontend")
    nginx_content = read_template("nginx.agent-office.conf")

    assert "FROM node:22-alpine AS build" in dockerfile_content
    assert "npm ci --no-audit --no-fund" in dockerfile_content
    assert "VITE_FINANCE_AGENT_API_BASE=/" in dockerfile_content
    assert "npm run build" in dockerfile_content
    assert "FROM nginx:alpine" in dockerfile_content
    assert "COPY --from=build /app/dist /usr/share/nginx/html" in dockerfile_content
    assert "resolver 127.0.0.11 valid=10s ipv6=off" in nginx_content
    assert "set $finance_agent_api finance-agent-api:8000" in nginx_content
    assert "proxy_pass http://$finance_agent_api" in nginx_content
    assert "proxy_buffering off" in nginx_content
    assert "try_files $uri $uri/ /index.html" in nginx_content


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


def test_gotdx_gateway_allows_build_time_module_proxy_override() -> None:
    """网络受限环境可覆盖模块代理，但不能关闭依赖完整性校验。"""

    content = read_template("Dockerfile.gotdx-gateway")
    declaration = "ARG GOPROXY=https://proxy.golang.org,direct"
    assert declaration in content
    assert content.index(declaration) < content.index("RUN go mod download")
    assert "GOSUMDB=off" not in content


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
    """根 Compose 应作为全部运行服务的一键启动入口。"""

    content = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert content.count("restart: unless-stopped") == 9
    assert "finance-agent-gotdx-gateway" in content
    assert "finance-agent-scheduler" in content
    assert "finance-agent-realtime-monitor" in content
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


def test_root_compose_builds_and_exposes_backend_and_frontend() -> None:
    """根 Compose 应构建前后端镜像，并让前端等待 API 健康。"""

    content = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "finance-agent-api:" in content
    assert "image: finance-agent-backend:local" in content
    assert "dockerfile: deploy/docker/Dockerfile.backend" in content
    assert "FINANCE_AGENT_PIP_INDEX_URL" in content
    assert (
        "PIP_INDEX_URL: ${FINANCE_AGENT_PIP_INDEX_URL:-https://pypi.org/simple}"
        in content
    )
    assert "FINANCE_AGENT_API_PORT:-8000" in content
    assert "target: ${FINANCE_AGENT_DOCKER_RUNTIME_DIR:-/app/runtime}" in content
    assert "finance-agent-web:" in content
    assert "image: finance-agent-frontend:local" in content
    assert "dockerfile: deploy/docker/Dockerfile.frontend" in content
    assert "VITE_FINANCE_AGENT_API_BASE: ${FINANCE_AGENT_WEB_API_BASE:-/}" in content
    assert "FINANCE_AGENT_WEB_PORT:-5173" in content
    assert "finance-agent-api:\n        condition: service_healthy" in content


def test_root_compose_runs_schema_migration_before_database_consumers_start() -> None:
    """根 Compose 必须先迁移 schema，再启动 scheduler、MCP 和 API。"""

    compose_content = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    dockerfile_content = read_template("Dockerfile.scheduler")

    assert "finance-agent-migrate:" in compose_content
    assert "python -m alembic -c /app/alembic.ini upgrade head" in compose_content
    assert compose_content.count("condition: service_completed_successfully") == 5
    assert "COPY pyproject.toml README.md alembic.ini ./" in dockerfile_content


def test_compose_defines_independent_realtime_monitor_service() -> None:
    """根 Compose 和调度模板都必须运行独立实时监控进程。"""

    root_content = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    scheduler_content = read_template("compose.scheduler.yml")

    for content in (root_content, scheduler_content):
        assert "finance-agent-realtime-monitor:" in content
        assert "scripts/data/run_realtime_quote_monitor.py" in content
        assert "--loop" in content
        assert "FINANCE_AGENT_GOTDX_GATEWAY_URL" in content
        assert "finance-agent-gotdx-gateway:\n        condition: service_healthy" in content
    assert "finance-agent-migrate:\n        condition: service_completed_successfully" in root_content
    assert "postgres:\n        condition: service_healthy" in root_content


def test_compose_defines_independent_position_monitor_service() -> None:
    """根 Compose 和调度模板必须单独运行盘中持仓监控进程。"""

    root_content = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    scheduler_content = read_template("compose.scheduler.yml")
    for content in (root_content, scheduler_content):
        assert "finance-agent-position-monitor:" in content
        assert "scripts/runtime/run_position_monitor.py" in content
        assert "--loop" in content
        assert "finance-agent-realtime-monitor" in content
    assert "finance-agent-migrate" in root_content
    assert "postgres" in root_content
    assert "redis" in root_content
