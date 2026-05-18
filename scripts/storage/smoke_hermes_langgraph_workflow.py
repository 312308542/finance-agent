"""验证 Hermes 调用 finance-agent 的地基入口。

Hermes 自己负责长期 loop；本脚本只验证它未来可调用的本项目入口已经具备：
`FinanceAssistantService`、金融事实工具运行时和 LangGraph Workflow 审计适配层。
"""

from __future__ import annotations

from datetime import datetime

from finance_agent.agents import FinanceAssistantService
from finance_agent.agents.runtime import LangGraphWorkflowAdapter, WorkflowNodeEvent
from finance_agent.agents.tools import FinanceToolRuntime
from finance_agent.storage.db import create_session_factory, session_scope


def main() -> None:
    """执行 Hermes 调用地基冒烟。"""

    session_factory = create_session_factory()
    with session_scope(session_factory) as session:
        assistant = FinanceAssistantService(session)
        tools = FinanceToolRuntime(session)
        adapter = LangGraphWorkflowAdapter(session)

        tool_names = set(tools.list_tools())
        if "workflow.list_workflows" not in tool_names:
            raise AssertionError("Hermes 入口必须能发现 workflow.list_workflows")
        workflows = tools.call("workflow.list_workflows")["workflows"]
        if not workflows:
            raise AssertionError("Hermes 入口必须能发现至少一个 Workflow")

        started_at = datetime(2026, 5, 18, 10, 0, 0).astimezone()
        final_state = adapter.record_completed_graph(
            workflow_run_id="workflow:smoke:hermes_langgraph:202605181000",
            owner_id="owner:smoke",
            workflow_type="hermes_langgraph_smoke",
            trigger_type="hermes_tool",
            trigger_ref="hermes:smoke",
            started_at=started_at,
            node_events=(
                WorkflowNodeEvent("hermes_tool_entry", {"tool_count": len(tool_names)}),
                WorkflowNodeEvent("workflow_discovery", {"workflow_count": len(workflows)}),
            ),
            initial_state={"source": "hermes"},
            final_state={
                "source": "hermes",
                "assistant": assistant.__class__.__name__,
                "workflow_count": len(workflows),
            },
        )
        if final_state["assistant"] != "FinanceAssistantService":
            raise AssertionError("Hermes 入口应依赖 FinanceAssistantService")

        print(
            {
                "assistant": final_state["assistant"],
                "tool_count": len(tool_names),
                "workflow_count": final_state["workflow_count"],
                "hermes_entry": "ready",
            }
        )


if __name__ == "__main__":
    main()
