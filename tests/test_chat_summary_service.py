from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from finance_agent.agents.chat_summary import ChatSummaryService
from finance_agent.agents.runtime.model_client import ModelClientResponse
from finance_agent.agents.runtime.model_config import ModelEndpointConfig, ModelRegistry


@dataclass
class FakeMessage:
    role: str
    content: str
    sequence_no: int
    intent: str | None = None


class FakeChatSummaryStore:
    def __init__(self, messages: list[FakeMessage]) -> None:
        self.messages = messages
        self.summary_updates: list[dict[str, Any]] = []
        self.memories: list[dict[str, Any]] = []

    def get_session(self, *, owner_id: str, chat_session_id: str) -> dict[str, Any] | None:
        return {
            "owner_id": owner_id,
            "chat_session_id": chat_session_id,
            "message_count": len(self.messages),
        }

    def list_messages(
        self,
        *,
        owner_id: str,
        chat_session_id: str,
        limit: int,
    ) -> list[FakeMessage]:
        assert owner_id == "owner:demo"
        assert chat_session_id == "chat:demo"
        return self.messages[-limit:]

    def update_session_summary(
        self,
        *,
        owner_id: str,
        chat_session_id: str,
        summary: str,
        payload: dict[str, Any],
    ) -> None:
        self.summary_updates.append(
            {
                "owner_id": owner_id,
                "chat_session_id": chat_session_id,
                "summary": summary,
                "payload": payload,
            }
        )

    def upsert_summary_memory(
        self,
        *,
        memory_id: str,
        owner_id: str,
        chat_session_id: str,
        summary: str,
        payload: dict[str, Any],
    ) -> str:
        self.memories.append(
            {
                "memory_id": memory_id,
                "owner_id": owner_id,
                "chat_session_id": chat_session_id,
                "summary": summary,
                "payload": payload,
            }
        )
        return memory_id


class FakeSummaryModelClient:
    def __init__(self, responses: list[dict[str, Any] | None]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def invoke_json(
        self,
        *,
        config: ModelEndpointConfig,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        temperature: float = 0.1,
    ) -> ModelClientResponse:
        self.calls.append(
            {
                "config": config,
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
                "temperature": temperature,
            }
        )
        parsed = self.responses.pop(0)
        return ModelClientResponse(
            model_key=config.model_key,
            provider=config.provider,
            model_name=config.model_name,
            content="{}",
            parsed_json=parsed,
            raw_response={"choices": []},
        )


def test_chat_summary_service_writes_session_summary_and_finance_memory() -> None:
    store = FakeChatSummaryStore(messages=build_messages(8))
    model_client = FakeSummaryModelClient(
        [
            {
                "summary": "用户持续关注贵州茅台和宁德时代，偏好稳健仓位与风险先行。",
                "key_topics": ["贵州茅台", "宁德时代", "仓位控制"],
                "follow_up_items": ["下次复盘估值和风险事件"],
                "user_preferences": ["稳健", "先看风险"],
            }
        ]
    )
    service = ChatSummaryService(
        store=store,
        model_registry=build_ready_registry(),
        model_client=model_client,
    )

    result = service.summarize_session(
        owner_id="owner:demo",
        chat_session_id="chat:demo",
        min_messages=8,
    )

    assert result["status"] == "available"
    assert result["summary"].startswith("用户持续关注")
    assert result["memory_id"].startswith("memory:chat_summary:")
    assert store.summary_updates[0]["summary"] == result["summary"]
    assert store.summary_updates[0]["payload"]["key_topics"] == [
        "贵州茅台",
        "宁德时代",
        "仓位控制",
    ]
    assert store.memories[0]["summary"] == result["summary"]
    assert store.memories[0]["payload"]["memory_type"] == "chat_summary"
    assert model_client.calls[0]["temperature"] == 0.0


def test_chat_summary_service_skips_short_session_without_model_call() -> None:
    store = FakeChatSummaryStore(messages=build_messages(3))
    model_client = FakeSummaryModelClient([])
    service = ChatSummaryService(
        store=store,
        model_registry=build_ready_registry(),
        model_client=model_client,
    )

    result = service.summarize_session(
        owner_id="owner:demo",
        chat_session_id="chat:demo",
        min_messages=8,
    )

    assert result == {
        "status": "skipped",
        "reason": "message_count_below_threshold",
        "owner_id": "owner:demo",
        "chat_session_id": "chat:demo",
        "message_count": 3,
        "min_messages": 8,
    }
    assert model_client.calls == []
    assert store.summary_updates == []
    assert store.memories == []


def test_chat_summary_service_retries_once_when_model_json_is_invalid() -> None:
    store = FakeChatSummaryStore(messages=build_messages(8))
    model_client = FakeSummaryModelClient(
        [
            {"summary": ""},
            {
                "summary": "第二次返回合法摘要。",
                "key_topics": ["风险"],
                "follow_up_items": [],
                "user_preferences": [],
            },
        ]
    )
    service = ChatSummaryService(
        store=store,
        model_registry=build_ready_registry(),
        model_client=model_client,
    )

    result = service.summarize_session(
        owner_id="owner:demo",
        chat_session_id="chat:demo",
        min_messages=8,
    )

    assert result["status"] == "available"
    assert result["summary"] == "第二次返回合法摘要。"
    assert len(model_client.calls) == 2
    assert "上次输出不是合法 JSON 摘要" in model_client.calls[1]["messages"][-1]["content"]


def build_messages(count: int) -> list[FakeMessage]:
    messages: list[FakeMessage] = []
    for index in range(1, count + 1):
        role = "user" if index % 2 else "assistant"
        messages.append(
            FakeMessage(
                role=role,
                content=f"第 {index} 条关于持仓、风险和观察池的对话。",
                sequence_no=index,
            )
        )
    return messages


def build_ready_registry() -> ModelRegistry:
    return ModelRegistry(
        source="test",
        models={
            "deepseek-v4-pro": ModelEndpointConfig(
                model_key="deepseek-v4-pro",
                provider="deepseek",
                model_name="deepseek-chat",
                base_url="https://example.test/v1",
                api_key="test-api-key-chat-summary",
                role="primary_financial_analyst",
            )
        },
    )
