"""验证金融助手内部 Workflow 注册表和 6 个 LangGraph 构建器。

本脚本不接 Hermes，只验证 finance-agent 自己已经具备多 Workflow 发现能力。
"""

from __future__ import annotations

from finance_agent.agents import FinanceAssistantService
from finance_agent.agents.tools import FinanceToolRuntime
from finance_agent.agents.workflows import (
    LangGraphWorkflowUnavailable,
    list_langgraph_workflow_builders,
)
from finance_agent.storage.db import create_session_factory, session_scope


def main() -> None:
    """执行 Workflow 注册与构建器冒烟。"""

    required = {
        "portfolio_monitoring",
        "watchlist_management",
        "recommendation_decision",
        "asset_deep_analysis",
        "swap_decision",
        "daily_review",
    }
    session_factory = create_session_factory()
    with session_scope(session_factory) as session:
        assistant = FinanceAssistantService(session)
        tool_runtime = FinanceToolRuntime(session)

        service_types = {item["workflow_type"] for item in assistant.list_workflows()["workflows"]}
        tool_types = {
            item["workflow_type"]
            for item in tool_runtime.call("workflow.list_workflows")["workflows"]
        }
        builder_types = {builder.workflow_type for builder in list_langgraph_workflow_builders()}

        missing = {
            "service": sorted(required - service_types),
            "tool": sorted(required - tool_types),
            "builder": sorted(required - builder_types),
        }
        if any(missing.values()):
            raise AssertionError(f"Workflow 注册不完整: {missing}")

        built: list[str] = []
        unavailable: list[str] = []
        for builder in list_langgraph_workflow_builders():
            try:
                builder.build()
                built.append(builder.workflow_type)
            except LangGraphWorkflowUnavailable:
                unavailable.append(builder.workflow_type)
        if unavailable:
            raise AssertionError(f"LangGraph 依赖不可用: {unavailable}")

        print(
            {
                "service_count": len(service_types),
                "tool_count": len(tool_types),
                "builder_count": len(builder_types),
                "built": sorted(built),
            }
        )


if __name__ == "__main__":
    main()
