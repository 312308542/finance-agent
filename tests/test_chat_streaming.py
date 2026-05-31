import json
from dataclasses import dataclass
from typing import Any

from finance_agent.agents.interfaces import AgentInterfaceResult
from finance_agent.agents.runtime.model_client import ModelClientResponse
from finance_agent.agents.runtime.model_config import ModelEndpointConfig, ModelRegistry
from finance_agent.api.routes import format_sse_event, stream_chat_response
from finance_agent.api.schemas import ChatRequest

JsonDict = dict[str, Any]


class FakeRecommendationInterface:
    """用于验证推荐类聊天会调用本地事实工具。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def list_tools(self) -> AgentInterfaceResult:
        return AgentInterfaceResult(
            status="ok",
            data={
                "tools": [
                    {
                        "name": "recommendation.get_latest",
                        "description": "读取最近入库的推荐运行和候选推荐。",
                    },
                    {
                        "name": "factor.get_asset_factor_context",
                        "description": "读取单标的因子、指标和证据上下文。",
                    },
                    {
                        "name": "signal_risk.get_asset_context",
                        "description": "读取单标的信号、风险和数据质量上下文。",
                    },
                    {
                        "name": "memory.get_asset_memory_context",
                        "description": "读取单标的金融记忆上下文。",
                    },
                ]
            },
        )

    def call_tool(
        self,
        *,
        name: str,
        arguments: dict[str, object] | None = None,
    ) -> AgentInterfaceResult:
        self.calls.append((name, arguments or {}))
        if name == "factor.get_asset_factor_context":
            return AgentInterfaceResult(
                status="ok",
                data={
                    "tool": name,
                    "result": {
                        "asset_id": arguments.get("asset_id") if arguments else None,
                        "score": {"total_score": 91.5, "confidence": 0.82},
                        "factor_frame": {"status": "available"},
                        "evidence": [],
                    },
                },
            )
        if name == "signal_risk.get_asset_context":
            return AgentInterfaceResult(
                status="ok",
                data={
                    "tool": name,
                    "result": {
                        "asset_id": arguments.get("asset_id") if arguments else None,
                        "signal": {"direction": "bullish", "confidence": 0.81},
                        "risks": [],
                        "data_quality": [{"status": "available"}],
                    },
                },
            )
        if name == "memory.get_asset_memory_context":
            return AgentInterfaceResult(
                status="ok",
                data={
                    "tool": name,
                    "result": {
                        "asset_id": arguments.get("asset_id") if arguments else None,
                        "similar_memories": [],
                        "timeline": [],
                    },
                },
            )
        return AgentInterfaceResult(
            status="ok",
            data={
                "tool": name,
                "result": {
                    "active_run": {
                        "run_id": "run-1",
                        "market": "ashare",
                        "strategy": "test",
                        "finished_at": "2026-05-28T09:30:00+08:00",
                    },
                    "recommendations": [
                        {
                            "asset_id": "ashare:600519",
                            "symbol": "600519",
                            "name": "贵州茅台",
                            "market": "ashare",
                            "action": "buy_candidate",
                            "rank": 1,
                            "total_score": 91.5,
                            "confidence": 0.82,
                            "conviction": "high",
                            "summary": "趋势和质量得分靠前。",
                        }
                    ],
                },
            },
        )


@dataclass
class FakeRecommendationModelClient:
    """模拟推荐聊天模型自主规划工具调用。"""

    responses: list[JsonDict]
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
                "model_key": config.model_key,
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
                "temperature": temperature,
            }
        )
        payload = self.responses.pop(0)
        tool_calls = tuple(payload.get("tool_calls") or ())
        content = str(payload.get("content") or "")
        return ModelClientResponse(
            model_key=config.model_key,
            provider=config.provider,
            model_name=config.model_name,
            content=content,
            parsed_json=payload.get("parsed_json"),
            raw_response={"fake": True},
            assistant_message={
                "role": "assistant",
                "content": content or None,
                **(
                    {"reasoning_content": payload["reasoning_content"]}
                    if payload.get("reasoning_content")
                    else {}
                ),
                "tool_calls": list(tool_calls),
            },
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else "stop",
        )


def test_format_sse_event_outputs_named_json_event() -> None:
    """SSE 事件使用明确的事件名和 JSON payload，方便前端增量解析。"""

    event = format_sse_event("delta", {"content": "你好"})

    assert event.endswith("\n\n")
    lines = event.strip().splitlines()
    assert lines[0] == "event: delta"
    assert lines[1].startswith("data: ")
    assert json.loads(lines[1].removeprefix("data: ")) == {"content": "你好"}


def test_stream_chat_recommendation_emits_tool_events(monkeypatch) -> None:
    """推荐类问题必须由模型自主规划工具调用并流式输出。"""

    fake_interface = FakeRecommendationInterface()
    fake_model_client = FakeRecommendationModelClient(
        calls=[],
        responses=[
            {
                "reasoning_content": "先读取推荐列表，避免直接臆测。",
                "tool_calls": [
                    {
                        "id": "call_rec",
                        "type": "function",
                        "function": {
                            "name": "recommendation_get_latest",
                            "arguments": json.dumps(
                                {
                                    "limit": 8,
                                    "market": "ashare",
                                    "owner_id": "owner-1",
                                },
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
            },
            {
                "reasoning_content": "补充候选标的上下文。",
                "tool_calls": [
                    {
                        "id": "call_factor",
                        "type": "function",
                        "function": {
                            "name": "factor_get_asset_factor_context",
                            "arguments": json.dumps(
                                {
                                    "asset_id": "ashare:600519",
                                    "horizon": "swing",
                                    "timeframe": "1d",
                                    "evidence_limit": 5,
                                },
                                ensure_ascii=False,
                            ),
                        },
                    },
                    {
                        "id": "call_risk",
                        "type": "function",
                        "function": {
                            "name": "signal_risk_get_asset_context",
                            "arguments": json.dumps(
                                {
                                    "asset_id": "ashare:600519",
                                    "horizon": "swing",
                                    "risk_limit": 5,
                                    "quality_limit": 5,
                                },
                                ensure_ascii=False,
                            ),
                        },
                    },
                    {
                        "id": "call_memory",
                        "type": "function",
                        "function": {
                            "name": "memory_get_asset_memory_context",
                            "arguments": json.dumps(
                                {
                                    "owner_id": "owner-1",
                                    "asset_id": "ashare:600519",
                                    "query": "有什么股票推荐",
                                    "limit": 5,
                                },
                                ensure_ascii=False,
                            ),
                        },
                    },
                ],
            },
            {
                "content": (
                    "模型综合工具结果：贵州茅台可作为高置信候选继续跟踪，"
                    "但仍需结合实时行情和仓位纪律。"
                ),
            },
        ],
    )

    monkeypatch.setattr("finance_agent.api.routes.create_session_factory", lambda: object())
    monkeypatch.setattr("finance_agent.api.routes.session_scope", lambda _factory: _NullSession())
    monkeypatch.setattr(
        "finance_agent.api.routes.FinanceAgentInterface",
        lambda _session: fake_interface,
    )
    monkeypatch.setattr(
        "finance_agent.api.routes.load_model_registry",
        lambda **_kwargs: _ReadyRegistry(),
    )
    monkeypatch.setattr(
        "finance_agent.agents.chat.OpenAICompatibleModelClient",
        lambda: fake_model_client,
    )
    monkeypatch.setattr("finance_agent.api.routes.ChatMemoryRepository", lambda _session: None)

    events = list(
        stream_chat_response(
            ChatRequest(owner_id="owner-1", message="有什么股票推荐", session_id=None)
        )
    )
    event_names = [event.splitlines()[0] for event in events]

    assert "event: agent_step" in event_names
    assert "event: workflow_step" in event_names
    assert "event: model_call" in event_names
    assert "event: model_result" in event_names
    assert "event: tool_call" in event_names
    assert "event: tool_result" in event_names
    assert "event: delta" in event_names
    assert event_names.index("event: model_call") < event_names.index("event: tool_call")
    first_model_call = fake_model_client.calls[0]
    assert first_model_call["tool_choice"] == "auto"
    assert first_model_call["tools"]
    rec_tool = next(
        tool
        for tool in first_model_call["tools"]
        if tool["function"]["name"] == "recommendation_get_latest"
    )
    assert rec_tool["type"] == "function"
    assert "owner_id" not in rec_tool["function"]["parameters"]["properties"]
    assert rec_tool["function"]["parameters"]["additionalProperties"] is False
    second_messages = fake_model_client.calls[1]["messages"]
    assert any(
        message.get("role") == "assistant" and message.get("tool_calls")
        for message in second_messages
    )
    assert any(
        message.get("role") == "assistant"
        and message.get("reasoning_content") == "先读取推荐列表，避免直接臆测。"
        for message in second_messages
    )
    assert any(
        message.get("role") == "tool" and message.get("tool_call_id") == "call_rec"
        for message in second_messages
    )
    done_event = next(event for event in events if event.startswith("event: done"))
    done_data = json.loads(done_event.splitlines()[1].removeprefix("data: "))
    done_assistant_data = done_data["turn"]["assistant_message"].get("data", {})
    assert "candidate_contexts" not in done_assistant_data
    assert "payload" not in json.dumps(done_data, ensure_ascii=False)
    assert "raw_result" not in json.dumps(done_data, ensure_ascii=False)
    assert fake_interface.calls == [
        ("recommendation.get_latest", {"limit": 8, "market": "ashare"}),
        (
            "factor.get_asset_factor_context",
            {
                "asset_id": "ashare:600519",
                "horizon": "swing",
                "timeframe": "1d",
                "evidence_limit": 5,
            },
        ),
        (
            "signal_risk.get_asset_context",
            {
                "asset_id": "ashare:600519",
                "horizon": "swing",
                "risk_limit": 5,
                "quality_limit": 5,
            },
        ),
        (
            "memory.get_asset_memory_context",
            {
                "owner_id": "owner-1",
                "asset_id": "ashare:600519",
                "query": "有什么股票推荐",
                "limit": 5,
            },
        ),
    ]
    assert len(fake_model_client.calls) == 3
    assert "模型综合工具结果" in "".join(events)
    assert "贵州茅台" in "".join(events)


class _NullSession:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_args: object) -> bool:
        return False


class _ReadyRegistry(ModelRegistry):
    def __init__(self) -> None:
        super().__init__(
            source="test",
            models={
                "deepseek-v4-pro": ModelEndpointConfig(
                    model_key="deepseek-v4-pro",
                    provider="openai_compatible",
                    model_name="deepseek-v4-pro",
                    base_url="https://model.test/v1",
                    api_key="test-key",
                    role="primary_financial_analyst",
                )
            },
        )
    source = "test"
