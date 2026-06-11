"""验证 Hermes skill 中列出的 finance-agent 常用命令。

direct 模式在 Windows 本机直接调用项目 venv Python；wsl-bridge 模式由 T4 补齐。
脚本只调用本项目 CLI，不直接访问外部行情源，不创建业务数据。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


JsonDict = dict[str, Any]
Validator = Callable[["CommandResult"], JsonDict | None]

EXPECTED_WORKFLOWS = {
    "portfolio_monitoring",
    "watchlist_management",
    "recommendation_decision",
    "asset_deep_analysis",
    "swap_decision",
    "daily_review",
}
ALLOWED_TOOL_PREFIXES = (
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
FORBIDDEN_TOOL_KEYWORDS = ("order", "trade", "execute", "broker", "place")
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}"),
)


@dataclass(frozen=True)
class CommandResult:
    """单条冒烟命令的执行结果。"""

    name: str
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    elapsed_seconds: float


@dataclass(frozen=True)
class SmokeStep:
    """一条 Hermes skill 命令冒烟步骤。"""

    name: str
    cli_args: tuple[str, ...]
    validator: Validator
    timeout_seconds: int = 60


def project_root_from_script() -> Path:
    """推断仓库根目录。"""

    return Path(__file__).resolve().parents[2]


def default_venv_python(project_root: Path) -> Path:
    """返回当前平台对应的项目 venv Python。"""

    windows_python = project_root / ".venv" / "Scripts" / "python.exe"
    if windows_python.exists():
        return windows_python
    return project_root / ".venv" / "bin" / "python"


def build_direct_cli_command(
    *,
    python_executable: str,
    cli_args: Sequence[str],
) -> list[str]:
    """构造 direct 模式 CLI 命令。"""

    return [python_executable, "-m", "finance_agent.cli", *cli_args]


def run_command(
    *,
    name: str,
    command: list[str],
    cwd: Path,
    timeout_seconds: int,
) -> CommandResult:
    """执行子进程并返回完整输出。"""

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    started = time.perf_counter()
    process = subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout_seconds,
    )
    return CommandResult(
        name=name,
        command=command,
        returncode=process.returncode,
        stdout=process.stdout,
        stderr=process.stderr,
        elapsed_seconds=time.perf_counter() - started,
    )


def parse_json_stdout(result: CommandResult) -> JsonDict:
    """解析 stdout 中的 JSON，失败时附带命令上下文。"""

    if result.returncode != 0:
        raise AssertionError(format_failure(result))
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"{result.name} stdout 不是合法 JSON：{result.stdout}") from exc
    if not isinstance(payload, dict):
        raise AssertionError(f"{result.name} stdout JSON 顶层必须是对象。")
    return payload


def require_status_ok(payload: JsonDict, *, step_name: str) -> None:
    """断言 CLI 返回统一 ok 状态。"""

    if payload.get("status") != "ok":
        raise AssertionError(f"{step_name} 返回状态不是 ok：{payload}")


def validate_workflow_list(payload: JsonDict) -> JsonDict:
    """校验 Workflow 清单。"""

    require_status_ok(payload, step_name="workflows list")
    workflows = payload.get("data", {}).get("workflows", [])
    workflow_types = {
        item.get("workflow_type")
        for item in workflows
        if isinstance(item, dict)
    }
    missing = EXPECTED_WORKFLOWS - workflow_types
    if missing:
        raise AssertionError(f"workflows list 缺少 Workflow：{sorted(missing)}")
    return payload


def validate_tool_list(payload: JsonDict) -> JsonDict:
    """校验 Hermes 可见工具清单。"""

    require_status_ok(payload, step_name="tools list")
    tools = payload.get("data", {}).get("tools", [])
    if not tools:
        raise AssertionError("tools list 必须返回至少一个工具。")
    for tool in tools:
        name = str(tool.get("name") or "")
        if tool.get("read_only") is not True:
            raise AssertionError(f"{name} 缺少 read_only=true 元数据。")
        if not name.startswith(ALLOWED_TOOL_PREFIXES):
            raise AssertionError(f"{name} 不在 Hermes 允许的只读工具命名空间内。")
        if any(keyword in name.lower() for keyword in FORBIDDEN_TOOL_KEYWORDS):
            raise AssertionError(f"{name} 疑似交易/执行类工具，不应暴露给 Hermes。")
    return payload


def validate_model_config_text(text: str) -> None:
    """校验模型配置输出没有泄漏真实 API key。"""

    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            raise AssertionError("models config 输出疑似真实 API key。")


def validate_model_config(result: CommandResult) -> JsonDict:
    """校验模型配置摘要。"""

    payload = parse_json_stdout(result)
    require_status_ok(payload, step_name="models config")
    validate_model_config_text(result.stdout)
    return payload


def validate_json_ok(result: CommandResult) -> JsonDict:
    """校验通用 JSON ok 命令。"""

    payload = parse_json_stdout(result)
    require_status_ok(payload, step_name=result.name)
    return payload


def validate_agent_run_once(result: CommandResult) -> JsonDict:
    """校验内部 Agent 单轮消费命令。"""

    payload = validate_json_ok(result)
    data = payload.get("data", {})
    if not isinstance(data, dict):
        raise AssertionError("agent run-once data 必须是对象。")
    return payload


def validate_workflow_run(result: CommandResult) -> JsonDict:
    """校验 Workflow 运行结果并返回 run payload。"""

    payload = validate_json_ok(result)
    run_id = payload.get("data", {}).get("workflow_run_id")
    if not run_id:
        raise AssertionError(f"workflows run 未返回 workflow_run_id：{payload}")
    return payload


def validate_report_markdown(result: CommandResult) -> None:
    """校验中文 Markdown 报告。"""

    if result.returncode != 0:
        raise AssertionError(format_failure(result))
    text = result.stdout
    required_keywords = ("执行摘要", "风险")
    missing = [keyword for keyword in required_keywords if keyword not in text]
    if missing:
        raise AssertionError(f"reports show --markdown 缺少中文报告关键字：{missing}")
    return None


def format_failure(result: CommandResult) -> str:
    """格式化失败命令，保留完整 stdout/stderr。"""

    return (
        f"命令失败：{result.name}\n"
        f"returncode={result.returncode}\n"
        f"command={result.command}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def build_direct_steps(
    *,
    owner_id: str,
    asset_id: str,
    portfolio_id: str,
    watchlist_id: str,
) -> list[SmokeStep]:
    """构造 direct 模式固定冒烟步骤。"""

    return [
        SmokeStep(
            name="workflows list",
            cli_args=("workflows", "list"),
            validator=lambda result: validate_workflow_list(parse_json_stdout(result)),
        ),
        SmokeStep(
            name="tools list",
            cli_args=("tools", "list"),
            validator=lambda result: validate_tool_list(parse_json_stdout(result)),
        ),
        SmokeStep(
            name="models config",
            cli_args=("models", "config"),
            validator=validate_model_config,
        ),
        SmokeStep(
            name="graph health",
            cli_args=("graph", "health"),
            validator=validate_json_ok,
        ),
        SmokeStep(
            name="agent run-once",
            cli_args=("agent", "run-once", "--limit", "1"),
            validator=validate_agent_run_once,
        ),
        SmokeStep(
            name="workflows run asset_deep_analysis",
            cli_args=(
                "workflows",
                "run",
                "asset_deep_analysis",
                "--owner-id",
                owner_id,
                "--asset-id",
                asset_id,
                "--portfolio-id",
                portfolio_id,
                "--watchlist-id",
                watchlist_id,
            ),
            validator=validate_workflow_run,
            timeout_seconds=120,
        ),
    ]


def run_direct_smoke(args: argparse.Namespace) -> list[dict[str, Any]]:
    """执行 direct 模式冒烟。"""

    project_root = Path(args.project_root).resolve()
    python_executable = str(Path(args.python_executable).resolve())
    summaries: list[dict[str, Any]] = []
    workflow_run_id: str | None = None

    for step in build_direct_steps(
        owner_id=args.owner_id,
        asset_id=args.asset_id,
        portfolio_id=args.portfolio_id,
        watchlist_id=args.watchlist_id,
    ):
        command = build_direct_cli_command(
            python_executable=python_executable,
            cli_args=step.cli_args,
        )
        result = run_command(
            name=step.name,
            command=command,
            cwd=project_root,
            timeout_seconds=step.timeout_seconds,
        )
        payload = step.validator(result)
        if step.name == "workflows run asset_deep_analysis" and payload is not None:
            workflow_run_id = str(payload["data"]["workflow_run_id"])
        summaries.append(command_summary(result))

    if workflow_run_id:
        report_result = run_command(
            name="reports show markdown",
            command=build_direct_cli_command(
                python_executable=python_executable,
                cli_args=("reports", "show", workflow_run_id, "--markdown"),
            ),
            cwd=project_root,
            timeout_seconds=60,
        )
        validate_report_markdown(report_result)
        summaries.append(command_summary(report_result))

    return summaries


def command_summary(result: CommandResult) -> dict[str, Any]:
    """生成简洁执行摘要。"""

    return {
        "name": result.name,
        "returncode": result.returncode,
        "elapsed_seconds": round(result.elapsed_seconds, 3),
    }


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""

    project_root = project_root_from_script()
    parser = argparse.ArgumentParser(description="Hermes skill 常用命令冒烟验证。")
    parser.add_argument("--mode", choices=("direct", "wsl-bridge"), default="direct")
    parser.add_argument("--project-root", default=str(project_root))
    parser.add_argument("--python-executable", default=str(default_venv_python(project_root)))
    parser.add_argument("--owner-id", default="owner:demo")
    parser.add_argument("--asset-id", default="asset:demo:600519")
    parser.add_argument("--portfolio-id", default="portfolio:demo")
    parser.add_argument("--watchlist-id", default="watchlist:demo")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    """脚本入口。"""

    args = parse_args(argv)
    if args.mode != "direct":
        raise NotImplementedError("wsl-bridge 模式将在方案 01-T4 中实现。")

    try:
        summaries = run_direct_smoke(args)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"status": "ok", "mode": args.mode, "steps": summaries}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
