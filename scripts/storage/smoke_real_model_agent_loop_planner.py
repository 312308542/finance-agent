"""验证真实模型 Planner 的受控工具循环。

该脚本不请求外部模型，也不依赖数据库。它用假的模型客户端模拟真实 LLM：
第一轮要求读取因子和图谱记忆，第二轮基于工具观察选择执行 Domain Workflow。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from finance_agent.agents.loop import AgentLoopContext, AgentLoopLimits, ModelFinanceAgentPlanner
from finance_agent.agents.runtime.model_client import ModelClientResponse
from finance_agent.agents.runtime.model_config import ModelEndpointConfig, ModelRegistry

JsonDict = dict[str, Any]


@dataclass
class FakeModelClient:
    """按顺序返回预置 JSON，模拟 OpenAI-compatible 模型客户端。"""

    responses: list[JsonDict]
    calls: list[JsonDict]

    def invoke_json(
        self,
        *,
        config: ModelEndpointConfig,
        messages: list[JsonDict],
        temperature: float = 0.1,
    ) -> ModelClientResponse:
        """返回下一条模型响应，并记录调用输入。"""

        self.calls.append(
            {
                "model_key": config.model_key,
                "message_count": len(messages),
                "temperature": temperature,
            }
        )
        if not self.responses:
            raise AssertionError("假的模型客户端响应已耗尽。")
        payload = self.responses.pop(0)
        return ModelClientResponse(
            model_key=config.model_key,
            provider=config.provider,
            model_name=config.model_name,
            content=str(payload),
            parsed_json=payload,
            raw_response={"fake": True},
        )


class FakeToolRuntime:
    """只记录模型实际请求的工具，避免 planner 预取所有事实。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, JsonDict]] = []

    def call(self, name: str, **kwargs: Any) -> JsonDict:
        """返回可被 Planner 摘要压缩的结构化事实。"""

        self.calls.append((name, dict(kwargs)))
        if name == "factor.get_asset_factor_context":
            return {
                "asset_id": kwargs["asset_id"],
                "factor_frame": {"keys": ["technical", "flow"]},
                "evidence": [{"evidence_id": "evidence:factor:1", "summary": "因子强势"}],
            }
        if name == "memory.trace_asset_graph":
            return {
                "paths": [
                    {
                        "source": "watchlist_item:1",
                        "relation": "supported_by",
                        "target": "memory:1",
                    }
                ],
                "memories": [{"memory_id": "memory:1", "content": "此前入池原因仍有效"}],
            }
        raise AssertionError(f"Planner 不应调用未请求的工具：{name}")


def main() -> None:
    """执行真实模型 Planner 的最小闭环验证。"""

    owner_id = "owner:smoke:real_model_planner"
    asset_id = "asset:smoke:real_model_planner:600519"
    event = SimpleNamespace(
        trigger_event_id="trigger:smoke:real_model_planner",
        owner_id=owner_id,
        trigger_type="watchlist_condition_hit",
        trigger_ref=asset_id,
        severity="high",
        status="dispatched",
        agent_runtime="internal_agent_loop",
        agent_task_id="agent_task:smoke:real_model_planner",
        requested_workflow_type="asset_deep_analysis",
        portfolio_id=None,
        watchlist_id="watchlist:smoke:real_model_planner",
        recommendation_run_id=None,
        asset_id=asset_id,
        payload={
            "market": "ashare",
            "reason": "观察池启动条件命中，需要模型决定是否深度分析。",
        },
    )
    registry = ModelRegistry(
        source="smoke",
        models={
            "deepseek-v4-pro": ModelEndpointConfig(
                model_key="deepseek-v4-pro",
                provider="deepseek",
                model_name="DeepSeek V4 Pro",
                base_url="http://model.example/v1",
                api_key="test-key",
                role="primary_financial_analyst",
            )
        },
    )
    model_client = FakeModelClient(
        calls=[],
        responses=[
            {
                "status": "need_more_data",
                "summary_zh": "先读取因子和图谱记忆，再判断是否拉起工作流。",
                "tool_requests": [
                    {
                        "tool": "factor.get_asset_factor_context",
                        "arguments": {"asset_id": asset_id, "horizon": "swing"},
                        "reason": "确认技术和因子状态。",
                    },
                    {
                        "tool": "memory.trace_asset_graph",
                        "arguments": {
                            "owner_id": owner_id,
                            "asset_id": asset_id,
                            "max_depth": 2,
                            "limit": 5,
                        },
                        "reason": "追踪入池原因和历史记忆。",
                    },
                ],
                "reasoning_brief_zh": "缺少事实观察，先补证据。",
            },
            {
                "status": "ready",
                "action": "run_workflow",
                "workflow_type": "asset_deep_analysis",
                "summary_zh": "因子和历史入池原因均支持继续深度分析。",
                "confidence": 0.82,
                "risk_flags": ["需要风险反驳复核"],
                "reasoning_brief_zh": "证据足够，调用单标的深度分析 Workflow。",
            },
        ],
    )
    tool_runtime = FakeToolRuntime()
    planner = ModelFinanceAgentPlanner(
        model_registry=registry,
        model_client=model_client,
        tool_runtime=tool_runtime,
    )
    plan = planner.build_plan(
        AgentLoopContext(
            event=event,  # type: ignore[arg-type]
            as_of=datetime.now(UTC),
            limits=AgentLoopLimits(max_steps=6, max_tool_calls=4, max_workflow_calls=1),
            session=None,
        )
    )

    if plan.action != "run_workflow":
        raise AssertionError(f"模型 Planner 应选择运行 Workflow，实际={plan.action}")
    if plan.workflow_type != "asset_deep_analysis":
        raise AssertionError(f"模型 Planner 应选择 asset_deep_analysis，实际={plan.workflow_type}")
    if len(model_client.calls) != 2:
        raise AssertionError(f"模型 Planner 应执行两轮模型调用，实际={len(model_client.calls)}")
    called_tools = [name for name, _ in tool_runtime.calls]
    if called_tools != ["factor.get_asset_factor_context", "memory.trace_asset_graph"]:
        raise AssertionError(f"Planner 应只调用模型请求的工具，实际={called_tools}")

    model_planner = plan.initial_state.get("model_planner") or {}
    if model_planner.get("status") != "model_planned_workflow":
        raise AssertionError(f"模型 Planner 状态应为 model_planned_workflow，实际={model_planner}")
    if not model_planner.get("model_call_executed"):
        raise AssertionError("模型 Planner 必须记录已执行真实模型调用。")
    if model_planner.get("model_loop_iterations") != 2:
        raise AssertionError(f"模型循环轮次应为 2，实际={model_planner}")
    if model_planner.get("tool_result_count") != 2:
        raise AssertionError(f"工具观察数量应为 2，实际={model_planner}")
    last_observation = plan.initial_state.get("model_tool_observations", [{}])[-1]
    if last_observation.get("tool") != "memory.trace_asset_graph":
        raise AssertionError("Planner 必须把图谱工具观察写入初始状态审计。")

    print(
        {
            "workflow_type": plan.workflow_type,
            "model_loop_iterations": model_planner.get("model_loop_iterations"),
            "called_tools": called_tools,
        }
    )


if __name__ == "__main__":
    main()
