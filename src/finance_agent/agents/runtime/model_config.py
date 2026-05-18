"""模型配置、路由预览和本地连通测试。

当前模块不接 Hermes，也不把模型调用写进业务 Workflow。它只为 CLI/TUI
提供可验证的模型配置入口，后续真实模型客户端可以复用这里的配置结构。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import requests

from finance_agent.agents.runtime.model_router import ModelRoute, ModelRoutingPolicy

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class ModelEndpointConfig:
    """单个模型的本地调用配置。"""

    model_key: str
    provider: str
    model_name: str
    base_url: str | None = None
    api_key: str | None = None
    role: str | None = None
    enabled: bool = True
    timeout_seconds: float = 30.0

    @property
    def ready(self) -> bool:
        """判断模型是否具备最基本的调用配置。"""

        return bool(self.enabled and self.model_name and self.base_url and self.api_key)

    def to_safe_dict(self) -> JsonDict:
        """输出脱敏后的配置，避免 CLI 泄露密钥。"""

        data = asdict(self)
        data["api_key"] = mask_secret(self.api_key)
        data["ready"] = self.ready
        return data


@dataclass(frozen=True)
class ModelRegistry:
    """模型配置注册表。"""

    models: dict[str, ModelEndpointConfig]
    source: str

    def get(self, model_key: str) -> ModelEndpointConfig | None:
        """按模型 key 读取配置。"""

        return self.models.get(model_key)

    def to_safe_dict(self) -> JsonDict:
        """输出脱敏后的模型注册表。"""

        return {
            "source": self.source,
            "models": {
                model_key: config.to_safe_dict()
                for model_key, config in sorted(self.models.items())
            },
        }


def load_model_registry(config_file: str | None = None) -> ModelRegistry:
    """从 JSON 文件或环境变量加载模型配置。"""

    resolved_file = config_file or os.getenv("FINANCE_AGENT_MODELS_CONFIG_FILE")
    if resolved_file:
        return load_model_registry_from_file(Path(resolved_file))
    return load_model_registry_from_env()


def load_model_registry_from_file(path: Path) -> ModelRegistry:
    """从 JSON 文件加载模型配置。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    models_payload = payload.get("models", payload)
    if not isinstance(models_payload, dict):
        raise ValueError("模型配置文件必须包含 models 对象。")
    return ModelRegistry(
        models={
            model_key: build_model_config(model_key, config)
            for model_key, config in models_payload.items()
        },
        source=str(path),
    )


def load_model_registry_from_env() -> ModelRegistry:
    """从环境变量加载默认双模型配置。"""

    models = {
        "deepseek-v4-pro": ModelEndpointConfig(
            model_key="deepseek-v4-pro",
            provider="deepseek",
            model_name=os.getenv("FINANCE_AGENT_DEEPSEEK_MODEL", "DeepSeek V4 Pro"),
            base_url=os.getenv("FINANCE_AGENT_DEEPSEEK_BASE_URL"),
            api_key=os.getenv("FINANCE_AGENT_DEEPSEEK_API_KEY"),
            role="primary_financial_analyst",
            enabled=parse_bool(os.getenv("FINANCE_AGENT_DEEPSEEK_ENABLED"), default=True),
        ),
        "gpt-5.5-pro": ModelEndpointConfig(
            model_key="gpt-5.5-pro",
            provider="openai",
            model_name=os.getenv("FINANCE_AGENT_OPENAI_MODEL", "GPT-5.5 Pro"),
            base_url=os.getenv("FINANCE_AGENT_OPENAI_BASE_URL"),
            api_key=os.getenv("FINANCE_AGENT_OPENAI_API_KEY"),
            role="high_risk_reviewer",
            enabled=parse_bool(os.getenv("FINANCE_AGENT_OPENAI_ENABLED"), default=True),
        ),
    }
    return ModelRegistry(models=models, source="env")


