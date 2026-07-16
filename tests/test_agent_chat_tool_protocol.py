import copy
import json
from dataclasses import dataclass
from typing import Any

from finance_agent.agents.chat import (
    OPENAI_CHAT_TOOL_SCHEMAS,
    FinanceAgentChatSession,
    to_openai_tool_name,
)
from finance_agent.agents.interfaces import AgentInterfaceResult
from finance_agent.agents.runtime.model_client import ModelClientResponse
from finance_agent.agents.runtime.model_config import ModelEndpointConfig, ModelRegistry

JsonDict = dict[str, Any]


class _FakeToolInterface:
    """只暴露一个白名单工具，用于验证模型工具协议历史。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, JsonDict]] = []

    def list_tools(self) -> AgentInterfaceResult:
        return AgentInterfaceResult(
            status="ok",
            data={
                "tools": [
                    {
                        "name": "memory.find_memory_conflicts",
                        "description": "查询记忆冲突",
                    }
                ]
            },
        )

    def call_tool(self, *, name: str, arguments: JsonDict | None = None) -> AgentInterfaceResult:
        self.calls.append((name, arguments or {}))
        return AgentInterfaceResult(
            status="ok",
            data={"tool": name, "result": {"ok": True, "arguments": arguments or {}}},
        )


@dataclass
class _RecordingModelClient:
    """记录每次模型请求的完整消息快照。"""

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
                "messages": copy.deepcopy(messages),
                "tools": copy.deepcopy(tools),
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
                **({"tool_calls": list(tool_calls)} if tool_calls else {}),
            },
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else "stop",
        )


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


def _tool_call(call_id: str, asset_id: str) -> JsonDict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": to_openai_tool_name("memory.find_memory_conflicts"),
            "arguments": json.dumps({"asset_id": asset_id}, ensure_ascii=False),
        },
    }


def test_chat_model_history_only_keeps_answered_tool_calls_when_budget_truncates() -> None:
    """工具调用被预算截断时，下一轮历史不能保留未回填结果的 tool_call。"""

    model_client = _RecordingModelClient(
        calls=[],
        responses=[
            {
                "tool_calls": [
                    _tool_call("call_keep", "ashare:600519"),
                    _tool_call("call_drop", "ashare:000001"),
                ]
            },
            {"content": "已根据工具结果完成分析。"},
        ],
    )
    interface = _FakeToolInterface()
    session = FinanceAgentChatSession(
        owner_id="owner:test",
        interface=interface,  # type: ignore[arg-type]
        model_registry=_ready_registry(),
        model_client=model_client,
        max_model_tool_calls=1,
    )

    turn = session.handle_message("分析今日行情")

    assert turn.assistant_message.content == "已根据工具结果完成分析。"
    assert interface.calls == [
        ("memory.find_memory_conflicts", {"owner_id": "owner:test", "asset_id": "ashare:600519"})
    ]
    assert len(model_client.calls) == 2
    second_messages = model_client.calls[1]["messages"]
    assistant_tool_messages = [
        message
        for message in second_messages
        if message.get("role") == "assistant" and message.get("tool_calls")
    ]
    tool_result_messages = [
        message for message in second_messages if message.get("role") == "tool"
    ]

    assert [call["id"] for call in assistant_tool_messages[-1]["tool_calls"]] == ["call_keep"]
    assert [message["tool_call_id"] for message in tool_result_messages] == ["call_keep"]
    assert "call_drop" not in json.dumps(second_messages, ensure_ascii=False)


def test_chat_tool_schemas_include_profile_tools_with_audit_requirements() -> None:
    """模型工具协议必须暴露画像工具，并要求 profile.upsert 携带审计来源和证据。"""

    assert "profile.get" in OPENAI_CHAT_TOOL_SCHEMAS
    assert "profile.upsert" in OPENAI_CHAT_TOOL_SCHEMAS
    assert "advice.suggest_style" in OPENAI_CHAT_TOOL_SCHEMAS
    assert OPENAI_CHAT_TOOL_SCHEMAS["profile.upsert"]["required"] == [
        "owner_id",
        "updates",
        "source",
        "evidence",
    ]
