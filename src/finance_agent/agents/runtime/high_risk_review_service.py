"""高风险模型复核服务。

本模块负责异步消费 `model_review` 审计事件、调用复核模型、校验结构化输出，
再把复核结果写回同一条 Workflow 审计链和对应决策日志。Workflow 主路径只生成
复核协议，不在同步路径里等待模型。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_agent.agents.runtime.model_client import (
    ModelClient,
    ModelClientResponse,
    OpenAICompatibleModelClient,
)
from finance_agent.agents.runtime.model_config import (
    ModelEndpointConfig,
    load_model_registry,
)
from finance_agent.storage.orm import (
    AgentWorkflowEventORM,
    AgentWorkflowRunORM,
    DecisionLogORM,
)
from finance_agent.storage.repositories import WorkflowAuditRepository

JsonDict = dict[str, Any]

REVIEW_PENDING_STATUSES = {"requires_model_review", "review_unavailable"}
REVIEW_VERDICTS = {"approve", "reject", "needs_human"}
REVIEW_STATUS_BY_VERDICT = {
    "approve": "approved_by_review",
    "reject": "rejected_by_review",
    "needs_human": "pending_user_confirmation",
}
SUMMARY_COUNT_KEYS = {
    "approve": "approved_count",
    "reject": "rejected_count",
    "needs_human": "needs_human_count",
}
UNAVAILABLE_CONFIDENCE_MULTIPLIER = 0.7


class ReviewStore(Protocol):
    """高风险复核服务需要的持久化端口。"""

    def list_pending_reviews(self, *, owner_id: str, limit: int) -> list[JsonDict]:
        """列出待复核事件。"""

    def update_review_event_status(
        self,
        *,
        workflow_event_id: str,
        review_status: str,
        result_payload: JsonDict,
    ) -> None:
        """更新源 `model_review` 事件的复核状态。"""

    def append_review_result_event(
        self,
        *,
        source_event: JsonDict,
        result_payload: JsonDict,
        created_at: datetime,
    ) -> None:
        """追加 `model_review_result` 审计事件。"""

    def update_decision_review_status(
        self,
        *,
        decision_id: str,
        review_status: str,
        review_result: JsonDict,
        confidence_multiplier: float | None = None,
        user_action: str | None = None,
    ) -> None:
        """更新关联决策日志的复核状态。"""


class ModelRegistryLike(Protocol):
    """复核服务读取模型配置所需的最小接口。"""

    def get(self, model_key: str) -> ModelEndpointConfig | None:
        """按模型 key 读取模型端点配置。"""


@dataclass(frozen=True)
class ReviewExecutionResult:
    """单条复核执行结果。"""

    review_status: str
    verdict: str
    result_payload: JsonDict
    confidence_multiplier: float | None = None
    user_action: str | None = None


class HighRiskReviewService:
    """执行待处理高风险复核事件。"""

    def __init__(
        self,
        *,
        review_store: ReviewStore | None = None,
        session: Session | None = None,
        model_client: ModelClient | None = None,
        model_registry: ModelRegistryLike | None = None,
        now: Callable[[], datetime] | None = None,
        unavailable_confidence_multiplier: float = UNAVAILABLE_CONFIDENCE_MULTIPLIER,
    ) -> None:
        if review_store is None:
            if session is None:
                raise ValueError("HighRiskReviewService 需要 review_store 或 session。")
            review_store = SqlAlchemyHighRiskReviewStore(session)
        self.review_store = review_store
        self.model_client = model_client or OpenAICompatibleModelClient()
        self.model_registry = model_registry or load_model_registry()
        self.now = now or (lambda: datetime.now().astimezone())
        self.unavailable_confidence_multiplier = unavailable_confidence_multiplier

    def run_pending_reviews(self, *, owner_id: str, limit: int = 10) -> JsonDict:
        """批量执行待复核事件并返回计数摘要。"""

        events = self.review_store.list_pending_reviews(owner_id=owner_id, limit=limit)
        summary = {
            "processed_count": 0,
            "approved_count": 0,
            "rejected_count": 0,
            "needs_human_count": 0,
            "unavailable_count": 0,
        }
        for event in events:
            result = self.execute_review(event)
            summary["processed_count"] += 1
            if result.verdict in SUMMARY_COUNT_KEYS:
                summary[SUMMARY_COUNT_KEYS[result.verdict]] += 1
            elif result.verdict == "review_unavailable":
                summary["unavailable_count"] += 1
        return summary

    def execute_review(self, review_event: JsonDict) -> ReviewExecutionResult:
        """执行单条复核事件并回写审计链。"""

        try:
            result = self._call_review_model(review_event)
        except Exception as exc:  # noqa: BLE001 - 复核失败必须降级为可重试状态
            result = self._build_unavailable_result(error=exc)
        self._persist_review_result(review_event=review_event, result=result)
        return result

    def _call_review_model(self, review_event: JsonDict) -> ReviewExecutionResult:
        output = extract_review_output(review_event)
        route = output.get("route") or {}
        model_key = str(route.get("model_key") or output.get("review_model") or "")
        config = self.model_registry.get(model_key)
        if config is None or not config.ready:
            raise RuntimeError(f"复核模型未配置或不可用：{model_key}")

        messages = build_review_messages(review_input=output.get("review_input") or {})
        response = self.model_client.invoke_json(
            config=config,
            messages=messages,
            temperature=0.0,
        )
        parsed = normalize_review_payload(response.parsed_json)
        if parsed is None:
            retry_messages = [
                *messages,
                {
                    "role": "user",
                    "content": (
                        "上次输出不是合法 JSON，或字段不符合复核协议。"
                        "请只输出包含 verdict/confidence/reasons/blocking_risks/data_gaps 的 JSON。"
                    ),
                },
            ]
            response = self.model_client.invoke_json(
                config=config,
                messages=retry_messages,
                temperature=0.0,
            )
            parsed = normalize_review_payload(response.parsed_json)
        if parsed is None:
            raise RuntimeError("复核模型连续两次没有返回合法 JSON。")
        return build_review_execution_result(parsed=parsed, response=response)

    def _build_unavailable_result(self, *, error: Exception) -> ReviewExecutionResult:
        payload = {
            "verdict": "review_unavailable",
            "confidence": 0.0,
            "reasons": [str(error)],
            "blocking_risks": [],
            "data_gaps": ["复核模型不可用，保留待重试状态。"],
            "review_status": "review_unavailable",
        }
        return ReviewExecutionResult(
            review_status="review_unavailable",
            verdict="review_unavailable",
            result_payload=payload,
            confidence_multiplier=self.unavailable_confidence_multiplier,
            user_action=None,
        )

    def _persist_review_result(
        self,
        *,
        review_event: JsonDict,
        result: ReviewExecutionResult,
    ) -> None:
        workflow_event_id = str(review_event.get("workflow_event_id") or "")
        decision_id = str(review_event.get("decision_id") or "")
        self.review_store.update_review_event_status(
            workflow_event_id=workflow_event_id,
            review_status=result.review_status,
            result_payload=result.result_payload,
        )
        self.review_store.append_review_result_event(
            source_event=review_event,
            result_payload=result.result_payload,
            created_at=self.now(),
        )
        if decision_id:
            self.review_store.update_decision_review_status(
                decision_id=decision_id,
                review_status=result.review_status,
                review_result=result.result_payload,
                confidence_multiplier=result.confidence_multiplier,
                user_action=result.user_action,
            )


def build_review_execution_result(
    *,
    parsed: JsonDict,
    response: ModelClientResponse,
) -> ReviewExecutionResult:
    """把模型 JSON 转换为内部复核结果。"""

    verdict = str(parsed["verdict"])
    review_status = REVIEW_STATUS_BY_VERDICT[verdict]
    payload = {
        **parsed,
        "review_status": review_status,
        "model": response.to_audit_dict(),
    }
    return ReviewExecutionResult(
        review_status=review_status,
        verdict=verdict,
        result_payload=payload,
        user_action=review_status if verdict in {"reject", "needs_human"} else None,
    )


def build_review_messages(*, review_input: JsonDict) -> list[JsonDict]:
    """构造高风险复核模型消息。"""

    return [
        {
            "role": "system",
            "content": (
                "你是高风险动作复核员。你只能复查是否放行、驳回或转人工，"
                "不能重新生成交易建议。只输出 JSON。"
            ),
        },
        {
            "role": "user",
            "content": (
                "请复核以下高风险金融动作，并输出 JSON："
                f"{review_input}"
            ),
        },
    ]


def extract_review_output(review_event: JsonDict) -> JsonDict:
    """从 `agent_workflow_events.payload` 中取出 `model_review` 输出。"""

    payload = review_event.get("payload") if isinstance(review_event, dict) else None
    if not isinstance(payload, dict):
        return {}
    output = payload.get("output")
    return output if isinstance(output, dict) else {}


def normalize_review_payload(payload: JsonDict | None) -> JsonDict | None:
    """严格校验复核模型 JSON。"""

    if not isinstance(payload, dict):
        return None
    verdict = str(payload.get("verdict") or "")
    if verdict not in REVIEW_VERDICTS:
        return None
    confidence_value = payload.get("confidence")
    if not isinstance(confidence_value, int | float | str):
        return None
    try:
        confidence = float(confidence_value)
    except ValueError:
        return None
    if confidence < 0 or confidence > 1:
        return None
    reasons = normalize_string_list(payload.get("reasons"))
    blocking_risks = normalize_string_list(payload.get("blocking_risks"))
    data_gaps = normalize_string_list(payload.get("data_gaps"))
    if reasons is None or blocking_risks is None or data_gaps is None:
        return None
    return {
        "verdict": verdict,
        "confidence": confidence,
        "reasons": reasons,
        "blocking_risks": blocking_risks,
        "data_gaps": data_gaps,
    }


def normalize_string_list(value: object) -> list[str] | None:
    """校验并清洗字符串列表。"""

    if value is None:
        return []
    if not isinstance(value, list):
        return None
    if not all(isinstance(item, str) for item in value):
        return None
    return value


class SqlAlchemyHighRiskReviewStore:
    """基于 SQLAlchemy 的高风险复核持久化端口。"""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.workflow_audit = WorkflowAuditRepository(session)

    def list_pending_reviews(self, *, owner_id: str, limit: int) -> list[JsonDict]:
        """从 Workflow 审计链读取待复核事件。"""

        statement = (
            select(AgentWorkflowEventORM, AgentWorkflowRunORM)
            .join(
                AgentWorkflowRunORM,
                AgentWorkflowRunORM.workflow_run_id
                == AgentWorkflowEventORM.workflow_run_id,
            )
            .where(
                AgentWorkflowRunORM.owner_id == owner_id,
                AgentWorkflowEventORM.event_type == "model_review",
            )
            .order_by(AgentWorkflowEventORM.created_at.asc())
        )
        pending: list[JsonDict] = []
        for event, run in self.session.execute(statement):
            output = extract_review_output({"payload": event.payload})
            if output.get("review_status") not in REVIEW_PENDING_STATUSES:
                continue
            pending.append(
                {
                    "workflow_event_id": event.workflow_event_id,
                    "workflow_run_id": event.workflow_run_id,
                    "owner_id": run.owner_id,
                    "workflow_type": run.workflow_type,
                    "decision_id": self._find_decision_id(
                        workflow_run_id=event.workflow_run_id,
                        review_output=output,
                    ),
                    "payload": event.payload or {},
                }
            )
            if len(pending) >= limit:
                break
        return pending

    def update_review_event_status(
        self,
        *,
        workflow_event_id: str,
        review_status: str,
        result_payload: JsonDict,
    ) -> None:
        """更新源 `model_review` 事件 payload。"""

        event = self.session.get(AgentWorkflowEventORM, workflow_event_id)
        if event is None:
            return
        payload = dict(cast(JsonDict, event.payload or {}))
        output = dict(payload.get("output") or {})
        output["review_status"] = review_status
        output["review_result"] = result_payload
        payload["output"] = output
        event.payload = payload
        self.session.flush()

    def append_review_result_event(
        self,
        *,
        source_event: JsonDict,
        result_payload: JsonDict,
        created_at: datetime,
    ) -> None:
        """追加复核结果审计事件。"""

        workflow_event_id = str(source_event.get("workflow_event_id") or "")
        review_status = str(result_payload.get("review_status") or result_payload.get("verdict"))
        self.workflow_audit.insert_event(
            workflow_event_id=f"{workflow_event_id}:result:{review_status}",
            workflow_run_id=str(source_event.get("workflow_run_id") or ""),
            event_type="model_review_result",
            agent_name="model_review_result",
            message=f"高风险模型复核完成：{review_status}",
            created_at=created_at,
            payload=result_payload,
        )

    def update_decision_review_status(
        self,
        *,
        decision_id: str,
        review_status: str,
        review_result: JsonDict,
        confidence_multiplier: float | None = None,
        user_action: str | None = None,
    ) -> None:
        """局部更新决策日志的复核状态。"""

        decision = self.session.get(DecisionLogORM, decision_id)
        if decision is None:
            return
        payload = dict(cast(JsonDict, decision.payload or {}))
        payload["review_status"] = review_status
        payload["review_result"] = review_result
        if confidence_multiplier is not None:
            payload["review_confidence_multiplier"] = confidence_multiplier
        decision.payload = payload
        if user_action:
            decision.user_action = user_action
        self.session.flush()

    def _find_decision_id(
        self,
        *,
        workflow_run_id: str,
        review_output: JsonDict,
    ) -> str | None:
        review_input = review_output.get("review_input") or {}
        asset_id = review_input.get("asset_id")
        decision_type = review_input.get("decision_type")
        if not asset_id or not decision_type:
            return None
        statement = (
            select(DecisionLogORM)
            .where(
                DecisionLogORM.workflow_run_id == workflow_run_id,
                DecisionLogORM.asset_id == str(asset_id),
                DecisionLogORM.decision_type == str(decision_type),
            )
            .order_by(DecisionLogORM.created_at.desc())
            .limit(1)
        )
        decision = self.session.scalars(statement).one_or_none()
        if decision is None:
            return None
        decision_id = getattr(decision, "decision_id", None)
        return str(decision_id) if decision_id else None
