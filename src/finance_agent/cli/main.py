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

from finance_agent.agents.chat import FinanceAgentChatSession
from finance_agent.agents.interfaces import FinanceAgentInterface, parse_datetime
from finance_agent.agents.loop import InternalFinanceAgentLoopRunner
from finance_agent.agents.runtime import (
    load_model_registry,
    preview_model_routes,
    test_model_endpoint,
)
from finance_agent.agents.runtime.model_tui import render_model_tui
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.repositories import ChatMemoryRepository
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

    chat = subparsers.add_parser("chat", help="打开类似 Hermes-Agent 的 CLI 聊天窗口。")
    chat.add_argument("--owner-id", required=True, help="用户/账户 ID。")
    chat.add_argument("--config-file", default=None, help="模型配置 JSON 文件路径。")
    chat.add_argument("--session-id", default=None, help="恢复指定聊天会话。")
    chat.add_argument(
        "--new-session",
        action="store_true",
        help="忽略最近会话，强制创建新的聊天会话。",
    )
    chat.add_argument(
        "--history-limit",
        type=int,
        default=20,
        help="恢复聊天历史时最多读取的消息条数。",
    )
    chat.add_argument(
        "--message",
        action="append",
        default=[],
        help="脚本化输入消息；传多次可模拟多轮对话。",
    )

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

    graph = subparsers.add_parser("graph", help="Finance Memory 知识图谱。")
    graph_commands = graph.add_subparsers(dest="command", required=True)
    graph_commands.add_parser("health", help="检查图谱后端健康状态。")
    graph_commands.add_parser("init", help="初始化图谱后端约束、索引或图空间。")
    sync_asset = graph_commands.add_parser("sync-asset", help="同步单标的图谱投影。")
    sync_asset.add_argument("--owner-id", required=True, help="用户/账户 ID。")
    sync_asset.add_argument("--asset-id", required=True, help="资产 ID。")
    sync_asset.add_argument("--limit", type=int, default=20, help="单类事实读取数量。")
    sync_owner = graph_commands.add_parser("sync-owner", help="同步某个用户的图谱投影。")
    sync_owner.add_argument("--owner-id", required=True, help="用户/账户 ID。")
    sync_owner.add_argument("--asset-ids", default=None, help="逗号分隔的资产 ID。")
    sync_owner.add_argument("--limit-assets", type=int, default=100, help="最多同步资产数。")
    sync_owner.add_argument("--limit-per-asset", type=int, default=20, help="每个资产读取数量。")
    sync_all = graph_commands.add_parser("sync-all", help="同步全部或指定用户图谱投影。")
    sync_all.add_argument("--owner-id", default=None, help="可选：只同步指定用户。")
    sync_all.add_argument("--limit-assets", type=int, default=200, help="最多同步资产数。")
    sync_all.add_argument("--limit-per-asset", type=int, default=20, help="每个资产读取数量。")
    trace = graph_commands.add_parser("trace", help="追踪单标的图谱路径。")
    trace.add_argument("--owner-id", required=True, help="用户/账户 ID。")
    trace.add_argument("--asset-id", required=True, help="资产 ID。")
    trace.add_argument("--max-depth", type=int, default=2, help="最大路径深度。")
    trace.add_argument("--limit", type=int, default=20, help="返回上限。")
    reason_chain = graph_commands.add_parser("reason-chain", help="解释入池和持续关注原因链。")
    reason_chain.add_argument("--owner-id", required=True, help="用户/账户 ID。")
    reason_chain.add_argument("--asset-id", required=True, help="资产 ID。")
    reason_chain.add_argument("--limit", type=int, default=5, help="返回上限。")
    similar = graph_commands.add_parser("similar-decisions", help="查找相似历史决策路径。")
    similar.add_argument("--owner-id", required=True, help="用户/账户 ID。")
    similar.add_argument("--asset-id", required=True, help="资产 ID。")
    similar.add_argument("--limit", type=int, default=10, help="返回上限。")
    risk_contagion = graph_commands.add_parser("risk-contagion", help="检测风险传导路径。")
    risk_contagion.add_argument("--owner-id", required=True, help="用户/账户 ID。")
    risk_contagion.add_argument("--asset-id", default=None, help="可选：限制到单资产。")
    risk_contagion.add_argument("--max-depth", type=int, default=3, help="最大路径深度。")
    risk_contagion.add_argument("--limit", type=int, default=20, help="返回上限。")
    conflicts = graph_commands.add_parser("conflicts", help="发现 Finance Memory 冲突。")
    conflicts.add_argument("--owner-id", required=True, help="用户/账户 ID。")
    conflicts.add_argument("--asset-id", default=None, help="可选：限制到单资产。")
    conflicts.add_argument("--limit", type=int, default=10, help="返回上限。")

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

    agent = subparsers.add_parser("agent", help="内部金融 Agent Loop。")
    agent_commands = agent.add_subparsers(dest="command", required=True)
    agent_run_once = agent_commands.add_parser(
        "run-once",
        help="消费已派发的 Agent 唤醒事件，并按需调用底层金融团队 Workflow。",
    )
    agent_run_once.add_argument("--owner-id", default=None, help="只处理指定用户的 Agent 任务。")
    agent_run_once.add_argument("--limit", type=int, default=20, help="本次最多处理任务数。")
    agent_run_once.add_argument("--as-of", default=None, help="ISO 时间，默认当前时间。")
    agent_run_task = agent_commands.add_parser(
        "run-task",
        help="按 Agent 任务 ID 处理单个唤醒事件。",
    )
    agent_run_task.add_argument("agent_task_id", help="触发层派发出的 Agent 任务 ID。")
    agent_run_task.add_argument("--as-of", default=None, help="ISO 时间，默认当前时间。")
    agent_run_loop = agent_commands.add_parser(
        "run-loop",
        help="持续轮询内部 Agent 唤醒队列；可用 max-iterations 限制本地测试轮数。",
    )
    agent_run_loop.add_argument("--owner-id", default=None, help="只处理指定用户的 Agent 任务。")
    agent_run_loop.add_argument("--limit", type=int, default=20, help="每轮最多处理任务数。")
    agent_run_loop.add_argument(
        "--interval-seconds",
        type=float,
        default=5.0,
        help="空闲轮询间隔秒数。",
    )
    agent_run_loop.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="最多轮询次数；本地测试建议传 1，常驻运行可不传。",
    )
    agent_run_loop.add_argument("--as-of", default=None, help="ISO 时间，默认每轮当前时间。")

    models = subparsers.add_parser("models", help="模型配置、路由预览和本地测试。")
    model_commands = models.add_subparsers(dest="command", required=True)
    model_config = model_commands.add_parser("config", help="查看模型配置脱敏摘要。")
    add_model_common_arguments(model_config)
    route_preview = model_commands.add_parser("route-preview", help="预览 Workflow 模型路由。")
    add_model_common_arguments(route_preview)
    route_preview.add_argument("--workflow-type", required=True, help="Workflow 类型。")
    route_preview.add_argument("--task", default="roundtable_discussion", help="模型任务名。")
    route_preview.add_argument("--asset-id", default=None, help="资产 ID。")
    route_preview.add_argument("--decision-type", default=None, help="决策类型。")
    route_preview.add_argument("--high-risk", action="store_true", help="同时预览高风险复核路由。")
    model_test = model_commands.add_parser("test", help="测试模型配置，默认 dry-run。")
    add_model_common_arguments(model_test)
    model_test.add_argument("--model-key", required=True, help="模型 key。")
    model_test.add_argument(
        "--prompt",
        default="用一句中文说明模型配置已就绪。",
        help="测试提示词。",
    )
    model_test.add_argument("--dry-run", action="store_true", help="只生成请求预览，不发起 HTTP。")
    model_test.add_argument(
        "--real-request",
        action="store_true",
        help="发起真实 HTTP 连通测试；仅在确认 API 配置后使用。",
    )
    model_tui = model_commands.add_parser("tui", help="打开轻量文本模型配置 TUI。")
    add_model_common_arguments(model_tui)
    model_tui.add_argument(
        "--scripted",
        choices=["config", "route-preview", "test"],
        default=None,
        help="脚本化 TUI 动作，用于本地 smoke。",
    )
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


