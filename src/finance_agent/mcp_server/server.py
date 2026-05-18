"""finance-agent MCP Server。

MCP 是正式给 Hermes-Agent、Codex 或其他长期运行 Agent 使用的工具入口。
本模块保持薄封装：每个 MCP tool 都调用 `FinanceAgentInterface`，业务逻辑仍在
服务层和 Workflow 层。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from finance_agent.agents.interfaces import FinanceAgentInterface, parse_datetime
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.triggers import TriggerEvaluationRequest, TriggerService

JsonDict = dict[str, Any]


def create_mcp_server() -> Any:
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

    mcp = FastMCP("finance-agent")

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
                started_at=parse_datetime(started_at),
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
        """派发待处理触发事件到金融团队 Workflow。"""

        return run_with_trigger_service(
            lambda service: service.dispatch_pending(
                owner_id=owner_id,
                limit=limit,
                as_of=parse_datetime(as_of),
            ).to_dict()
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
        """执行一次触发评估并立即派发。"""

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
                )
            )
        )

    return mcp


def run_with_interface(callback: Callable[[FinanceAgentInterface], JsonDict]) -> JsonDict:
    """创建事务边界并执行 MCP 工具回调。"""

    session_factory = create_session_factory()
    with session_scope(session_factory) as session:
        interface = FinanceAgentInterface(session)
        return callback(interface)


def run_with_trigger_service(callback: Callable[[TriggerService], JsonDict]) -> JsonDict:
    """创建事务边界并执行触发事件服务回调。"""

    session_factory = create_session_factory()
    with session_scope(session_factory) as session:
        return callback(TriggerService(session))


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
) -> TriggerEvaluationRequest:
    """构建 MCP 触发评估请求。"""

    from datetime import UTC, datetime
    from decimal import Decimal

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


def main() -> None:
    """启动 MCP Server。"""

    create_mcp_server().run()


if __name__ == "__main__":
    main()
