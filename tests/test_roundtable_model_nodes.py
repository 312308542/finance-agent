from __future__ import annotations

from typing import Any

from finance_agent.agents.runtime.model_client import ModelClientResponse
from finance_agent.agents.runtime.model_config import ModelEndpointConfig
from finance_agent.agents.workflows.roundtable_model_nodes import (
    RoundtableOpinionRequest,
    generate_model_opinion,
)


class FakeModelClient:
    """记录圆桌模型节点的调用，并按顺序返回预设响应。"""

    def __init__(self, responses: list[ModelClientResponse | Exception]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def invoke_json(
        self,
        *,
        config: ModelEndpointConfig,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        temperature: float = 0.1,
    ) -> ModelClientResponse:
        self.calls.append(
            {
                "config": config,
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
                "temperature": temperature,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_generate_model_opinion_accepts_valid_json_and_marks_model() -> None:
    client = FakeModelClient(
        [
            model_response(
                {
                    "role": "technical_analyst",
                    "stance": "bullish",
                    "confidence": 0.72,
                    "summary": "趋势结构偏强，但需要观察量能延续。",
                    "key_points": ["MA20 上穿 MA60，证据 ev:ma-cross 支持。"],
                    "rebuttals": ["若量能无法放大，突破可能失效。"],
                    "evidence_ids": ["ev:ma-cross"],
                    "data_gaps": [],
                }
            )
        ]
    )

    opinion = generate_model_opinion(
        request=build_request(role="technical_analyst"),
        model_client=client,
        model_config=build_model_config(),
    )

    assert opinion["generated_by"] == "model"
    assert opinion["model_instance_id"] == "deepseek-v4-pro"
    assert opinion["role"] == "technical_analyst"
    assert opinion["stance"] == "bullish"
    assert opinion["evidence_ids"] == ["ev:ma-cross"]
    assert client.calls[0]["temperature"] == 0.0


def test_generate_model_opinion_retries_once_when_json_is_invalid() -> None:
    client = FakeModelClient(
        [
            ModelClientResponse(
                model_key="deepseek-v4-pro",
                provider="deepseek",
                model_name="DeepSeek V4 Pro",
                content="不是 JSON",
                parsed_json=None,
                raw_response={"id": "bad-json"},
            ),
            model_response(
                {
                    "role": "factor_analyst",
                    "stance": "neutral",
                    "confidence": 0.51,
                    "summary": "因子分歧较大，暂不支持强方向判断。",
                    "key_points": ["质量因子改善，但估值证据 ev:valuation 偏弱。"],
                    "rebuttals": ["若估值继续扩张，安全边际会下降。"],
                    "evidence_ids": ["ev:valuation"],
                    "data_gaps": [],
                }
            ),
        ]
    )

    opinion = generate_model_opinion(
        request=build_request(role="factor_analyst", allowed_evidence_ids=["ev:valuation"]),
        model_client=client,
        model_config=build_model_config(),
    )

    assert opinion["generated_by"] == "model"
    assert len(client.calls) == 2
    assert "上次输出不是合法 JSON" in client.calls[1]["messages"][-1]["content"]


def test_generate_model_opinion_removes_hallucinated_evidence_ids() -> None:
    client = FakeModelClient(
        [
            model_response(
                {
                    "role": "risk_rebuttal",
                    "stance": "bearish",
                    "confidence": 0.81,
                    "summary": "风险证据强，需要降低动作置信度。",
                    "key_points": ["风险记录 ev:risk-ok 显示事件未解除。"],
                    "rebuttals": ["若事件澄清，可重新评估。"],
                    "evidence_ids": ["ev:risk-ok", "ev:made-up"],
                    "data_gaps": ["缺少最新公告原文。"],
                }
            )
        ]
    )

    opinion = generate_model_opinion(
        request=build_request(role="risk_rebuttal", allowed_evidence_ids=["ev:risk-ok"]),
        model_client=client,
        model_config=build_model_config(),
    )

    assert opinion["evidence_ids"] == ["ev:risk-ok"]
    assert any("ev:made-up" in item for item in opinion["data_gaps"])


def test_generate_model_opinion_returns_fallback_when_model_unavailable() -> None:
    fallback = {
        "role": "portfolio_manager",
        "asset_id": "ashare:600519",
        "stance": "coordinate",
        "summary": "规则版组合经理观点。",
        "evidence_ids": [],
    }
    client = FakeModelClient([RuntimeError("connect timeout")])

    opinion = generate_model_opinion(
        request=build_request(
            role="portfolio_manager",
            fallback_opinion=fallback,
            allowed_evidence_ids=[],
        ),
        model_client=client,
        model_config=build_model_config(),
    )

    assert opinion["generated_by"] == "fallback"
    assert opinion["summary"] == "规则版组合经理观点。"
    assert opinion["model_error"] == "connect timeout"


def test_generate_model_opinion_keeps_generated_by_marker_for_schema_failure() -> None:
    client = FakeModelClient(
        [
            model_response({"role": "memory_manager", "stance": "unknown"}),
            model_response({"role": "memory_manager", "stance": "unknown"}),
        ]
    )

    opinion = generate_model_opinion(
        request=build_request(role="memory_manager"),
        model_client=client,
        model_config=build_model_config(),
    )

    assert opinion["generated_by"] == "fallback"
    assert opinion["role"] == "memory_manager"
    assert opinion["data_gaps"] == ["圆桌模型连续两次没有返回合法 JSON，已使用规则版观点。"]


def build_request(
    *,
    role: str,
    allowed_evidence_ids: list[str] | None = None,
    fallback_opinion: dict[str, Any] | None = None,
) -> RoundtableOpinionRequest:
    return RoundtableOpinionRequest(
        role=role,
        asset_id="ashare:600519",
        workflow_type="asset_deep_analysis",
        context={
            "profile": {"asset_id": "ashare:600519", "symbol": "600519"},
            "factor": {
                "evidence": [
                    {"evidence_id": "ev:ma-cross", "summary": "MA20 上穿 MA60。"},
                    {"evidence_id": "ev:valuation", "summary": "估值分位偏高。"},
                    {"evidence_id": "ev:risk-ok", "summary": "事件风险未解除。"},
                ]
            },
        },
        question="请输出该角色对当前标的的圆桌观点。",
        allowed_evidence_ids=allowed_evidence_ids,
        fallback_opinion=fallback_opinion,
    )


def build_model_config() -> ModelEndpointConfig:
    return ModelEndpointConfig(
        model_key="deepseek-v4-pro",
        provider="deepseek",
        model_name="DeepSeek V4 Pro",
        base_url="https://model.local/v1",
        api_key="test-api-key-roundtable",
        role="primary_financial_analyst",
    )


def model_response(payload: dict[str, Any]) -> ModelClientResponse:
    return ModelClientResponse(
        model_key="deepseek-v4-pro",
        provider="deepseek",
        model_name="DeepSeek V4 Pro",
        content=str(payload),
        parsed_json=payload,
        raw_response={"id": "response:roundtable"},
        finish_reason="stop",
    )
