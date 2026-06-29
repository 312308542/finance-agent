from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from finance_agent.agents.chat import FinanceAgentChatSession
from finance_agent.agents.interfaces import AgentInterfaceResult
from finance_agent.agents.runtime.model_client import ModelClientResponse
from finance_agent.agents.runtime.model_config import ModelEndpointConfig, ModelRegistry

JsonDict = dict[str, Any]


class _WorkflowInterface:
    """记录聊天入口触发的 Workflow 和事实工具。"""

    def __init__(self) -> None:
        self.workflow_calls: list[JsonDict] = []
        self.tool_calls: list[tuple[str, JsonDict]] = []
        self.latest_payload: JsonDict = {
            "active_run": {
                        "run_id": "run:latest:ashare",
                        "market": "ashare",
                        "status": "available",
                        "finished_at": datetime.now(UTC).isoformat(),
                        "payload": {"data_quality": {"status": "available"}},
                    },
            "recommendations": [
                {
                    "asset_id": "ashare:600519",
                    "symbol": "600519",
                    "action": "buy_candidate",
                    "rank": 1,
                    "payload": {
                        "data_quality": {"status": "available"},
                        "backtest_evidence": {"status": "available"},
                    },
                }
            ],
        }

    def list_tools(self) -> AgentInterfaceResult:
        return AgentInterfaceResult(
            status="ok",
            data={
                "tools": [
                    {
                        "name": "recommendation.get_latest",
                        "description": "读取最新推荐运行。",
                    },
                    {
                        "name": "factor.get_asset_factor_context",
                        "description": "读取单标的因子上下文。",
                    },
                ]
            },
        )

    def call_tool(self, *, name: str, arguments: JsonDict | None = None) -> AgentInterfaceResult:
        self.tool_calls.append((name, arguments or {}))
        if name == "recommendation.get_latest":
            return AgentInterfaceResult(
                status="ok",
                data={"tool": name, "result": self.latest_payload},
            )
        return AgentInterfaceResult(status="ok", data={"tool": name, "result": {}})

    def run_workflow(self, **kwargs: Any) -> AgentInterfaceResult:
        self.workflow_calls.append(dict(kwargs))
        return AgentInterfaceResult(
            status="ok",
            data={
                "workflow_run_id": f"workflow:{kwargs['workflow_type']}:chat",
                "workflow_type": kwargs["workflow_type"],
                "final_state": {
                    "workflow_decisions": [
                        {
                            "asset_id": kwargs.get("asset_id") or "ashare:600519",
                            "action": "watch",
                            "summary": "圆桌认为需要等待风险反驳确认。",
                        }
                    ],
                    "roundtable_opinions": [
                        {
                            "role": "risk_rebuttal",
                            "summary": "风险反驳：关注数据质量和仓位边界。",
                        }
                    ],
                },
                "report": {
                    "summary": "Workflow 已生成排序、理由、缺口、风险反驳和确认边界。",
                },
            },
        )


@dataclass
class _RecordingModelClient:
    """记录聊天模型入参。"""

    calls: list[JsonDict]

    def invoke_json(
        self,
        *,
        config: ModelEndpointConfig,
        messages: list[JsonDict],
        tools: list[JsonDict] | None = None,
        tool_choice: str | JsonDict | None = None,
        temperature: float = 0.1,
    ) -> ModelClientResponse:
        self.calls.append(
            {
                "messages": copy.deepcopy(messages),
                "tools": copy.deepcopy(tools),
                "tool_choice": tool_choice,
                "temperature": temperature,
            }
        )
        return ModelClientResponse(
            model_key=config.model_key,
            provider=config.provider,
            model_name=config.model_name,
            content="基于 Workflow 圆桌结果：先观察，等待确认边界满足后再行动。",
            parsed_json=None,
            raw_response={"fake": True},
            assistant_message={
                "role": "assistant",
                "content": "基于 Workflow 圆桌结果：先观察，等待确认边界满足后再行动。",
            },
            tool_calls=(),
            finish_reason="stop",
        )


