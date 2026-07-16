from finance_agent.agents.runtime.model_config import (
    ModelEndpointConfig,
    ModelRegistry,
)
from finance_agent.agents.runtime.model_config import (
    test_model_endpoint as run_model_endpoint_test,
)


class _HtmlResponse:
    status_code = 200
    ok = True
    text = "<!doctype html><html><body>gateway console</body></html>"

    def json(self) -> dict[str, object]:
        raise ValueError("response is not json")


def test_model_endpoint_rejects_html_200_response(monkeypatch) -> None:
    """CLI 真实连通测试必须校验 Chat Completions JSON，而不只看 HTTP 200。"""

    monkeypatch.setattr(
        "finance_agent.agents.runtime.model_config.requests.post",
        lambda *_args, **_kwargs: _HtmlResponse(),
    )
    registry = ModelRegistry(
        source="test",
        models={
            "gpt-5.5": ModelEndpointConfig(
                model_key="gpt-5.5",
                provider="openai_compatible",
                model_name="gpt-5.5",
                base_url="https://gateway.example.com",
                api_key="test-api-key",
            )
        },
    )

    result = run_model_endpoint_test(
        registry=registry,
        model_key="gpt-5.5",
        prompt="ping",
        dry_run=False,
    )

    assert result["http_status"] == 200
    assert result["ok"] is False
    assert result["response_format_valid"] is False
    assert result["error"] == "模型端点未返回 OpenAI-compatible JSON。"
