"""内部金融 Agent Loop 的 LangGraph 扩展占位。

当前 runner 使用确定性 planner，先保证触发事件到 Workflow 的闭环可审计、
可幂等。后续要接入真正的 LangGraph loop 时，应复用这里的状态命名和 runner
边界，而不是让触发层直接运行 Workflow。
"""

from __future__ import annotations

from typing import Any


class InternalAgentLoopGraphUnavailable(RuntimeError):
    """内部 Agent Loop 图运行时尚未启用。"""


def build_internal_agent_loop_graph() -> Any:
    """预留内部 Agent Loop 的 LangGraph 构建入口。"""

    raise InternalAgentLoopGraphUnavailable(
        "内部 Agent Loop 当前使用确定性 runner；LangGraph loop 将在后续版本接入。"
    )
