"""Dashboard API 请求模型。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

JsonDict = dict[str, Any]


class WorkflowRunRequest(BaseModel):
    """运行 Workflow 的请求。"""

    workflow_type: str = Field(..., description="Workflow 类型")
    owner_id: str = Field(..., description="用户 ID")
    portfolio_id: str | None = None
    watchlist_id: str | None = None
    recommendation_run_id: str | None = None
    asset_id: str | None = None
    asset_ids: list[str] | None = None
    source_asset_id: str | None = None
    candidate_asset_id: str | None = None
    horizon: str = "swing"
    timeframe: str = "1d"
    recommendation_limit: int = 20
    initial_state: JsonDict | None = None


class ChatRequest(BaseModel):
    """Web 聊天请求。"""

    owner_id: str = Field(..., description="用户 ID")
    message: str = Field(..., description="用户消息")
    session_id: str | None = None
    history_limit: int = 20


class DecisionFeedbackRequest(BaseModel):
    """用户对待确认决策的反馈请求。"""

    feedback: Literal["accepted", "rejected", "modified", "deferred"] = Field(
        ...,
        description="用户反馈类型",
    )
    comment: str | None = Field(default=None, description="用户备注")
    modified_action: str | None = Field(default=None, description="用户修改后的动作")


class DecisionConfirmationRequest(BaseModel):
    """人工确认闭环中的决策确认请求。"""

    feedback: Literal["accepted", "rejected", "modified", "deferred"] = Field(
        ...,
        description="用户确认反馈类型",
    )
    comment: str | None = Field(default=None, description="用户确认备注")
    modified_action: str | None = Field(default=None, description="用户修改后的动作")


class ExecutionRecordRequest(BaseModel):
    """用户在外部交易软件完成操作后的执行登记请求。"""

    owner_id: str = Field(..., description="用户 ID")
    portfolio_id: str = Field(..., description="组合 ID")
    asset_id: str = Field(..., description="资产 ID")
    market: str = Field(..., description="市场")
    action: str = Field(..., description="执行动作")
    executed_price: Decimal = Field(..., gt=0, description="实际执行价格")
    executed_quantity: Decimal = Field(..., gt=0, description="实际执行数量")
    executed_at: datetime = Field(..., description="实际执行时间")
    execution_id: str | None = Field(default=None, description="执行登记 ID")
    order_draft_id: str | None = Field(default=None, description="关联订单草案 ID")
    decision_log_id: str | None = Field(default=None, description="关联原始决策 ID")
    fee: Decimal | None = Field(default=None, ge=0, description="手续费")
    note: str | None = Field(default=None, description="用户备注")
    source: Literal["user_reported"] = Field(
        default="user_reported",
        description="执行来源，当前仅允许用户手工登记",
    )


class ModelProviderUpdateRequest(BaseModel):
    """模型供应商保存请求。"""

    provider_vendor: str = Field(..., description="供应商类型")
    provider_name: str = Field(..., description="供应商名称")
    base_url: str | None = None
    api_key: str | None = None
    timeout_seconds: int = 30
    is_enabled: bool = True
    is_default: bool = False
    payload: JsonDict | None = None


class ModelProviderConnectivityTestRequest(BaseModel):
    """OpenAI 兼容供应商连通性测试请求。"""

    provider_key: str = Field(..., description="供应商 Key")
    model_key: str = Field(..., description="模型 Key")
    model_name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    timeout_seconds: int = 30


class ModelInstanceUpdateRequest(BaseModel):
    """模型实例保存请求。"""

    provider_key: str = Field(..., description="供应商 Key")
    model_name: str = Field(..., description="模型名称")
    model_type: str = "llm"
    role: str | None = None
    route_priority: int = 0
    timeout_seconds: int = 30
    is_enabled: bool = True
    is_default: bool = False
    payload: JsonDict | None = None


class ModelRouteUpdateRequest(BaseModel):
    """模型路由规则保存请求。"""

    workflow_type: str = "*"
    task: str = "*"
    model_key: str = Field(..., description="目标模型 Key")
    decision_type: str = ""
    reason: str | None = None
    priority: int = 0
    is_enabled: bool = True
    payload: JsonDict | None = None


class DataSyncConfigUpdateRequest(BaseModel):
    """数据同步配置保存请求。"""

    preset: str = "personal-ashare"
    markets: list[str] = Field(default_factory=lambda: ["ashare", "fund"])
    enabled: bool = True
    cache_backend: str = "redis"
    max_concurrent_jobs: int = Field(4, ge=1, le=16)
    resource_pools: JsonDict | None = None
    config: JsonDict | None = None


class DataSchedulerStartRequest(BaseModel):
    """基础数据调度器启动请求。"""

    dry_run: bool = False
    max_cycles: int | None = None


class DataSchedulerJobUpdateRequest(BaseModel):
    """单个基础数据调度任务配置更新请求。"""

    enabled: bool | None = None
    interval_seconds: int | None = Field(default=None, ge=0)
    limit: int | None = Field(default=None, ge=1)
    batch_size: int | None = Field(default=None, ge=1)
    max_workers: int | None = Field(default=None, ge=1)
    schedule_type: str | None = None
    run_at: list[str] | None = None
    timezone: str | None = None
    trading_day_policy: str | None = None


class DataSchedulerJobRunRequest(BaseModel):
    """单个基础数据调度任务立即执行请求。"""

    dry_run: bool = False


class DataSchedulerFailedRerunRequest(BaseModel):
    """单个基础数据调度任务失败项重跑请求。"""

    dry_run: bool = False
