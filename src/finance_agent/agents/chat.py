"""CLI 聊天窗口的内部金融 Agent 会话。

固定命令继续走确定性接口；自然语言问题在模型端点可用时进入受控模型循环，
由模型自主规划并按需调用只读事实工具，再输出中文摘要。模型不可用或输出
不可执行时，聊天窗口只报告模型失败，不伪造分析结论。
"""

from __future__ import annotations

import json
from collections.abc import Callable
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
ChatEventSink = Callable[[str, JsonDict], None]


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
        event_sink: ChatEventSink | None = None,
        chat_session_id: str | None = None,
        history_limit: int = 20,
        max_model_turns: int = 3,
        max_model_tool_calls: int = 8,
    ) -> None:
        self.owner_id = owner_id
        self.interface = interface
        self.model_registry = model_registry
        self.model_client = model_client or OpenAICompatibleModelClient()
        self.chat_memory = chat_memory
        self.event_sink = event_sink
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
        self._emit_event(
            "agent_step",
            {
                "stage": "intent_detected",
                "intent": intent,
                "message": f"识别意图：{intent}",
            },
        )
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
        elif intent == "recommendation_lookup":
            assistant = self._answer_model_chat(
                content,
                intent=intent,
                task_kind="recommendation_decision",
            )
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

    def _answer_model_chat(
        self,
        content: str,
        *,
        intent: str = "model_chat",
        task_kind: str = "general_chat",
    ) -> ChatMessage:
        """使用真实模型进行受控聊天；必要时按需调用只读事实工具。"""

        config = select_chat_model_config(self.model_registry)
        if config is None:
            return ChatMessage(
                role="assistant",
                content=(
                    "当前没有可用的主分析模型配置，无法执行模型驱动的 Agent 分析。"
                    "请先在模型配置页启用可调用的 primary_financial_analyst 模型。"
                ),
                intent=f"{intent}_model_unavailable",
                data={"model_registry": self.model_registry.to_safe_dict()},
            )

        tool_catalog = self._load_tool_catalog()
        openai_tools = build_openai_chat_tools(tool_catalog)
        tool_name_map = build_openai_tool_name_map(tool_catalog)
        messages = build_chat_model_messages(
            owner_id=self.owner_id,
            question=content,
            intent=intent,
            task_kind=task_kind,
            tool_catalog=tool_catalog,
            restored_messages=self.restored_messages,
        )
        observations: list[JsonDict] = []
        model_audit: list[JsonDict] = []
        tool_call_count = 0
        error_message: str | None = None
        final_payload: JsonDict | None = None
        self._emit_event(
            "agent_step",
            {
                "stage": "model_loop_start",
                "task_kind": task_kind,
                "message": "进入模型驱动的 Agent 工具循环，模型将自主规划事实工具",
            },
        )
        for iteration in range(1, self.max_model_turns + 1):
            try:
                self._emit_event(
                    "model_call",
                    {
                        "iteration": iteration,
                        "model": config.model_key,
                        "message": f"第 {iteration} 轮模型规划事实工具",
                    },
                )
                response = self.model_client.invoke_json(
                    config=config,
                    messages=messages,
                    tools=openai_tools,
                    tool_choice="auto" if openai_tools else None,
                    temperature=0.1,
                )
            except Exception as exc:
                error_message = str(exc)
                break
            model_audit.append(response.to_audit_dict() | {"iteration": iteration})
            requested_tools = normalize_openai_chat_tool_calls(
                response.tool_calls,
                tool_catalog=tool_catalog,
                tool_name_map=tool_name_map,
                owner_id=self.owner_id,
                remaining=self.max_model_tool_calls - tool_call_count,
            )
            assistant_message = filter_assistant_tool_calls_for_history(
                normalize_assistant_message_for_history(response),
                allowed_tool_call_ids={
                    str(request.get("tool_call_id") or "") for request in requested_tools
                },
            )
            messages.append(assistant_message)
            self._emit_event(
                "model_result",
                {
                    "iteration": iteration,
                    "status": response.finish_reason or "unknown",
                    "tool_call_count": len(response.tool_calls),
                    "message": f"模型完成第 {iteration} 轮输出",
                },
            )
            if requested_tools:
                for request in requested_tools:
                    observation = self._call_chat_tool(request)
                    observations.append(observation)
                    tool_call_count += 1
                    messages.append(build_openai_tool_result_message(observation))
                    self._emit_workflow_step_from_tool_call(observation)
                continue

            if response.content.strip():
                final_payload = response.parsed_json or {
                    "summary_zh": response.content.strip(),
                    "status": "ready",
                }
            else:
                error_message = "模型没有返回最终文本，也没有继续发起工具调用。"
            break

        if final_payload is None:
            content_zh = (
                "模型未能完成本次 Agent 分析，因此没有生成结论。"
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
            intent=intent,
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
        tool_call_id = str(request.get("tool_call_id") or "")
        self._emit_event(
            "tool_call",
            {
                "tool": tool_name,
                "arguments": arguments,
                **({"tool_call_id": tool_call_id} if tool_call_id else {}),
                "message": f"调用事实工具：{tool_name}",
            },
        )
        try:
            result = self.interface.call_tool(name=tool_name, arguments=arguments).to_dict()
            observation = {
                "tool": tool_name,
                "tool_call_id": tool_call_id,
                "arguments": arguments,
                "status": "ok",
                "raw_result": result.get("data", {}).get("result", {}),
                "summary": compact_json(result.get("data", {})),
            }
            self._emit_event(
                "tool_result",
                {
                    "tool": tool_name,
                    "status": "ok",
                    "message": summarize_chat_tool_observation(observation),
                },
            )
            return observation
        except Exception as exc:
            observation = {
                "tool": tool_name,
                "tool_call_id": tool_call_id,
                "arguments": arguments,
                "status": "failed",
                "error": str(exc),
            }
            self._emit_event(
                "tool_result",
                {
                    "tool": tool_name,
                    "status": "failed",
                    "message": summarize_chat_tool_observation(observation),
                },
            )
            return observation

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

    def _emit_event(self, event: str, data: JsonDict) -> None:
        """向流式调用方推送 Agent 执行过程事件。"""

        if self.event_sink is None:
            return
        self.event_sink(event, data)

    def _emit_workflow_step_from_model(self, payload: JsonDict) -> None:
        """把模型规划出的 workflow_step 透传为前端可见的流程事件。"""

        step = payload.get("workflow_step")
        if not isinstance(step, dict):
            return
        node = str(step.get("node") or payload.get("status") or "model_planning")
        message = str(step.get("message") or step.get("title") or f"模型规划节点：{node}")
        event_data = dict(step)
        event_data["node"] = node
        event_data["message"] = message
        self._emit_event("workflow_step", event_data)

    def _emit_workflow_step_from_tool_call(self, observation: JsonDict) -> None:
        """按工具调用阶段推送可见工作流节点。"""

        tool_name = str(observation.get("tool") or "")
        node = chat_tool_workflow_node(tool_name)
        self._emit_event(
            "workflow_step",
            {
                "node": node,
                "tool": tool_name,
                "message": f"模型选择执行流程节点：{node}",
            },
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
    if any(
        keyword in normalized
        for keyword in (
            "股票推荐",
            "股票",
            "买入",
            "适合买",
            "推荐",
            "recommendation",
            "buy",
        )
    ):
        return "recommendation_lookup"
    return "model_chat"


def summarize_chat_tool_observation(observation: JsonDict) -> str:
    """生成聊天流式工具结果摘要。"""

    tool = observation.get("tool") or "工具"
    if observation.get("status") != "ok":
        return f"{tool} 调用失败：{str(observation.get('error') or '')[:120]}"
    raw = observation.get("raw_result")
    if isinstance(raw, dict):
        recommendations = raw.get("recommendations")
        if isinstance(recommendations, list):
            return f"{tool} 返回 {len(recommendations)} 条推荐"
        risks = raw.get("risks")
        if isinstance(risks, list):
            return f"{tool} 返回 {len(risks)} 条风险记录"
        evidence = raw.get("evidence")
        if isinstance(evidence, list):
            return f"{tool} 返回 {len(evidence)} 条证据"
        timeline = raw.get("timeline")
        if isinstance(timeline, list):
            return f"{tool} 返回 {len(timeline)} 条记忆时间线"
    return f"{tool} 调用完成"


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
    intent: str,
    task_kind: str,
    tool_catalog: dict[str, JsonDict],
    restored_messages: list[ChatMessage],
) -> list[JsonDict]:
    """构建 OpenAI Chat Completions 工具调用消息。"""

    history = [message.to_dict() for message in restored_messages[-6:]]
    user_payload = {
        "owner_id": owner_id,
        "question": question,
        "intent": intent,
        "task_kind": task_kind,
        "recent_chat_history": history,
        "available_tools": list(tool_catalog.values()),
        "agent_rules": [
            "必须由模型自主规划需要调用的事实工具，并使用 Chat Completions tools 发起调用。",
            "不能编造实时行情、买入建议或工具结果；事实不足时要明确说明不足。",
            (
                "推荐决策类问题应优先考虑 recommendation.get_latest，"
                "然后按候选情况自主选择因子、信号风险、记忆等工具。"
            ),
            (
                "如果推荐数据看起来是 smoke、样例、过期或非实时数据，"
                "最终回答必须如实标注，不能说成当前真实买入清单。"
            ),
        ],
        "final_answer_contract": "完成必要工具调用后，直接输出面向用户的中文回答，不要输出 JSON。",
    }
    return [
        {
            "role": "system",
            "content": (
                "你是私人金融助手的 CLI 聊天 Agent。你只能基于用户问题、聊天历史和"
                "已入库事实工具回答；需要事实时必须使用 OpenAI tools 调用事实工具。"
                "工具选择、参数和分析规划必须由你完成。最终回答使用简洁中文。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(user_payload, ensure_ascii=False, sort_keys=True),
        },
    ]


OPENAI_CHAT_TOOL_SCHEMAS: dict[str, JsonDict] = {
    "portfolio.get_snapshot": {
        "properties": {"portfolio_id": {"type": "string", "description": "组合 ID。"}},
        "required": ["portfolio_id"],
    },
    "watchlist.get_active_items": {
        "properties": {
            "owner_id": {"type": "string", "description": "用户/租户 ID。"},
            "watchlist_id": {"type": "string", "description": "可选观察池 ID。"},
        },
        "required": ["owner_id"],
    },
    "recommendation.get_latest": {
        "properties": {
            "market": {
                "type": "string",
                "enum": ["ashare", "crypto_spot", "crypto_future"],
                "description": "可选市场过滤。不确定时可以省略。",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "description": "返回推荐数量。",
            },
        },
        "required": [],
    },
    "recommendation.get_run": {
        "properties": {
            "run_id": {"type": "string", "description": "推荐运行 ID。"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ["run_id"],
    },
    "profile.get": {
        "properties": {
            "owner_id": {"type": "string", "description": "用户/租户 ID。"},
        },
        "required": ["owner_id"],
    },
    "profile.upsert": {
        "properties": {
            "owner_id": {"type": "string", "description": "用户/租户 ID。"},
            "updates": {
                "type": "object",
                "description": "需要写入的画像维度，例如 risk_appetite、horizon、style_tendency。",
            },
            "source": {
                "type": "object",
                "description": "每个画像维度的来源，例如 elicited、inferred。",
            },
            "evidence": {
                "type": "array",
                "description": "画像写入证据链，例如聊天轮次、决策或复盘 ID。",
                "items": {"type": "object"},
            },
        },
        "required": ["owner_id", "updates", "source", "evidence"],
    },
    "advice.suggest_style": {
        "properties": {
            "owner_id": {"type": "string", "description": "用户/租户 ID。"},
        },
        "required": ["owner_id"],
    },
    "factor.get_asset_factor_context": {
        "properties": {
            "asset_id": {"type": "string", "description": "标准资产 ID，例如 ashare:600519。"},
            "horizon": {"type": "string", "default": "swing"},
            "timeframe": {"type": "string", "default": "1d"},
            "library": {"type": "string"},
            "evidence_limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["asset_id"],
    },
    "signal_risk.get_asset_context": {
        "properties": {
            "asset_id": {"type": "string", "description": "标准资产 ID，例如 ashare:600519。"},
            "horizon": {"type": "string", "default": "swing"},
            "risk_limit": {"type": "integer", "minimum": 1, "maximum": 20},
            "quality_limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["asset_id"],
    },
    "memory.get_asset_memory_context": {
        "properties": {
            "owner_id": {"type": "string", "description": "用户/租户 ID。"},
            "asset_id": {"type": "string", "description": "标准资产 ID。"},
            "query": {"type": "string", "description": "用于语义召回的用户问题或核验目标。"},
            "memory_type": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["owner_id", "asset_id", "query"],
    },
    "memory.recall_asset_memories": {
        "properties": {
            "owner_id": {"type": "string"},
            "asset_id": {"type": "string"},
            "memory_type": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            "query": {"type": "string"},
        },
        "required": ["owner_id"],
    },
    "memory.get_asset_memory_timeline": {
        "properties": {
            "owner_id": {"type": "string"},
            "asset_id": {"type": "string"},
            "memory_type": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ["owner_id", "asset_id"],
    },
    "memory.trace_asset_graph": {
        "properties": {
            "owner_id": {"type": "string"},
            "asset_id": {"type": "string"},
            "max_depth": {"type": "integer", "minimum": 1, "maximum": 5},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ["owner_id", "asset_id"],
    },
    "memory.explain_candidate_reason_chain": {
        "properties": {
            "owner_id": {"type": "string"},
            "asset_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["owner_id", "asset_id"],
    },
    "memory.find_memory_conflicts": {
        "properties": {
            "owner_id": {"type": "string"},
            "asset_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["owner_id"],
    },
    "memory.find_similar_decision_paths": {
        "properties": {
            "owner_id": {"type": "string"},
            "asset_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["owner_id", "asset_id"],
    },
    "memory.detect_risk_contagion": {
        "properties": {
            "owner_id": {"type": "string"},
            "asset_id": {"type": "string"},
            "max_depth": {"type": "integer", "minimum": 1, "maximum": 5},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ["owner_id"],
    },
    "workflow.list_workflows": {"properties": {}, "required": []},
}


def build_openai_chat_tools(tool_catalog: dict[str, JsonDict]) -> list[JsonDict]:
    """把内部事实工具转换为 OpenAI Chat Completions tools schema。"""

    tools: list[JsonDict] = []
    for tool_name in sorted(tool_catalog):
        tool = tool_catalog[tool_name]
        schema = OPENAI_CHAT_TOOL_SCHEMAS.get(tool_name, {"properties": {}, "required": []})
        properties = dict(schema.get("properties") or {})
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": to_openai_tool_name(tool_name),
                    "description": str(tool.get("description") or tool_name),
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": list(schema.get("required") or []),
                        "additionalProperties": False,
                    },
                },
            }
        )
    return tools


def build_openai_tool_name_map(tool_catalog: dict[str, JsonDict]) -> dict[str, str]:
    """建立 OpenAI 函数名到内部工具名的映射。"""

    return {to_openai_tool_name(tool_name): tool_name for tool_name in tool_catalog}


def to_openai_tool_name(tool_name: str) -> str:
    """转换为 Chat Completions function name 允许的字符集合。"""

    return "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in tool_name)


def normalize_openai_chat_tool_calls(
    tool_calls: tuple[JsonDict, ...],
    *,
    tool_catalog: dict[str, JsonDict],
    tool_name_map: dict[str, str],
    owner_id: str,
    remaining: int,
) -> tuple[JsonDict, ...]:
    """校验并转换模型返回的 OpenAI tool_calls。"""

    if remaining <= 0:
        return ()
    requests: list[JsonDict] = []
    for item in tool_calls:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if not isinstance(function, dict):
            continue
        openai_tool_name = str(function.get("name") or "")
        tool_name = tool_name_map.get(openai_tool_name, openai_tool_name)
        if tool_name not in tool_catalog:
            continue
        arguments = parse_openai_tool_arguments(function.get("arguments"))
        arguments = sanitize_chat_tool_arguments(
            tool_name=tool_name,
            arguments=arguments,
            owner_id=owner_id,
        )
        missing = missing_required_tool_arguments(tool_name=tool_name, arguments=arguments)
        if missing:
            arguments = {**arguments, "_missing_required_arguments": missing}
        requests.append(
            {
                "tool": tool_name,
                "arguments": arguments,
                "tool_call_id": str(item.get("id") or ""),
                "openai_tool_name": openai_tool_name,
            }
        )
        if len(requests) >= remaining:
            break
    return tuple(requests)


def parse_openai_tool_arguments(value: object) -> JsonDict:
    """解析 OpenAI tool_call.function.arguments。"""

    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def sanitize_chat_tool_arguments(
    *,
    tool_name: str,
    arguments: JsonDict,
    owner_id: str,
) -> JsonDict:
    """只保留工具 schema 允许的参数，并补齐 owner_id 类上下文参数。"""

    schema = OPENAI_CHAT_TOOL_SCHEMAS.get(tool_name, {"properties": {}})
    allowed = set((schema.get("properties") or {}).keys())
    sanitized = {key: value for key, value in arguments.items() if key in allowed}
    if (
        tool_name.startswith(("memory.", "profile.", "advice."))
        and "owner_id" in allowed
        and "owner_id" not in sanitized
    ):
        sanitized["owner_id"] = owner_id
    if tool_name == "watchlist.get_active_items" and "owner_id" not in sanitized:
        sanitized["owner_id"] = owner_id
    return sanitized


def missing_required_tool_arguments(*, tool_name: str, arguments: JsonDict) -> list[str]:
    """检查工具调用缺失的必填参数。"""

    schema = OPENAI_CHAT_TOOL_SCHEMAS.get(tool_name, {"required": []})
    return [
        str(key)
        for key in schema.get("required") or []
        if arguments.get(str(key)) in (None, "")
    ]


def normalize_assistant_message_for_history(response: Any) -> JsonDict:
    """把模型返回的 assistant message 压成可回填给 Chat Completions 的格式。"""

    message = response.assistant_message if isinstance(response.assistant_message, dict) else None
    if message is None:
        message = {"role": "assistant", "content": response.content or None}
        if response.tool_calls:
            message["tool_calls"] = list(response.tool_calls)
    if "role" not in message:
        message = {"role": "assistant", **message}
    if "content" not in message:
        message = {**message, "content": None}
    return dict(message)


def filter_assistant_tool_calls_for_history(
    assistant_message: JsonDict,
    *,
    allowed_tool_call_ids: set[str],
) -> JsonDict:
    """只保留后续会回填 tool 结果的 assistant tool_calls，避免模型协议断链。"""

    message = dict(assistant_message)
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return message

    filtered = [
        tool_call
        for tool_call in tool_calls
        if isinstance(tool_call, dict) and str(tool_call.get("id") or "") in allowed_tool_call_ids
    ]
    if filtered:
        message["tool_calls"] = filtered
        if "content" not in message:
            message["content"] = None
        return message

    message.pop("tool_calls", None)
    if message.get("content") is None:
        message["content"] = ""
    return message


def build_openai_tool_result_message(observation: JsonDict) -> JsonDict:
    """把本地工具结果回填为 OpenAI Chat Completions tool 消息。"""

    return {
        "role": "tool",
        "tool_call_id": str(observation.get("tool_call_id") or ""),
        "content": json.dumps(
            {
                "tool": observation.get("tool"),
                "status": observation.get("status"),
                "result": compact_json(observation.get("raw_result", {})),
                **({"error": observation["error"]} if observation.get("error") else {}),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    }


def chat_tool_workflow_node(tool_name: str) -> str:
    """根据工具名生成前端可展示的工作流节点名。"""

    if tool_name.startswith("recommendation."):
        return "load_recommendations"
    if tool_name.startswith("factor."):
        return "load_factors"
    if tool_name.startswith("signal_risk."):
        return "data_quality_check"
    if tool_name.startswith("memory."):
        return "roundtable_memory"
    if tool_name.startswith("watchlist."):
        return "load_watchlist"
    if tool_name.startswith("portfolio."):
        return "load_portfolio"
    return "model_tool_call"


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
