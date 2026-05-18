"""验证内部金融 Agent Loop 的 LangGraph 图入口和轮询入口。

这条冒烟用例覆盖 O2/O3 的衔接边界：
- 触发层已经派发的事件可以被内部 Agent Loop 图消费；
- 图内按“加载任务 -> 规划 -> 调用 Workflow -> 持久化结果”流转；
- CLI `agent run-loop` 可以按最大迭代次数运行一轮后退出；
- 重复运行不会重复处理已完成的 Agent 任务。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime

from sqlalchemy import func, select

from finance_agent.agents.loop import AgentLoopLimits, build_internal_agent_loop_graph
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.orm import AgentWorkflowRunORM, AssistantTriggerEventORM
from finance_agent.storage.repositories import AssistantTriggerRepository


def main() -> None:
    """执行内部 Agent Loop 图化和轮询冒烟验证。"""

    session_factory = create_session_factory()
    as_of = datetime.now(UTC).replace(microsecond=0)
    stamp = as_of.strftime("%Y%m%d%H%M%S")
    owner_id = f"owner:smoke:internal_loop_graph:{stamp}"
    asset_id = f"asset:smoke:internal_loop_graph:{stamp}:candidate"

    graph_agent_task_id = f"agent_task:{stamp}:graph"
    loop_agent_task_id = f"agent_task:{stamp}:loop"
    graph_trigger_event_id = f"trigger:smoke:internal_loop_graph:graph:{stamp}"
    loop_trigger_event_id = f"trigger:smoke:internal_loop_graph:loop:{stamp}"

    with session_scope(session_factory) as session:
        triggers = AssistantTriggerRepository(session)
        for trigger_event_id, agent_task_id in (
            (graph_trigger_event_id, graph_agent_task_id),
            (loop_trigger_event_id, loop_agent_task_id),
        ):
            triggers.upsert_trigger_event(
                trigger_event_id=trigger_event_id,
                owner_id=owner_id,
                trigger_type="watchlist_condition_hit",
                trigger_ref=asset_id,
                dedup_key=f"{owner_id}:{trigger_event_id}",
                severity="medium",
                status="dispatched",
                agent_runtime="internal_agent_loop",
                agent_task_id=agent_task_id,
                requested_workflow_type="asset_deep_analysis",
                triggered_at=as_of,
                dispatched_at=as_of,
                asset_id=asset_id,
                payload={
                    "dispatch_status": "agent_wakeup_queued",
                    "agent_runtime": "internal_agent_loop",
                    "agent_task_id": agent_task_id,
                    "requested_workflow_type": "asset_deep_analysis",
                    "reason": "内部 Agent Loop 图化冒烟：观察池启动条件满足。",
                },
            )

        graph = build_internal_agent_loop_graph()
        graph_event = session.get_one(AssistantTriggerEventORM, graph_trigger_event_id)
        final_state = graph.invoke(
            {
                "event": graph_event,
                "session": session,
                "as_of": as_of,
                "limits": AgentLoopLimits(),
            }
        )
        task_result = final_state["task_result"].to_dict()
        if task_result["status"] != "workflow_completed":
            raise AssertionError(f"图入口应完成 Workflow，实际={task_result}")
        if final_state.get("node_trace") != [
            "load_task",
            "plan",
            "execute_workflow",
            "persist_result",
        ]:
            raise AssertionError(f"图流转节点不符合预期：{final_state.get('node_trace')}")

        graph_event = session.get_one(AssistantTriggerEventORM, graph_trigger_event_id)
        if graph_event.payload.get("agent_loop_status") != "workflow_completed":
            raise AssertionError("图入口处理后必须回写 agent_loop_status。")
        if graph_event.payload.get("agent_node_trace") != final_state.get("node_trace"):
            raise AssertionError("图入口处理后必须记录 Agent Loop 节点轨迹。")

    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "finance_agent.cli",
            "agent",
            "run-loop",
            "--owner-id",
            owner_id,
            "--limit",
            "5",
            "--interval-seconds",
            "0",
            "--max-iterations",
            "1",
            "--as-of",
            as_of.isoformat(),
        ],
        check=True,
        capture_output=True,
        encoding="utf-8",
        text=True,
    )
    cli_payload = json.loads(process.stdout)
    if cli_payload["status"] != "ok":
        raise AssertionError("内部 Agent Loop 轮询 CLI 必须返回 ok。")
    if cli_payload["data"]["iterations"] != 1:
        raise AssertionError("run-loop 使用 max-iterations=1 时必须只运行一轮。")
    if cli_payload["data"]["processed_count"] != 1:
        raise AssertionError(f"run-loop 应处理剩余 1 个事件，实际={cli_payload}")

    repeat_process = subprocess.run(
        [
            sys.executable,
            "-m",
            "finance_agent.cli",
            "agent",
            "run-loop",
            "--owner-id",
            owner_id,
            "--limit",
            "5",
            "--interval-seconds",
            "0",
            "--max-iterations",
            "1",
            "--as-of",
            as_of.isoformat(),
        ],
        check=True,
        capture_output=True,
        encoding="utf-8",
        text=True,
    )
    repeat_payload = json.loads(repeat_process.stdout)
    if repeat_payload["data"]["processed_count"] != 0:
        raise AssertionError("run-loop 重复运行不应重复处理已完成 Agent 任务。")

    with session_scope(session_factory) as session:
        workflow_run_count = session.scalar(
            select(func.count())
            .select_from(AgentWorkflowRunORM)
            .where(AgentWorkflowRunORM.owner_id == owner_id)
        )
        if workflow_run_count != 2:
            raise AssertionError(
                f"两个 Agent 任务应各创建 1 条 Workflow run，实际={workflow_run_count}"
            )
        loop_event = session.get_one(AssistantTriggerEventORM, loop_trigger_event_id)
        if loop_event.payload.get("agent_loop_status") != "workflow_completed":
            raise AssertionError("run-loop 处理的事件也必须回写完成状态。")

    print(
        {
            "owner_id": owner_id,
            "graph_workflow_run_id": task_result["workflow_run_id"],
            "cli_processed_count": cli_payload["data"]["processed_count"],
            "repeat_processed_count": repeat_payload["data"]["processed_count"],
            "workflow_run_count": workflow_run_count,
        }
    )


if __name__ == "__main__":
    main()
