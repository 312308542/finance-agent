"""finance-agent MCP Server。

MCP 是正式给 Hermes-Agent、Codex 或其他长期运行 Agent 使用的工具入口。
本模块保持薄封装：每个 MCP tool 都调用 `FinanceAgentInterface`，业务逻辑仍在
服务层和 Workflow 层。
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
from typing import Any

JsonDict = dict[str, Any]


def create_mcp_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    streamable_http_path: str = "/mcp",
) -> Any:
    """创建 MCP Server。

    需要安装 Python MCP SDK：`pip install "mcp[cli]"`。
    """

    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError(
            "缺少 MCP Python SDK。请先安装依赖："
            '.venv\\Scripts\\python.exe -m pip install "mcp[cli]"'
        ) from exc

    mcp = FastMCP(
        "finance-agent",
        host=host,
        port=port,
        streamable_http_path=streamable_http_path,
    )

    from starlette.requests import Request
    from starlette.responses import JSONResponse

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        """返回 MCP 服务存活状态，不触发数据库或外部数据源访问。"""

        return JSONResponse({"status": "ok", "service": "finance-agent-mcp"})

    @mcp.tool()
    def list_workflows() -> JsonDict:
        """列出可调度的金融团队 Workflow。"""

        return run_with_interface(lambda interface: interface.list_workflows().to_dict())

    @mcp.tool()
    def list_tools() -> JsonDict:
        """列出可读取已入库金融事实的工具。"""

        return run_with_interface(lambda interface: interface.list_tools().to_dict())

    @mcp.tool()
    def call_tool(name: str, arguments: JsonDict | None = None) -> JsonDict:
        """调用只读金融事实工具。"""

        return run_with_interface(
            lambda interface: interface.call_tool(name=name, arguments=arguments).to_dict()
        )

    @mcp.tool()
    def run_workflow(
        workflow_type: str,
        owner_id: str,
        workflow_run_id: str | None = None,
        trigger_type: str = "manual",
        trigger_ref: str | None = None,
        started_at: str | None = None,
        portfolio_id: str | None = None,
        watchlist_id: str | None = None,
        recommendation_run_id: str | None = None,
        asset_id: str | None = None,
        asset_ids: list[str] | None = None,
        source_asset_id: str | None = None,
        candidate_asset_id: str | None = None,
        horizon: str = "swing",
        timeframe: str = "1d",
        recommendation_limit: int = 20,
        initial_state: JsonDict | None = None,
    ) -> JsonDict:
        """运行一个金融团队 Workflow 并返回结构化结果。"""

        return run_with_interface(
            lambda interface: interface.run_workflow(
                workflow_type=workflow_type,
                owner_id=owner_id,
                workflow_run_id=workflow_run_id,
                trigger_type=trigger_type,
                trigger_ref=trigger_ref,
                started_at=_parse_datetime(started_at),
                initial_state=initial_state,
                portfolio_id=portfolio_id,
                watchlist_id=watchlist_id,
                recommendation_run_id=recommendation_run_id,
                asset_id=asset_id,
                asset_ids=asset_ids,
                source_asset_id=source_asset_id,
                candidate_asset_id=candidate_asset_id,
                horizon=horizon,
                timeframe=timeframe,
                recommendation_limit=recommendation_limit,
            ).to_dict()
        )

    @mcp.tool()
    def get_workflow_run(workflow_run_id: str) -> JsonDict:
        """读取 Workflow 运行记录和审计事件。"""

        return run_with_interface(
            lambda interface: interface.get_workflow_run(
                workflow_run_id=workflow_run_id
            ).to_dict()
        )

    @mcp.tool()
    def get_report(workflow_run_id: str, markdown: bool = False) -> JsonDict:
        """读取 Workflow 的中文解释报告。"""

        return run_with_interface(
            lambda interface: interface.get_report(
                workflow_run_id=workflow_run_id,
                markdown=markdown,
            ).to_dict()
        )

    @mcp.tool()
    def graph_health() -> JsonDict:
        """检查当前配置选择的图谱后端健康状态。"""

        return run_with_interface(lambda interface: interface.graph_health().to_dict())

    @mcp.tool()
    def graph_initialize() -> JsonDict:
        """初始化当前配置选择的图谱后端。"""

        return run_with_interface(lambda interface: interface.graph_initialize().to_dict())

    @mcp.tool()
    def graph_sync_asset(owner_id: str, asset_id: str, limit: int = 20) -> JsonDict:
        """同步单标的 Finance Memory 知识图谱投影。"""

        return run_with_interface(
            lambda interface: interface.graph_sync_asset(
                owner_id=owner_id,
                asset_id=asset_id,
                limit=limit,
            ).to_dict()
        )

    @mcp.tool()
    def graph_sync_owner(
        owner_id: str,
        asset_ids: list[str] | None = None,
        limit_assets: int = 100,
        limit_per_asset: int = 20,
    ) -> JsonDict:
        """同步某个用户的 Finance Memory 知识图谱投影。"""

        return run_with_interface(
            lambda interface: interface.graph_sync_owner(
                owner_id=owner_id,
                asset_ids=asset_ids,
                limit_assets=limit_assets,
                limit_per_asset=limit_per_asset,
            ).to_dict()
        )

    @mcp.tool()
    def graph_trace_asset(
        owner_id: str,
        asset_id: str,
        max_depth: int = 2,
        limit: int = 20,
    ) -> JsonDict:
        """追踪单标的决策、记忆、观察池、风险和证据路径。"""

        return run_with_interface(
            lambda interface: interface.graph_trace_asset(
                owner_id=owner_id,
                asset_id=asset_id,
                max_depth=max_depth,
                limit=limit,
            ).to_dict()
        )

    @mcp.tool()
    def graph_explain_candidate_reason_chain(
        owner_id: str,
        asset_id: str,
        limit: int = 5,
    ) -> JsonDict:
        """解释标的被纳入候选/观察池或持续关注的原因链。"""

        return run_with_interface(
            lambda interface: interface.graph_explain_candidate_reason_chain(
                owner_id=owner_id,
                asset_id=asset_id,
                limit=limit,
            ).to_dict()
        )

    @mcp.tool()
    def graph_find_similar_decision_paths(
        owner_id: str,
        asset_id: str,
        limit: int = 10,
    ) -> JsonDict:
        """查找与目标标的结构相似的历史决策路径。"""

        return run_with_interface(
            lambda interface: interface.graph_find_similar_decision_paths(
                owner_id=owner_id,
                asset_id=asset_id,
                limit=limit,
            ).to_dict()
        )

    @mcp.tool()
    def graph_detect_risk_contagion(
        owner_id: str,
        asset_id: str | None = None,
        max_depth: int = 3,
        limit: int = 20,
    ) -> JsonDict:
        """检测风险、证据、决策、记忆和资产之间的传导路径。"""

        return run_with_interface(
            lambda interface: interface.graph_detect_risk_contagion(
                owner_id=owner_id,
                asset_id=asset_id,
                max_depth=max_depth,
                limit=limit,
            ).to_dict()
        )

    @mcp.tool()
    def graph_find_memory_conflicts(
        owner_id: str,
        asset_id: str | None = None,
        limit: int = 10,
    ) -> JsonDict:
        """发现 Finance Memory 中看多、失效、卖出等冲突。"""

        return run_with_interface(
            lambda interface: interface.graph_find_memory_conflicts(
                owner_id=owner_id,
                asset_id=asset_id,
                limit=limit,
            ).to_dict()
        )

    @mcp.tool()
    def memory_recall_asset_context(
        owner_id: str,
        asset_id: str,
        query: str,
        memory_type: str | None = None,
        limit: int = 10,
    ) -> JsonDict:
        """按语义召回标的 Finance Memory，并返回资产时间线。"""

        return run_with_interface(
            lambda interface: interface.memory_recall_asset_context(
                owner_id=owner_id,
                asset_id=asset_id,
                query=query,
                memory_type=memory_type,
                limit=limit,
            ).to_dict()
        )

    @mcp.tool()
    def memory_get_asset_timeline(
        owner_id: str,
        asset_id: str,
        memory_type: str | None = None,
        limit: int = 20,
    ) -> JsonDict:
        """读取标的 Finance Memory 时间线。"""

        return run_with_interface(
            lambda interface: interface.memory_get_asset_timeline(
                owner_id=owner_id,
                asset_id=asset_id,
                memory_type=memory_type,
                limit=limit,
            ).to_dict()
        )

    @mcp.tool()
    def evaluate_triggers(
        owner_id: str,
        as_of: str | None = None,
        portfolio_id: str | None = None,
        watchlist_id: str | None = None,
        recommendation_run_id: str | None = None,
        horizon: str = "swing",
        timeframe: str = "1d",
        since_minutes: int = 60,
        cooldown_minutes: int = 15,
        recommendation_limit: int = 20,
        drawdown_threshold: str = "0.050000",
    ) -> JsonDict:
        """评估已入库事实并生成触发事件。"""

        return run_with_trigger_service(
            lambda service: service.evaluate(
                build_trigger_request(
                    owner_id=owner_id,
                    as_of=as_of,
                    portfolio_id=portfolio_id,
                    watchlist_id=watchlist_id,
                    recommendation_run_id=recommendation_run_id,
                    horizon=horizon,
                    timeframe=timeframe,
                    since_minutes=since_minutes,
                    cooldown_minutes=cooldown_minutes,
                    recommendation_limit=recommendation_limit,
                    drawdown_threshold=drawdown_threshold,
                )
            ).to_dict()
        )

    @mcp.tool()
    def dispatch_triggers(
        owner_id: str | None = None,
        limit: int = 20,
        as_of: str | None = None,
    ) -> JsonDict:
        """派发待处理触发事件到 Agent 唤醒队列。"""

        return run_with_trigger_service(
            lambda service: _dispatch_with_hermes_publisher(
                service,
                owner_id=owner_id,
                limit=limit,
                as_of=_parse_datetime(as_of),
            )
        )

    @mcp.tool()
    def run_triggers_once(
        owner_id: str,
        as_of: str | None = None,
        portfolio_id: str | None = None,
        watchlist_id: str | None = None,
        recommendation_run_id: str | None = None,
        horizon: str = "swing",
        timeframe: str = "1d",
        since_minutes: int = 60,
        cooldown_minutes: int = 15,
        recommendation_limit: int = 20,
        drawdown_threshold: str = "0.050000",
    ) -> JsonDict:
        """执行一次触发评估并立即唤醒 Agent。"""

        return run_with_trigger_service(
            lambda service: service.run_once(
                build_trigger_request(
                    owner_id=owner_id,
                    as_of=as_of,
                    portfolio_id=portfolio_id,
                    watchlist_id=watchlist_id,
                    recommendation_run_id=recommendation_run_id,
                    horizon=horizon,
                    timeframe=timeframe,
                    since_minutes=since_minutes,
                    cooldown_minutes=cooldown_minutes,
                    recommendation_limit=recommendation_limit,
                    drawdown_threshold=drawdown_threshold,
                ),
                publisher=_hermes_publisher_callback(),
            )
        )

    # ------------------------------------------------------------------
    # 停跑恢复补跑（DataRecoveryModule 门面；独立会话，不经过 Interface）
    # ------------------------------------------------------------------

    def _run_recovery(callback):
        from finance_agent.storage.db import create_session_factory, session_scope

        factory = create_session_factory()
        with session_scope(factory) as session:
            from finance_agent.data_recovery.assembly import (
                build_default_recovery_module,
            )

            return callback(build_default_recovery_module(session))

    @mcp.tool()
    def data_recovery_preview(requested_by: str | None = None) -> JsonDict:
        """A 股停跑恢复：只读扫描缺口并生成或复用补跑计划草稿。"""

        return _run_recovery(lambda module: module.preview(requested_by=requested_by))

    @mcp.tool()
    def data_recovery_list_runs(limit: int = 20) -> JsonDict:
        """A 股停跑恢复：列出补跑批次（最新在前）。"""

        return _run_recovery(lambda module: {"runs": module.list_runs(limit=limit)})

    @mcp.tool()
    def data_recovery_run_status(run_id: str) -> JsonDict:
        """A 股停跑恢复：查看批次稳定状态、步骤进度与例外清单。"""

        return _run_recovery(lambda module: module.get(run_id).to_dict())

    @mcp.tool()
    def data_recovery_approve(
        run_id: str,
        plan_hash: str,
        approved_by: str | None = None,
    ) -> JsonDict:
        """A 股停跑恢复：用户确认执行补跑；plan_hash 用于过期检测。"""

        return _run_recovery(
            lambda module: module.approve(
                run_id=run_id,
                plan_hash=plan_hash,
                approved_by=approved_by,
            ).to_dict()
        )

    @mcp.tool()
    def data_recovery_control(run_id: str, action: str, actor: str | None = None) -> JsonDict:
        """A 股停跑恢复：pause / resume / cancel 控制补跑批次。"""

        return _run_recovery(
            lambda module: module.control(run_id, action, actor=actor).to_dict()
        )

    return mcp


def run_with_interface(callback: Callable[[Any], JsonDict]) -> JsonDict:
    """创建事务边界并执行 MCP 工具回调。"""

    from finance_agent.agents.interfaces import FinanceAgentInterface
    from finance_agent.storage.db import create_session_factory, session_scope

    session_factory = create_session_factory()
    with session_scope(session_factory) as session:
        interface = FinanceAgentInterface(session)
        return callback(interface)


def run_with_trigger_service(callback: Callable[[Any], JsonDict]) -> JsonDict:
    """创建事务边界并执行触发事件服务回调。"""

    from finance_agent.storage.db import create_session_factory, session_scope
    from finance_agent.triggers import TriggerService

    session_factory = create_session_factory()
    with session_scope(session_factory) as session:
        return callback(TriggerService(session))


def _hermes_publisher_callback() -> Callable[[Any], None] | None:
    """返回环境配置的 Hermes 发布回调。"""

    from finance_agent.triggers.webhook import HermesWebhookPublisher

    publisher = HermesWebhookPublisher.from_environment()
    return publisher.publish if publisher else None


def _dispatch_with_hermes_publisher(
    service: Any,
    *,
    owner_id: str | None,
    limit: int,
    as_of: Any,
) -> JsonDict:
    """使用 Hermes 发布器派发事件，避免 MCP 调用绕过 HMAC 门控。"""

    return service.dispatch_pending(
        owner_id=owner_id,
        limit=limit,
        as_of=as_of,
        publisher=_hermes_publisher_callback(),
    ).to_dict()


def build_trigger_request(
    *,
    owner_id: str,
    as_of: str | None,
    portfolio_id: str | None,
    watchlist_id: str | None,
    recommendation_run_id: str | None,
    horizon: str,
    timeframe: str,
    since_minutes: int,
    cooldown_minutes: int,
    recommendation_limit: int,
    drawdown_threshold: str,
) -> Any:
    """构建 MCP 触发评估请求。"""

    from datetime import UTC, datetime
    from decimal import Decimal

    from finance_agent.agents.interfaces import parse_datetime
    from finance_agent.triggers import TriggerEvaluationRequest

    return TriggerEvaluationRequest(
        owner_id=owner_id,
        as_of=parse_datetime(as_of) or datetime.now(UTC),
        portfolio_id=portfolio_id,
        watchlist_id=watchlist_id,
        recommendation_run_id=recommendation_run_id,
        horizon=horizon,
        timeframe=timeframe,
        since_minutes=since_minutes,
        cooldown_minutes=cooldown_minutes,
        recommendation_limit=recommendation_limit,
        drawdown_threshold=Decimal(drawdown_threshold),
    )


def _parse_datetime(value: str | None) -> Any:
    """按需加载时间解析器，避免 MCP 启动阶段导入完整 Agent。"""

    from finance_agent.agents.interfaces import parse_datetime

    return parse_datetime(value)


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    """解析 MCP stdio 和 Streamable HTTP 启动参数。"""

    parser = argparse.ArgumentParser(description="finance-agent MCP Server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http", "sse"),
        default="stdio",
        help="MCP 传输协议，默认使用 stdio 兼容本地调用",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP 监听地址")
    parser.add_argument("--port", type=int, default=8000, help="HTTP 监听端口")
    parser.add_argument(
        "--streamable-http-path",
        default="/mcp",
        help="Streamable HTTP MCP 路径",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> None:
    """启动 MCP Server。"""

    args = parse_args(argv)
    create_mcp_server(
        host=args.host,
        port=args.port,
        streamable_http_path=args.streamable_http_path,
    ).run(transport=args.transport)


if __name__ == "__main__":
    main()
