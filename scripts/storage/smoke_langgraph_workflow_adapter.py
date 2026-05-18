"""验证 LangGraph Workflow 审计适配层。

这里不验证 LangGraph 图本身的复杂行为，只验证本项目的适配层可以把节点摘要
写入既有 `agent_workflow_runs` 和 `agent_workflow_events` 审计表。
"""

from __future__ import annotations

from datetime import datetime

from finance_agent.agents.runtime import LangGraphWorkflowAdapter, WorkflowNodeEvent
from finance_agent.storage.db import create_session_factory, session_scope


def main() -> None:
    """执行 LangGraph 审计适配层冒烟。"""

    session_factory = create_session_factory()
    with session_scope(session_factory) as session:
        adapter = LangGraphWorkflowAdapter(session)
        started_at = datetime(2026, 5, 18, 9, 30, 0).astimezone()
        result = adapter.record_completed_graph(
            workflow_run_id="workflow:smoke:langgraph_adapter:202605180930",
            owner_id="owner:smoke",
            workflow_type="langgraph_adapter_smoke",
            trigger_type="manual",
            started_at=started_at,
            node_events=(
                WorkflowNodeEvent("load_context", {"loaded": True}),
                WorkflowNodeEvent("decision_synthesis", {"decision": "watch"}),
            ),
            initial_state={"asset_id": "asset:smoke"},
            final_state={"asset_id": "asset:smoke", "loaded": True, "decision": "watch"},
        )
        if result["loaded"] is not True:
            raise AssertionError("final_state.loaded 应为 True")
        if result["decision"] != "watch":
            raise AssertionError("final_state.decision 应为 watch")

        events = adapter.list_events("workflow:smoke:langgraph_adapter:202605180930")
        if len(events) != 2:
            raise AssertionError(f"应写入 2 条节点事件，实际 {len(events)}")
        print(
            {
                "workflow_run_id": "workflow:smoke:langgraph_adapter:202605180930",
                "event_count": len(events),
                "decision": result["decision"],
            }
        )


if __name__ == "__main__":
    main()
