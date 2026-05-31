"""OpenAI-compatible 模型客户端。

本模块只负责发起模型请求、解析响应和提取 JSON。金融决策边界、工具白名单、
Workflow 白名单和 fallback 策略仍由 Agent Loop / Workflow 层负责。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

import requests

from finance_agent.agents.runtime.model_config import ModelEndpointConfig

JsonDict = dict[str, Any]


class ModelClient(Protocol):
    """模型客户端协议，便于 smoke 中注入假的模型响应。"""

    def invoke_json(
        self,
        *,
        config: ModelEndpointConfig,
        messages: list[JsonDict],
        tools: list[JsonDict] | None = None,
        tool_choice: str | JsonDict | None = None,
        temperature: float = 0.1,
    ) -> ModelClientResponse:
        """调用模型并返回 JSON 解析结果。"""


@dataclass(frozen=True)
class ModelClientResponse:
    """一次模型调用的结构化响应。"""

    model_key: str
    provider: str
    model_name: str
    content: str
    parsed_json: JsonDict | None
    raw_response: JsonDict
    assistant_message: JsonDict | None = None
    tool_calls: tuple[JsonDict, ...] = ()
    finish_reason: str | None = None

    def to_audit_dict(self) -> JsonDict:
        """输出适合审计落库的脱敏摘要。"""

        return {
            "model_key": self.model_key,
            "provider": self.provider,
            "model_name": self.model_name,
            "content_preview": self.content[:1000],
            "parsed_json": self.parsed_json,
            "finish_reason": self.finish_reason,
            "tool_call_count": len(self.tool_calls),
            "tool_call_names": [
                str((call.get("function") or {}).get("name") or "")
                for call in self.tool_calls
                if isinstance(call, dict)
            ],
            "raw_keys": sorted(self.raw_response),
        }


class ToolCallProtocolStrategy:
    """模型供应商工具调用协议策略。"""

    name = "openai"
    assistant_message_keys = ("role", "content", "tool_calls", "function_call")

    def build_request_payload(
        self,
        *,
        config: ModelEndpointConfig,
        messages: list[JsonDict],
        tools: list[JsonDict] | None,
        tool_choice: str | JsonDict | None,
        temperature: float,
    ) -> JsonDict:
        """构造 Chat Completions 请求体。"""

        request_payload: JsonDict = {
            "model": config.model_name,
            "messages": messages,
            "temperature": temperature,
        }
        if tools is not None:
            request_payload["tools"] = tools
        if tool_choice is not None:
            request_payload["tool_choice"] = tool_choice
        return request_payload

    def normalize_assistant_message(self, message: JsonDict) -> JsonDict:
        """保留下一轮请求可安全回传的 assistant 字段。"""

        return {
            key: value
            for key, value in message.items()
            if key in self.assistant_message_keys
        }

    def extract_assistant_message(self, payload: JsonDict) -> JsonDict | None:
        """从响应中提取并规范化 assistant message。"""

        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        first = choices[0]
        if not isinstance(first, dict):
            return None
        message = first.get("message")
        if not isinstance(message, dict):
            return None
        return self.normalize_assistant_message(message)

    def extract_content(self, payload: JsonDict) -> str:
        """从响应中提取 assistant 文本。"""

        message = self.extract_assistant_message(payload) or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
            return "".join(parts)
        choices = payload.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else {}
        text = first.get("text") if isinstance(first, dict) else None
        return text if isinstance(text, str) else ""

    def extract_tool_calls(self, payload: JsonDict) -> tuple[JsonDict, ...]:
        """从 assistant message 中提取工具调用。"""

        message = self.extract_assistant_message(payload) or {}
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            return ()
        return tuple(call for call in tool_calls if isinstance(call, dict))


class OpenAIToolCallProtocolStrategy(ToolCallProtocolStrategy):
    """OpenAI Chat Completions 工具调用协议。"""

    name = "openai"


class DeepSeekToolCallProtocolStrategy(OpenAIToolCallProtocolStrategy):
    """DeepSeek Chat Completions 工具调用协议。"""

    name = "deepseek"
    assistant_message_keys = (
        "role",
        "content",
        "tool_calls",
        "function_call",
        # DeepSeek thinking mode 要求二轮请求原样带回该字段。
        "reasoning_content",
    )


def resolve_tool_call_protocol_strategy(config: ModelEndpointConfig) -> ToolCallProtocolStrategy:
    """根据模型配置选择供应商工具调用协议策略。"""

    provider = str(config.provider or "").lower()
    provider_key = str(config.provider_key or "").lower()
    model_key = str(config.model_key or "").lower()
    model_name = str(config.model_name or "").lower()
    if "deepseek" in {provider, provider_key} or any(
        "deepseek" in value for value in (model_key, model_name, provider_key)
    ):
        return DeepSeekToolCallProtocolStrategy()
    return OpenAIToolCallProtocolStrategy()


class OpenAICompatibleModelClient:
    """调用 OpenAI-compatible Chat Completions 接口。"""

    def invoke_json(
        self,
        *,
        config: ModelEndpointConfig,
        messages: list[JsonDict],
        tools: list[JsonDict] | None = None,
        tool_choice: str | JsonDict | None = None,
        temperature: float = 0.1,
    ) -> ModelClientResponse:
        """发送聊天补全请求并尝试解析 JSON 输出。"""

        if not config.ready:
            raise ValueError(f"模型 {config.model_key} 未配置 base_url/api_key，不能真实调用。")
        url = f"{str(config.base_url).rstrip('/')}/chat/completions"
        strategy = resolve_tool_call_protocol_strategy(config)
        request_payload = strategy.build_request_payload(
            config=config,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
        )
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {config.api_key}"},
            json=request_payload,
            timeout=config.timeout_seconds,
        )
        if not response.ok:
            raise RuntimeError(
                f"模型接口请求失败：HTTP {response.status_code}，"
                f"响应摘要：{response.text[:1000]}"
            )
        payload = response.json()
        assistant_message = strategy.extract_assistant_message(payload)
        content = strategy.extract_content(payload)
        tool_calls = strategy.extract_tool_calls(payload)
        return ModelClientResponse(
            model_key=config.model_key,
            provider=config.provider,
            model_name=config.model_name,
            content=content,
            parsed_json=extract_json_object(content),
            raw_response=payload,
            assistant_message=assistant_message,
            tool_calls=tool_calls,
            finish_reason=extract_finish_reason(payload),
        )


def extract_chat_message(payload: JsonDict) -> JsonDict | None:
    """从 Chat Completions 响应中提取 assistant message。"""

    return OpenAIToolCallProtocolStrategy().extract_assistant_message(payload)


def normalize_assistant_protocol_message(message: JsonDict) -> JsonDict:
    """保留可在下一轮 Chat Completions 请求中合法回传的 assistant 字段。"""

    return OpenAIToolCallProtocolStrategy().normalize_assistant_message(message)


def extract_chat_content(payload: JsonDict) -> str:
    """从 Chat Completions 响应中提取 assistant 文本。"""

    return OpenAIToolCallProtocolStrategy().extract_content(payload)


def extract_chat_tool_calls(payload: JsonDict) -> tuple[JsonDict, ...]:
    """从 Chat Completions assistant message 中提取工具调用。"""

    return OpenAIToolCallProtocolStrategy().extract_tool_calls(payload)


def extract_finish_reason(payload: JsonDict) -> str | None:
    """提取 Chat Completions 的 finish_reason。"""

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    reason = first.get("finish_reason")
    return reason if isinstance(reason, str) else None


def extract_json_object(content: str) -> JsonDict | None:
    """从模型输出中提取第一个 JSON 对象。

    支持纯 JSON、Markdown fenced JSON 和前后带解释文字的输出。解析失败返回空，
    由上层 planner 触发 fallback 或再次请求。
    """

    text = content.strip()
    if not text:
        return None
    if text.startswith("```"):
        text = strip_markdown_fence(text)
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def strip_markdown_fence(text: str) -> str:
    """移除模型常见的 Markdown JSON 代码块包裹。"""

    lines = text.strip().splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()
