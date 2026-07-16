"""聊天会话摘要抽取服务。

本模块负责把较长的 CLI 聊天会话压缩成可复用摘要，并沉淀到 Finance
Memory。它不改变聊天主路径；调用方可以在 CLI、调度器或后续前端入口中按阈值触发。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from hashlib import sha1
from typing import Any, Protocol

from sqlalchemy.orm import Session

from finance_agent.agents.chat import select_chat_model_config
from finance_agent.agents.runtime.model_client import (
    ModelClient,
    OpenAICompatibleModelClient,
)
from finance_agent.agents.runtime.model_config import ModelRegistry, load_model_registry
from finance_agent.application.memory_service import MemoryService
from finance_agent.storage.repositories import ChatMemoryRepository

JsonDict = dict[str, Any]


class ChatSummaryStore(Protocol):
    """聊天摘要服务需要的持久化端口。"""

    def get_session(self, *, owner_id: str, chat_session_id: str) -> JsonDict | None:
        """读取聊天会话元信息。"""

    def list_messages(
        self,
        *,
        owner_id: str,
        chat_session_id: str,
        limit: int,
    ) -> Sequence[Any]:
        """按对话顺序读取聊天消息。"""

    def update_session_summary(
        self,
        *,
        owner_id: str,
        chat_session_id: str,
        summary: str,
        payload: JsonDict,
    ) -> None:
        """把摘要写回聊天会话。"""

    def upsert_summary_memory(
        self,
        *,
        memory_id: str,
        owner_id: str,
        chat_session_id: str,
        summary: str,
        payload: JsonDict,
    ) -> str:
        """把聊天摘要沉淀为 Finance Memory。"""


class SqlAlchemyChatSummaryStore:
    """基于 SQLAlchemy session 的聊天摘要持久化实现。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.chat_memory = ChatMemoryRepository(session)
        self.memory_service = MemoryService(session)

    def get_session(self, *, owner_id: str, chat_session_id: str) -> JsonDict | None:
        session = self.chat_memory.get_session(
            owner_id=owner_id,
            chat_session_id=chat_session_id,
        )
        if session is None:
            return None
        return {
            "owner_id": session.owner_id,
            "chat_session_id": session.chat_session_id,
            "message_count": session.message_count,
            "summary": session.summary,
            "payload": dict(session.payload or {}),
        }

    def list_messages(
        self,
        *,
        owner_id: str,
        chat_session_id: str,
        limit: int,
    ) -> Sequence[Any]:
        return self.chat_memory.list_messages(
            owner_id=owner_id,
            chat_session_id=chat_session_id,
            limit=limit,
        )

    def update_session_summary(
        self,
        *,
        owner_id: str,
        chat_session_id: str,
        summary: str,
        payload: JsonDict,
    ) -> None:
        session = self.chat_memory.get_session(
            owner_id=owner_id,
            chat_session_id=chat_session_id,
        )
        if session is None:
            raise ValueError(f"聊天会话不存在：{chat_session_id}")
        session.summary = summary
        session.payload = {**dict(session.payload or {}), "chat_summary": payload}
        session.updated_at = datetime.now().astimezone()
        self.session.flush()

    def upsert_summary_memory(
        self,
        *,
        memory_id: str,
        owner_id: str,
        chat_session_id: str,
        summary: str,
        payload: JsonDict,
    ) -> str:
        memory = self.memory_service.upsert_memory(
            memory_id=memory_id,
            owner_id=owner_id,
            memory_type="chat_summary",
            scope="owner",
            content=summary,
            confidence=Decimal("0.750000"),
            payload={
                **payload,
                "memory_type": "chat_summary",
                "source_type": "assistant_chat_session",
                "source_id": chat_session_id,
            },
        )
        return memory.memory_id


