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
