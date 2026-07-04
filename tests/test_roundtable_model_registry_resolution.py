from __future__ import annotations

from typing import Any

from finance_agent.agents.runtime.model_config import ModelEndpointConfig, ModelRegistry
from finance_agent.agents.workflows import langgraph_graphs


def test_resolve_roundtable_model_registry_prefers_current_session_repository(
    monkeypatch,
) -> None:
    """真实 Workflow 应复用当前 session 的模型配置表，避免只记录路由但无法调用模型。"""

    session = object()
    repository = object()
    calls: list[Any] = []

    def fake_repository_factory(bound_session: object) -> object:
        calls.append(bound_session)
        return repository

    monkeypatch.setattr(
        langgraph_graphs,
        "ModelRuntimeConfigRepository",
        fake_repository_factory,
    )
    monkeypatch.setattr(
        langgraph_graphs,
        "build_model_configs_from_repository",
        lambda repo: {"deepseek-v4-pro": build_ready_model_config()},
        raising=False,
    )
    monkeypatch.setattr(
        langgraph_graphs,
        "build_retrieval_profiles_from_repository",
        lambda repo: {"default": {"usage_scope": "workflow"}},
        raising=False,
    )
    monkeypatch.setattr(
        langgraph_graphs,
        "load_model_registry",
        lambda: ModelRegistry(models={}, source="env"),
    )

    registry = langgraph_graphs.resolve_roundtable_model_registry({"session": session})

    assert calls == [session]
    assert registry.source == "database-session"
    assert registry.get("deepseek-v4-pro").ready is True
    assert registry.retrieval_profiles == {"default": {"usage_scope": "workflow"}}


def build_ready_model_config() -> ModelEndpointConfig:
    return ModelEndpointConfig(
        model_key="deepseek-v4-pro",
        provider="deepseek",
        model_name="DeepSeek V4 Pro",
        base_url="https://api.deepseek.com/v1",
        api_key="test-key",
        role="primary_financial_analyst",
    )