def test_recommendation_chat_preloads_recommendation_decision_workflow() -> None:
    interface = _WorkflowInterface()
    model_client = _RecordingModelClient(calls=[])
    session = FinanceAgentChatSession(
        owner_id="owner-1",
        interface=interface,  # type: ignore[arg-type]
        model_registry=_ready_registry(),
        model_client=model_client,
    )

    turn = session.handle_message("有什么股票推荐")

    assert interface.workflow_calls[0]["workflow_type"] == "recommendation_decision"
    assert interface.workflow_calls[0]["recommendation_run_id"] == "run:latest:ashare"
    assert interface.workflow_calls[0]["portfolio_id"] == "portfolio:owner-1:default"
    assert interface.workflow_calls[0]["watchlist_id"] == "watchlist:owner-1:ashare:research"
    assert turn.assistant_message.data is not None
    assert turn.assistant_message.data["workflow_context"]["status"] == "ok"
    assert turn.assistant_message.data["workflow_context"]["readiness"]["status"] == "ready"
    first_user_payload = model_client.calls[0]["messages"][1]["content"]
    assert "workflow_context" in first_user_payload
    assert "排序 + 每只为什么 + 还差什么才升级 + 风险反驳 + 确认边界" in first_user_payload


def test_recommendation_chat_marks_blocked_readiness_for_sandbox_data() -> None:
    interface = _WorkflowInterface()
    interface.latest_payload = {
        "active_run": {
            "run_id": "run:smoke:ashare",
            "market": "ashare",
            "status": "available",
            "finished_at": "2026-06-30T08:30:00+00:00",
            "payload": {"source": "smoke"},
        },
        "recommendations": [
            {
                "asset_id": "ashare:600519",
                "symbol": "600519",
                "payload": {"data_quality": {"status": "stale"}},
            }
        ],
    }
    model_client = _RecordingModelClient(calls=[])
    session = FinanceAgentChatSession(
        owner_id="owner-1",
        interface=interface,  # type: ignore[arg-type]
        model_registry=_ready_registry(),
        model_client=model_client,
    )

    turn = session.handle_message("有什么股票推荐")

    readiness = turn.assistant_message.data["workflow_context"]["readiness"]  # type: ignore[index]
    assert readiness["status"] == "blocked"
    assert "smoke" in readiness["reasons"]
    assert "data_quality" in readiness["reasons"]
    first_user_payload = model_client.calls[0]["messages"][1]["content"]
    assert "不能把该结果称为当前可执行买入清单" in first_user_payload


def test_single_asset_chat_preloads_asset_deep_analysis_workflow() -> None:
    interface = _WorkflowInterface()
    model_client = _RecordingModelClient(calls=[])
    session = FinanceAgentChatSession(
        owner_id="owner-1",
        interface=interface,  # type: ignore[arg-type]
        model_registry=_ready_registry(),
        model_client=model_client,
    )

    session.handle_message("帮我分析 600519 是否值得买")

    assert interface.workflow_calls == [
        {
            "workflow_type": "asset_deep_analysis",
            "owner_id": "owner-1",
            "trigger_type": "chat",
            "trigger_ref": session.chat_session_id,
            "asset_id": "ashare:600519",
            "asset_ids": ["ashare:600519"],
            "horizon": "swing",
            "timeframe": "1d",
            "recommendation_limit": 20,
            "initial_state": {
                "chat_question": "帮我分析 600519 是否值得买",
                "chat_task_kind": "asset_deep_analysis",
            },
        }
    ]
    first_user_payload = model_client.calls[0]["messages"][1]["content"]
    assert "asset_deep_analysis" in first_user_payload
    assert "ashare:600519" in first_user_payload


def _ready_registry() -> ModelRegistry:
    return ModelRegistry(
        source="test",
        models={
            "test-model": ModelEndpointConfig(
                model_key="test-model",
                provider="openai_compatible",
                model_name="test-model",
                base_url="https://model.test/v1",
                api_key="test-key",
                role="primary_financial_analyst",
            )
        },
    )
