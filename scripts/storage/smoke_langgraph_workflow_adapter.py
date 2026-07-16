"""验证 LangGraph Workflow 审计适配层。

这里不验证 LangGraph 图本身的复杂行为，只验证本项目的适配层可以把节点摘要
写入既有 `agent_workflow_runs` 和 `agent_workflow_events` 审计表。
"""

from __future__ import annotations

from datetime import datetime

from finance_agent.agents.runtime import (
    CONTEXT_ENVELOPE_VERSION,
    LangGraphWorkflowAdapter,
    WorkflowNodeEvent,
    build_workflow_context_envelope,
)
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.orm import AgentWorkflowRunORM


def main() -> None:
    """执行 LangGraph 审计适配层冒烟。"""

    session_factory = create_session_factory()
    with session_scope(session_factory) as session:
        adapter = LangGraphWorkflowAdapter(session)
        started_at = datetime(2026, 5, 18, 9, 30, 0).astimezone()
        context_envelope = build_workflow_context_envelope(
            workflow_type="langgraph_adapter_smoke",
            market_type="ashare",
            asset_ids=["asset:smoke"],
            asset_contexts={
                "asset:smoke": {
                    "profile": {
                        "asset_id": "asset:smoke",
                        "symbol": "SMOKE",
                        "market": "ashare",
                    },
                    "signal_risk": {
                        "risks": [
                            {
                                "risk_id": "risk:smoke",
                                "severity": "medium",
                                "title": "冒烟风险",
                            }
                        ]
                    },
                }
            },
        ).to_dict()
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
            final_state={
                "asset_id": "asset:smoke",
                "loaded": True,
                "decision": "watch",
                "context_envelope": context_envelope,
            },
        )
        if result["loaded"] is not True:
            raise AssertionError("final_state.loaded 应为 True")
        if result["decision"] != "watch":
            raise AssertionError("final_state.decision 应为 watch")

        events = adapter.list_events("workflow:smoke:langgraph_adapter:202605180930")
        if len(events) != 2:
            raise AssertionError(f"应写入 2 条节点事件，实际 {len(events)}")
        workflow_run = session.get(
            AgentWorkflowRunORM,
            "workflow:smoke:langgraph_adapter:202605180930",
        )
        if workflow_run is None:
            raise AssertionError("应写入 Workflow 运行审计。")
        payload = workflow_run.payload
        persisted_envelope = payload.get("context_envelope") or {}
        if persisted_envelope.get("version") != CONTEXT_ENVELOPE_VERSION:
            raise AssertionError("Workflow 审计必须保存 context_envelope。")
        if persisted_envelope.get("market_type") != "ashare":
            raise AssertionError("Workflow 审计必须保存市场链路。")
        summary = payload.get("context_envelope_summary") or {}
        if summary.get("role_view_count") != 5:
            raise AssertionError("Workflow 审计必须保存角色视图摘要。")
        print(
            {
                "workflow_run_id": "workflow:smoke:langgraph_adapter:202605180930",
                "event_count": len(events),
                "context_envelope_version": persisted_envelope["version"],
                "decision": result["decision"],
            }
        )


if __name__ == "__main__":
    main()
