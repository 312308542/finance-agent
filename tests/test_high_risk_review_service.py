from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from finance_agent.agents.runtime.high_risk_review_service import (
    HighRiskReviewService,
    SqlAlchemyHighRiskReviewStore,
)
from finance_agent.agents.runtime.model_client import ModelClientResponse
from finance_agent.agents.runtime.model_config import ModelEndpointConfig
from finance_agent.cli import main as cli_main

NOW = datetime(2026, 6, 12, 9, 30, tzinfo=timezone.utc)


@dataclass
class FakeReviewStore:
    """记录高风险复核服务对审计链和决策日志的写入。"""

    events: list[dict[str, Any]]
    review_status_updates: list[dict[str, Any]] = field(default_factory=list)
    result_events: list[dict[str, Any]] = field(default_factory=list)
    decision_updates: list[dict[str, Any]] = field(default_factory=list)

    def list_pending_reviews(self, *, owner_id: str, limit: int) -> list[dict[str, Any]]:
        return [
            event
            for event in self.events
            if event["owner_id"] == owner_id
            and event["payload"]["output"]["review_status"]
            in {"requires_model_review", "review_unavailable"}
        ][:limit]

    def update_review_event_status(
        self,
        *,
        workflow_event_id: str,
        review_status: str,
        result_payload: dict[str, Any],
    ) -> None:
        self.review_status_updates.append(
            {
                "workflow_event_id": workflow_event_id,
                "review_status": review_status,
                "result_payload": result_payload,
            }
        )

    def append_review_result_event(
        self,
        *,
        source_event: dict[str, Any],
        result_payload: dict[str, Any],
        created_at: datetime,
    ) -> None:
        self.result_events.append(
            {
                "workflow_run_id": source_event["workflow_run_id"],
                "event_type": "model_review_result",
                "created_at": created_at,
                "payload": result_payload,
            }
        )

    def update_decision_review_status(
        self,
        *,
        decision_id: str,
        review_status: str,
        review_result: dict[str, Any],
        confidence_multiplier: float | None = None,
        user_action: str | None = None,
    ) -> None:
        self.decision_updates.append(
            {
                "decision_id": decision_id,
                "review_status": review_status,
                "review_result": review_result,
                "confidence_multiplier": confidence_multiplier,
                "user_action": user_action,
            }
        )


class FakeModelRegistry:
    def __init__(self) -> None:
        self.config = ModelEndpointConfig(
            model_key="gpt-5.5-pro",
            provider="openai",
            model_name="GPT-5.5 Pro",
            base_url="https://model.local/v1",
            api_key="test-api-key-review",
            role="high_risk_reviewer",
        )

    def get(self, model_key: str) -> ModelEndpointConfig | None:
        if model_key == self.config.model_key:
            return self.config
        return None


class FakeModelClient:
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


def test_high_risk_review_service_records_approve_result() -> None:
    store = FakeReviewStore([build_review_event(decision_id="decision:approve")])
    client = FakeModelClient([model_response({"verdict": "approve", "confidence": 0.82})])

    summary = HighRiskReviewService(
        review_store=store,
        model_client=client,
        model_registry=FakeModelRegistry(),
        now=lambda: NOW,
    ).run_pending_reviews(owner_id="owner:demo", limit=5)

    assert summary["processed_count"] == 1
    assert summary["approved_count"] == 1
    assert store.review_status_updates[0]["review_status"] == "approved_by_review"
    assert store.result_events[0]["payload"]["verdict"] == "approve"
    assert store.decision_updates[0] == {
        "decision_id": "decision:approve",
        "review_status": "approved_by_review",
        "review_result": store.result_events[0]["payload"],
        "confidence_multiplier": None,
        "user_action": None,
    }


