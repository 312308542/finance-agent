from finance_agent.agents.runtime.model_client import (
    DeepSeekToolCallProtocolStrategy,
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
