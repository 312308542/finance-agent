"""检查 Hermes 调用 finance-agent 前的项目可见性。

脚本只验证最小可见性：项目目录、pyproject.toml、venv Python 和 CLI 模块是否存在。
默认使用 WSL 路径，允许通过 FINANCE_AGENT_PROJECT_ROOT 覆盖，便于 Windows 本地和 Hermes WSL 桥接共用。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

DEFAULT_PROJECT_ROOT = "/mnt/d/Code/aiAgents/finance-agent"


def _venv_python_path(project_root: Path) -> Path:
    """根据运行环境返回项目 venv Python 的候选路径。"""

    windows_candidate = project_root / ".venv" / "Scripts" / "python.exe"
    posix_candidate = project_root / ".venv" / "bin" / "python"
    if windows_candidate.exists():
        return windows_candidate
    return posix_candidate


def check_visibility(project_root: Path) -> dict[str, Any]:
    """返回 Hermes 集成前置可见性检查结果。"""

    pyproject = project_root / "pyproject.toml"
    venv_python = _venv_python_path(project_root)
    cli_package = project_root / "src" / "finance_agent" / "cli" / "main.py"
    mcp_server = project_root / "src" / "finance_agent" / "mcp_server" / "server.py"

    checks = {
        "project_root": project_root.exists() and project_root.is_dir(),
        "pyproject": pyproject.exists(),
        "venv_python": venv_python.exists(),
        "cli_entry": cli_package.exists(),
        "mcp_server": mcp_server.exists(),
    }

    return {
        "ok": all(checks.values()),
        "project_root": str(project_root),
        "venv_python": str(venv_python),
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 Hermes 是否能看到 finance-agent 项目入口。")
    parser.add_argument(
        "--project-root",
        default=os.environ.get("FINANCE_AGENT_PROJECT_ROOT", DEFAULT_PROJECT_ROOT),
        help="finance-agent 项目根目录，默认读取 FINANCE_AGENT_PROJECT_ROOT 或 WSL 路径。",
    )
    args = parser.parse_args()

    result = check_visibility(Path(args.project_root).expanduser())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
