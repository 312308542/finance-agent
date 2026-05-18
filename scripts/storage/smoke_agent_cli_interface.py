"""验证 CLI 和共用 Agent 接口能读取 Workflow/工具清单。"""

from __future__ import annotations

import json
import subprocess
import sys

from finance_agent.agents.interfaces import FinanceAgentInterface
from finance_agent.storage.db import create_session_factory, session_scope


def main() -> None:
    """执行 CLI/接口冒烟。"""

    session_factory = create_session_factory()
    with session_scope(session_factory) as session:
        interface = FinanceAgentInterface(session)
        workflow_result = interface.list_workflows().to_dict()
        tool_result = interface.list_tools().to_dict()

    workflow_types = {
        item["workflow_type"]
        for item in workflow_result["data"]["workflows"]
    }
    if "recommendation_decision" not in workflow_types:
        raise AssertionError("接口必须暴露 recommendation_decision Workflow。")

    tool_names = {item["name"] for item in tool_result["data"]["tools"]}
    if "factor.get_asset_factor_context" not in tool_names:
        raise AssertionError("接口必须暴露因子上下文工具。")

    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "finance_agent.cli",
            "workflows",
            "list",
        ],
        check=True,
        capture_output=True,
        encoding="utf-8",
        text=True,
    )
    cli_payload = json.loads(process.stdout)
    cli_workflow_types = {
        item["workflow_type"]
        for item in cli_payload["data"]["workflows"]
    }
    if workflow_types != cli_workflow_types:
        raise AssertionError("CLI Workflow 清单必须与接口层一致。")

    print(
        {
            "workflow_count": len(workflow_types),
            "tool_count": len(tool_names),
            "cli_status": cli_payload["status"],
        }
    )


if __name__ == "__main__":
    main()
