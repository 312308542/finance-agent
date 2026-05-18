"""验证内部金融 Agent Loop 能消费触发层派发的 Agent 唤醒事件。"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime

from sqlalchemy import func, select

from finance_agent.agents.loop import InternalFinanceAgentLoopRunner
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.orm import AgentWorkflowRunORM, AssistantTriggerEventORM
from finance_agent.storage.repositories import AssistantTriggerRepository


def main() -> None:
    """执行内部 Agent Loop 冒烟验证。"""

    session_factory = create_session_factory()
    as_of = datetime.now(UTC).replace(microsecond=0)
    stamp = as_of.strftime("%Y%m%d%H%M%S")
    owner_id = f"owner:smoke:internal_loop:{stamp}"
    asset_id = f"asset:smoke:internal_loop:{stamp}:candidate"

    direct_agent_task_id = f"agent_task:{stamp}:direct"
    cli_agent_task_id = f"agent_task:{stamp}:cli"
    direct_trigger_event_id = f"trigger:smoke:internal_loop:direct:{stamp}"
    cli_trigger_event_id = f"trigger:smoke:internal_loop:cli:{stamp}"

    with session_scope(session_factory) as session:
        triggers = AssistantTriggerRepository(session)
        for trigger_event_id, agent_task_id in (
            (direct_trigger_event_id, direct_agent_task_id),
            (cli_trigger_event_id, cli_agent_task_id),
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
                    "reason": "内部 Agent Loop 冒烟：观察池启动条件满足。",
                },
            )

        runner = InternalFinanceAgentLoopRunner(session)
        direct_result = runner.run_task(agent_task_id=direct_agent_task_id, as_of=as_of).to_dict()
        if direct_result["processed_count"] != 1:
            raise AssertionError(f"直接运行应处理 1 个任务，实际={direct_result}")
        direct_run = direct_result["runs"][0]
        if direct_run["workflow_type"] != "asset_deep_analysis":
            raise AssertionError("内部 Agent Loop 应调用触发事件建议的 Workflow。")

        direct_event = session.get_one(AssistantTriggerEventORM, direct_trigger_event_id)
        if direct_event.payload.get("agent_loop_status") != "workflow_completed":
            raise AssertionError("处理完成后必须回写 agent_loop_status。")
        if direct_event.payload.get("handled_by") != "InternalFinanceAgentLoop":
            raise AssertionError("处理完成后必须记录内部 Agent Loop 处理者。")
        if direct_event.payload.get("workflow_run_id") != direct_run["workflow_run_id"]:
            raise AssertionError("触发事件必须记录关联 Workflow Run ID。")

        repeat_result = runner.run_task(agent_task_id=direct_agent_task_id, as_of=as_of).to_dict()
        if repeat_result["processed_count"] != 0:
            raise AssertionError("同一个 Agent 任务重复运行时不应重复创建 Workflow。")

    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "finance_agent.cli",
            "agent",
            "run-once",
            "--owner-id",
            owner_id,
            "--limit",
            "5",
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
        raise AssertionError("内部 Agent Loop CLI 必须返回 ok。")
    if cli_payload["data"]["processed_count"] != 1:
        raise AssertionError(f"CLI 应只处理剩余 1 个事件，实际={cli_payload}")

    repeat_process = subprocess.run(
        [
            sys.executable,
            "-m",
            "finance_agent.cli",
            "agent",
            "run-once",
            "--owner-id",
            owner_id,
            "--limit",
            "5",
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
        raise AssertionError("CLI 重复运行不应重复处理已完成 Agent 任务。")

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
        cli_event = session.get_one(AssistantTriggerEventORM, cli_trigger_event_id)
        if cli_event.payload.get("agent_loop_status") != "workflow_completed":
            raise AssertionError("CLI 处理的事件也必须回写完成状态。")

    print(
        {
            "owner_id": owner_id,
            "direct_workflow_run_id": direct_run["workflow_run_id"],
            "cli_processed_count": cli_payload["data"]["processed_count"],
            "repeat_processed_count": repeat_payload["data"]["processed_count"],
            "workflow_run_count": workflow_run_count,
        }
    )


if __name__ == "__main__":
    main()
