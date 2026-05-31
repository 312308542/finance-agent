"""验证 CLI 聊天窗口可用模型按需调用事实工具。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from finance_agent.agents.chat import FinanceAgentChatSession, to_openai_tool_name
from finance_agent.agents.interfaces import AgentInterfaceResult
from finance_agent.agents.runtime.model_client import ModelClientResponse
from finance_agent.agents.runtime.model_config import ModelEndpointConfig, ModelRegistry

JsonDict = dict[str, Any]


class FakeInterface:
    """只实现聊天模型测试需要的接口。"""

    def __init__(self) -> None:
        self.tool_calls: list[tuple[str, JsonDict]] = []

    def list_workflows(self) -> AgentInterfaceResult:
        return AgentInterfaceResult(status="ok", data={"workflows": []})

    def list_tools(self) -> AgentInterfaceResult:
        return AgentInterfaceResult(
            status="ok",
            data={
                "tools": [
                    {
                        "name": "memory.find_memory_conflicts",
                        "description": "发现 Finance Memory 冲突。",
                    }
                ]
            },
        )

    def call_tool(self, *, name: str, arguments: JsonDict | None = None) -> AgentInterfaceResult:
        self.tool_calls.append((name, arguments or {}))
        return AgentInterfaceResult(
            status="ok",
            data={
                "tool": name,
                "result": {
                    "conflicts": [
                        {
                            "asset_id": (arguments or {}).get("asset_id"),
                            "summary": "存在看多记忆和回避记忆冲突。",
                        }
                    ]
                },
            },
        )


@dataclass
class FakeChatModelClient:
    """模拟聊天模型两轮输出。"""

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
                "message_count": len(messages),
                "tools": tools,
                "tool_choice": tool_choice,
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
            parsed_json=None,
            raw_response={"fake": True},
            assistant_message={
                "role": "assistant",
                "content": content or None,
                "tool_calls": list(tool_calls),
            },
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else "stop",
        )


def main() -> None:
    """执行聊天模型工具循环冒烟验证。"""

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
    model_client = FakeChatModelClient(
        calls=[],
        responses=[
            {
                "tool_calls": [
                    {
                        "id": "call_conflicts",
                        "type": "function",
                        "function": {
                            "name": to_openai_tool_name("memory.find_memory_conflicts"),
                            "arguments": (
                                '{"owner_id":"owner:smoke:chat-model",'
                                '"asset_id":"asset:600519","limit":5}'
                            ),
                        },
                    }
                ],
            },
            {
                "content": "该标的存在记忆冲突，建议先做风险复核再决定是否继续观察。",
            },
        ],
    )
    interface = FakeInterface()
    session = FinanceAgentChatSession(
        owner_id="owner:smoke:chat-model",
        interface=interface,  # type: ignore[arg-type]
        model_registry=registry,
        model_client=model_client,
    )
    turn = session.handle_message("帮我检查 asset:600519 的记忆冲突")
    if turn.assistant_message.intent != "model_chat":
        raise AssertionError(
            f"普通自然语言问题应进入模型聊天，实际={turn.assistant_message.intent}"
        )
    if len(model_client.calls) != 2:
        raise AssertionError(f"模型应调用两轮，实际={len(model_client.calls)}")
    if interface.tool_calls != [
        (
            "memory.find_memory_conflicts",
            {"owner_id": "owner:smoke:chat-model", "asset_id": "asset:600519", "limit": 5},
        )
    ]:
        raise AssertionError(f"聊天模型应按需调用记忆图谱工具，实际={interface.tool_calls}")
    if "风险复核" not in turn.assistant_message.content:
        raise AssertionError(f"聊天回复应返回模型中文结论，实际={turn.assistant_message.content}")

    print(
        {
            "intent": turn.assistant_message.intent,
            "model_calls": len(model_client.calls),
            "tool_calls": len(interface.tool_calls),
        }
    )


if __name__ == "__main__":
    main()