def add_model_common_arguments(parser: argparse.ArgumentParser) -> None:
    """注册模型命令通用参数。"""

    parser.add_argument("--config-file", default=None, help="模型配置 JSON 文件路径。")


def dispatch(args: argparse.Namespace) -> JsonDict:
    """执行具体命令。"""

    if args.group == "models":
        return dispatch_models(args)

    session_factory = create_session_factory(args.database_url)
    with session_scope(session_factory) as session:
        interface = FinanceAgentInterface(session)
        if args.group == "chat":
            return dispatch_chat(interface, args)
        if args.group == "workflows":
            return dispatch_workflows(interface, args).to_dict()
        if args.group == "tools":
            return dispatch_tools(interface, args).to_dict()
        if args.group == "reports":
            return dispatch_reports(interface, args).to_dict()
        if args.group == "graph":
            return dispatch_graph(interface, args).to_dict()
        if args.group == "triggers":
            return dispatch_triggers(session, args)
        if args.group == "agent":
            return dispatch_agent(session, args)
    raise ValueError(f"未知命令组：{args.group}")


def dispatch_chat(interface: FinanceAgentInterface, args: argparse.Namespace) -> JsonDict:
    """处理 CLI 聊天窗口命令。"""

    chat_memory = ChatMemoryRepository(interface.session)
    chat_session_id = resolve_chat_session_id(chat_memory, args)
    session = FinanceAgentChatSession(
        owner_id=args.owner_id,
        interface=interface,
        model_registry=load_model_registry(args.config_file),
        chat_memory=chat_memory,
        chat_session_id=chat_session_id,
        history_limit=args.history_limit,
    )
    if args.message:
        return {"status": "ok", "data": session.run_scripted(args.message).to_dict()}

    print(f"finance-agent 聊天窗口已启动，会话：{session.chat_session_id}，输入 /exit 退出。")
    while True:
        try:
            content = input("你> ")
        except EOFError:
            content = "/exit"
        turn = session.handle_message(content)
        print(f"Agent> {turn.assistant_message.content}")
        if turn.assistant_message.intent == "exit":
            break
    return {"status": "ok", "data": session.run_scripted([]).to_dict()}


