"""人工确认与操作闭环端到端冒烟脚本。

默认模式会连接本地 PostgreSQL，写入带 `smoke_action_loop_closed_cycle` 标记的测试数据，
验证“建议 -> 用户确认 -> 订单草案 -> 外部执行登记 -> 持仓更新 -> 到期复盘 -> 记忆召回”闭环。
`--dry-run` 仅输出稳定摘要，供单元测试验证脚本入口和输出契约。
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from finance_agent.application.action_loop_service import ActionLoopService, ExecutionRegistration
from finance_agent.application.memory_service import (
    MemoryService,
    build_review_result_memory_id,
)
from finance_agent.application.portfolio_service import PortfolioService
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.orm import (
    AssistantMemoryORM,
    DecisionLogORM,
    ExecutionRecordORM,
    OrderDraftORM,
    PositionORM,
    ReviewTaskORM,
)
from finance_agent.storage.repositories import AssetRepository

JsonDict = dict[str, Any]
SMOKE_SOURCE = "smoke_action_loop_closed_cycle"


def main() -> None:
    """执行 08-T7 人工确认与操作闭环冒烟。"""

    args = parse_args()
    if args.dry_run:
        print_summary(build_dry_run_summary())
        return
    print_summary(run_database_smoke())


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="执行人工确认与操作闭环端到端冒烟。")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="不连接数据库，仅输出固定闭环摘要，供自动化测试验证脚本入口。",
    )
    return parser.parse_args()


def run_database_smoke() -> JsonDict:
    """连接真实数据库执行完整闭环并返回审计摘要。"""

    session_factory = create_session_factory()
    as_of = datetime.now(UTC).replace(microsecond=0)
    stamp = as_of.strftime("%Y%m%d%H%M%S")
    owner_id = f"owner:smoke:action_loop:{stamp}"
    portfolio_id = f"portfolio:smoke:action_loop:{stamp}"
    asset_id = f"asset:smoke:action_loop:{stamp}:600519"
    symbol = f"SMK{stamp[-6:]}"
    decision_id = f"decision:smoke:action_loop:{stamp}:recommendation"

    with session_scope(session_factory) as session:
        seed_asset_and_portfolio(
            session=session,
            owner_id=owner_id,
            portfolio_id=portfolio_id,
            asset_id=asset_id,
            symbol=symbol,
            as_of=as_of,
        )
        decision = MemoryService(session).record_decision(
            decision_id=decision_id,
            owner_id=owner_id,
            portfolio_id=portfolio_id,
            asset_id=asset_id,
            decision_type="asset_deep_analysis",
            suggested_action="buy",
            user_action="pending_user_confirmation",
            summary="冒烟建议：技术趋势改善，等待用户确认后生成外部执行草案。",
            reason_ids=[f"reason:smoke:{stamp}:trend"],
            risk_ids=[f"risk:smoke:{stamp}:position_size"],
            evidence_ids=[f"evidence:smoke:{stamp}:factor"],
            created_at=as_of,
            payload={
                "source": SMOKE_SOURCE,
                "review_status": "pending_user_confirmation",
                "suggested_price_range": {"low": "10.00", "high": "10.20"},
                "suggested_position_ratio": "0.100000",
                "constraints": {
                    "max_position_ratio": "0.10",
                    "stop_loss_reference": "9.50",
                    "note": "仅用于冒烟验证，不代表真实投资建议。",
                },
                "closed_loop_stage": "recommendation",
            },
        )

        service = ActionLoopService(
            session,
            latest_price_loader=lambda **_: {
                "price": Decimal("12.00"),
                "as_of": as_of + timedelta(days=21),
                "source": "smoke_latest_price",
            },
            now=lambda: as_of + timedelta(minutes=1),
        )
        confirmation = service.confirm_decision(
            decision_log_id=decision.decision_id,
            feedback="accepted",
            comment="冒烟验证：用户接受建议，准备生成订单草案。",
        )
        if not confirmation.can_create_order_draft:
            raise AssertionError("用户 accepted 后必须允许生成订单草案。")

        draft = service.create_order_draft(decision.decision_id)
        if "非投资建议" not in draft.disclaimer:
            raise AssertionError("订单草案必须保留非投资建议免责声明。")

        execution = service.record_execution(
            ExecutionRegistration(
                owner_id=owner_id,
                portfolio_id=portfolio_id,
                asset_id=asset_id,
                market="ashare",
                action="buy",
                executed_price=Decimal("10.10"),
                executed_quantity=Decimal("100"),
                executed_at=as_of + timedelta(minutes=5),
                order_draft_id=draft.order_draft_id,
                fee=Decimal("1.50"),
                note="用户已在外部交易软件手工执行，本系统仅登记结果。",
            )
        )

        position = get_single_position(
            session=session,
            portfolio_id=portfolio_id,
            asset_id=asset_id,
        )
        if position.quantity != Decimal("100.0000000000") or position.status != "active":
            raise AssertionError(f"执行登记后持仓数量或状态不正确：{position}")

        review_task = get_review_task_for_execution(
            session=session,
            execution_id=execution.execution_id,
        )
        review_task.due_at = as_of + timedelta(days=1)
        session.flush()

        review_summary = service.run_due_reviews(
            owner_id=owner_id,
            limit=5,
            due_at=as_of + timedelta(days=30),
        )
        if review_summary["completed_count"] != 1:
            raise AssertionError(f"到期复盘必须完成 1 条任务：{review_summary}")

        completed_review = session.get(ReviewTaskORM, review_task.review_task_id)
        review_memory = session.get(
            AssistantMemoryORM,
            build_review_result_memory_id(review_task.review_task_id),
        )
        if completed_review is None or completed_review.status != "completed":
            raise AssertionError("复盘任务必须被标记为 completed。")
        if review_memory is None:
            raise AssertionError("复盘完成后必须反写 review_result 记忆。")

        memories = MemoryService(session).get_asset_memory_timeline(
            owner_id=owner_id,
            asset_id=asset_id,
            limit=10,
        )
        if not memories:
            raise AssertionError("闭环完成后必须能召回该资产相关 Finance Memory。")

        refreshed_decision = session.get_one(DecisionLogORM, decision.decision_id)
        audit_counts = collect_audit_counts(
            session=session,
            owner_id=owner_id,
            asset_id=asset_id,
        )
        return build_summary(
            owner_id=owner_id,
            asset_id=asset_id,
            decision=refreshed_decision,
            draft=draft,
            execution=execution,
            position=position,
            review_task=completed_review,
            memory_recall_count=len(memories),
            audit_counts=audit_counts,
        )


def seed_asset_and_portfolio(
    *,
    session: Any,
    owner_id: str,
    portfolio_id: str,
    asset_id: str,
    symbol: str,
    as_of: datetime,
) -> None:
    """写入闭环冒烟所需的资产和组合基础数据。"""

    AssetRepository(session).upsert_asset(
        asset_id=asset_id,
        symbol=symbol,
        name="人工确认闭环冒烟资产",
        market="ashare",
        asset_type="stock",
        exchange="SSE",
        currency="CNY",
        payload={"source": SMOKE_SOURCE},
    )
    PortfolioService(session).upsert_portfolio(
        portfolio_id=portfolio_id,
        owner_id=owner_id,
        name="人工确认闭环冒烟组合",
        portfolio_type="manual",
        base_currency="CNY",
        risk_profile="balanced",
        total_equity=Decimal("100000"),
        cash=Decimal("100000"),
        market_value=Decimal("0"),
        as_of=as_of,
        snapshot_source=SMOKE_SOURCE,
        payload={"source": SMOKE_SOURCE},
    )


def get_single_position(*, session: Any, portfolio_id: str, asset_id: str) -> PositionORM:
    """读取闭环冒烟生成的当前持仓。"""

    statement = select(PositionORM).where(
        PositionORM.portfolio_id == portfolio_id,
        PositionORM.asset_id == asset_id,
    )
    return session.scalars(statement).one()


def get_review_task_for_execution(*, session: Any, execution_id: str) -> ReviewTaskORM:
    """读取执行登记自动生成的复盘任务。"""

    statement = select(ReviewTaskORM).where(
        ReviewTaskORM.review_type == "execution_outcome",
        ReviewTaskORM.payload["execution_id"].astext == execution_id,
    )
    return session.scalars(statement).one()


def collect_audit_counts(*, session: Any, owner_id: str, asset_id: str) -> JsonDict:
    """统计闭环各节点审计记录数量。"""

    return {
        "decisions": count_rows(session, DecisionLogORM, owner_id=owner_id, asset_id=asset_id),
        "drafts": count_rows(session, OrderDraftORM, owner_id=owner_id, asset_id=asset_id),
        "executions": count_rows(session, ExecutionRecordORM, owner_id=owner_id, asset_id=asset_id),
        "reviews": count_rows(session, ReviewTaskORM, owner_id=owner_id, asset_id=asset_id),
        "memories": count_rows(session, AssistantMemoryORM, owner_id=owner_id, asset_id=asset_id),
    }


def count_rows(session: Any, model: Any, *, owner_id: str, asset_id: str) -> int:
    """按 owner 和 asset 统计 ORM 表记录数。"""

    statement = select(model).where(model.owner_id == owner_id, model.asset_id == asset_id)
    return len(list(session.scalars(statement)))


def build_summary(
    *,
    owner_id: str,
    asset_id: str,
    decision: DecisionLogORM,
    draft: OrderDraftORM,
    execution: ExecutionRecordORM,
    position: PositionORM,
    review_task: ReviewTaskORM,
    memory_recall_count: int,
    audit_counts: JsonDict,
) -> JsonDict:
    """构造闭环冒烟摘要。"""

    return {
        "closed_loop_status": "completed",
        "owner_id": owner_id,
        "asset_id": asset_id,
        "decision_id": decision.decision_id,
        "decision_status": decision.user_action,
        "order_draft_id": draft.order_draft_id,
        "order_draft_status": draft.status,
        "execution_id": execution.execution_id,
        "execution_source": execution.source,
        "position_id": position.position_id,
        "position_status": position.status,
        "review_task_id": review_task.review_task_id,
        "review_outcome": (review_task.payload or {}).get("outcome"),
        "memory_recall_count": memory_recall_count,
        "audit_counts": audit_counts,
        "audit_chain": audit_chain(),
    }


def build_dry_run_summary() -> JsonDict:
    """返回不依赖数据库的固定闭环摘要。"""

    return {
        "closed_loop_status": "completed",
        "owner_id": "owner:smoke:dry-run",
        "asset_id": "asset:smoke:dry-run",
        "decision_id": "decision:smoke:dry-run:recommendation",
        "decision_status": "accepted",
        "order_draft_id": "draft:smoke:dry-run",
        "order_draft_status": "drafted",
        "execution_id": "execution:smoke:dry-run",
        "execution_source": "user_reported",
        "position_id": "position:smoke:dry-run",
        "position_status": "active",
        "review_task_id": "review:smoke:dry-run:outcome",
        "review_outcome": "confirmed",
        "memory_recall_count": 1,
        "audit_counts": {
            "decisions": 2,
            "drafts": 1,
            "executions": 1,
            "reviews": 1,
            "memories": 2,
        },
        "audit_chain": audit_chain(),
    }


def audit_chain() -> list[str]:
    """返回闭环审计链节点顺序。"""

    return [
        "recommendation",
        "confirmation",
        "order_draft",
        "execution",
        "review",
        "memory",
    ]


def print_summary(summary: JsonDict) -> None:
    """以 UTF-8 JSON 输出冒烟摘要。"""

    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
