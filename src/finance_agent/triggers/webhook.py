"""Hermes Webhook 发布器。

该模块只负责把已经通过程序触发规则的事件发送给 Hermes，不负责阈值判断、
Workflow 选择或交易决策。Webhook 不可用时由调用方保留事件并安排重试。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Any

import requests

from finance_agent.triggers.service import serialize_trigger_event


class HermesWebhookError(RuntimeError):
    """Hermes Webhook 请求失败。"""


@dataclass(frozen=True)
class HermesWebhookPublisher:
    """向受控 Hermes 路由发布触发事件。"""

    url: str
    secret: str
    timeout_seconds: float = 10.0
    session: Any = requests

    def publish(self, event: Any) -> None:
        """发送一次带 HMAC-SHA256 签名的事件，非 2xx 均视为失败。"""

        payload = {
            "event_type": "finance_agent_trigger",
            "source": "finance-agent",
            "payload": serialize_trigger_event(event),
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        signature = "sha256=" + hmac.new(
            self.secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        try:
            response = self.session.post(
                self.url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": signature,
                    "X-Finance-Agent-Event": event.trigger_event_id,
                },
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except Exception as exc:  # requests 异常和测试替身类型不同，统一转换
            raise HermesWebhookError(f"Hermes Webhook 请求失败: {exc}") from exc

    @classmethod
    def from_environment(cls) -> HermesWebhookPublisher | None:
        """从环境变量构造发布器；未配置 URL 或密钥时返回空。"""

        url = os.getenv("FINANCE_AGENT_HERMES_WEBHOOK_URL", "").strip()
        secret = os.getenv("FINANCE_AGENT_HERMES_WEBHOOK_SECRET", "").strip()
        if not url or not secret:
            return None
        try:
            timeout = float(os.getenv("FINANCE_AGENT_HERMES_WEBHOOK_TIMEOUT_SECONDS", "10"))
        except ValueError:
            timeout = 10.0
        return cls(url=url, secret=secret, timeout_seconds=max(timeout, 1.0))
