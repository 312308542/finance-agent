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
from finance_agent.triggers import TriggerEvaluationRequest, TriggerService

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

    triggers = subparsers.add_parser("triggers", help="V1.2 触发事件评估与 Agent 唤醒。")
    trigger_commands = triggers.add_subparsers(dest="command", required=True)
    evaluate = trigger_commands.add_parser("evaluate", help="评估已入库事实并生成触发事件。")
    add_trigger_request_arguments(evaluate)
    dispatch_parser = trigger_commands.add_parser(
        "dispatch",
        help="把待处理触发事件派发到 Agent 唤醒队列。",
    )
    dispatch_parser.add_argument("--owner-id", default=None, help="只派发指定用户的触发事件。")
    dispatch_parser.add_argument("--limit", type=int, default=20, help="本次最多派发事件数。")
    dispatch_parser.add_argument("--as-of", default=None, help="ISO 时间，默认当前时间。")
    run_once = trigger_commands.add_parser("run-once", help="执行一次触发评估并立即唤醒 Agent。")
    add_trigger_request_arguments(run_once)
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


def add_trigger_request_arguments(parser: argparse.ArgumentParser) -> None:
    """注册触发评估请求参数。"""

    parser.add_argument("--owner-id", required=True, help="用户/账户 ID。")
    parser.add_argument("--as-of", default=None, help="ISO 时间，默认当前时间。")
    parser.add_argument("--portfolio-id", default=None, help="只评估指定组合。")
    parser.add_argument("--watchlist-id", default=None, help="只评估指定观察池。")
    parser.add_argument("--recommendation-run-id", default=None, help="只评估指定推荐运行。")
    parser.add_argument("--horizon", default="swing", help="信号和因子分析周期。")
    parser.add_argument("--timeframe", default="1d", help="TA 指标周期。")
    parser.add_argument("--since-minutes", type=int, default=60, help="触发回看窗口分钟数。")
    parser.add_argument("--cooldown-minutes", type=int, default=15, help="同类触发冷却分钟数。")
    parser.add_argument("--recommendation-limit", type=int, default=20, help="推荐运行读取数量。")
    parser.add_argument(
        "--drawdown-threshold",
        default="0.050000",
        help="持仓回撤触发阈值，Decimal 字符串。",
    )


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
        if args.group == "triggers":
            return dispatch_triggers(session, args)
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


def dispatch_triggers(session: Any, args: argparse.Namespace) -> JsonDict:
    """处理触发事件命令。"""

    service = TriggerService(session)
    if args.command == "evaluate":
        return {
            "status": "ok",
            "data": service.evaluate(build_trigger_request(args)).to_dict(),
        }
    if args.command == "dispatch":
        result = service.dispatch_pending(
            owner_id=args.owner_id,
            limit=args.limit,
            as_of=parse_datetime(args.as_of),
        )
        return {"status": "ok", "data": result.to_dict()}
    if args.command == "run-once":
        return {
            "status": "ok",
            "data": service.run_once(build_trigger_request(args)),
        }
    raise ValueError(f"未知 triggers 命令：{args.command}")


def build_trigger_request(args: argparse.Namespace) -> TriggerEvaluationRequest:
    """从 CLI 参数构建触发评估请求。"""

    from datetime import UTC, datetime
    from decimal import Decimal

    return TriggerEvaluationRequest(
        owner_id=args.owner_id,
        as_of=parse_datetime(args.as_of) or datetime.now(UTC),
        portfolio_id=args.portfolio_id,
        watchlist_id=args.watchlist_id,
        recommendation_run_id=args.recommendation_run_id,
        horizon=args.horizon,
        timeframe=args.timeframe,
        since_minutes=args.since_minutes,
        cooldown_minutes=args.cooldown_minutes,
        recommendation_limit=args.recommendation_limit,
        drawdown_threshold=Decimal(args.drawdown_threshold),
    )


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
