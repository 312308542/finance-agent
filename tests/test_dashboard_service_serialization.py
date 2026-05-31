from types import SimpleNamespace

from finance_agent.application.dashboard_service import serialize_model_provider


def test_serialize_model_provider_marks_api_key_configured() -> None:
    """模型供应商序列化时只返回脱敏密钥，同时保留是否已配置标记。"""

    provider = SimpleNamespace(
        provider_key="openai-compatible",
        provider_vendor="openai_compatible",
        provider_name="OpenAI 兼容接入",
        base_url="https://proxy.example.com/v1",
        api_key="test-api-key-live",
        timeout_seconds=30,
        is_enabled=True,
        is_default=True,
        updated_at=None,
    )

    data = serialize_model_provider(provider)

    assert data["api_key"] == "test***live"
    assert data["api_key_configured"] is True


def test_serialize_model_provider_marks_empty_api_key_unconfigured() -> None:
    """未保存密钥时前端不能把脱敏占位符误认为已经配置。"""

    provider = SimpleNamespace(
        provider_key="openai-compatible",
        provider_vendor="openai_compatible",
        provider_name="OpenAI 兼容接入",
        base_url="https://proxy.example.com/v1",
        api_key=None,
        timeout_seconds=30,
        is_enabled=True,
        is_default=True,
        updated_at=None,
    )

    data = serialize_model_provider(provider)

    assert data["api_key"] is None
    assert data["api_key_configured"] is False
