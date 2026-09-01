"""Dashboard API 路由。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Queue
from time import perf_counter, sleep
from typing import Any

import requests
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from finance_agent.agents.chat import FinanceAgentChatSession
from finance_agent.agents.interfaces import FinanceAgentInterface
from finance_agent.agents.runtime import load_model_registry, preview_model_routes
from finance_agent.agents.runtime.model_config import (
    is_openai_compatible_chat_completion_response,
)
from finance_agent.api.deps import get_session
from finance_agent.api.schemas import (
    ChatRequest,
    DataRecoveryApproveRequest,
    DataRecoveryControlRequest,
    DataRecoveryPreviewRequest,
    DataSchedulerFailedRerunRequest,
    DataSchedulerJobRunRequest,
    DataSchedulerJobUpdateRequest,
    DataSchedulerStartRequest,
    DataSyncConfigUpdateRequest,
    DecisionConfirmationRequest,
    DecisionFeedbackRequest,
    ExecutionRecordRequest,
    ModelInstanceUpdateRequest,
    ModelProviderConnectivityTestRequest,
    ModelProviderUpdateRequest,
    ModelRouteUpdateRequest,
    WorkflowRunRequest,
)
from finance_agent.application import MemoryService
from finance_agent.application.action_loop_service import ActionLoopService, ExecutionRegistration
from finance_agent.application.dashboard_service import (
    DashboardService,
    serialize_model_instance,
    serialize_model_provider,
    serialize_model_route,
    unavailable_summary,
)
from finance_agent.application.data_sync_control_service import DataSyncControlService
from finance_agent.storage.db import (
    create_engine_from_settings,
    create_session_factory,
    session_scope,
)
from finance_agent.storage.orm import DecisionLogORM, ReviewTaskORM
from finance_agent.storage.repositories import (
    ActionLoopRepository,
    ChatMemoryRepository,
    ModelRuntimeConfigRepository,
)

JsonDict = dict[str, Any]

router = APIRouter()
SESSION_DEPENDENCY = Depends(get_session)
CHAT_STREAM_HEARTBEAT_SECONDS = 3.0
CHAT_STREAM_EVENT_POLL_SECONDS = 0.1


def format_sse_event(event: str, data: JsonDict) -> str:
    """格式化单条 SSE 事件。"""

    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


@router.get("/health")
def health() -> JsonDict:
    """返回 API 和数据库健康状态。"""

    db_status: JsonDict
    try:
        engine = create_engine_from_settings()
        with engine.connect() as connection:
            connection.execute(text("select 1"))
        db_status = {"status": "ok", "message": "数据库连接正常。"}
    except Exception as exc:
        db_status = {
            "status": "unavailable",
            "message": f"数据库暂不可用：{str(exc)[:180]}",
        }
    api_status = "ok" if db_status["status"] == "ok" else "unavailable"
    return {
        "status": api_status,
        "api": {"status": "ok", "name": "finance-agent-dashboard-api"},
        "database": db_status,
        "generated_at": datetime.now(UTC).isoformat(),
    }


@router.get("/dashboard/summary")
def dashboard_summary(
    owner_id: str = "default-owner",
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """返回总览工作台快照。"""

    try:
        return DashboardService(session).build_summary(owner_id=owner_id)
    except Exception as exc:
        return unavailable_summary(owner_id=owner_id, message=str(exc)[:240])


@router.get("/portfolio/overview")
def portfolio_overview(
    owner_id: str,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """返回组合和持仓概览。"""

    try:
        return DashboardService(session).get_portfolio_overview(owner_id=owner_id)
    except Exception as exc:
        return {"status": "unavailable", "message": str(exc)[:240], "portfolios": []}


@router.get("/watchlists")
def watchlists(
    owner_id: str,
    limit: int = 20,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """返回活跃观察池条目。"""

    try:
        return DashboardService(session).get_watchlists(owner_id=owner_id, limit=limit)
    except Exception as exc:
        return {"status": "unavailable", "message": str(exc)[:240], "items": []}


@router.get("/recommendations/latest")
def latest_recommendations(
    owner_id: str,
    market: str | None = None,
    limit: int = 20,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """返回最近推荐结果。"""

    try:
        return DashboardService(session).get_latest_recommendations(
            owner_id=owner_id,
            market=market,
            limit=limit,
        )
    except Exception as exc:
        return {
            "status": "unavailable",
            "message": str(exc)[:240],
            "runs": [],
            "recommendations": [],
        }


@router.get("/risks")
def risks(
    owner_id: str,
    limit: int = 20,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """返回风险、提醒和数据质量摘要。"""

    try:
        return DashboardService(session).get_risk_overview(owner_id=owner_id, limit=limit)
    except Exception as exc:
        return {
            "status": "unavailable",
            "message": str(exc)[:240],
            "triggers": [],
            "alerts": [],
            "data_quality": [],
        }


@router.get("/workflows")
def workflows(session: Session = SESSION_DEPENDENCY) -> JsonDict:
    """列出可调用 Workflow。"""

    try:
        return FinanceAgentInterface(session).list_workflows().to_dict()
    except Exception as exc:
        return {"status": "unavailable", "message": str(exc)[:240], "data": {"workflows": []}}


@router.post("/workflows/run")
def run_workflow(
    request: WorkflowRunRequest,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """运行金融团队 Workflow。"""

    try:
        return FinanceAgentInterface(session).run_workflow(**request.model_dump()).to_dict()
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400], "data": {}}


@router.get("/workflows/{workflow_run_id}")
def workflow_run(
    workflow_run_id: str,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """查询 Workflow 运行审计。"""

    try:
        return FinanceAgentInterface(session).get_workflow_run(workflow_run_id).to_dict()
    except Exception as exc:
        return {"status": "unavailable", "message": str(exc)[:240], "data": {}}


@router.get("/reports")
def reports(
    owner_id: str,
    limit: int = 20,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """返回最近中文报告列表。"""

    try:
        return DashboardService(session).get_report_list(owner_id=owner_id, limit=limit)
    except Exception as exc:
        return {"status": "unavailable", "message": str(exc)[:240], "items": []}


@router.get("/reports/{workflow_run_id}")
def report(
    workflow_run_id: str,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """读取 Workflow 中文报告。"""

    try:
        return FinanceAgentInterface(session).get_report(workflow_run_id=workflow_run_id).to_dict()
    except Exception as exc:
        return {"status": "unavailable", "message": str(exc)[:240], "data": {}}


@router.get("/alerts")
def alerts(
    owner_id: str,
    status: str | None = None,
    limit: int = 20,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """返回提醒中心合并视图。"""

    try:
        return DashboardService(session).get_alert_center(
            owner_id=owner_id,
            status=status,
            limit=limit,
        )
    except Exception as exc:
        return {
            "status": "unavailable",
            "message": str(exc)[:240],
            "items": [],
            "alerts": [],
            "triggers": [],
        }


@router.get("/memories/recent")
def recent_memories(
    owner_id: str,
    limit: int = 20,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """返回最近 Finance Memory 流。"""

    try:
        return DashboardService(session).get_recent_memories(owner_id=owner_id, limit=limit)
    except Exception as exc:
        return {"status": "unavailable", "message": str(exc)[:240], "items": []}


@router.post("/decisions/{decision_id}/feedback")
def submit_decision_feedback(
    decision_id: str,
    request: DecisionFeedbackRequest,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """记录用户对待确认决策的反馈。"""

    decision = session.get(DecisionLogORM, decision_id)
    if decision is None:
        return {"status": "error", "message": "决策不存在", "data": {}}
    user_action = resolve_feedback_user_action(request)
    if user_action is None:
        return {
            "status": "error",
            "message": "modified 反馈必须提供 modified_action。",
            "data": {},
        }
    feedback_payload = build_decision_feedback_payload(
        decision=decision,
        request=request,
        user_action=user_action,
    )
    update_decision_feedback_state(
        decision=decision,
        feedback_payload=feedback_payload,
        user_action=user_action,
    )
    feedback_decision = MemoryService(session).record_user_feedback(
        feedback_id=build_feedback_decision_id(decision_id=decision_id),
        owner_id=decision.owner_id,
        feedback_type="decision_feedback",
        suggested_action=decision.suggested_action,
        user_action=user_action,
        summary=build_decision_feedback_summary(
            decision=decision,
            request=request,
            user_action=user_action,
        ),
        as_of=datetime.now(UTC),
        asset_id=decision.asset_id,
        portfolio_id=decision.portfolio_id,
        payload=feedback_payload,
    )
    session.flush()
    return {
        "status": "ok",
        "data": {
            "decision_id": decision_id,
            "feedback_decision_id": feedback_decision.decision_id,
            "user_action": user_action,
        },
    }


@router.get("/decisions/pending-confirmation")
def list_pending_confirmation_decisions(
    owner_id: str,
    limit: int = 20,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """列出等待用户确认的决策。"""

    statement = (
        select(DecisionLogORM)
        .where(
            DecisionLogORM.owner_id == owner_id,
            DecisionLogORM.user_action == "pending_user_confirmation",
        )
        .order_by(DecisionLogORM.created_at.desc())
        .limit(limit)
    )
    decisions = session.scalars(statement).all()
    return {
        "status": "ok",
        "data": {
            "items": [serialize_pending_decision(decision) for decision in decisions],
        },
    }


@router.post("/decisions/{decision_id}/confirm")
def confirm_decision(
    decision_id: str,
    request: DecisionConfirmationRequest,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """推进人工确认闭环，并返回是否可继续生成订单草案。"""

    try:
        result = ActionLoopService(session).confirm_decision(
            decision_log_id=decision_id,
            feedback=request.feedback,
            comment=request.comment,
            modified_action=request.modified_action,
        )
        session.flush()
        return {
            "status": "ok",
            "data": {
                "decision_id": result.decision_id,
                "feedback_decision_id": result.feedback_decision_id,
                "user_action": result.status,
                "can_create_order_draft": result.can_create_order_draft,
                "suggested_action": result.suggested_action,
            },
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400], "data": {}}


@router.post("/decisions/{decision_id}/order-draft")
def create_order_draft(
    decision_id: str,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """为已接受的决策生成文档性质订单草案。"""

    try:
        draft = ActionLoopService(session).create_order_draft(decision_log_id=decision_id)
        session.flush()
        return {"status": "ok", "data": serialize_order_draft(draft)}
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400], "data": {}}


@router.get("/order-drafts")
def list_order_drafts(
    owner_id: str,
    status: str | None = None,
    limit: int = 50,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """查询用户订单草案列表。"""

    try:
        drafts = ActionLoopRepository(session).list_order_drafts(
            owner_id=owner_id,
            status=status,
            limit=limit,
        )
        return {
            "status": "ok",
            "data": {"items": [serialize_order_draft(draft) for draft in drafts]},
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400], "data": {"items": []}}


@router.post("/executions")
def record_execution(
    request: ExecutionRecordRequest,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """登记用户在外部交易软件完成的执行结果。"""

    try:
        registration = ExecutionRegistration(
            execution_id=request.execution_id,
            owner_id=request.owner_id,
            portfolio_id=request.portfolio_id,
            asset_id=request.asset_id,
            market=request.market,
            action=request.action,
            executed_price=request.executed_price,
            executed_quantity=request.executed_quantity,
            executed_at=request.executed_at,
            order_draft_id=request.order_draft_id,
            decision_log_id=request.decision_log_id,
            fee=request.fee,
            note=request.note,
            source=request.source,
        )
        record = ActionLoopService(session).record_execution(registration)
        session.flush()
        return {"status": "ok", "data": serialize_execution_record(record)}
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400], "data": {}}


@router.get("/executions")
def list_execution_records(
    owner_id: str,
    asset_id: str | None = None,
    limit: int = 50,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """查询用户手工执行登记列表。"""

    try:
        records = ActionLoopRepository(session).list_execution_records(
            owner_id=owner_id,
            asset_id=asset_id,
            limit=limit,
        )
        return {
            "status": "ok",
            "data": {"items": [serialize_execution_record(record) for record in records]},
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400], "data": {"items": []}}


@router.get("/reviews/upcoming")
def list_upcoming_reviews(
    owner_id: str,
    limit: int = 20,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """查询等待执行复盘的任务。"""

    try:
        statement = (
            select(ReviewTaskORM)
            .where(
                ReviewTaskORM.owner_id == owner_id,
                ReviewTaskORM.status == "pending",
                ReviewTaskORM.review_type == "execution_outcome",
            )
            .order_by(ReviewTaskORM.due_at.asc())
            .limit(limit)
        )
        tasks = list(session.scalars(statement))
        return {
            "status": "ok",
            "data": {"items": [serialize_review_task(task) for task in tasks]},
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400], "data": {"items": []}}


def resolve_feedback_user_action(request: DecisionFeedbackRequest) -> str | None:
    """把反馈类型转换为决策日志中的用户动作。"""

    if request.feedback == "modified":
        action = (request.modified_action or "").strip()
        return action or None
    return request.feedback


def build_decision_feedback_payload(
    *,
    decision: DecisionLogORM,
    request: DecisionFeedbackRequest,
    user_action: str,
) -> JsonDict:
    """构造反馈落库 payload。"""

    return {
        "source_decision_id": decision.decision_id,
        "feedback": request.feedback,
        "comment": request.comment,
        "modified_action": request.modified_action,
        "resolved_user_action": user_action,
        "original_user_action": decision.user_action,
        "suggested_action": decision.suggested_action,
        "workflow_run_id": decision.workflow_run_id,
    }


def update_decision_feedback_state(
    *,
    decision: DecisionLogORM,
    feedback_payload: JsonDict,
    user_action: str,
) -> None:
    """把用户反馈写回原决策日志。"""

    payload = dict(decision.payload or {})
    payload["user_feedback"] = feedback_payload
    decision.payload = payload
    decision.user_action = user_action


def build_feedback_decision_id(*, decision_id: str) -> str:
    """生成用户反馈决策日志 ID。"""

    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    return f"feedback:{decision_id}:{timestamp}"


def build_decision_feedback_summary(
    *,
    decision: DecisionLogORM,
    request: DecisionFeedbackRequest,
    user_action: str,
) -> str:
    """生成可沉淀为 Finance Memory 的用户反馈摘要。"""

    comment = f"备注：{request.comment}" if request.comment else "无备注。"
    return (
        f"用户对决策 {decision.decision_id} 反馈为 {request.feedback}；"
        f"系统建议动作 {decision.suggested_action}，用户最终动作 {user_action}。"
        f"{comment}"
    )


def serialize_pending_decision(decision: DecisionLogORM) -> JsonDict:
    """序列化待用户确认的决策。"""

    payload = decision.payload or {}
    return {
        "decision_id": decision.decision_id,
        "owner_id": decision.owner_id,
        "portfolio_id": decision.portfolio_id,
        "asset_id": decision.asset_id,
        "decision_type": decision.decision_type,
        "suggested_action": decision.suggested_action,
        "user_action": decision.user_action,
        "summary": decision.summary,
        "workflow_run_id": decision.workflow_run_id,
        "source_recommendation_id": decision.source_recommendation_id,
        "source_alert_id": decision.source_alert_id,
        "reason_ids": decision.reason_ids,
        "risk_ids": decision.risk_ids,
        "evidence_ids": decision.evidence_ids,
        "created_at": decision.created_at.isoformat() if decision.created_at else None,
        "review_status": payload.get("review_status") or decision.user_action,
        "payload": payload,
    }


def serialize_order_draft(draft: Any) -> JsonDict:
    """序列化订单草案，保留免责声明和约束快照。"""

    return {
        "order_draft_id": draft.order_draft_id,
        "owner_id": draft.owner_id,
        "portfolio_id": draft.portfolio_id,
        "asset_id": draft.asset_id,
        "market": draft.market,
        "decision_log_id": draft.decision_log_id,
        "action": draft.action,
        "suggested_price_range": draft.suggested_price_range or {},
        "suggested_position_ratio": serialize_decimal(draft.suggested_position_ratio),
        "constraints": draft.constraints or {},
        "status": draft.status,
        "disclaimer": draft.disclaimer,
        "created_at": serialize_datetime(draft.created_at),
        "updated_at": serialize_datetime(draft.updated_at),
    }


def serialize_execution_record(record: Any) -> JsonDict:
    """序列化用户手工执行登记。"""

    return {
        "execution_id": record.execution_id,
        "owner_id": record.owner_id,
        "portfolio_id": record.portfolio_id,
        "asset_id": record.asset_id,
        "market": record.market,
        "order_draft_id": record.order_draft_id,
        "decision_log_id": record.decision_log_id,
        "action": record.action,
        "executed_price": serialize_decimal(record.executed_price),
        "executed_quantity": serialize_decimal(record.executed_quantity),
        "executed_at": serialize_datetime(record.executed_at),
        "fee": serialize_decimal(record.fee),
        "note": record.note,
        "source": record.source,
        "created_at": serialize_datetime(record.created_at),
    }


def serialize_review_task(task: Any) -> JsonDict:
    """序列化待复盘任务。"""

    return {
        "review_task_id": task.review_task_id,
        "owner_id": task.owner_id,
        "asset_id": task.asset_id,
        "source_decision_id": task.source_decision_id,
        "review_type": task.review_type,
        "due_at": serialize_datetime(task.due_at),
        "status": task.status,
        "review_questions": task.review_questions or [],
        "result_summary": task.result_summary,
        "finished_at": serialize_datetime(task.finished_at),
        "payload": task.payload or {},
    }


def serialize_decimal(value: Any) -> str | None:
    """把金额、数量和比例字段转为不丢精度的字符串。"""

    if value is None:
        return None
    return format(value, "f")


def serialize_datetime(value: Any) -> str | None:
    """把时间字段转为 ISO 字符串。"""

    return value.isoformat() if hasattr(value, "isoformat") else None


@router.get("/memory/assets/{asset_id}/timeline")
def memory_timeline(
    asset_id: str,
    owner_id: str,
    memory_type: str | None = None,
    limit: int = 20,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """读取标的 Finance Memory 时间线。"""

    try:
        return FinanceAgentInterface(session).memory_get_asset_timeline(
            owner_id=owner_id,
            asset_id=asset_id,
            memory_type=memory_type,
            limit=limit,
        ).to_dict()
    except Exception as exc:
        return {"status": "unavailable", "message": str(exc)[:240], "data": {"timeline": []}}


@router.get("/models/config")
def model_config(session: Session = SESSION_DEPENDENCY) -> JsonDict:
    """读取模型配置摘要。"""

    try:
        return {"status": "ok", "data": DashboardService(session).get_model_config()}
    except Exception as exc:
        registry = load_model_registry(prefer_database=False)
        return {
            "status": "partial",
            "message": str(exc)[:240],
            "data": {"status": "partial", "registry": registry.to_safe_dict()},
        }


@router.put("/models/providers/{provider_key}")
def upsert_model_provider(
    provider_key: str,
    request: ModelProviderUpdateRequest,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """保存模型供应商配置。"""

    try:
        repository = ModelRuntimeConfigRepository(session)
        api_key = request.api_key
        if api_key is None:
            try:
                api_key = repository.get_provider(provider_key).api_key
            except NoResultFound:
                api_key = None
        provider = repository.upsert_provider(
            provider_key=provider_key,
            provider_vendor=request.provider_vendor,
            provider_name=request.provider_name,
            base_url=request.base_url,
            api_key=api_key,
            timeout_seconds=request.timeout_seconds,
            is_enabled=request.is_enabled,
            is_default=request.is_default,
            payload=request.payload,
        )
        return {"status": "ok", "data": serialize_model_provider(provider)}
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400], "data": {}}


@router.get("/models/providers/{provider_key}/secret")
def reveal_model_provider_secret(
    provider_key: str,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """显式读取模型供应商密钥，用于前端点击可见时回填。"""

    try:
        provider = ModelRuntimeConfigRepository(session).get_provider(provider_key)
        return {
            "status": "ok",
            "data": {
                "provider_key": provider.provider_key,
                "api_key": provider.api_key,
                "api_key_configured": bool(provider.api_key),
            },
        }
    except NoResultFound:
        return {"status": "error", "message": "模型供应商不存在", "data": {}}
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400], "data": {}}


@router.post("/models/providers/test-connectivity")
def test_model_provider_connectivity(
    request: ModelProviderConnectivityTestRequest,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """测试当前模型的一对一 OpenAI 兼容接入端点。"""

    repository = ModelRuntimeConfigRepository(session)
    saved_base_url: str | None = None
    saved_api_key: str | None = None
    try:
        provider = repository.get_provider(request.provider_key)
        saved_base_url = provider.base_url
        saved_api_key = provider.api_key
    except NoResultFound:
        pass
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400], "data": {"ready": False}}

    return run_model_provider_connectivity_test(
        provider_key=request.provider_key,
        model_key=request.model_key,
        model_name=request.model_name,
        base_url=request.base_url or saved_base_url,
        api_key=request.api_key or saved_api_key,
        timeout_seconds=request.timeout_seconds,
    )


def run_model_provider_connectivity_test(
    *,
    provider_key: str,
    model_key: str,
    model_name: str | None,
    base_url: str | None,
    api_key: str | None,
    timeout_seconds: int = 30,
) -> JsonDict:
    """向 OpenAI 兼容 Chat Completions 端点发送最小 ping 请求。"""

    clean_base_url = (base_url or "").strip().rstrip("/")
    clean_api_key = (api_key or "").strip()
    clean_model_key = (model_key or "").strip()
    clean_model_name = (model_name or "").strip() or clean_model_key
    endpoint_url = f"{clean_base_url}/chat/completions" if clean_base_url else ""
    common_data: JsonDict = {
        "provider_key": provider_key,
        "model_key": clean_model_key,
        "model_name": clean_model_name,
        "endpoint_url": endpoint_url,
        "ready": False,
    }
    if not clean_base_url:
        return {"status": "error", "message": "请先填写 Base URL", "data": common_data}
    if not clean_model_key:
        return {"status": "error", "message": "请先填写模型 ID", "data": common_data}
    if not clean_api_key:
        return {"status": "error", "message": "请先填写或保存 API Key", "data": common_data}

    timeout = max(1, min(int(timeout_seconds or 30), 60))
    started_at = perf_counter()
    try:
        response = requests.post(
            endpoint_url,
            headers={"Authorization": f"Bearer {clean_api_key}"},
            json={
                "model": clean_model_name,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
                "temperature": 0,
            },
            timeout=timeout,
        )
        latency_ms = round((perf_counter() - started_at) * 1000)
        response_format_valid = is_openai_compatible_chat_completion_response(response)
        ready = bool(response.ok and response_format_valid)
        data = {
            **common_data,
            "ready": ready,
            "http_status": response.status_code,
            "latency_ms": latency_ms,
            "response_format_valid": response_format_valid,
            "response_preview": response.text[:240],
        }
        if ready:
            return {"status": "ok", "message": "模型接入连通性正常", "data": data}
        if response.ok:
            return {
                "status": "error",
                "message": "模型端点返回的不是 OpenAI-compatible JSON",
                "data": data,
            }
        return {
            "status": "error",
            "message": f"模型端点返回 HTTP {response.status_code}",
            "data": data,
        }
    except requests.Timeout:
        return {
            "status": "error",
            "message": f"连接超时（{timeout} 秒）",
            "data": common_data,
        }
    except requests.RequestException as exc:
        return {"status": "error", "message": str(exc)[:400], "data": common_data}


@router.put("/models/instances/{model_key}")
def upsert_model_instance(
    model_key: str,
    request: ModelInstanceUpdateRequest,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """保存模型实例配置。"""

    try:
        model = ModelRuntimeConfigRepository(session).upsert_model_instance(
            provider_key=request.provider_key,
            model_key=model_key,
            model_name=request.model_name,
            model_type=request.model_type,
            role=request.role,
            route_priority=request.route_priority,
            timeout_seconds=request.timeout_seconds,
            is_enabled=request.is_enabled,
            is_default=request.is_default,
            payload=request.payload,
        )
        return {"status": "ok", "data": serialize_model_instance(model)}
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400], "data": {}}


@router.delete("/models/instances/{model_key}")
def delete_model_instance(
    model_key: str,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """停用模型实例，并同步停用指向该模型的路由规则。"""

    try:
        model = ModelRuntimeConfigRepository(session).disable_model_instance(model_key)
        return {"status": "ok", "data": serialize_model_instance(model)}
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400], "data": {}}


@router.put("/models/routes/{role}")
def upsert_model_route(
    role: str,
    request: ModelRouteUpdateRequest,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """保存 Agent 角色到模型实例的路由规则。"""

    try:
        route = ModelRuntimeConfigRepository(session).upsert_routing_rule(
            workflow_type=request.workflow_type,
            task=request.task,
            role=role,
            model_key=request.model_key,
            decision_type=request.decision_type,
            reason=request.reason,
            priority=request.priority,
            is_enabled=request.is_enabled,
            payload=request.payload,
        )
        return {"status": "ok", "data": serialize_model_route(route)}
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400], "data": {}}


@router.get("/models/routes/preview")
def model_route_preview(
    workflow_type: str = "portfolio_monitoring",
    task: str = "agent_loop_planning",
    asset_id: str | None = None,
    decision_type: str | None = None,
    high_risk: bool = False,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """预览当前 Agent/Workflow 会路由到哪些模型。"""

    try:
        repository = ModelRuntimeConfigRepository(session)
        registry = load_model_registry(prefer_database=True)
        routes = preview_model_routes(
            registry=registry,
            workflow_type=workflow_type,
            task=task,
            asset_id=asset_id,
            decision_type=decision_type,
            high_risk=high_risk,
            model_config_repository=repository,
        )
        return {"status": "ok", "data": {"routes": routes}}
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400], "data": {"routes": []}}


@router.get("/data/scheduler/status")
def data_scheduler_status(
    status_file: str = "runtime/base_data_scheduler/status.json",
    max_age_seconds: int | None = None,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """读取基础数据调度器健康状态。"""

    options: JsonDict = {
        "session": session,
        "max_age_seconds": max_age_seconds,
    }
    if status_file != "runtime/base_data_scheduler/status.json":
        options["status_file"] = Path(status_file)
    return DataSyncControlService().read_scheduler_status(**options)


@router.get("/data/scheduler/progress")
def data_scheduler_progress(
    event_limit: int = 80,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """读取基础数据调度器运行态进度。"""

    try:
        return DataSyncControlService().read_scheduler_progress(
            session=session,
            event_limit=event_limit,
        )
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400], "data": {}}


@router.get("/data/scheduler/jobs")
def data_scheduler_jobs(session: Session = SESSION_DEPENDENCY) -> JsonDict:
    """读取基础数据调度任务目录，供前端选择执行和编辑配置。"""

    _ = session
    try:
        return DataSyncControlService().read_scheduler_jobs()
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400], "data": {"jobs": []}}


@router.put("/data/scheduler/jobs/{job_name}")
def update_data_scheduler_job(
    job_name: str,
    request: DataSchedulerJobUpdateRequest,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """保存单个基础数据调度任务的运行时配置。"""

    _ = session
    try:
        return DataSyncControlService().update_scheduler_job(
            job_name=job_name,
            enabled=request.enabled,
            interval_seconds=request.interval_seconds,
            limit=request.limit,
            batch_size=request.batch_size,
            max_workers=request.max_workers,
            schedule_type=request.schedule_type,
            run_at=request.run_at,
            timezone=request.timezone,
            trading_day_policy=request.trading_day_policy,
        )
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400], "data": {}}


@router.post("/data/scheduler/jobs/{job_name}/run")
def run_data_scheduler_job(
    job_name: str,
    request: DataSchedulerJobRunRequest,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """使用单任务 run-once 配置立即执行选中的调度任务。"""

    _ = session
    try:
        return DataSyncControlService().run_scheduler_job(
            job_name=job_name,
            dry_run=request.dry_run,
        )
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400], "data": {}}


@router.post("/data/scheduler/jobs/{job_name}/rerun-failed")
def rerun_failed_data_scheduler_job(
    job_name: str,
    request: DataSchedulerFailedRerunRequest,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """把选中任务的失败项重跑加入后台串行队列。"""

    _ = session
    try:
        return DataSyncControlService().rerun_failed_scheduler_job(
            job_name=job_name,
            dry_run=request.dry_run,
        )
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400], "data": {}}


@router.post("/data/scheduler/jobs/{job_name}/cancel")
def cancel_data_scheduler_job(
    job_name: str,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """取消由 Web 页面启动的单个基础数据调度任务。"""

    _ = session
    try:
        return DataSyncControlService().cancel_scheduler_job(job_name=job_name)
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400], "data": {}}


@router.post("/data/scheduler/jobs/{job_name}/pause")
def pause_data_scheduler_job(
    job_name: str,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """暂停单个基础数据调度任务；采集进程会在下一只标的前等待。"""

    _ = session
    try:
        return DataSyncControlService().pause_scheduler_job(job_name=job_name)
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400], "data": {}}


@router.post("/data/scheduler/jobs/{job_name}/resume")
def resume_data_scheduler_job(
    job_name: str,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """继续已暂停的基础数据调度任务。"""

    _ = session
    try:
        return DataSyncControlService().resume_scheduler_job(job_name=job_name)
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400], "data": {}}


@router.get("/data/sync/config")
def data_sync_config(session: Session = SESSION_DEPENDENCY) -> JsonDict:
    """读取数据同步配置、任务预览和调度计划。"""

    _ = session
    try:
        return {"status": "ok", "data": DataSyncControlService().read_config()}
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400], "data": {}}


@router.put("/data/sync/config")
def save_data_sync_config(
    request: DataSyncConfigUpdateRequest,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """保存数据同步配置并导出基础数据调度计划。"""

    _ = session
    try:
        return DataSyncControlService().save_config(
            preset=request.preset,
            markets=request.markets,
            enabled=request.enabled,
            cache_backend=request.cache_backend,
            max_concurrent_jobs=request.max_concurrent_jobs,
            resource_pools=request.resource_pools,
            config_payload=request.config,
        )
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400], "data": {}}


@router.post("/data/scheduler/start")
def start_data_scheduler(
    request: DataSchedulerStartRequest,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """读取 Docker 调度器状态；不在 Windows API 进程内启动 scheduler。"""

    _ = session
    try:
        return DataSyncControlService().start_scheduler(
            dry_run=request.dry_run,
            max_cycles=request.max_cycles,
        )
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400], "data": {}}


@router.post("/data/scheduler/stop")
def stop_data_scheduler(session: Session = SESSION_DEPENDENCY) -> JsonDict:
    """读取 Docker 调度器状态；不在 Windows API 进程内停止 scheduler。"""

    _ = session
    try:
        return DataSyncControlService().stop_scheduler()
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400], "data": {}}


@router.post("/chat")
def chat(
    request: ChatRequest,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """Web 聊天入口，复用 CLI 聊天会话。"""

    try:
        interface = FinanceAgentInterface(session)
        chat_session = FinanceAgentChatSession(
            owner_id=request.owner_id,
            interface=interface,
            model_registry=load_model_registry(prefer_database=True),
            chat_memory=ChatMemoryRepository(session),
            chat_session_id=request.session_id,
            history_limit=request.history_limit,
        )
        turn = chat_session.handle_message(request.message)
        return {
            "status": "ok",
            "data": {
                "chat_session_id": chat_session.chat_session_id,
                "turn": turn.to_dict(),
            },
        }
    except Exception as exc:
        return {"status": "unavailable", "message": str(exc)[:240], "data": {}}


@router.post("/chat/stream")
def chat_stream(request: ChatRequest) -> StreamingResponse:
    """Web 聊天流式入口，用 SSE 分阶段返回状态和最终回复。"""

    return StreamingResponse(
        stream_chat_response(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def stream_chat_response(request: ChatRequest) -> Iterator[str]:
    """生成聊天 SSE 事件，避免前端长时间等待同步响应。"""

    started = perf_counter()
    chat_session_id = request.session_id
    event_queue: Queue[tuple[str, JsonDict]] = Queue()
    yield format_sse_event(
        "status",
        {
            "message": "已收到问题，正在连接金融 Agent",
            "chat_session_id": chat_session_id,
        },
    )
    sleep(0.05)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run_chat_turn_for_stream, request, event_queue)
        last_heartbeat = perf_counter()
        while not future.done():
            try:
                event, data = event_queue.get(timeout=CHAT_STREAM_EVENT_POLL_SECONDS)
            except Empty:
                now = perf_counter()
                if now - last_heartbeat < CHAT_STREAM_HEARTBEAT_SECONDS:
                    continue
                yield format_sse_event(
                    "status",
                    {
                        "message": "Agent 正在分析并调用只读事实工具",
                        "chat_session_id": chat_session_id,
                        "elapsed_seconds": round(perf_counter() - started, 1),
                    },
                )
                last_heartbeat = now
                continue
            yield format_sse_event(event, data)
            yield from drain_chat_event_queue(event_queue)

        yield from drain_chat_event_queue(event_queue)
        try:
            chat_payload = future.result()
        except Exception as exc:
            yield format_sse_event(
                "error",
                {
                    "message": str(exc)[:240],
                    "chat_session_id": chat_session_id,
                    "elapsed_seconds": round(perf_counter() - started, 2),
                },
            )
            return

    while True:
        try:
            event, data = event_queue.get_nowait()
        except Empty:
            break
        yield format_sse_event(event, data)

    for chunk in chunk_text(str(chat_payload["content"]), 36):
        yield format_sse_event("delta", {"content": chunk})
        sleep(0.015)
    yield format_sse_event(
        "done",
        {
            "chat_session_id": chat_payload["chat_session_id"],
            "turn": chat_payload["turn"],
            "elapsed_seconds": round(perf_counter() - started, 2),
        },
    )


def run_chat_turn_for_stream(
    request: ChatRequest,
    event_queue: Queue[tuple[str, JsonDict]] | None = None,
) -> JsonDict:
    """在独立数据库会话里执行聊天，避免 SSE 主循环被长耗时调用阻塞。"""

    def emit_event(event: str, data: JsonDict) -> None:
        if event_queue is not None:
            event_queue.put((event, data))

    session_factory = create_session_factory()
    with session_scope(session_factory) as session:
        interface = FinanceAgentInterface(session)
        chat_session = FinanceAgentChatSession(
            owner_id=request.owner_id,
            interface=interface,
            model_registry=load_model_registry(prefer_database=True),
            chat_memory=ChatMemoryRepository(session),
            event_sink=emit_event,
            chat_session_id=request.session_id,
            history_limit=request.history_limit,
        )
        turn = chat_session.handle_message(request.message)
        turn_payload = turn.to_dict()
        return {
            "chat_session_id": chat_session.chat_session_id,
            "turn": compact_chat_turn_for_stream(turn_payload),
            "content": turn.assistant_message.content,
        }


def chunk_text(text: str, size: int) -> Iterator[str]:
    """按固定长度切分文本，用于前端逐段渲染。"""

    clean_size = max(1, size)
    for index in range(0, len(text), clean_size):
        yield text[index : index + clean_size]


def drain_chat_event_queue(event_queue: Queue[tuple[str, JsonDict]]) -> Iterator[str]:
    """尽快吐出 Agent 执行过程中已经产生的事件。"""

    while True:
        try:
            event, data = event_queue.get_nowait()
        except Empty:
            break
        yield format_sse_event(event, data)


def compact_chat_turn_for_stream(turn: JsonDict) -> JsonDict:
    """生成轻量 done 负载，避免把完整工具原始结果重复推给浏览器。"""

    user_message = turn.get("user_message")
    assistant_message = turn.get("assistant_message")
    compact_turn: JsonDict = {}
    if isinstance(user_message, dict):
        compact_turn["user_message"] = compact_chat_message_for_stream(user_message)
    if isinstance(assistant_message, dict):
        compact_turn["assistant_message"] = compact_chat_message_for_stream(assistant_message)
    return compact_turn


def compact_chat_message_for_stream(message: JsonDict) -> JsonDict:
    """保留聊天消息必要字段，并压缩工具观测。"""

    compact: JsonDict = {
        "role": message.get("role"),
        "content": message.get("content"),
    }
    if message.get("intent"):
        compact["intent"] = message["intent"]
    data = message.get("data")
    if isinstance(data, dict):
        compact_data: JsonDict = {}
        for key in ("workflows", "tools", "routes"):
            if key in data:
                compact_data[key] = data[key]
        recommendations = data.get("recommendations")
        if isinstance(recommendations, list):
            compact_data["recommendations"] = [
                compact_chat_recommendation(item)
                for item in recommendations
                if isinstance(item, dict)
            ]
        observations = data.get("tool_observations")
        if isinstance(observations, list):
            compact_data["tool_observations"] = [
                compact_chat_tool_observation(item)
                for item in observations
                if isinstance(item, dict)
            ]
        if compact_data:
            compact["data"] = compact_data
    return compact


def compact_chat_tool_observation(observation: JsonDict) -> JsonDict:
    """移除工具观测中的原始明细，只保留前端需要的审计摘要。"""

    compact: JsonDict = {}
    for key in ("tool", "arguments", "status", "summary", "error"):
        if key in observation:
            compact[key] = observation[key]
    return compact


def compact_chat_recommendation(item: JsonDict) -> JsonDict:
    """压缩推荐条目，只保留聊天前端需要展示和审计的摘要字段。"""

    compact: JsonDict = {}
    for key in (
        "recommendation_id",
        "run_id",
        "asset_id",
        "symbol",
        "name",
        "market",
        "horizon",
        "action",
        "rank",
        "total_score",
        "confidence",
        "conviction",
        "summary",
    ):
        if key in item:
            compact[key] = item[key]
    return compact


# ---------------------------------------------------------------------------
# 停跑恢复补跑（DataRecoveryModule 门面；规格 5.1）
# ---------------------------------------------------------------------------


def _data_recovery_module(session: Session):
    """构造补跑门面；统一走生产装配（含日历只读刷新，规格 6.1）。"""

    from finance_agent.data_recovery.assembly import build_default_recovery_module

    return build_default_recovery_module(session)


@router.post("/data/recovery/preview")
def data_recovery_preview(
    request: DataRecoveryPreviewRequest,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """只读扫描生产资产池缺口并生成或复用计划草稿（不采集）。"""

    try:
        return _data_recovery_module(session).preview(requested_by=request.requested_by)
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400]}


@router.get("/data/recovery/runs")
def data_recovery_runs(
    limit: int = 20,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """列出补跑批次（最新在前）。"""

    try:
        return {"runs": _data_recovery_module(session).list_runs(limit=limit)}
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400], "runs": []}


@router.get("/data/recovery/runs/{run_id}")
def data_recovery_run_detail(
    run_id: str,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """返回批次稳定状态：步骤进度、例外清单与质量结论。"""

    try:
        return _data_recovery_module(session).get(run_id).to_dict()
    except LookupError as exc:
        return {"status": "not_found", "message": str(exc)[:200]}
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400]}


@router.post("/data/recovery/runs/{run_id}/approve")
def data_recovery_approve(
    run_id: str,
    request: DataRecoveryApproveRequest,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """用户确认补跑；plan_hash 不一致时拒绝（规格 6.2 stale_plan）。"""

    try:
        view = _data_recovery_module(session).approve(
            run_id=run_id,
            plan_hash=request.plan_hash,
            approved_by=request.approved_by,
        )
        session.flush()
        return view.to_dict()
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400]}


@router.post("/data/recovery/runs/{run_id}/control")
def data_recovery_control(
    run_id: str,
    request: DataRecoveryControlRequest,
    session: Session = SESSION_DEPENDENCY,
) -> JsonDict:
    """暂停、继续或取消补跑批次（规格 12.3）。"""

    try:
        view = _data_recovery_module(session).control(
            run_id,
            request.action,
            actor=request.actor,
        )
        session.flush()
        return view.to_dict()
    except ValueError as exc:
        return {"status": "invalid_action", "message": str(exc)[:200]}
    except Exception as exc:
        return {"status": "error", "message": str(exc)[:400]}
