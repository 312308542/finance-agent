import requests

from finance_agent.agents.runtime.model_client import (
    DeepSeekToolCallProtocolStrategy,
    OpenAICompatibleModelClient,
    OpenAIToolCallProtocolStrategy,
    resolve_tool_call_protocol_strategy,
)
from finance_agent.agents.runtime.model_config import ModelEndpointConfig


def test_openai_strategy_drops_deepseek_reasoning_content() -> None:
    """OpenAI 策略只回传标准 Chat Completions assistant 字段。"""

    message = {
        "role": "assistant",
        "content": None,
        "reasoning_content": "供应商私有思考字段",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "recommendation_get_latest", "arguments": "{}"},
            }
        ],
    }

    normalized = OpenAIToolCallProtocolStrategy().normalize_assistant_message(message)

    assert normalized == {
        "role": "assistant",
        "content": None,
        "tool_calls": message["tool_calls"],
    }


def test_deepseek_strategy_preserves_reasoning_content_for_next_turn() -> None:
    """DeepSeek thinking mode 二轮请求必须原样带回 reasoning_content。"""

    message = {
        "role": "assistant",
        "content": None,
        "reasoning_content": "必须回传的 thinking 内容",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "recommendation_get_latest", "arguments": "{}"},
            }
        ],
    }

    normalized = DeepSeekToolCallProtocolStrategy().normalize_assistant_message(message)

    assert normalized["reasoning_content"] == "必须回传的 thinking 内容"
    assert normalized["tool_calls"] == message["tool_calls"]


def test_resolve_tool_call_protocol_strategy_uses_provider() -> None:
    """模型 provider 决定工具协议策略。"""

    deepseek_config = ModelEndpointConfig(
        model_key="deepseek-v4-pro",
        provider="deepseek",
        model_name="deepseek-chat",
        base_url="https://api.deepseek.com",
        api_key="test",
    )
    openai_config = ModelEndpointConfig(
        model_key="gpt-4.1",
        provider="openai",
        model_name="gpt-4.1",
        base_url="https://api.openai.com/v1",
        api_key="test",
    )

    assert isinstance(
        resolve_tool_call_protocol_strategy(deepseek_config),
        DeepSeekToolCallProtocolStrategy,
    )
    assert isinstance(
        resolve_tool_call_protocol_strategy(openai_config),
        OpenAIToolCallProtocolStrategy,
    )


def test_openai_compatible_model_client_retries_transient_http_errors(
    monkeypatch,
) -> None:
    """模型端点临时连接超时时会重试，而不是立即终止 Agent 工具循环。"""

    calls: list[dict[str, object]] = []

    class _FakeResponse:
        ok = True
        status_code = 200
        text = '{"choices":[{"message":{"content":"完成"}}]}'

        def json(self) -> dict[str, object]:
            return {"choices": [{"message": {"content": "完成"}, "finish_reason": "stop"}]}

    def fake_post(url: str, **kwargs: object) -> _FakeResponse:
        calls.append({"url": url, **kwargs})
        if len(calls) == 1:
            raise requests.exceptions.ConnectTimeout("connect timed out")
        return _FakeResponse()

    monkeypatch.setenv("FINANCE_AGENT_MODEL_HTTP_RETRIES", "1")
    monkeypatch.setenv("FINANCE_AGENT_MODEL_HTTP_RETRY_BACKOFF_SECONDS", "0")
    monkeypatch.setattr(
        "finance_agent.agents.runtime.model_client.requests.post",
        fake_post,
    )

    config = ModelEndpointConfig(
        model_key="deepseek-chat",
        provider="deepseek",
        model_name="deepseek-chat",
        base_url="https://api.deepseek.com",
        api_key="test-api-key",
        timeout_seconds=12,
    )

    response = OpenAICompatibleModelClient().invoke_json(
        config=config,
        messages=[{"role": "user", "content": "ping"}],
    )

    assert len(calls) == 2
    assert calls[0]["timeout"] == 12
    assert calls[1]["url"] == "https://api.deepseek.com/chat/completions"
    assert response.content == "完成"
