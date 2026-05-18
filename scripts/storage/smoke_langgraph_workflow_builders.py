"""验证 LangGraph Workflow 构建器注册。

如果当前环境尚未安装 LangGraph，本脚本不会假装成功构建图，而是验证构建器
可发现，并输出依赖缺失状态。安装 LangGraph 后会进一步真实构建图。
"""

from __future__ import annotations

from finance_agent.agents.workflows import (
    LangGraphWorkflowUnavailable,
    list_langgraph_workflow_builders,
)


def main() -> None:
    """执行 LangGraph Workflow 构建器冒烟。"""

    builders = list_langgraph_workflow_builders()
    workflow_types = {builder.workflow_type for builder in builders}
    required = {"portfolio_monitoring", "watchlist_management", "recommendation_decision"}
    missing = required - workflow_types
    if missing:
        raise AssertionError(f"缺少 LangGraph Workflow 构建器: {sorted(missing)}")

    built = 0
    unavailable: list[str] = []
    for builder in builders:
        try:
            builder.build()
            built += 1
        except LangGraphWorkflowUnavailable:
            unavailable.append(builder.workflow_type)

    print(
        {
            "builder_count": len(builders),
            "built": built,
            "langgraph_unavailable": unavailable,
        }
    )


if __name__ == "__main__":
    main()