def build_model_config(model_key: str, payload: object) -> ModelEndpointConfig:
    """把配置字典转换为模型配置对象。"""

    if not isinstance(payload, dict):
        raise ValueError(f"模型配置必须是对象：{model_key}")
    return ModelEndpointConfig(
        model_key=model_key,
        provider=str(payload.get("provider") or infer_provider(model_key)),
        model_name=str(payload.get("model_name") or payload.get("model") or model_key),
        base_url=payload.get("base_url"),
        api_key=payload.get("api_key"),
        role=payload.get("role"),
        enabled=parse_bool(payload.get("enabled"), default=True),
        timeout_seconds=float(payload.get("timeout_seconds") or 30.0),
    )


def preview_model_routes(
    *,
    registry: ModelRegistry,
    workflow_type: str,
    task: str,
    asset_id: str | None = None,
    decision_type: str | None = None,
    high_risk: bool = False,
) -> list[JsonDict]:
    """预览当前 Workflow 会路由到哪些模型。"""

    policy = ModelRoutingPolicy()
    routes: list[ModelRoute] = [
        policy.route_primary(
            workflow_type=workflow_type,
            task=task,
            asset_id=asset_id,
            decision_type=decision_type,
        )
    ]
    if high_risk:
        routes.append(
            policy.route_high_risk_review(
                workflow_type=workflow_type,
                asset_id=asset_id or "",
                decision_type=decision_type or "high_risk_review",
                reason="CLI 路由预览指定 high-risk，展示 GPT-5.5 Pro 复核路径。",
            )
        )
    return [attach_config_status(route, registry) for route in routes]


def attach_config_status(route: ModelRoute, registry: ModelRegistry) -> JsonDict:
    """给模型路由附加配置状态。"""

    data = route.to_dict()
    config = registry.get(route.model_key)
    data["configured"] = config is not None
    data["ready"] = bool(config and config.ready)
    data["config_source"] = registry.source
    if config:
        data["endpoint"] = {
            "provider": config.provider,
            "base_url": config.base_url,
            "enabled": config.enabled,
            "role": config.role,
            "api_key": mask_secret(config.api_key),
        }
    return data


def build_chat_completion_request(config: ModelEndpointConfig, prompt: str) -> JsonDict:
    """构建 OpenAI-compatible Chat Completions 请求预览。"""

    if not config.base_url:
        raise ValueError(f"模型 {config.model_key} 缺少 base_url。")
    return {
        "url": f"{config.base_url.rstrip('/')}/chat/completions",
        "model": config.model_name,
        "messages": [
            {
                "role": "system",
                "content": "你是私人金融助手的模型连通性测试节点，只输出简短中文。",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }


def test_model_endpoint(
    *,
    registry: ModelRegistry,
    model_key: str,
    prompt: str,
    dry_run: bool = True,
    timeout_seconds: float | None = None,
) -> JsonDict:
    """测试模型配置；默认 dry-run，不发起真实请求。"""

    config = registry.get(model_key)
    if config is None:
        raise ValueError(f"未找到模型配置：{model_key}")
    request_payload = build_chat_completion_request(config, prompt)
    result: JsonDict = {
        "model_key": model_key,
        "provider": config.provider,
        "ready": config.ready,
        "dry_run": dry_run,
        "request": request_payload,
        "config_source": registry.source,
    }
    if dry_run:
        result["status"] = "dry_run_ready"
        return result
    if not config.ready:
        raise ValueError(f"模型 {model_key} 未配置 base_url/api_key，不能真实请求。")

    response = requests.post(
        request_payload["url"],
        headers={"Authorization": f"Bearer {config.api_key}"},
        json={
            "model": request_payload["model"],
            "messages": request_payload["messages"],
            "temperature": request_payload["temperature"],
        },
        timeout=timeout_seconds or config.timeout_seconds,
    )
    result["http_status"] = response.status_code
    result["ok"] = response.ok
    result["response_preview"] = response.text[:500]
    return result


def mask_secret(secret: str | None) -> str | None:
    """对密钥做短显示脱敏。"""

    if not secret:
        return None
    if len(secret) <= 6:
        return "***"
    return f"{secret[:3]}...{secret[-3:]}"


def parse_bool(value: object, *, default: bool) -> bool:
    """解析布尔配置。"""

    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def infer_provider(model_key: str) -> str:
    """根据模型 key 推断 provider。"""

    if model_key.startswith("deepseek"):
        return "deepseek"
    if model_key.startswith("gpt"):
        return "openai"
    return "openai_compatible"