def test_high_risk_review_service_rejects_and_marks_decision() -> None:
    store = FakeReviewStore([build_review_event(decision_id="decision:reject")])
    client = FakeModelClient(
        [
            model_response(
                {
                    "verdict": "reject",
                    "confidence": 0.91,
                    "reasons": ["风险证据强于原建议"],
                    "blocking_risks": ["高风险事件未解除"],
                }
            )
        ]
    )

    summary = HighRiskReviewService(
        review_store=store,
        model_client=client,
        model_registry=FakeModelRegistry(),
        now=lambda: NOW,
    ).run_pending_reviews(owner_id="owner:demo", limit=5)

    assert summary["rejected_count"] == 1
    assert store.review_status_updates[0]["review_status"] == "rejected_by_review"
    assert store.decision_updates[0]["user_action"] == "rejected_by_review"
    assert store.result_events[0]["payload"]["blocking_risks"] == ["高风险事件未解除"]


def test_high_risk_review_service_marks_needs_human() -> None:
    store = FakeReviewStore([build_review_event(decision_id="decision:human")])
    client = FakeModelClient(
        [
            model_response(
                {
                    "verdict": "needs_human",
                    "confidence": 0.65,
                    "reasons": ["证据冲突，需要人工确认"],
                    "data_gaps": ["缺少最新持仓成本"],
                }
            )
        ]
    )

    summary = HighRiskReviewService(
        review_store=store,
        model_client=client,
        model_registry=FakeModelRegistry(),
        now=lambda: NOW,
    ).run_pending_reviews(owner_id="owner:demo", limit=5)

    assert summary["needs_human_count"] == 1
    assert store.review_status_updates[0]["review_status"] == "pending_user_confirmation"
    assert store.decision_updates[0]["user_action"] == "pending_user_confirmation"
    assert store.result_events[0]["payload"]["data_gaps"] == ["缺少最新持仓成本"]


def test_high_risk_review_service_retries_once_when_json_is_invalid() -> None:
    store = FakeReviewStore([build_review_event(decision_id="decision:retry")])
    client = FakeModelClient(
        [
            ModelClientResponse(
                model_key="gpt-5.5-pro",
                provider="openai",
                model_name="GPT-5.5 Pro",
                content="不是 JSON",
                parsed_json=None,
                raw_response={"id": "bad"},
            ),
            model_response({"verdict": "approve", "confidence": 0.77}),
        ]
    )

    summary = HighRiskReviewService(
        review_store=store,
        model_client=client,
        model_registry=FakeModelRegistry(),
        now=lambda: NOW,
    ).run_pending_reviews(owner_id="owner:demo", limit=5)

    assert summary["approved_count"] == 1
    assert len(client.calls) == 2
    assert "上次输出不是合法 JSON" in client.calls[1]["messages"][-1]["content"]


def test_high_risk_review_service_keeps_retryable_when_model_unavailable() -> None:
    store = FakeReviewStore([build_review_event(decision_id="decision:unavailable")])
    client = FakeModelClient([RuntimeError("connect timeout")])

    summary = HighRiskReviewService(
        review_store=store,
        model_client=client,
        model_registry=FakeModelRegistry(),
        now=lambda: NOW,
    ).run_pending_reviews(owner_id="owner:demo", limit=5)

    assert summary["unavailable_count"] == 1
    assert store.review_status_updates[0]["review_status"] == "review_unavailable"
    assert store.result_events[0]["payload"]["verdict"] == "review_unavailable"
    assert store.decision_updates[0]["review_status"] == "review_unavailable"
    assert store.decision_updates[0]["confidence_multiplier"] == pytest.approx(0.7)
    assert store.decision_updates[0]["user_action"] is None


def test_successful_review_clears_previous_unavailable_confidence_penalty() -> None:
    """额度恢复后复核成功，不应保留上一轮 unavailable 的置信度惩罚。"""

    decision = SimpleNamespace(
        payload={
            "review_status": "review_unavailable",
            "review_confidence_multiplier": 0.7,
        },
        user_action="unknown",
    )

    class FakeSession:
        def get(self, model: object, decision_id: str) -> object:
            assert decision_id == "decision:retry-success"
            return decision

        def flush(self) -> None:
            return None

    store = SqlAlchemyHighRiskReviewStore(FakeSession())  # type: ignore[arg-type]

    store.update_decision_review_status(
        decision_id="decision:retry-success",
        review_status="rejected_by_review",
        review_result={"verdict": "reject"},
        confidence_multiplier=None,
        user_action="rejected_by_review",
    )

    assert "review_confidence_multiplier" not in decision.payload
    assert decision.user_action == "rejected_by_review"


