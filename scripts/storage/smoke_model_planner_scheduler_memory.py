"""验证模型 Planner、常驻触发调度和 Finance Memory 闭环。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from finance_agent.agents.loop import InternalFinanceAgentLoopRunner, ModelFinanceAgentPlanner
from finance_agent.application import MemoryService, WatchlistService
from finance_agent.scheduler import AssistantLoopScheduler, AssistantLoopSchedulerConfig
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.orm import DecisionLogORM, MemoryEmbeddingORM, ReviewTaskORM
from finance_agent.storage.repositories import (
    AssetRepository,
    SignalSnapshotRepository,
)
from finance_agent.triggers import TriggerEvaluationRequest


def main() -> None:
    """执行 O2/O3/O4 衔接冒烟验证。"""

    session_factory = create_session_factory()
    as_of = datetime.now(UTC).replace(microsecond=0)
    stamp = as_of.strftime("%Y%m%d%H%M%S")
    owner_id = f"owner:smoke:model_planner:{stamp}"
    asset_id = f"asset:smoke:model_planner:{stamp}:candidate"
    symbol = f"MPS{stamp[-6:]}"

    with session_scope(session_factory) as session:
        AssetRepository(session).upsert_asset(
            asset_id=asset_id,
            symbol=symbol,
            name="模型 Planner 候选",
            market="ashare",
            asset_type="stock",
            exchange="SSE",
            currency="CNY",
            payload={"source": "smoke_model_planner_scheduler_memory"},
        )
        SignalSnapshotRepository(session).upsert_signal_snapshot(
            signal_id=f"signal:{asset_id}:latest",
            asset_id=asset_id,
            symbol=symbol,
            market="ashare",
            horizon="swing",
            direction="bullish",
            score=Decimal("86.000000"),
            confidence=Decimal("0.820000"),
            rule_version="smoke_model_planner",
            status="available",
            as_of=as_of - timedelta(minutes=1),
            payload={"source": "smoke_model_planner_scheduler_memory"},
        )
        watchlist_id = f"watchlist:smoke:model_planner:{stamp}"
        watchlists = WatchlistService(session)
        watchlists.upsert_watchlist(
            watchlist_id=watchlist_id,
            owner_id=owner_id,
            name="模型 Planner 调度观察池",
            market="ashare",
            purpose="model_planner_scheduler_smoke",
            payload={"source": "smoke_model_planner_scheduler_memory"},
        )
        watchlists.add_or_update_item(
            watchlist_item_id=f"watchlist_item:{watchlist_id}:{asset_id}",
            watchlist_id=watchlist_id,
            asset_id=asset_id,
            symbol=symbol,
            market="ashare",
            source_type="manual",
            reason="验证 Scheduler 能从观察池条件唤醒 Agent。",
            trigger_conditions={
                "signal_direction": "bullish",
                "min_signal_score": "80",
                "min_signal_confidence": "0.700000",
            },
            payload={"source": "smoke_model_planner_scheduler_memory"},
        )

        memory = MemoryService(session)
        memory_a = memory.upsert_memory(
            memory_id=f"memory:{stamp}:a",
            owner_id=owner_id,
            memory_type="decision_summary",
            scope="asset",
            asset_id=asset_id,
            content="模型 Planner 候选延续强势信号，但需要等待成交量确认。",
            confidence=Decimal("0.900000"),
            payload={"source": "smoke_model_planner_scheduler_memory"},
        )
        memory_b = memory.upsert_memory(
            memory_id=f"memory:{stamp}:b",
            owner_id=owner_id,
            memory_type="review_lesson",
            scope="asset",
            asset_id=asset_id,
            content="历史复盘显示：强势信号若缺少成交量确认，次日容易回落。",
            confidence=Decimal("0.850000"),
            payload={"source": "smoke_model_planner_scheduler_memory"},
        )
        embedding_a = memory.index_memory_embedding(memory_id=memory_a.memory_id)
        embedding_b = memory.index_memory_embedding(memory_id=memory_b.memory_id)
        if not embedding_a.embedding or not embedding_b.embedding:
            raise AssertionError("Finance Memory 必须写入可召回 embedding。")

        similar = memory.recall_similar_memories(
            owner_id=owner_id,
            query="强势信号需要成交量确认",
            asset_id=asset_id,
            limit=3,
        )
        if not similar or similar[0]["memory_id"] not in {memory_a.memory_id, memory_b.memory_id}:
            raise AssertionError(f"相似召回必须返回相关记忆，实际={similar}")

        decision = memory.record_user_feedback(
            feedback_id=f"feedback:{stamp}:accept",
            owner_id=owner_id,
            asset_id=asset_id,
            feedback_type="user_feedback",
            suggested_action="watch",
            user_action="accepted",
            summary="用户确认先加入观察，不立即买入。",
            as_of=as_of,
            source_memory_id=memory_a.memory_id,
            payload={"source": "smoke_model_planner_scheduler_memory"},
        )
        feedback_memory_id = f"memory:{decision.decision_id}:feedback"
        if session.get(MemoryEmbeddingORM, f"emb:{feedback_memory_id}") is None:
            raise AssertionError("用户反馈反写后必须同步生成 embedding。")

        review_task = memory.schedule_review(
            review_task_id=f"review:{stamp}:followup",
            owner_id=owner_id,
            asset_id=asset_id,
            source_decision_id=decision.decision_id,
            review_type="decision_outcome",
            due_at=as_of,
            review_questions=[{"question": "观察后是否出现成交量确认？"}],
            payload={"source": "smoke_model_planner_scheduler_memory"},
        )
        completed_review = memory.complete_review_task(
            review_task_id=review_task.review_task_id,
            owner_id=owner_id,
            result_summary="复盘结论：信号延续，但量能不足，应继续观察。",
            finished_at=as_of,
            outcome="needs_more_confirmation",
            payload={"source": "smoke_model_planner_scheduler_memory"},
        )
        if completed_review.status != "completed":
            raise AssertionError("复盘任务必须被标记为 completed。")
        review_memory = memory.memories.list_active_memories(
            owner_id=owner_id,
            asset_id=asset_id,
            memory_type="review_result",
            limit=1,
        )
        if not review_memory:
            raise AssertionError("复盘结果必须反写为 Finance Memory。")

        scheduler = AssistantLoopScheduler(
            session=session,
            config=AssistantLoopSchedulerConfig(
                owner_id=owner_id,
                interval_seconds=0,
                trigger_limit=10,
                agent_limit=10,
                max_cycles=1,
                run_agent_once=True,
            ),
            runner=InternalFinanceAgentLoopRunner(
                session,
                planner=ModelFinanceAgentPlanner(),
            ),
        )
        scheduler_result = scheduler.run_loop(
            request=TriggerEvaluationRequest(
                owner_id=owner_id,
                as_of=as_of,
                watchlist_id=watchlist_id,
                since_minutes=30,
                cooldown_minutes=30,
            )
        ).to_dict()
        if scheduler_result["cycles"] != 1:
            raise AssertionError(f"常驻调度必须运行 1 轮，实际={scheduler_result}")
        if scheduler_result["created_count"] < 1:
            raise AssertionError(f"常驻调度必须生成触发事件，实际={scheduler_result}")
        if scheduler_result["agent_processed_count"] < 1:
            raise AssertionError(f"常驻调度必须派发并处理 Agent 事件，实际={scheduler_result}")

        decision_count = session.scalar(
            select(func.count())
            .select_from(DecisionLogORM)
            .where(DecisionLogORM.owner_id == owner_id)
        )
        if decision_count is None:
            raise AssertionError("必须能查询到决策日志。")
        completed = session.get_one(ReviewTaskORM, review_task.review_task_id)
        if completed.result_summary is None:
            raise AssertionError("复盘结果必须保存在 review_tasks.result_summary。")

    print(
        {
            "owner_id": owner_id,
            "similar_memory_count": len(similar),
            "scheduler_created_count": scheduler_result["created_count"],
            "scheduler_agent_processed_count": scheduler_result["agent_processed_count"],
            "feedback_decision_id": decision.decision_id,
            "review_task_id": review_task.review_task_id,
        }
    )


if __name__ == "__main__":
    main()
