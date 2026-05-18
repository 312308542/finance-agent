"""验证金融事实工具运行时的最小入口。

本脚本只验证工具注册和结构化返回协议，不要求本地数据库提前存在特定组合、
观察池或推荐运行数据。
"""

from __future__ import annotations

from finance_agent.agents.tools import FinanceToolRuntime
from finance_agent.storage.db import create_session_factory, session_scope


def main() -> None:
    """执行金融事实工具运行时冒烟。"""

    session_factory = create_session_factory()
    with session_scope(session_factory) as session:
        runtime = FinanceToolRuntime(session)
        tool_names = set(runtime.list_tools())
        required = {
            "portfolio.get_snapshot",
            "watchlist.get_active_items",
            "recommendation.get_run",
            "signal_risk.get_asset_context",
            "factor.get_asset_factor_context",
            "memory.recall_asset_memories",
            "workflow.list_workflows",
        }
        missing = required - tool_names
        if missing:
            raise AssertionError(f"缺少工具: {sorted(missing)}")

        workflow_result = runtime.call("workflow.list_workflows")
        if not isinstance(workflow_result, dict):
            raise AssertionError("工具返回必须是结构化 dict")
        if "workflows" not in workflow_result:
            raise AssertionError("workflow.list_workflows 必须返回 workflows 字段")

        memory_result = runtime.call(
            "memory.recall_asset_memories",
            owner_id="owner:smoke",
            asset_id="asset:missing",
            limit=3,
        )
        if memory_result["memories"] != []:
            raise AssertionError("不存在的 smoke 资产不应返回记忆")

        print(
            {
                "tool_count": len(tool_names),
                "required": sorted(required),
                "workflow_count": len(workflow_result["workflows"]),
            }
        )


if __name__ == "__main__":
    main()
