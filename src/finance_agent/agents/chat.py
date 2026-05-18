"""CLI 聊天窗口的内部金融 Agent 会话。

第一版聊天窗口不直接替代 Hermes，也不默认调用外部模型。它负责把用户在
终端里的自然语言输入路由到已完成的接口层能力：Workflow 清单、工具清单、
模型配置、模型路由预览和简单帮助。后续真实模型回答器可以在同一会话边界内
接入。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from finance_agent.agents.interfaces import FinanceAgentInterface
from finance_agent.agents.runtime import ModelRegistry, preview_model_routes
from finance_agent.storage.repositories import ChatMemoryRepository

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class ChatMessage:
    """一条聊天消息。"""

    role: str
    content: str
    intent: str | None = None
    data: JsonDict | None = None

    def to_dict(self) -> JsonDict:
        """转换为 JSON 友好的字典。"""

        result: JsonDict = {"role": self.role, "content": self.content}
        if self.intent:
            result["intent"] = self.intent
        if self.data is not None:
            result["data"] = self.data
        return result


@dataclass(frozen=True)
class ChatTurn:
    """一次用户输入和 Agent 回复。"""

    user_message: ChatMessage
    assistant_message: ChatMessage

    def to_dict(self) -> JsonDict:
        """转换为 JSON 友好的字典。"""

        return {
            "user_message": self.user_message.to_dict(),
            "assistant_message": self.assistant_message.to_dict(),
        }


@dataclass(frozen=True)
class ChatSessionResult:
    """一次聊天会话结果。"""

    owner_id: str
    chat_session_id: str
    turns: tuple[ChatTurn, ...]
    restored_message_count: int = 0

    def to_dict(self) -> JsonDict:
        """转换为 JSON 友好的字典。"""

        return {
            "owner_id": self.owner_id,
            "chat_session_id": self.chat_session_id,
            "turn_count": len(self.turns),
            "restored_message_count": self.restored_message_count,
            "turns": [turn.to_dict() for turn in self.turns],
        }


class FinanceAgentChatSession:
    """内部金融 Agent 的 CLI 聊天会话。"""

    def __init__(
        self,
        *,
        owner_id: str,
        interface: FinanceAgentInterface,
        model_registry: ModelRegistry,
        chat_memory: ChatMemoryRepository | None = None,
        chat_session_id: str | None = None,
        history_limit: int = 20,
    ) -> None:
        self.owner_id = owner_id
        self.interface = interface
        self.model_registry = model_registry
        self.chat_memory = chat_memory
        self.chat_session_id = chat_session_id or build_chat_session_id(owner_id=owner_id)
        self.history_limit = history_limit
        self.turns: list[ChatTurn] = []
        self.restored_messages: list[ChatMessage] = []
        self._ensure_chat_session()
        self._restore_recent_messages()

    def handle_message(self, content: str) -> ChatTurn:
        """处理一条用户输入。"""

        user_message = ChatMessage(role="user", content=content)
        intent = detect_chat_intent(content)
        if intent == "exit":
            assistant = ChatMessage(
                role="assistant",
                content="已退出 finance-agent 聊天窗口。",
                intent=intent,
            )
        elif intent == "list_workflows":
            assistant = self._answer_workflows(intent)
        elif intent == "list_tools":
            assistant = self._answer_tools(intent)
        elif intent == "model_config":
            assistant = self._answer_model_config(intent)
        elif intent == "route_preview":
            assistant = self._answer_route_preview(intent)
        elif intent == "history":
            assistant = self._answer_history(intent)
        else:
            assistant = self._answer_help(intent)

        turn = ChatTurn(user_message=user_message, assistant_message=assistant)
        self.turns.append(turn)
        self._persist_turn(turn)
        return turn

    def run_scripted(self, messages: list[str]) -> ChatSessionResult:
        """执行脚本化聊天，用于 smoke 和批处理验证。"""

        for message in messages:
            turn = self.handle_message(message)
            if turn.assistant_message.intent == "exit":
                break
        return ChatSessionResult(
            owner_id=self.owner_id,
            chat_session_id=self.chat_session_id,
            restored_message_count=len(self.restored_messages),
            turns=tuple(self.turns),
        )

    def _answer_workflows(self, intent: str) -> ChatMessage:
        result = self.interface.list_workflows().to_dict()["data"]
        workflows = result.get("workflows", [])
        lines = ["当前可用金融团队 Workflow："]
        for item in workflows:
            lines.append(f"- {item['workflow_type']}：{item.get('description', '')}")
        return ChatMessage(
            role="assistant",
            content="\n".join(lines),
            intent=intent,
            data={"workflows": workflows},
        )

    def _answer_tools(self, intent: str) -> ChatMessage:
        result = self.interface.list_tools().to_dict()["data"]
        tools = result.get("tools", [])
        lines = ["当前可用只读金融事实工具："]
        for item in tools:
            lines.append(f"- {item['name']}：{item.get('description', '')}")
        return ChatMessage(
            role="assistant",
            content="\n".join(lines),
            intent=intent,
            data={"tools": tools},
        )

    def _answer_model_config(self, intent: str) -> ChatMessage:
        data = self.model_registry.to_safe_dict()
        lines = [f"模型配置来源：{data['source']}"]
        for model_key, config in data["models"].items():
            ready = "ready" if config.get("ready") else "not-ready"
            lines.append(
                f"- {model_key}：{config.get('provider')} / "
                f"{config.get('model_name')} / {ready}"
            )
        return ChatMessage(
            role="assistant",
            content="\n".join(lines),
            intent=intent,
            data=data,
        )

    def _answer_route_preview(self, intent: str) -> ChatMessage:
        routes = preview_model_routes(
            registry=self.model_registry,
            workflow_type="recommendation_decision",
            task="roundtable_discussion",
            decision_type="high_risk_review",
            high_risk=True,
        )
        lines = ["推荐决策高风险场景的模型路由预览："]
        for route in routes:
            ready = "ready" if route.get("ready") else "not-ready"
            lines.append(f"- {route['model_key']}：{route['role']} / {ready}")
        return ChatMessage(
            role="assistant",
            content="\n".join(lines),
            intent=intent,
            data={"routes": routes},
        )

    def _answer_help(self, intent: str) -> ChatMessage:
        return ChatMessage(
            role="assistant",
            content=(
                "我现在可以在 CLI 聊天窗口里帮你查询：可用 Workflow、只读金融工具、"
                "模型配置、模型路由预览、最近聊天历史。你也可以输入 /exit 退出。"
            ),
            intent=intent,
            data={
                "supported_intents": [
                    "list_workflows",
                    "list_tools",
                    "model_config",
                    "route_preview",
                    "history",
                    "exit",
                ]
            },
        )

    def _answer_history(self, intent: str) -> ChatMessage:
        messages = self._load_recent_messages()
        if not messages:
            return ChatMessage(
                role="assistant",
                content="当前聊天会话还没有可恢复的历史消息。",
                intent=intent,
                data={"messages": []},
            )

        lines = [f"最近 {len(messages)} 条聊天历史："]
        for message in messages:
            role = "你" if message.role == "user" else "Agent"
            lines.append(f"- {role}：{message.content}")
        return ChatMessage(
            role="assistant",
            content="\n".join(lines),
            intent=intent,
            data={"messages": [message.to_dict() for message in messages]},
        )

    def _ensure_chat_session(self) -> None:
        if self.chat_memory is None:
            return
        existing = self.chat_memory.get_session(
            owner_id=self.owner_id,
            chat_session_id=self.chat_session_id,
        )
        if existing is None:
            self.chat_memory.upsert_session(
                chat_session_id=self.chat_session_id,
                owner_id=self.owner_id,
                payload={"runtime": "finance_agent_cli"},
            )

    def _restore_recent_messages(self) -> None:
        self.restored_messages = self._load_recent_messages()

    def _load_recent_messages(self) -> list[ChatMessage]:
        if self.chat_memory is None:
            return []
        records = self.chat_memory.list_recent_messages(
            owner_id=self.owner_id,
            chat_session_id=self.chat_session_id,
            limit=self.history_limit,
        )
        return [
            ChatMessage(
                role=record.role,
                content=record.content,
                intent=record.intent,
                data=record.data or None,
            )
            for record in records
        ]

    def _persist_turn(self, turn: ChatTurn) -> None:
        if self.chat_memory is None:
            return
        self.chat_memory.append_message(
            chat_session_id=self.chat_session_id,
            owner_id=self.owner_id,
            role=turn.user_message.role,
            content=turn.user_message.content,
            intent=turn.user_message.intent,
            data=turn.user_message.data,
        )
        self.chat_memory.append_message(
            chat_session_id=self.chat_session_id,
            owner_id=self.owner_id,
            role=turn.assistant_message.role,
            content=turn.assistant_message.content,
            intent=turn.assistant_message.intent,
            data=turn.assistant_message.data,
        )


def detect_chat_intent(content: str) -> str:
    """用轻量规则识别 CLI 聊天意图。"""

    normalized = content.strip().lower()
    if normalized in {"/exit", "exit", "quit", "q", "退出"}:
        return "exit"
    if any(keyword in normalized for keyword in ("workflow", "工作流", "流程", "能用")):
        return "list_workflows"
    if any(keyword in normalized for keyword in ("工具", "tool", "mcp")):
        return "list_tools"
    if any(keyword in normalized for keyword in ("模型配置", "model config", "配置")):
        return "model_config"
    if any(keyword in normalized for keyword in ("路由", "route", "高风险复核")):
        return "route_preview"
    if any(keyword in normalized for keyword in ("/history", "history", "历史", "聊天记录")):
        return "history"
    return "help"


def build_chat_session_id(*, owner_id: str) -> str:
    """生成聊天会话 ID。"""

    safe_owner = "".join(char if char.isalnum() else "-" for char in owner_id.lower())
    return f"chat:{safe_owner}:{uuid4().hex[:12]}"
