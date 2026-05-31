from finance_agent.api.routes import run_model_provider_connectivity_test


class _FakeResponse:
    status_code = 200
    ok = True
    text = '{"choices":[{"message":{"content":"pong"}}]}'


def test_model_provider_connectivity_posts_minimal_chat_request(monkeypatch) -> None:
    """连通性测试只发送最小 Chat Completions 请求，并且不会把密钥写入返回值。"""

    captured: dict[str, object] = {}

    def fake_post(url: str, **kwargs: object) -> _FakeResponse:
        captured["url"] = url
        captured.update(kwargs)
        return _FakeResponse()

    monkeypatch.setattr("finance_agent.api.routes.requests.post", fake_post)

    result = run_model_provider_connectivity_test(
        provider_key="openai-compatible:gpt-4.1",
        model_key="gpt-4.1",
        model_name="GPT-4.1",
        base_url="https://api.example.com/v1",
        api_key="test-api-key-live",
        timeout_seconds=12,
    )

    assert result["status"] == "ok"
    assert result["data"]["ready"] is True
    assert result["data"]["endpoint_url"] == "https://api.example.com/v1/chat/completions"
    assert captured["url"] == "https://api.example.com/v1/chat/completions"
    assert captured["headers"] == {"Authorization": "Bearer test-api-key-live"}
    assert captured["timeout"] == 12
    assert captured["json"] == {
        "model": "GPT-4.1",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
    }
    assert "test-api-key-live" not in str(result)


def test_model_provider_connectivity_requires_real_secret() -> None:
    """未解析到真实 API Key 时直接返回错误，不发起外部请求。"""

    result = run_model_provider_connectivity_test(
        provider_key="openai-compatible:gpt-4.1",
        model_key="gpt-4.1",
        model_name="GPT-4.1",
        base_url="https://api.example.com/v1",
        api_key=None,
        timeout_seconds=12,
    )

    assert result["status"] == "error"
    assert result["data"]["ready"] is False
    assert "API Key" in result["message"]
