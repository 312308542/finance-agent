"""金融 Agent 模型路由与高风险复核协议。

当前模块只定义本项目内部的路由和复核结果结构，不直接调用外部 LLM。
后续 Hermes-Agent 或模型客户端可以按这里输出的 `model_key`、`task` 和
`review_input` 执行真实模型调用。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

from finance_agent.storage.repositories import ModelRuntimeConfigRepository


@dataclass(frozen=True)
class ModelRoute:
    """一次模型路由决策。"""

    task: str
    model_key: str
    model_name: str
    provider: str
    role: str
    reason: str
    workflow_type: str | None = None
    asset_id: str | None = None
    decision_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为可写入 JSONB 的结构。"""

        return asdict(self)


class ModelRoutingPolicy:
    """金融决策模型路由策略。"""

    primary_model_key = "deepseek-v4-pro"
    primary_model_name = "DeepSeek V4 Pro"
    primary_provider = "deepseek"
    review_model_key = "gpt-5.5-pro"
    review_model_name = "GPT-5.5 Pro"
    review_provider = "openai"

    def __init__(
        self,
        model_config_repository: ModelRuntimeConfigRepository | None = None,
    ) -> None:
        self.model_config_repository = model_config_repository

    def route_primary(
        self,
        *,
        workflow_type: str,
        task: str,
        asset_id: str | None = None,
        decision_type: str | None = None,
        reason: str = "常规金融分析、圆桌摘要和中文报告生成优先使用 DeepSeek V4 Pro。",
    ) -> ModelRoute:
        """生成常规分析模型路由。"""

        db_route = self._route_from_db(
            workflow_type=workflow_type,
            task=task,
            role="primary_financial_analyst",
            decision_type=decision_type,
            fallback_reason=reason,
        )
        if db_route is not None:
            return replace(
                db_route,
                asset_id=asset_id,
                decision_type=decision_type,
            )
        return ModelRoute(
            task=task,
            model_key=self.primary_model_key,
            model_name=self.primary_model_name,
            provider=self.primary_provider,
            role="primary_financial_analyst",
            reason=reason,
            workflow_type=workflow_type,
            asset_id=asset_id,
            decision_type=decision_type,
        )

    def route_high_risk_review(
        self,
        *,
        workflow_type: str,
        asset_id: str,
        decision_type: str,
        reason: str,
    ) -> ModelRoute:
        """生成高风险复核模型路由。"""

        db_route = self._route_from_db(
            workflow_type=workflow_type,
            task="high_risk_review",
            role="high_risk_reviewer",
            decision_type=decision_type,
            fallback_reason=reason,
        )
        if db_route is not None:
            return replace(
                db_route,
                asset_id=asset_id,
                decision_type=decision_type,
            )
        return ModelRoute(
            task="high_risk_review",
            model_key=self.review_model_key,
            model_name=self.review_model_name,
            provider=self.review_provider,
            role="high_risk_reviewer",
            reason=reason,
            workflow_type=workflow_type,
            asset_id=asset_id,
            decision_type=decision_type,
        )

    def build_review_result(
        self,
        *,
        workflow_type: str,
        review_item: dict[str, Any],
        decision_summary: str | None = None,
    ) -> dict[str, Any]:
        """把高风险判断结果扩展成可审计的模型复核协议。"""

        asset_id = str(review_item.get("asset_id") or "")
        decision_type = str(review_item.get("decision_type") or "")
        if not review_item.get("requires_review"):
            return {
                "review_status": "not_required",
                "review_model": None,
                "route": None,
                "summary": "当前决策未触发高风险复核条件。",
                "review_input": {},
            }

        route = self.route_high_risk_review(
            workflow_type=workflow_type,
            asset_id=asset_id,
            decision_type=decision_type,
            reason="卖出、换股/换币、强风险、数据缺口或信号冲突需要 GPT-5.5 Pro 复核。",
        )
        reason = review_item.get("reason") or {}
        return {
            "review_status": "requires_model_review",
            "review_model": route.model_key,
            "route": route.to_dict(),
            "summary": (
                f"{asset_id} 的 {decision_type} 已触发高风险复核；"
                "当前阶段记录复核输入和模型路由，等待上层模型客户端执行。"
            ),
            "review_input": {
                "asset_id": asset_id,
                "decision_type": decision_type,
                "trade_action": review_item.get("trade_action"),
                "decision_summary": decision_summary,
                "risk_context": reason,
            },
        }

    def _route_from_db(
        self,
        *,
        workflow_type: str,
        task: str,
        role: str,
        decision_type: str | None,
        fallback_reason: str,
    ) -> ModelRoute | None:
        """从数据库路由规则生成模型路由，未配置时返回空。"""

        if self.model_config_repository is None:
            return None
        match = self.model_config_repository.find_route_model(
            workflow_type=workflow_type,
            task=task,
            role=role,
            decision_type=decision_type,
        )
        if match is None:
            return None
        rule, model = match
        providers = self.model_config_repository.get_enabled_provider_map()
        provider = providers.get(model.provider_key)
        if provider is None:
            return None
        return ModelRoute(
            task=task,
            model_key=model.model_key,
            model_name=model.model_name,
            provider=provider.provider_vendor,
            role=role,
            reason=rule.reason or fallback_reason,
            workflow_type=workflow_type,
            asset_id=None,
            decision_type=decision_type,
        )