def test_agent_review_pending_cli_dry_run_lists_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeService:
        def __init__(self, *, session: object) -> None:
            self.session = session

        def list_pending_reviews(self, *, owner_id: str, limit: int) -> list[dict[str, Any]]:
            assert owner_id == "owner:demo"
            assert limit == 5
            return [{"workflow_event_id": "event:1"}]

    monkeypatch.setattr(cli_main, "HighRiskReviewService", FakeService)

    args = cli_main.build_parser().parse_args(
        ["agent", "review-pending", "--owner-id", "owner:demo", "--limit", "5", "--dry-run"]
    )
    result = cli_main.dispatch_agent(object(), args)

    assert result == {
        "status": "ok",
        "data": {
            "dry_run": True,
            "pending_count": 1,
            "reviews": [{"workflow_event_id": "event:1"}],
        },
    }


def test_agent_review_pending_cli_runs_service(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeService:
        def __init__(self, *, session: object) -> None:
            self.session = session

        def run_pending_reviews(self, *, owner_id: str, limit: int) -> dict[str, Any]:
            assert owner_id == "owner:demo"
            assert limit == 3
            return {"processed_count": 2, "approved_count": 1, "rejected_count": 1}

    monkeypatch.setattr(cli_main, "HighRiskReviewService", FakeService)

    args = cli_main.build_parser().parse_args(
        ["agent", "review-pending", "--owner-id", "owner:demo", "--limit", "3"]
    )
    result = cli_main.dispatch_agent(object(), args)

    assert result == {
        "status": "ok",
        "data": {"processed_count": 2, "approved_count": 1, "rejected_count": 1},
    }


def build_review_event(*, decision_id: str) -> dict[str, Any]:
    return {
        "workflow_event_id": f"event:{decision_id}",
        "workflow_run_id": "workflow:review:1",
        "owner_id": "owner:demo",
        "workflow_type": "recommendation_decision",
        "decision_id": decision_id,
        "payload": {
            "output": {
                "review_status": "requires_model_review",
                "review_model": "gpt-5.5-pro",
                "route": {
                    "task": "high_risk_review",
                    "model_key": "gpt-5.5-pro",
                    "model_name": "GPT-5.5 Pro",
                    "provider": "openai",
                    "role": "high_risk_reviewer",
                    "workflow_type": "recommendation_decision",
                    "asset_id": "ashare:600519",
                    "decision_type": "recommendation_sell",
                },
                "review_input": {
                    "asset_id": "ashare:600519",
                    "decision_type": "recommendation_sell",
                    "trade_action": "sell",
                    "decision_summary": "建议卖出该标的。",
                    "risk_context": {
                        "severity": "high",
                        "confidence": 0.7,
                        "data_quality_status": "available",
                        "risk_severities": ["high"],
                        "has_conflicting_signal": False,
                    },
                },
            }
        },
    }


def model_response(payload: dict[str, Any]) -> ModelClientResponse:
    normalized = {
        "verdict": payload["verdict"],
        "confidence": payload.get("confidence", 0.8),
        "reasons": payload.get("reasons", ["复核通过"]),
        "blocking_risks": payload.get("blocking_risks", []),
        "data_gaps": payload.get("data_gaps", []),
    }
    return ModelClientResponse(
        model_key="gpt-5.5-pro",
        provider="openai",
        model_name="GPT-5.5 Pro",
        content=str(normalized),
        parsed_json=normalized,
        raw_response={"id": f"response:{normalized['verdict']}"},
        finish_reason="stop",
    )