class ChatSummaryService:
    """生成聊天会话摘要，并回写聊天会话与 Finance Memory。"""

    def __init__(
        self,
        *,
        store: ChatSummaryStore | None = None,
        session: Session | None = None,
        model_registry: ModelRegistry | None = None,
        model_client: ModelClient | None = None,
    ) -> None:
        if store is None:
            if session is None:
                raise ValueError("ChatSummaryService 需要 store 或 session。")
            store = SqlAlchemyChatSummaryStore(session)
        self.store = store
        self.model_registry = model_registry or load_model_registry()
        self.model_client = model_client or OpenAICompatibleModelClient()

    def summarize_session(
        self,
        *,
        owner_id: str,
        chat_session_id: str,
        min_messages: int = 12,
        message_limit: int = 80,
    ) -> JsonDict:
        """摘要单个聊天会话；消息不足或模型不可用时返回可审计状态。"""

        session = self.store.get_session(owner_id=owner_id, chat_session_id=chat_session_id)
        if session is None:
            return {
                "status": "skipped",
                "reason": "session_not_found",
                "owner_id": owner_id,
                "chat_session_id": chat_session_id,
            }
        messages = list(
            self.store.list_messages(
                owner_id=owner_id,
                chat_session_id=chat_session_id,
                limit=message_limit,
            )
        )
        message_count = len(messages)
        if message_count < min_messages:
            return {
                "status": "skipped",
                "reason": "message_count_below_threshold",
                "owner_id": owner_id,
                "chat_session_id": chat_session_id,
                "message_count": message_count,
                "min_messages": min_messages,
            }

        config = select_chat_model_config(self.model_registry)
        if config is None:
            return {
                "status": "unavailable",
                "reason": "model_not_ready",
                "owner_id": owner_id,
                "chat_session_id": chat_session_id,
                "message_count": message_count,
            }

        prompt_messages = build_chat_summary_messages(
            owner_id=owner_id,
            chat_session_id=chat_session_id,
            messages=messages,
        )
        parsed = self._invoke_and_parse(config=config, messages=prompt_messages)
        if parsed is None:
            retry_messages = [
                *prompt_messages,
                {
                    "role": "user",
                    "content": (
                        "上次输出不是合法 JSON 摘要。请严格返回包含 summary、key_topics、"
                        "follow_up_items、user_preferences 的 JSON。"
                    ),
                },
            ]
            parsed = self._invoke_and_parse(config=config, messages=retry_messages)
        if parsed is None:
            return {
                "status": "unavailable",
                "reason": "invalid_model_json",
                "owner_id": owner_id,
                "chat_session_id": chat_session_id,
                "message_count": message_count,
            }

        summary = str(parsed["summary"])
        payload = {
            **parsed,
            "owner_id": owner_id,
            "chat_session_id": chat_session_id,
            "message_count": message_count,
            "model_key": config.model_key,
            "memory_type": "chat_summary",
        }
        memory_id = build_chat_summary_memory_id(
            owner_id=owner_id,
            chat_session_id=chat_session_id,
        )
        self.store.update_session_summary(
            owner_id=owner_id,
            chat_session_id=chat_session_id,
            summary=summary,
            payload=payload,
        )
        persisted_memory_id = self.store.upsert_summary_memory(
            memory_id=memory_id,
            owner_id=owner_id,
            chat_session_id=chat_session_id,
            summary=summary,
            payload=payload,
        )
        return {
            "status": "available",
            "owner_id": owner_id,
            "chat_session_id": chat_session_id,
            "message_count": message_count,
            "summary": summary,
            "memory_id": persisted_memory_id,
            "key_topics": parsed["key_topics"],
            "follow_up_items": parsed["follow_up_items"],
            "user_preferences": parsed["user_preferences"],
        }

    def _invoke_and_parse(
        self,
        *,
        config: Any,
        messages: list[JsonDict],
    ) -> JsonDict | None:
        response = self.model_client.invoke_json(
            config=config,
            messages=messages,
            temperature=0.0,
        )
        return normalize_chat_summary_payload(response.parsed_json)


def build_chat_summary_messages(
    *,
    owner_id: str,
    chat_session_id: str,
    messages: Sequence[Any],
) -> list[JsonDict]:
    """构建聊天摘要模型提示。"""

    recent_messages = [
        {
            "sequence_no": int(getattr(message, "sequence_no", index + 1)),
            "role": str(getattr(message, "role", "")),
            "intent": getattr(message, "intent", None),
            "content": str(getattr(message, "content", ""))[:2000],
        }
        for index, message in enumerate(messages)
    ]
    return [
        {
            "role": "system",
            "content": (
                "你是私人金融助手的聊天摘要员。只能基于给定聊天消息生成摘要，"
                "必须输出严格 JSON，不得补充未出现的事实。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "owner_id": owner_id,
                    "chat_session_id": chat_session_id,
                    "messages": recent_messages,
                    "output_schema": {
                        "summary": "一段中文摘要",
                        "key_topics": ["主题"],
                        "follow_up_items": ["后续待跟进事项"],
                        "user_preferences": ["用户偏好"],
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
    ]


def normalize_chat_summary_payload(payload: JsonDict | None) -> JsonDict | None:
    """校验并规范化模型返回的聊天摘要 JSON。"""

    if not isinstance(payload, dict):
        return None
    summary = str(payload.get("summary") or "").strip()
    if not summary:
        return None
    return {
        "summary": summary,
        "key_topics": normalize_string_list(payload.get("key_topics")),
        "follow_up_items": normalize_string_list(payload.get("follow_up_items")),
        "user_preferences": normalize_string_list(payload.get("user_preferences")),
    }


def normalize_string_list(value: Any) -> list[str]:
    """把模型返回的列表字段规范化为非空字符串数组。"""

    if not isinstance(value, list | tuple):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def build_chat_summary_memory_id(*, owner_id: str, chat_session_id: str) -> str:
    """构造稳定的聊天摘要记忆 ID，避免长会话 ID 超过主键长度。"""

    digest = sha1(f"{owner_id}:{chat_session_id}".encode()).hexdigest()[:24]
    return f"memory:chat_summary:{digest}"
