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

    def to_audit_dict(self) -> JsonDict:
        """输出适合审计落库的脱敏摘要。"""

        return {
            "model_key": self.model_key,
            "provider": self.provider,
            "model_name": self.model_name,
            "content_preview": self.content[:1000],
            "parsed_json": self.parsed_json,
            "raw_keys": sorted(self.raw_response),
        }


class OpenAICompatibleModelClient:
    """调用 OpenAI-compatible Chat Completions 接口。"""

    def invoke_json(
        self,
        *,
        config: ModelEndpointConfig,
        messages: list[JsonDict],
        temperature: float = 0.1,
    ) -> ModelClientResponse:
        """发送聊天补全请求并尝试解析 JSON 输出。"""

        if not config.ready:
            raise ValueError(f"模型 {config.model_key} 未配置 base_url/api_key，不能真实调用。")
        url = f"{str(config.base_url).rstrip('/')}/chat/completions"
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {config.api_key}"},
            json={
                "model": config.model_name,
                "messages": messages,
                "temperature": temperature,
            },
            timeout=config.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        content = extract_chat_content(payload)
        return ModelClientResponse(
            model_key=config.model_key,
            provider=config.provider,
            model_name=config.model_name,
            content=content,
            parsed_json=extract_json_object(content),
            raw_response=payload,
        )


def extract_chat_content(payload: JsonDict) -> str:
    """从 Chat Completions 响应中提取 assistant 文本。"""

    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message") or {}
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
    text = first.get("text")
    return text if isinstance(text, str) else ""


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