def resolve_chat_session_id(
    chat_memory: ChatMemoryRepository,
    args: argparse.Namespace,
) -> str | None:
    """解析聊天会话 ID；未指定时默认恢复最近会话。"""

    if args.session_id:
        existing = chat_memory.get_session(
            owner_id=args.owner_id,
            chat_session_id=args.session_id,
        )
        if existing is None:
            raise ValueError(f"找不到聊天会话：{args.session_id}")
        return args.session_id
    if args.new_session:
        return None
    latest = chat_memory.get_latest_session(owner_id=args.owner_id)
    if latest is not None:
        return latest.chat_session_id
    return None


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


def dispatch_graph(interface: FinanceAgentInterface, args: argparse.Namespace):
    """处理知识图谱命令。"""

    if args.command == "health":
        return interface.graph_health()
    if args.command == "init":
        return interface.graph_initialize()
    if args.command == "sync-asset":
        return interface.graph_sync_asset(
            owner_id=args.owner_id,
            asset_id=args.asset_id,
            limit=args.limit,
        )
    if args.command == "sync-owner":
        return interface.graph_sync_owner(
            owner_id=args.owner_id,
            asset_ids=parse_csv(args.asset_ids),
            limit_assets=args.limit_assets,
            limit_per_asset=args.limit_per_asset,
        )
    if args.command == "sync-all":
        return interface.graph_sync_all(
            owner_id=args.owner_id,
            limit_assets=args.limit_assets,
            limit_per_asset=args.limit_per_asset,
        )
    if args.command == "trace":
        return interface.graph_trace_asset(
            owner_id=args.owner_id,
            asset_id=args.asset_id,
            max_depth=args.max_depth,
            limit=args.limit,
        )
    if args.command == "reason-chain":
        return interface.graph_explain_candidate_reason_chain(
            owner_id=args.owner_id,
            asset_id=args.asset_id,
            limit=args.limit,
        )
    if args.command == "similar-decisions":
        return interface.graph_find_similar_decision_paths(
            owner_id=args.owner_id,
            asset_id=args.asset_id,
            limit=args.limit,
        )
    if args.command == "risk-contagion":
        return interface.graph_detect_risk_contagion(
            owner_id=args.owner_id,
            asset_id=args.asset_id,
            max_depth=args.max_depth,
            limit=args.limit,
        )
    if args.command == "conflicts":
        return interface.graph_find_memory_conflicts(
            owner_id=args.owner_id,
            asset_id=args.asset_id,
            limit=args.limit,
        )
    raise ValueError(f"未知 graph 命令：{args.command}")


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


def dispatch_agent(session: Any, args: argparse.Namespace) -> JsonDict:
    """处理内部金融 Agent Loop 命令。"""

    runner = InternalFinanceAgentLoopRunner(session)
    if args.command == "run-once":
        result = runner.run_once(
            owner_id=args.owner_id,
            limit=args.limit,
            as_of=parse_datetime(args.as_of),
        )
        return {"status": "ok", "data": result.to_dict()}
    if args.command == "run-task":
        result = runner.run_task(
            agent_task_id=args.agent_task_id,
            as_of=parse_datetime(args.as_of),
        )
        return {"status": "ok", "data": result.to_dict()}
    if args.command == "run-loop":
        result = runner.run_loop(
            owner_id=args.owner_id,
            limit=args.limit,
            interval_seconds=args.interval_seconds,
            max_iterations=args.max_iterations,
            as_of=parse_datetime(args.as_of),
        )
        return {"status": "ok", "data": result.to_dict()}
    raise ValueError(f"未知 agent 命令：{args.command}")


def dispatch_models(args: argparse.Namespace) -> JsonDict:
    """处理模型配置和测试命令。"""

    registry = load_model_registry(args.config_file)
    if args.command == "config":
        return {"status": "ok", "data": registry.to_safe_dict()}
    if args.command == "route-preview":
        return {
            "status": "ok",
            "data": {
                "routes": preview_model_routes(
                    registry=registry,
                    workflow_type=args.workflow_type,
                    task=args.task,
                    asset_id=args.asset_id,
                    decision_type=args.decision_type,
                    high_risk=args.high_risk,
                )
            },
        }
    if args.command == "test":
        dry_run = args.dry_run or not args.real_request
        return {
            "status": "ok",
            "data": test_model_endpoint(
                registry=registry,
                model_key=args.model_key,
                prompt=args.prompt,
                dry_run=dry_run,
            ),
        }
    if args.command == "tui":
        return {
            "status": "ok",
            "data": {
                "output": render_model_tui(
                    registry=registry,
                    scripted=args.scripted,
                )
            },
        }
    raise ValueError(f"未知 models 命令：{args.command}")


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
