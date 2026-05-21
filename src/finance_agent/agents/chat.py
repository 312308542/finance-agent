"""CLI 聊天窗口的内部金融 Agent 会话。

固定命令继续走确定性接口；普通自然语言问题在模型端点可用时进入受控模型循环，
由模型按需调用只读事实工具，再输出中文摘要。模型不可用或输出不可执行时，
聊天窗口会回到可操作的能力说明，不阻断会话。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from finance_agent.agents.interfaces import FinanceAgentInterface
from finance_agent.agents.runtime import (
    ModelClient,
    ModelEndpointConfig,
    ModelRegistry,
    OpenAICompatibleModelClient,
    preview_model_routes,
)
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
        model_client: ModelClient | None = None,
        chat_memory: ChatMemoryRepository | None = None,
        chat_session_id: str | None = None,
        history_limit: int = 20,
        max_model_turns: int = 3,
        max_model_tool_calls: int = 3,
    ) -> None:
        self.owner_id = owner_id
        self.interface = interface
        self.model_registry = model_registry
        self.model_client = model_client or OpenAICompatibleModelClient()
        self.chat_memory = chat_memory
        self.chat_session_id = chat_session_id or build_chat_session_id(owner_id=owner_id)
        self.history_limit = history_limit
        self.max_model_turns = max_model_turns
        self.max_model_tool_calls = max_model_tool_calls
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
            assistant = self._answer_model_chat(content)

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

    def _answer_model_chat(self, content: str) -> ChatMessage:
        """使用真实模型进行受控聊天；必要时按需调用只读事实工具。"""

        config = select_chat_model_config(self.model_registry)
        if config is None:
            return ChatMessage(
                role="assistant",
                content=(
                    "当前没有可用的主分析模型配置。我可以先帮你查询 Workflow、工具、"
                    "模型配置、路由预览和聊天历史；配置好 DeepSeek V4 Pro 或其他 "
                    "primary_financial_analyst 模型后，普通问题会进入模型工具循环。"
                ),
                intent="model_chat_unavailable",
                data={"model_registry": self.model_registry.to_safe_dict()},
            )

        tool_catalog = self._load_tool_catalog()
        observations: list[JsonDict] = []
        model_audit: list[JsonDict] = []
        tool_call_count = 0
        error_message: str | None = None
        final_payload: JsonDict | None = None
        for iteration in range(1, self.max_model_turns + 1):
            try:
                response = self.model_client.invoke_json(
                    config=config,
                    messages=build_chat_model_messages(
                        owner_id=self.owner_id,
                        question=content,
                        tool_catalog=tool_catalog,
                        observations=observations,
                        restored_messages=self.restored_messages,
                    ),
                    temperature=0.1,
                )
            except Exception as exc:
                error_message = str(exc)
                break
            model_audit.append(response.to_audit_dict() | {"iteration": iteration})
            payload = response.parsed_json or {}
            if not payload:
                error_message = "模型未返回可解析 JSON。"
                break
            requested_tools = normalize_chat_tool_requests(
                payload.get("tool_requests"),
                tool_catalog=tool_catalog,
                owner_id=self.owner_id,
                remaining=self.max_model_tool_calls - tool_call_count,
            )
            if requested_tools:
                for request in requested_tools:
                    observation = self._call_chat_tool(request)
                    observations.append(observation)
                    tool_call_count += 1
                continue
            final_payload = payload
            break

        if final_payload is None:
            content_zh = (
                "模型聊天暂时没有形成稳定结论，已保留会话历史。你可以先通过工具或 "
                "Workflow 命令查询已入库事实。"
            )
            if error_message:
                content_zh += f" 错误摘要：{error_message[:160]}"
        else:
            content_zh = str(
                final_payload.get("summary_zh")
                or final_payload.get("answer_zh")
                or final_payload.get("reasoning_brief_zh")
                or "模型已完成分析，但没有返回中文摘要。"
            )
        return ChatMessage(
            role="assistant",
            content=content_zh,
            intent="model_chat",
            data={
                "model": config.to_safe_dict(),
                "model_audit": model_audit,
                "tool_observations": observations,
                "final_payload": final_payload,
                "error_message": error_message,
            },
        )

    def _load_tool_catalog(self) -> dict[str, JsonDict]:
        """读取 CLI 聊天可用的只读事实工具。"""

        result = self.interface.list_tools().to_dict()["data"]
        tools = result.get("tools", [])
        catalog: dict[str, JsonDict] = {}
        for tool in tools:
            if not isinstance(tool, dict) or not tool.get("name"):
                continue
            catalog[str(tool["name"])] = {
                "name": str(tool["name"]),
                "description": str(tool.get("description") or ""),
            }
        return catalog

    def _call_chat_tool(self, request: JsonDict) -> JsonDict:
        """调用模型请求的白名单事实工具，并压缩结果。"""

        tool_name = str(request["tool"])
        arguments = dict(request.get("arguments") or {})
        try:
            result = self.interface.call_tool(name=tool_name, arguments=arguments).to_dict()
            return {
                "tool": tool_name,
                "arguments": arguments,
                "status": "ok",
                "summary": compact_json(result.get("data", {})),
            }
        except Exception as exc:
            return {
                "tool": tool_name,
                "arguments": arguments,
                "status": "failed",
                "error": str(exc),
            }

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
    return "model_chat"


def select_chat_model_config(registry: ModelRegistry) -> ModelEndpointConfig | None:
    """选择聊天窗口的主分析模型配置。"""

    preferred = registry.get("deepseek-v4-pro")
    if preferred and preferred.ready:
        return preferred
    for config in registry.models.values():
        if config.ready and config.role in {
            "primary_financial_analyst",
            "top_level_dispatcher",
            None,
        }:
            return config
    return None


def build_chat_model_messages(
    *,
    owner_id: str,
    question: str,
    tool_catalog: dict[str, JsonDict],
    observations: list[JsonDict],
    restored_messages: list[ChatMessage],
) -> list[JsonDict]:
    """构建聊天模型的严格 JSON 输出协议。"""

    history = [message.to_dict() for message in restored_messages[-6:]]
    user_payload = {
        "owner_id": owner_id,
        "question": question,
        "recent_chat_history": history,
        "available_tools": list(tool_catalog.values()),
        "tool_observations": observations,
        "output_contract": {
            "need_more_data": {
                "status": "need_more_data",
                "summary_zh": "说明为什么需要工具",
                "tool_requests": [
                    {
                        "tool": "必须来自 available_tools.name",
                        "arguments": {"owner_id": owner_id},
                    }
                ],
            },
            "ready": {
                "status": "ready",
                "summary_zh": "面向用户的中文回答",
                "tool_requests": [],
            },
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "你是私人金融助手的 CLI 聊天 Agent。你只能基于用户问题、聊天历史和"
                "已入库事实工具回答；需要事实时先请求工具。只输出一个 JSON 对象，"
                "不要输出 Markdown 或额外解释。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(user_payload, ensure_ascii=False, sort_keys=True),
        },
    ]


def normalize_chat_tool_requests(
    value: object,
    *,
    tool_catalog: dict[str, JsonDict],
    owner_id: str,
    remaining: int,
) -> tuple[JsonDict, ...]:
    """过滤聊天模型请求的工具调用。"""

    if remaining <= 0 or not isinstance(value, list):
        return ()
    requests: list[JsonDict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        tool_name = str(item.get("tool") or "")
        if tool_name not in tool_catalog:
            continue
        arguments = item.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        if tool_name.startswith("memory.") and "owner_id" not in arguments:
            arguments = {**arguments, "owner_id": owner_id}
        requests.append({"tool": tool_name, "arguments": arguments})
        if len(requests) >= remaining:
            break
    return tuple(requests)


def compact_json(value: Any) -> Any:
    """压缩工具结果，避免聊天消息携带过大的 JSON。"""

    if isinstance(value, list):
        return {"count": len(value), "sample": value[:2]}
    if isinstance(value, dict):
        compacted: JsonDict = {}
        for key, item in value.items():
            if isinstance(item, list):
                compacted[key] = {"count": len(item), "sample": item[:2]}
            elif isinstance(item, dict):
                compacted[key] = {"keys": sorted(str(child_key) for child_key in item)[:20]}
            else:
                compacted[key] = item
        return compacted
    return value


def build_chat_session_id(*, owner_id: str) -> str:
    """生成聊天会话 ID。"""

    safe_owner = "".join(char if char.isalnum() else "-" for char in owner_id.lower())
    return f"chat:{safe_owner}:{uuid4().hex[:12]}"
