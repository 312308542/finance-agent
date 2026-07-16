"""人工确认、订单草案和外部执行登记服务。

本服务只编排用户确认后的闭环状态，不连接券商或交易所，也不执行真实下单。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from finance_agent.application.memory_service import MemoryService
from finance_agent.application.portfolio_service import PortfolioService, PortfolioSnapshot
from finance_agent.storage.orm import (
    DecisionLogORM,
    ExecutionRecordORM,
    OrderDraftORM,
    PositionORM,
    ReviewTaskORM,
)
from finance_agent.storage.repositories import (
    ACTION_LOOP_DISCLAIMER,
    ActionLoopRepository,
    MarketDataRepository,
)

JsonDict = dict[str, Any]
EXECUTABLE_ACTIONS = {"buy", "sell", "add", "reduce"}
HIGH_RISK_ACTIONS = {"sell", "reduce"}
APPROVED_REVIEW_STATUSES = {"approved_by_review", "pending_user_confirmation"}
POSITION_QUANT = Decimal("0.0000000001")


class ActionLoopMemoryService(Protocol):
    """人工确认、执行登记和复盘任务需要的记忆端口。"""

    def record_user_feedback(self, **kwargs: Any) -> Any:
        """记录用户反馈并反写 Finance Memory。"""

    def record_decision(self, **kwargs: Any) -> Any:
        """记录执行登记审计节点。"""

    def upsert_memory(self, **kwargs: Any) -> Any:
        """沉淀执行登记记忆。"""

    def schedule_review(self, **kwargs: Any) -> Any:
        """创建后续复盘任务。"""

    def complete_review_task(self, **kwargs: Any) -> Any:
        """完成复盘任务并沉淀结论。"""


class ActionRepository(Protocol):
    """订单草案和执行登记仓储端口。"""

    def supersede_active_order_drafts(
        self,
        *,
        decision_log_id: str,
        superseded_at: datetime | None = None,
    ) -> int:
        """把同一决策下仍有效的旧草案置为 superseded。"""

    def upsert_order_draft(self, **kwargs: Any) -> OrderDraftORM:
        """写入订单草案。"""

    def get_order_draft(self, order_draft_id: str) -> OrderDraftORM | None:
        """查询订单草案。"""

    def get_execution_record(self, execution_id: str) -> ExecutionRecordORM | None:
        """查询执行登记。"""

    def upsert_execution_record(self, **kwargs: Any) -> ExecutionRecordORM:
        """写入执行登记。"""


class PortfolioPositionService(Protocol):
    """执行登记更新持仓需要的组合服务端口。"""

    def load_portfolio_snapshot(self, portfolio_id: str) -> PortfolioSnapshot:
        """读取组合当前持仓。"""

    def upsert_position(self, **kwargs: Any) -> PositionORM:
        """幂等写入当前持仓和快照。"""


@dataclass(frozen=True)
class DecisionConfirmationResult:
    """用户确认后的推进结果。"""

    decision_id: str
    feedback_decision_id: str
    status: str
    can_create_order_draft: bool
    suggested_action: str


@dataclass(frozen=True)
class ExecutionRegistration:
    """用户在外部交易软件完成操作后的手工登记。"""

    owner_id: str
    portfolio_id: str
    asset_id: str
    market: str
    action: str
    executed_price: Decimal
    executed_quantity: Decimal
    executed_at: datetime
    execution_id: str | None = None
    order_draft_id: str | None = None
    decision_log_id: str | None = None
    fee: Decimal | None = None
    note: str | None = None
    source: str = "user_reported"


@dataclass(frozen=True)
class PositionUpdate:
    """执行登记预校验后的持仓更新结果。"""

    position_id: str
    quantity: Decimal
    avg_cost: Decimal | None
    status: str


@dataclass(frozen=True)
class ExecutionOutcomeReviewResult:
    """执行复盘比较结果。"""

    review_task_id: str
    execution_id: str | None
    outcome: str
    result_summary: str
    payload: JsonDict


class ActionLoopService:
    """人工确认与订单草案服务。"""

    def __init__(
        self,
        session: Session,
        *,
        memory_service: ActionLoopMemoryService | None = None,
        action_repository: ActionRepository | None = None,
        portfolio_service: PortfolioPositionService | None = None,
        latest_price_loader: Callable[..., JsonDict | None] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.session = session
        self.memory_service = memory_service or MemoryService(session)
        self.action_repository = action_repository or ActionLoopRepository(session)
        self.portfolio_service = portfolio_service or PortfolioService(session)
        self.latest_price_loader = latest_price_loader or load_latest_market_bar_price
        self.now = now or (lambda: datetime.now().astimezone())

    def confirm_decision(
        self,
        *,
        decision_log_id: str,
        feedback: str,
        comment: str | None = None,
        modified_action: str | None = None,
    ) -> DecisionConfirmationResult:
        """记录用户确认/反馈，并判断是否可以继续生成订单草案。"""

        decision = self._get_decision(decision_log_id)
        user_action = resolve_feedback_user_action(
            feedback=feedback,
            modified_action=modified_action,
        )
        feedback_payload = build_action_loop_feedback_payload(
            decision=decision,
            feedback=feedback,
            comment=comment,
            modified_action=modified_action,
            user_action=user_action,
        )
        update_decision_feedback_state(
            decision=decision,
            feedback_payload=feedback_payload,
            user_action=user_action,
        )
        feedback_decision = self.memory_service.record_user_feedback(
            feedback_id=build_feedback_decision_id(decision_id=decision_log_id, as_of=self.now()),
            owner_id=decision.owner_id,
            feedback_type="decision_confirmation",
            suggested_action=decision.suggested_action,
            user_action=user_action,
            summary=build_action_loop_feedback_summary(
                decision=decision,
                feedback=feedback,
                comment=comment,
                user_action=user_action,
            ),
            as_of=self.now(),
            asset_id=decision.asset_id,
            portfolio_id=decision.portfolio_id,
            payload=feedback_payload,
        )
        self.session.flush()
        return DecisionConfirmationResult(
            decision_id=decision.decision_id,
            feedback_decision_id=feedback_decision.decision_id,
            status=user_action,
            can_create_order_draft=user_action == "accepted"
            and normalize_action(decision.suggested_action) in EXECUTABLE_ACTIONS,
            suggested_action=decision.suggested_action,
        )

    def create_order_draft(self, decision_log_id: str) -> OrderDraftORM:
        """基于已接受的决策生成订单草案。"""

        decision = self._get_decision(decision_log_id)
        action = normalize_action(decision.suggested_action)
        if decision.user_action != "accepted":
            raise ValueError(f"决策尚未被用户接受，不能生成订单草案：{decision_log_id}")
        if action not in EXECUTABLE_ACTIONS:
            raise ValueError(f"决策动作不可生成订单草案：{decision.suggested_action}")
        ensure_review_gate_allows_draft(decision=decision, action=action)

        created_at = self.now()
        self.action_repository.supersede_active_order_drafts(
            decision_log_id=decision.decision_id,
            superseded_at=created_at,
        )
        return self.action_repository.upsert_order_draft(
            order_draft_id=build_order_draft_id(decision=decision, as_of=created_at),
            owner_id=decision.owner_id,
            portfolio_id=decision.portfolio_id,
            asset_id=decision.asset_id,
            market=resolve_decision_market(decision),
            decision_log_id=decision.decision_id,
            action=action,
            suggested_price_range=extract_json_object(decision.payload, "suggested_price_range"),
            suggested_position_ratio=extract_decimal(decision.payload, "suggested_position_ratio"),
            constraints=extract_json_object(decision.payload, "constraints"),
            disclaimer=ACTION_LOOP_DISCLAIMER,
            created_at=created_at,
            updated_at=created_at,
        )

    def record_execution(self, registration: ExecutionRegistration) -> ExecutionRecordORM:
        """登记用户外部执行结果，并同步当前持仓与后续复盘任务。"""

        execution_id = registration.execution_id or build_execution_id(
            registration=registration,
            as_of=registration.executed_at,
        )
        existing = self.action_repository.get_execution_record(execution_id)
        if existing is not None:
            return existing
        normalized_action = normalize_action(registration.action)
        validate_execution_registration(
            registration=registration,
            action=normalized_action,
        )
        decision_log_id = resolve_execution_decision_log_id(
            repository=self.action_repository,
            registration=registration,
        )
        current = find_current_position(
            self.portfolio_service.load_portfolio_snapshot(registration.portfolio_id),
            asset_id=registration.asset_id,
        )
        position_update = build_position_update_from_registration(
            registration=registration,
            action=normalized_action,
            current=current,
        )
        record = self.action_repository.upsert_execution_record(
            execution_id=execution_id,
            owner_id=registration.owner_id,
            portfolio_id=registration.portfolio_id,
            asset_id=registration.asset_id,
            market=registration.market,
            order_draft_id=registration.order_draft_id,
            decision_log_id=decision_log_id,
            action=normalized_action,
            executed_price=registration.executed_price,
            executed_quantity=registration.executed_quantity,
            executed_at=registration.executed_at,
            fee=registration.fee,
            note=registration.note,
            source=registration.source,
            created_at=self.now(),
        )
        position = self.apply_position_update(record=record, update=position_update)
        self.record_execution_audit(record=record, position=position)
        return record

    def update_position_from_execution(self, execution: ExecutionRecordORM) -> PositionORM:
        """根据执行登记更新当前持仓。"""

        current = find_current_position(
            self.portfolio_service.load_portfolio_snapshot(execution.portfolio_id),
            asset_id=execution.asset_id,
        )
        update = build_position_update_from_registration(
            registration=ExecutionRegistration(
                execution_id=execution.execution_id,
                owner_id=execution.owner_id,
                portfolio_id=execution.portfolio_id,
                asset_id=execution.asset_id,
                market=execution.market,
                action=execution.action,
                executed_price=execution.executed_price,
                executed_quantity=execution.executed_quantity,
                executed_at=execution.executed_at,
                order_draft_id=execution.order_draft_id,
                decision_log_id=execution.decision_log_id,
                fee=execution.fee,
                note=execution.note,
                source=execution.source,
            ),
            action=execution.action,
            current=current,
        )
        return self.apply_position_update(record=execution, update=update)

    def apply_position_update(
        self,
        *,
        record: ExecutionRecordORM,
        update: PositionUpdate,
    ) -> PositionORM:
        """写入已校验的持仓更新。"""

        return self.portfolio_service.upsert_position(
            position_id=update.position_id,
            portfolio_id=record.portfolio_id,
            asset_id=record.asset_id,
            symbol=resolve_asset_symbol(record.asset_id),
            market=record.market,
            side="long",
            quantity=update.quantity,
            avg_cost=update.avg_cost,
            last_price=record.executed_price,
            market_value=(update.quantity * record.executed_price).quantize(POSITION_QUANT),
            status=update.status,
            as_of=record.executed_at,
            snapshot_source="action_loop_execution",
            payload={
                "source_execution_id": record.execution_id,
                "last_execution_action": record.action,
                "last_execution_at": record.executed_at.isoformat(),
            },
        )

    def record_execution_audit(
        self,
        *,
        record: ExecutionRecordORM,
        position: PositionORM,
    ) -> None:
        """沉淀执行登记审计节点、执行记忆和后续复盘任务。"""

        payload = {
            "execution_id": record.execution_id,
            "order_draft_id": record.order_draft_id,
            "source_decision_id": record.decision_log_id,
            "position_id": position.position_id,
            "action": record.action,
            "executed_price": str(record.executed_price),
            "executed_quantity": str(record.executed_quantity),
            "fee": str(record.fee) if record.fee is not None else None,
        }
        execution_decision = self.memory_service.record_decision(
            decision_id=build_execution_decision_id(record.execution_id),
            owner_id=record.owner_id,
            portfolio_id=record.portfolio_id,
            asset_id=record.asset_id,
            decision_type="execution_registration",
            suggested_action=record.action,
            user_action="executed",
            summary=build_execution_summary(record),
            source_recommendation_id=None,
            source_alert_id=None,
            workflow_run_id=None,
            reason_ids=[],
            risk_ids=[],
            evidence_ids=[],
            created_at=record.executed_at,
            payload=payload,
        )
        self.memory_service.upsert_memory(
            memory_id=build_execution_memory_id(record.execution_id),
            owner_id=record.owner_id,
            memory_type="execution_note",
            scope="asset",
            asset_id=record.asset_id,
            source_decision_id=execution_decision.decision_id,
            content=build_execution_summary(record),
            confidence=Decimal("0.900000"),
            payload=payload,
            auto_index=False,
        )
        self.memory_service.schedule_review(
            review_task_id=build_execution_review_task_id(record.execution_id),
            owner_id=record.owner_id,
            asset_id=record.asset_id,
            source_decision_id=record.decision_log_id,
            review_type="execution_outcome",
            due_at=record.executed_at + timedelta(days=20),
            status="pending",
            review_questions=[
                {
                    "question": "比较原建议、实际执行价格和后续表现，判断执行是否改善了结果。",
                    "source": "action_loop_execution",
                }
            ],
            payload=payload,
        )

    def compare_execution_outcome(self, review_task: ReviewTaskORM) -> ExecutionOutcomeReviewResult:
        """比较建议、执行和后续行情，并完成执行复盘任务。"""

        payload = dict(review_task.payload or {})
        execution_id = str(payload.get("execution_id") or "").strip() or None
        if not execution_id:
            outcome_payload = {
                "review_task_id": review_task.review_task_id,
                "execution_id": None,
                "missing_fields": ["execution_id"],
                "status": "partial",
            }
            return self._complete_execution_outcome_review(
                review_task=review_task,
                execution_id=None,
                outcome="partial",
                result_summary="执行复盘缺少 execution_id，暂时只能标记为部分完成。",
                outcome_payload=outcome_payload,
            )

        record = self.action_repository.get_execution_record(execution_id)
        if record is None:
            outcome_payload = {
                "review_task_id": review_task.review_task_id,
                "execution_id": execution_id,
                "missing_fields": ["execution_record"],
                "status": "partial",
            }
            return self._complete_execution_outcome_review(
                review_task=review_task,
                execution_id=execution_id,
                outcome="partial",
                result_summary=f"执行复盘找不到执行记录 {execution_id}，暂时无法比较后续表现。",
                outcome_payload=outcome_payload,
            )

        suggested_price = resolve_suggested_price(
            payload=payload,
            record=record,
            repository=self.action_repository,
        )
        latest = self.latest_price_loader(
            session=self.session,
            asset_id=record.asset_id,
            timeframe=str(payload.get("timeframe") or "1d"),
            executed_at=record.executed_at,
        )
        latest_at = latest.get("as_of") if isinstance(latest, dict) else None
        if not isinstance(latest_at, datetime) or latest_at <= record.executed_at:
            latest = None
        latest_price = extract_latest_price(latest)
        missing_fields = []
        if suggested_price is None:
            missing_fields.append("suggested_price")
        if latest_price is None:
            missing_fields.append("latest_price")

        slippage_pct = (
            calculate_price_slippage_pct(
                action=record.action,
                suggested_price=suggested_price,
                executed_price=record.executed_price,
            )
            if suggested_price is not None
            else None
        )
        holding_return_pct = (
            calculate_execution_return_pct(
                action=record.action,
                executed_price=record.executed_price,
                latest_price=latest_price,
            )
            if latest_price is not None
            else None
        )
        outcome = resolve_execution_review_outcome(
            missing_fields=missing_fields,
            holding_return_pct=holding_return_pct,
        )
        outcome_payload = build_execution_outcome_payload(
            review_task=review_task,
            record=record,
            suggested_price=suggested_price,
            latest=latest,
            latest_price=latest_price,
            slippage_pct=slippage_pct,
            holding_return_pct=holding_return_pct,
            missing_fields=missing_fields,
            outcome=outcome,
        )
        summary = build_execution_outcome_summary(
            record=record,
            outcome=outcome,
            missing_fields=missing_fields,
            slippage_pct=slippage_pct,
            holding_return_pct=holding_return_pct,
        )
        return self._complete_execution_outcome_review(
            review_task=review_task,
            execution_id=execution_id,
            outcome=outcome,
            result_summary=summary,
            outcome_payload=outcome_payload,
        )

    def run_due_reviews(
        self,
        *,
        owner_id: str,
        limit: int = 20,
        due_at: datetime | None = None,
    ) -> JsonDict:
        """扫描到期执行复盘任务并逐条完成。"""

        due_time = due_at or self.now()
        statement = (
            select(ReviewTaskORM)
            .where(
                ReviewTaskORM.owner_id == owner_id,
                ReviewTaskORM.review_type == "execution_outcome",
                ReviewTaskORM.status == "pending",
                ReviewTaskORM.due_at <= due_time,
            )
            .order_by(ReviewTaskORM.due_at.asc())
            .limit(max(int(limit), 1))
        )
        tasks = list(self.session.scalars(statement))
        items: list[JsonDict] = []
        completed_count = 0
        partial_count = 0
        failed_count = 0
        for task in tasks:
            try:
                result = self.compare_execution_outcome(task)
            except Exception as exc:  # pragma: no cover - 调度入口必须兜底单任务异常
                failed_count += 1
                items.append(
                    {
                        "review_task_id": task.review_task_id,
                        "status": "failed",
                        "error_message": str(exc),
                    }
                )
                continue
            completed_count += 1
            if result.outcome == "partial":
                partial_count += 1
            items.append(
                {
                    "review_task_id": result.review_task_id,
                    "execution_id": result.execution_id,
                    "status": "completed",
                    "outcome": result.outcome,
                }
            )
        return {
            "status": "available",
            "owner_id": owner_id,
            "limit": limit,
            "due_at": due_time.isoformat(),
            "processed_count": len(tasks),
            "completed_count": completed_count,
            "partial_count": partial_count,
            "failed_count": failed_count,
            "items": items,
        }

    def _complete_execution_outcome_review(
        self,
        *,
        review_task: ReviewTaskORM,
        execution_id: str | None,
        outcome: str,
        result_summary: str,
        outcome_payload: JsonDict,
    ) -> ExecutionOutcomeReviewResult:
        """完成执行复盘任务并返回本轮结构化结果。"""

        payload = {
            "execution_outcome": outcome_payload,
            "closed_loop_stage": "reviewed",
        }
        self.memory_service.complete_review_task(
            review_task_id=review_task.review_task_id,
            owner_id=review_task.owner_id,
            result_summary=result_summary,
            finished_at=self.now(),
            outcome=outcome,
            payload=payload,
        )
        return ExecutionOutcomeReviewResult(
            review_task_id=review_task.review_task_id,
            execution_id=execution_id,
            outcome=outcome,
            result_summary=result_summary,
            payload=payload,
        )

    def _get_decision(self, decision_log_id: str) -> DecisionLogORM:
        decision = self.session.get(DecisionLogORM, decision_log_id)
        if decision is None:
            raise ValueError(f"决策不存在：{decision_log_id}")
        return decision


def resolve_feedback_user_action(*, feedback: str, modified_action: str | None = None) -> str:
    """把用户反馈类型转换为决策日志动作。"""

    if feedback == "modified":
        action = (modified_action or "").strip()
        if not action:
            raise ValueError("modified 反馈必须提供 modified_action")
        return action
    if feedback not in {"accepted", "rejected", "deferred"}:
        raise ValueError(f"不支持的反馈类型：{feedback}")
    return feedback


def validate_execution_registration(
    *,
    registration: ExecutionRegistration,
    action: str,
) -> None:
    """校验用户执行登记输入。"""

    if registration.source != "user_reported":
        raise ValueError("执行登记 source 当前只能为 user_reported")
    if action not in EXECUTABLE_ACTIONS:
        raise ValueError(f"不支持的执行动作：{registration.action}")
    if registration.executed_price <= 0:
        raise ValueError("执行价格必须大于 0")
    if registration.executed_quantity <= 0:
        raise ValueError("执行数量必须大于 0")


def resolve_execution_decision_log_id(
    *,
    repository: ActionRepository,
    registration: ExecutionRegistration,
) -> str | None:
    """解析执行登记关联的原始决策 ID。"""

    if registration.decision_log_id:
        return registration.decision_log_id
    if not registration.order_draft_id:
        return None
    draft = repository.get_order_draft(registration.order_draft_id)
    return draft.decision_log_id if draft is not None else None


def find_current_position(
    snapshot: PortfolioSnapshot,
    *,
    asset_id: str,
    side: str = "long",
) -> PositionORM | None:
    """从组合快照里找到当前活跃持仓。"""

    for position in snapshot.positions:
        if (
            position.asset_id == asset_id
            and position.side == side
            and getattr(position, "status", "active") == "active"
        ):
            return position
    return None


def build_position_update_from_registration(
    *,
    registration: ExecutionRegistration,
    action: str,
    current: PositionORM | None,
) -> PositionUpdate:
    """根据执行登记预计算并校验持仓变化。"""

    position_id = getattr(
        current,
        "position_id",
        build_position_id(
            portfolio_id=registration.portfolio_id,
            asset_id=registration.asset_id,
            side="long",
        ),
    )
    if action in {"buy", "add"}:
        current_quantity = decimal_or_zero(getattr(current, "quantity", None))
        return PositionUpdate(
            position_id=position_id,
            quantity=current_quantity + registration.executed_quantity,
            avg_cost=weighted_average_cost(
                current_quantity=current_quantity,
                current_avg_cost=getattr(current, "avg_cost", None),
                added_quantity=registration.executed_quantity,
                added_price=registration.executed_price,
            ),
            status="active",
        )
    if action in {"sell", "reduce"}:
        if current is None or getattr(current, "status", "active") != "active":
            raise ValueError(f"当前没有可卖出持仓：{registration.asset_id}")
        current_quantity = decimal_or_zero(current.quantity)
        if registration.executed_quantity > current_quantity:
            raise ValueError("卖出数量不能超过当前持仓")
        quantity = current_quantity - registration.executed_quantity
        return PositionUpdate(
            position_id=position_id,
            quantity=quantity,
            avg_cost=current.avg_cost,
            status="closed" if quantity == 0 else "active",
        )
    raise ValueError(f"不支持的执行动作：{registration.action}")


def decimal_or_zero(value: Decimal | None) -> Decimal:
    """把可空 Decimal 归一为 0。"""

    return value if value is not None else Decimal("0")


def weighted_average_cost(
    *,
    current_quantity: Decimal,
    current_avg_cost: Decimal | None,
    added_quantity: Decimal,
    added_price: Decimal,
) -> Decimal:
    """计算买入/加仓后的加权平均成本。"""

    total_quantity = current_quantity + added_quantity
    if total_quantity <= 0:
        raise ValueError("持仓数量必须大于 0")
    current_cost = current_quantity * decimal_or_zero(current_avg_cost)
    added_cost = added_quantity * added_price
    return ((current_cost + added_cost) / total_quantity).quantize(POSITION_QUANT)


def build_execution_id(*, registration: ExecutionRegistration, as_of: datetime) -> str:
    """生成执行登记 ID。"""

    asset_part = registration.asset_id.replace(":", "-")
    stamp = as_of.astimezone(UTC).strftime("%Y%m%d%H%M%S%f")
    return f"execution:{registration.owner_id}:{asset_part}:{stamp}"


def build_position_id(*, portfolio_id: str, asset_id: str, side: str) -> str:
    """生成持仓 ID。"""

    asset_part = asset_id.replace(":", "-")
    return f"position:{portfolio_id}:{asset_part}:{side}"


def build_execution_decision_id(execution_id: str) -> str:
    """生成执行登记审计决策 ID。"""

    return f"decision:{execution_id}:registration"


def build_execution_memory_id(execution_id: str) -> str:
    """生成执行登记记忆 ID。"""

    return f"memory:{execution_id}:execution_note"


def build_execution_review_task_id(execution_id: str) -> str:
    """生成执行登记后续复盘任务 ID。"""

    return f"review:{execution_id}:outcome"


def build_execution_summary(record: ExecutionRecordORM) -> str:
    """生成执行登记摘要。"""

    note = f"备注：{record.note}" if record.note else "无备注。"
    return (
        f"用户登记外部执行：{record.action} {record.asset_id}，"
        f"价格 {record.executed_price}，数量 {record.executed_quantity}。{note}"
    )


def load_latest_market_bar_price(
    *,
    session: Session,
    asset_id: str,
    timeframe: str = "1d",
    **_: Any,
) -> JsonDict | None:
    """从本地日 K 表读取最近一根可用收盘价。"""

    bars = MarketDataRepository(session).list_recent_bars(
        asset_id=asset_id,
        timeframe=timeframe,
        limit=1,
        closed_only=True,
    )
    if not bars:
        return None
    latest = bars[-1]
    return {
        "price": latest.close,
        "as_of": latest.timestamp,
        "source": latest.source,
        "status": latest.status,
    }


def resolve_suggested_price(
    *,
    payload: JsonDict,
    record: ExecutionRecordORM,
    repository: ActionRepository,
) -> Decimal | None:
    """解析复盘可用的原始建议价。"""

    for key in ("suggested_price", "target_price", "reference_price"):
        value = payload.get(key)
        if value not in (None, ""):
            return Decimal(str(value))
    price_range = payload.get("suggested_price_range")
    price = midpoint_from_price_range(price_range)
    if price is not None:
        return price
    if not record.order_draft_id:
        return None
    draft = repository.get_order_draft(record.order_draft_id)
    if draft is None:
        return None
    return midpoint_from_price_range(getattr(draft, "suggested_price_range", None))


def midpoint_from_price_range(value: Any) -> Decimal | None:
    """从建议价区间取中点作为第一版复盘参考价。"""

    if not isinstance(value, dict):
        return None
    low = value.get("low")
    high = value.get("high")
    if low in (None, "") or high in (None, ""):
        return None
    return ((Decimal(str(low)) + Decimal(str(high))) / Decimal("2")).quantize(POSITION_QUANT)


def extract_latest_price(value: JsonDict | None) -> Decimal | None:
    """从行情加载结果中提取最新价格。"""

    if not value:
        return None
    price = value.get("price", value.get("close"))
    if price in (None, ""):
        return None
    return Decimal(str(price))


def calculate_price_slippage_pct(
    *,
    action: str,
    suggested_price: Decimal,
    executed_price: Decimal,
) -> Decimal:
    """计算执行价相对建议价的滑点比例。"""

    if suggested_price <= 0:
        return Decimal("0.000000")
    if normalize_action(action) in {"sell", "reduce"}:
        value = (suggested_price - executed_price) / suggested_price
    else:
        value = (executed_price - suggested_price) / suggested_price
    return quantize_ratio(value)


def calculate_execution_return_pct(
    *,
    action: str,
    executed_price: Decimal,
    latest_price: Decimal,
) -> Decimal:
    """计算执行后至复盘时点的表现。"""

    if executed_price <= 0:
        return Decimal("0.000000")
    if normalize_action(action) in {"sell", "reduce"}:
        value = (executed_price - latest_price) / executed_price
    else:
        value = (latest_price - executed_price) / executed_price
    return quantize_ratio(value)


def quantize_ratio(value: Decimal) -> Decimal:
    """比例字段统一保留 6 位小数。"""

    return value.quantize(Decimal("0.000001"))


def resolve_execution_review_outcome(
    *,
    missing_fields: list[str],
    holding_return_pct: Decimal | None,
) -> str:
    """根据数据完整性和后续表现给出规则版复盘结论。"""

    if missing_fields:
        return "partial"
    if holding_return_pct is None:
        return "partial"
    if holding_return_pct > 0:
        return "confirmed"
    if holding_return_pct < 0:
        return "failed"
    return "mixed"


def build_execution_outcome_payload(
    *,
    review_task: ReviewTaskORM,
    record: ExecutionRecordORM,
    suggested_price: Decimal | None,
    latest: JsonDict | None,
    latest_price: Decimal | None,
    slippage_pct: Decimal | None,
    holding_return_pct: Decimal | None,
    missing_fields: list[str],
    outcome: str,
) -> JsonDict:
    """构造执行复盘结构化 payload。"""

    return {
        "review_task_id": review_task.review_task_id,
        "execution_id": record.execution_id,
        "asset_id": record.asset_id,
        "action": record.action,
        "executed_at": record.executed_at.isoformat(),
        "executed_price": decimal_to_text(record.executed_price),
        "executed_quantity": decimal_to_text(record.executed_quantity),
        "suggested_price": decimal_to_text(suggested_price),
        "latest_price": decimal_to_text(latest_price),
        "latest_price_at": latest.get("as_of").isoformat()
        if latest and hasattr(latest.get("as_of"), "isoformat")
        else None,
        "latest_price_source": latest.get("source") if latest else None,
        "price_slippage_pct": decimal_to_text(slippage_pct),
        "holding_return_pct": decimal_to_text(holding_return_pct),
        "missing_fields": missing_fields,
        "outcome": outcome,
    }


def build_execution_outcome_summary(
    *,
    record: ExecutionRecordORM,
    outcome: str,
    missing_fields: list[str],
    slippage_pct: Decimal | None,
    holding_return_pct: Decimal | None,
) -> str:
    """生成执行复盘中文摘要。"""

    if outcome == "partial":
        reason = "、".join(missing_fields) or "复盘数据不完整"
        if "latest_price" in missing_fields:
            reason = f"行情缺失（{reason}）"
        return f"执行复盘部分完成：{record.asset_id} 的 {record.action} 登记缺少 {reason}，暂不下确定结论。"
    direction = "后续表现为正" if holding_return_pct and holding_return_pct > 0 else "后续表现为负"
    return (
        f"执行复盘完成：{record.asset_id} {record.action} 执行后{direction}，"
        f"执行滑点 {decimal_to_text(slippage_pct)}，"
        f"复盘收益 {decimal_to_text(holding_return_pct)}。"
    )


def decimal_to_text(value: Decimal | None) -> str | None:
    """把 Decimal 转为稳定、易读的字符串。"""

    if value is None:
        return None
    return format(value, "f")


def resolve_asset_symbol(asset_id: str) -> str:
    """从资产 ID 中解析交易代码。"""

    if ":" in asset_id:
        return asset_id.split(":", 1)[1]
    return asset_id


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


def build_action_loop_feedback_payload(
    *,
    decision: DecisionLogORM,
    feedback: str,
    comment: str | None,
    modified_action: str | None,
    user_action: str,
) -> JsonDict:
    """构造人工确认反馈 payload。"""

    return {
        "source_decision_id": decision.decision_id,
        "feedback": feedback,
        "comment": comment,
        "modified_action": modified_action,
        "resolved_user_action": user_action,
        "original_user_action": decision.user_action,
        "suggested_action": decision.suggested_action,
        "workflow_run_id": decision.workflow_run_id,
        "closed_loop_stage": "confirmed",
    }


def build_action_loop_feedback_summary(
    *,
    decision: DecisionLogORM,
    feedback: str,
    comment: str | None,
    user_action: str,
) -> str:
    """生成可沉淀为 Finance Memory 的确认摘要。"""

    note = f"备注：{comment}" if comment else "无备注。"
    return (
        f"用户对决策 {decision.decision_id} 做出 {feedback} 反馈，"
        f"系统建议动作为 {decision.suggested_action}，最终用户动作为 {user_action}。{note}"
    )


def build_feedback_decision_id(*, decision_id: str, as_of: datetime) -> str:
    """生成用户确认反馈决策日志 ID。"""

    stamp = as_of.astimezone(UTC).strftime("%Y%m%d%H%M%S%f")
    return f"feedback:{decision_id}:{stamp}"


def build_order_draft_id(*, decision: DecisionLogORM, as_of: datetime) -> str:
    """生成订单草案 ID。"""

    asset_part = str(decision.asset_id or "asset").replace(":", "-")
    stamp = as_of.astimezone(UTC).strftime("%Y%m%d%H%M%S%f")
    return f"draft:{decision.owner_id}:{asset_part}:{stamp}"


def normalize_action(action: str) -> str:
    """将推荐动作归一到订单草案动作集合。"""

    normalized = action.strip().lower()
    mapping = {
        "buy_more": "add",
        "increase": "add",
        "trim": "reduce",
        "decrease": "reduce",
    }
    return mapping.get(normalized, normalized)


def ensure_review_gate_allows_draft(*, decision: DecisionLogORM, action: str) -> None:
    """高风险动作生成草案前必须通过复核闸门。"""

    payload = dict(decision.payload or {})
    review_status = str(payload.get("review_status") or "")
    if review_status == "rejected_by_review":
        review_result = payload.get("review_result") or {}
        blocking = review_result.get("blocking_risks") if isinstance(review_result, dict) else None
        reason = "；".join(str(item) for item in blocking or []) or "高风险复核已驳回。"
        raise ValueError(f"复核已驳回，不能生成订单草案：{reason}")
    if action in HIGH_RISK_ACTIONS and review_status not in APPROVED_REVIEW_STATUSES:
        raise ValueError("高风险动作需要先完成复核后才能生成订单草案。")


def extract_json_object(payload: JsonDict | None, key: str) -> JsonDict:
    """从决策 payload 中读取 JSON 对象字段。"""

    value = (payload or {}).get(key)
    return dict(value) if isinstance(value, dict) else {}


def extract_decimal(payload: JsonDict | None, key: str) -> Decimal | None:
    """从决策 payload 中读取十进制字段。"""

    value = (payload or {}).get(key)
    if value is None or value == "":
        return None
    return Decimal(str(value))


def resolve_decision_market(decision: DecisionLogORM) -> str:
    """解析决策所属市场，优先使用显式字段，回退到 asset_id 前缀。"""

    market = getattr(decision, "market", None)
    if market:
        return str(market)
    asset_id = str(decision.asset_id or "")
    if ":" in asset_id:
        return asset_id.split(":", 1)[0]
    return "unknown"
