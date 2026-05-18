"""finance-agent CLI。

CLI 是上层 Agent 最轻量的本地调用入口：只解析参数、调用共用接口层并输出
结构化 JSON。它不直接抓行情、不计算因子、不调用模型。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from finance_agent.agents.interfaces import FinanceAgentInterface, parse_datetime
from finance_agent.storage.db import create_session_factory, session_scope

JsonDict = dict[str, Any]


def main(argv: list[str] | None = None) -> int:
    """CLI 入口函数。"""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        data = dispatch(args)
    except Exception as exc:
        print_json({"status": "error", "message": str(exc)}, stream=sys.stderr)
        return 1

    if getattr(args, "markdown", False):
        markdown = data.get("data", {}).get("markdown")
        if markdown:
            print(markdown)
            return 0
    print_json(data)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""

    parser = argparse.ArgumentParser(
        prog="finance-agent",
        description="私人金融助手 CLI：调用已入库数据工具和金融团队 Workflow。",
    )
    parser.add_argument("--database-url", default=None, help="覆盖 FINANCE_AGENT_DATABASE_URL。")
    subparsers = parser.add_subparsers(dest="group", required=True)

    workflows = subparsers.add_parser("workflows", help="Workflow 调度与查询。")
    workflow_commands = workflows.add_subparsers(dest="command", required=True)
    workflow_commands.add_parser("list", help="列出可调度 Workflow。")

    run = workflow_commands.add_parser("run", help="运行一个金融团队 Workflow。")
    run.add_argument("workflow_type", help="Workflow 类型，例如 recommendation_decision。")
    add_workflow_run_arguments(run)

    show = workflow_commands.add_parser("show", help="查看 Workflow 运行和审计事件。")
    show.add_argument("workflow_run_id", help="Workflow Run ID。")

    tools = subparsers.add_parser("tools", help="只读金融事实工具。")
    tool_commands = tools.add_subparsers(dest="command", required=True)
    tool_commands.add_parser("list", help="列出可调用工具。")
    call = tool_commands.add_parser("call", help="调用一个只读金融事实工具。")
    call.add_argument("tool_name", help="工具名，例如 factor.get_asset_factor_context。")
    call.add_argument("--arguments", default="{}", help="JSON 字符串参数。")
    call.add_argument("--arguments-file", default=None, help="从 JSON 文件读取参数。")

    reports = subparsers.add_parser("reports", help="中文解释报告。")
    report_commands = reports.add_subparsers(dest="command", required=True)
    report_show = report_commands.add_parser("show", help="读取 Workflow 中文报告。")
    report_show.add_argument("workflow_run_id", help="Workflow Run ID。")
    report_show.add_argument("--markdown", action="store_true", help="仅输出 Markdown 正文。")
    return parser


def add_workflow_run_arguments(parser: argparse.ArgumentParser) -> None:
    """注册 Workflow 运行参数。"""

    parser.add_argument("--owner-id", required=True, help="用户/账户 ID。")
    parser.add_argument("--workflow-run-id", default=None, help="自定义 Workflow Run ID。")
    parser.add_argument("--trigger-type", default="manual", help="触发类型。")
    parser.add_argument("--trigger-ref", default=None, help="触发对象引用。")
    parser.add_argument("--started-at", default=None, help="ISO 时间，默认当前时间。")
    parser.add_argument("--portfolio-id", default=None, help="组合 ID。")
    parser.add_argument("--watchlist-id", default=None, help="观察池 ID。")
    parser.add_argument("--recommendation-run-id", default=None, help="推荐运行 ID。")
    parser.add_argument("--asset-id", default=None, help="单标的资产 ID。")
    parser.add_argument("--asset-ids", default=None, help="逗号分隔的多个资产 ID。")
    parser.add_argument("--source-asset-id", default=None, help="换股/换币中的弱持仓资产 ID。")
    parser.add_argument("--candidate-asset-id", default=None, help="换股/换币中的强候选资产 ID。")
    parser.add_argument("--horizon", default="swing", help="分析周期。")
    parser.add_argument("--timeframe", default="1d", help="K 线周期。")
    parser.add_argument("--recommendation-limit", type=int, default=20, help="推荐结果读取数量。")
    parser.add_argument("--initial-state", default="{}", help="额外 Workflow state JSON。")
    parser.add_argument("--initial-state-file", default=None, help="从 JSON 文件读取额外 state。")


def dispatch(args: argparse.Namespace) -> JsonDict:
    """执行具体命令。"""

    session_factory = create_session_factory(args.database_url)
    with session_scope(session_factory) as session:
        interface = FinanceAgentInterface(session)
        if args.group == "workflows":
            return dispatch_workflows(interface, args).to_dict()
        if args.group == "tools":
            return dispatch_tools(interface, args).to_dict()
        if args.group == "reports":
            return dispatch_reports(interface, args).to_dict()
    raise ValueError(f"未知命令组：{args.group}")


def dispatch_workflows(
    interface: FinanceAgentInterface,
    args: argparse.Namespace,
):
    """处理 Workflow 命令。"""

    if args.command == "list":
        return interface.list_workflows()
    if args.command == "show":
        return interface.get_workflow_run(workflow_run_id=args.workflow_run_id)
    if args.command == "run":
        return interface.run_workflow(
            workflow_type=args.workflow_type,
            owner_id=args.owner_id,
            workflow_run_id=args.workflow_run_id,
            trigger_type=args.trigger_type,
            trigger_ref=args.trigger_ref,
            started_at=parse_datetime(args.started_at),
            initial_state=load_json_argument(args.initial_state, args.initial_state_file),
            portfolio_id=args.portfolio_id,
            watchlist_id=args.watchlist_id,
            recommendation_run_id=args.recommendation_run_id,
            asset_id=args.asset_id,
            asset_ids=parse_csv(args.asset_ids),
            source_asset_id=args.source_asset_id,
            candidate_asset_id=args.candidate_asset_id,
            horizon=args.horizon,
            timeframe=args.timeframe,
            recommendation_limit=args.recommendation_limit,
        )
    raise ValueError(f"未知 workflows 命令：{args.command}")


def dispatch_tools(interface: FinanceAgentInterface, args: argparse.Namespace):
    """处理工具命令。"""

    if args.command == "list":
        return interface.list_tools()
    if args.command == "call":
        return interface.call_tool(
            name=args.tool_name,
            arguments=load_json_argument(args.arguments, args.arguments_file),
        )
    raise ValueError(f"未知 tools 命令：{args.command}")


def dispatch_reports(interface: FinanceAgentInterface, args: argparse.Namespace):
    """处理报告命令。"""

    if args.command == "show":
        return interface.get_report(
            workflow_run_id=args.workflow_run_id,
            markdown=args.markdown,
        )
    raise ValueError(f"未知 reports 命令：{args.command}")


def load_json_argument(value: str, file_path: str | None) -> JsonDict:
    """从字符串或文件读取 JSON 参数。"""

    if file_path:
        value = Path(file_path).read_text(encoding="utf-8")
    parsed = json.loads(value or "{}")
    if not isinstance(parsed, dict):
        raise ValueError("JSON 参数必须是对象。")
    return parsed


def parse_csv(value: str | None) -> list[str]:
    """解析逗号分隔参数。"""

    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def print_json(data: JsonDict, *, stream: Any = sys.stdout) -> None:
    """输出 UTF-8 JSON。"""

    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), file=stream)


if __name__ == "__main__":
    raise SystemExit(main())
