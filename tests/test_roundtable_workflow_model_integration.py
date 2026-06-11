from __future__ import annotations

from typing import Any

from finance_agent.agents.runtime.model_client import ModelClientResponse
from finance_agent.agents.runtime.model_config import ModelEndpointConfig
from finance_agent.agents.workflows.langgraph_graphs import (
    enrich_roundtable_opinions_with_model,
    resolve_roundtable_model_roles,
)


class FakeModelRegistry:
    def __init__(self, config: ModelEndpointConfig | None = None) -> None:
        self.config = config or build_model_config()

    def get(self, model_key: str) -> ModelEndpointConfig | None:
        if self.config and model_key == self.config.model_key:
            return self.config
        return None


class EmptyModelRegistry:
    def get(self, model_key: str) -> None:
        return None


class FakeModelClient:
    """记录 Workflow 圆桌增强层的模型调用。"""

    def __init__(self, responses: list[ModelClientResponse]) -> None:
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
        return self.responses.pop(0)


def test_enrich_roundtable_opinions_calls_model_for_enabled_roles_only() -> None:
    client = FakeModelClient(
        [
            model_response(
                {
                    "role": "risk_rebuttal",
                    "stance": "bearish",
                    "confidence": 0.83,
                    "summary": "模型认为高风险事件需要反驳原建议。",
                    "key_points": ["证据 ev:risk 显示风险未解除。"],
                    "rebuttals": ["若风险澄清，反驳强度下降。"],
                    "evidence_ids": ["ev:risk"],
                    "data_gaps": [],
                }
            )
        ]
    )

    opinions = enrich_roundtable_opinions_with_model(
        workflow_type="recommendation_decision",
        fallback_opinions=build_fallback_opinions(),
        asset_contexts=build_asset_contexts(),
        state={
            "model_client": client,
            "model_registry": FakeModelRegistry(),
            "roundtable_model_roles": ["risk_rebuttal"],
        },
        model_routes=[build_model_route(asset_id="ashare:600519")],
    )

    technical = next(item for item in opinions if item["role"] == "technical_analyst")
    risk = next(item for item in opinions if item["role"] == "risk_rebuttal")
    assert technical["generated_by"] == "fallback"
    assert technical["summary"] == "规则版技术观点。"
    assert risk["generated_by"] == "model"
    assert risk["summary"] == "模型认为高风险事件需要反驳原建议。"
    assert risk["tool_calls"] == [{"tool": "signal_risk.get_asset_context"}]
    assert len(client.calls) == 1


def test_resolve_roundtable_model_roles_limits_daily_review_cost_by_default() -> None:
    assert resolve_roundtable_model_roles({}, workflow_type="daily_review") == [
        "risk_rebuttal",
        "portfolio_manager",
    ]
    assert resolve_roundtable_model_roles(
        {"roundtable_model_roles": ["all"]},
        workflow_type="daily_review",
    ) == [
        "technical_analyst",
        "factor_analyst",
        "risk_rebuttal",
        "portfolio_manager",
        "memory_manager",
    ]


def test_enrich_roundtable_opinions_keeps_fallback_when_model_config_missing() -> None:
    client = FakeModelClient([])

    opinions = enrich_roundtable_opinions_with_model(
        workflow_type="asset_deep_analysis",
        fallback_opinions=build_fallback_opinions(),
        asset_contexts=build_asset_contexts(),
        state={
            "model_client": client,
            "model_registry": EmptyModelRegistry(),
            "roundtable_model_roles": ["technical_analyst", "risk_rebuttal"],
        },
        model_routes=[build_model_route(asset_id="ashare:600519")],
    )

    assert {item["generated_by"] for item in opinions} == {"fallback"}
    assert client.calls == []
    assert any("圆桌模型配置不可用" in item for item in opinions[0]["data_gaps"])


def build_fallback_opinions() -> list[dict[str, Any]]:
    return [
        {
            "role": "technical_analyst",
            "asset_id": "ashare:600519",
            "stance": "support",
            "summary": "规则版技术观点。",
            "tool_calls": [{"tool": "factor.get_asset_factor_context"}],
            "evidence_ids": ["ev:tech"],
            "source_ids": ["indicator:1"],
        },
        {
            "role": "risk_rebuttal",
            "asset_id": "ashare:600519",
            "stance": "oppose",
            "summary": "规则版风险观点。",
            "tool_calls": [{"tool": "signal_risk.get_asset_context"}],
            "evidence_ids": ["ev:risk"],
            "source_ids": ["risk:1"],
        },
    ]


def build_asset_contexts() -> dict[str, dict[str, Any]]:
    return {
        "ashare:600519": {
            "profile": {"asset_id": "ashare:600519", "symbol": "600519"},
            "factor": {
                "evidence": [
                    {"evidence_id": "ev:tech", "summary": "均线结构改善。"},
                    {"evidence_id": "ev:risk", "summary": "高风险事件未解除。"},
                ]
            },
            "signal_risk": {"risks": [{"risk_id": "risk:1"}]},
        }
    }


def build_model_config() -> ModelEndpointConfig:
    return ModelEndpointConfig(
        model_key="deepseek-v4-pro",
        provider="deepseek",
        model_name="DeepSeek V4 Pro",
        base_url="https://model.local/v1",
        api_key="test-api-key-roundtable",
        role="primary_financial_analyst",
    )


def build_model_route(*, asset_id: str) -> dict[str, Any]:
    return {
        "task": "roundtable_discussion",
        "model_key": "deepseek-v4-pro",
        "model_name": "DeepSeek V4 Pro",
        "provider": "deepseek",
        "role": "primary_financial_analyst",
        "workflow_type": "recommendation_decision",
        "asset_id": asset_id,
    }


def model_response(payload: dict[str, Any]) -> ModelClientResponse:
    return ModelClientResponse(
        model_key="deepseek-v4-pro",
        provider="deepseek",
        model_name="DeepSeek V4 Pro",
        content=str(payload),
        parsed_json=payload,
        raw_response={"id": "response:workflow-roundtable"},
        finish_reason="stop",
    )
