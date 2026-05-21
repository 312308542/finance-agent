"""Finance Memory 增强闭环冒烟验证。

覆盖向量召回、用户反馈、复盘反写、候选池每日关注原因沉淀和可信度衰减。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import update

from finance_agent.application import MemoryService, WatchlistService
from finance_agent.application.memory_service import (
    build_feedback_memory_id,
    build_memory_embedding_id,
    build_review_result_memory_id,
    build_watchlist_daily_reason_memory_id,
)
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.orm import AssistantMemoryORM, MemoryEmbeddingORM
from finance_agent.storage.repositories import AssetRepository


def main() -> None:
    """执行 Finance Memory 闭环增强冒烟验证。"""

    session_factory = create_session_factory()
    as_of = datetime.now(UTC).replace(microsecond=0)
    stamp = as_of.strftime("%Y%m%d%H%M%S")
    owner_id = f"owner:smoke:finance_memory:{stamp}"
    asset_id = f"asset:smoke:finance_memory:{stamp}:candidate"
    symbol = f"FMC{stamp[-6:]}"
    watchlist_id = f"watchlist:smoke:finance_memory:{stamp}"
    watchlist_item_id = f"watchlist_item:{watchlist_id}:{asset_id}"

    with session_scope(session_factory) as session:
        AssetRepository(session).upsert_asset(
            asset_id=asset_id,
            symbol=symbol,
            name="Finance Memory 闭环候选",
            market="ashare",
            asset_type="stock",
            exchange="SSE",
            currency="CNY",
            payload={"source": "smoke_finance_memory_closed_loop"},
        )
        watchlists = WatchlistService(session)
        watchlists.upsert_watchlist(
            watchlist_id=watchlist_id,
            owner_id=owner_id,
            name="Finance Memory 闭环观察池",
            market="ashare",
            purpose="finance_memory_closed_loop",
            payload={"source": "smoke_finance_memory_closed_loop"},
        )
        watchlists.add_or_update_item(
            watchlist_item_id=watchlist_item_id,
            watchlist_id=watchlist_id,
            asset_id=asset_id,
            symbol=symbol,
            market="ashare",
            source_type="recommendation_agent",
            reason="趋势修复叠加资金流改善，纳入候选池持续观察。",
            trigger_conditions={"signal": "swing bullish"},
            invalid_conditions={"risk": "趋势跌破 20 日均线"},
            payload={"source": "smoke_finance_memory_closed_loop"},
        )

        memory_service = MemoryService(session)
        decision = memory_service.record_decision(
            decision_id=f"decision:{stamp}:candidate_intake",
            owner_id=owner_id,
            asset_id=asset_id,
            decision_type="candidate_intake",
            suggested_action="add_to_watchlist",
            user_action="unknown",
            summary="Agent 判断该标的进入启动前观察区间。",
            created_at=as_of,
            payload={"source": "smoke_finance_memory_closed_loop"},
        )
        intake_memory = memory_service.upsert_memory(
            memory_id=f"memory:{stamp}:candidate_intake",
            owner_id=owner_id,
            memory_type="candidate_intake_reason",
            scope="asset",
            asset_id=asset_id,
            source_decision_id=decision.decision_id,
            content=f"{symbol} 入池原因：趋势修复、量能温和放大，等待突破确认。",
            confidence=Decimal("0.820000"),
            payload={"source": "smoke_finance_memory_closed_loop"},
        )
        refreshed_intake = memory_service.memories.get_memory(intake_memory.memory_id)
        if refreshed_intake is None or refreshed_intake.embedding_ref != build_memory_embedding_id(
            intake_memory.memory_id
        ):
            raise AssertionError("候选入池原因写入后必须自动建立 embedding 引用。")
        if session.get(MemoryEmbeddingORM, refreshed_intake.embedding_ref) is None:
            raise AssertionError("候选入池原因必须写入 memory_embeddings。")

        first_daily = memory_service.record_watchlist_daily_reason(
            owner_id=owner_id,
            watchlist_id=watchlist_id,
            watchlist_item_id=watchlist_item_id,
            asset_id=asset_id,
            symbol=symbol,
            daily_watch_reason="第 1 天继续观察：价格站回短期均线，但尚未突破前高。",
            as_of=as_of,
            next_status="active",
            original_intake_reason="趋势修复叠加资金流改善，纳入候选池持续观察。",
            risk_rebuttal="若量能萎缩或跌回均线，入池逻辑失效。",
            source_decision_id=decision.decision_id,
            payload={"source": "smoke_finance_memory_closed_loop"},
        )
        second_daily = memory_service.record_watchlist_daily_reason(
            owner_id=owner_id,
            watchlist_id=watchlist_id,
            watchlist_item_id=watchlist_item_id,
            asset_id=asset_id,
            symbol=symbol,
            daily_watch_reason="第 2 天继续观察：回踩未破，资金流仍为正。",
            as_of=as_of + timedelta(days=1),
            next_status="active",
            original_intake_reason="趋势修复叠加资金流改善，纳入候选池持续观察。",
            risk_rebuttal="仍需等待买入触发，避免追高。",
            source_decision_id=decision.decision_id,
            payload={"source": "smoke_finance_memory_closed_loop"},
        )
        expected_second_daily_memory_id = build_watchlist_daily_reason_memory_id(
            owner_id=owner_id,
            watchlist_item_id=watchlist_item_id,
            as_of=as_of + timedelta(days=1),
        )
        if second_daily.memory.memory_id != expected_second_daily_memory_id:
            raise AssertionError("每日关注原因记忆 ID 必须可重复生成，便于审计和幂等写入。")
        if session.get(
            MemoryEmbeddingORM,
            build_memory_embedding_id(second_daily.memory.memory_id),
        ) is None:
            raise AssertionError("每日关注原因必须同步写入向量索引。")

        timeline = memory_service.get_asset_memory_timeline(
            owner_id=owner_id,
            asset_id=asset_id,
            memory_type="watchlist_daily_reason",
            limit=5,
        )
        if [item["memory_id"] for item in timeline[:2]] != [
            second_daily.memory.memory_id,
            first_daily.memory.memory_id,
        ]:
            raise AssertionError(f"每日关注原因时间线必须按时间倒序返回，实际={timeline}")
        if not all(item["payload"].get("daily_watch_reason") for item in timeline[:2]):
            raise AssertionError("每日关注原因时间线必须保留 daily_watch_reason。")

        intake_confidence_before_feedback = refreshed_intake.confidence
        accepted_feedback = memory_service.record_user_feedback(
            feedback_id=f"feedback:{stamp}:accepted",
            owner_id=owner_id,
            asset_id=asset_id,
            feedback_type="user_feedback",
            suggested_action="watch",
            user_action="accepted",
            summary="用户确认该入池逻辑有效，继续跟踪但暂不买入。",
            as_of=as_of + timedelta(days=1, hours=1),
            source_memory_id=intake_memory.memory_id,
            payload={"source": "smoke_finance_memory_closed_loop"},
        )
        feedback_memory_id = build_feedback_memory_id(accepted_feedback.decision_id)
        if session.get(MemoryEmbeddingORM, build_memory_embedding_id(feedback_memory_id)) is None:
            raise AssertionError("用户反馈反写记忆必须同步写入向量索引。")
        source_after_accept = memory_service.memories.get_memory(intake_memory.memory_id)
        if (
            source_after_accept is None
            or source_after_accept.confidence <= intake_confidence_before_feedback
        ):
            raise AssertionError("用户 accepted 反馈必须提升源记忆置信度。")
        if not (source_after_accept.payload or {}).get("feedback_history"):
            raise AssertionError("用户反馈必须写回源记忆 payload 的反馈历史。")

        confidence_after_accept = source_after_accept.confidence
        rejected_feedback = memory_service.record_user_feedback(
            feedback_id=f"feedback:{stamp}:rejected",
            owner_id=owner_id,
            asset_id=asset_id,
            feedback_type="user_feedback",
            suggested_action="buy",
            user_action="rejected",
            summary="用户认为买入建议过早，缺少突破确认。",
            as_of=as_of + timedelta(days=1, hours=2),
            source_memory_id=intake_memory.memory_id,
            payload={"source": "smoke_finance_memory_closed_loop"},
        )
        source_after_reject = memory_service.memories.get_memory(intake_memory.memory_id)
        if (
            source_after_reject is None
            or source_after_reject.confidence >= confidence_after_accept
        ):
            raise AssertionError("用户 rejected 反馈必须降低源记忆置信度。")
        if (
            source_after_reject.payload or {}
        ).get("last_feedback_decision_id") != rejected_feedback.decision_id:
            raise AssertionError("源记忆必须记录最近一次用户反馈来源。")

        review_task = memory_service.schedule_review(
            review_task_id=f"review:{stamp}:candidate_intake",
            owner_id=owner_id,
            asset_id=asset_id,
            source_decision_id=decision.decision_id,
            review_type="candidate_intake_outcome",
            due_at=as_of + timedelta(days=2),
            review_questions=[{"question": "入池后是否出现突破确认？"}],
            payload={"source": "smoke_finance_memory_closed_loop"},
        )
        source_before_review = memory_service.memories.get_memory(intake_memory.memory_id)
        confidence_before_review = source_before_review.confidence if source_before_review else None
        completed_review = memory_service.complete_review_task(
            review_task_id=review_task.review_task_id,
            owner_id=owner_id,
            result_summary="复盘结论：未突破前高，入池逻辑只部分成立，需降低追踪权重。",
            finished_at=as_of + timedelta(days=2),
            outcome="needs_more_confirmation",
            payload={"source": "smoke_finance_memory_closed_loop"},
        )
        if completed_review.status != "completed":
            raise AssertionError("复盘任务必须完成。")
        review_memory_id = build_review_result_memory_id(review_task.review_task_id)
        if session.get(MemoryEmbeddingORM, build_memory_embedding_id(review_memory_id)) is None:
            raise AssertionError("复盘结果记忆必须同步写入向量索引。")
        source_after_review = memory_service.memories.get_memory(intake_memory.memory_id)
        if (
            confidence_before_review is None
            or source_after_review is None
            or source_after_review.confidence >= confidence_before_review
        ):
            raise AssertionError("不确定复盘结论必须降低源决策记忆置信度。")
        if (
            source_after_review.payload or {}
        ).get("last_review_task_id") != review_task.review_task_id:
            raise AssertionError("复盘结果必须反写到源记忆 payload。")

        old_memory = memory_service.upsert_memory(
            memory_id=f"memory:{stamp}:old_signal",
            owner_id=owner_id,
            memory_type="stale_signal_context",
            scope="asset",
            asset_id=asset_id,
            content=f"{symbol} 很早以前的短线强势记忆，应随时间衰减。",
            confidence=Decimal("0.800000"),
            payload={"source": "smoke_finance_memory_closed_loop"},
        )
        old_timestamp = as_of - timedelta(days=240)
        session.execute(
            update(AssistantMemoryORM)
            .where(AssistantMemoryORM.memory_id == old_memory.memory_id)
            .values(created_at=old_timestamp, updated_at=old_timestamp)
        )
        session.flush()
        decay_result = memory_service.decay_memory_confidence(
            owner_id=owner_id,
            asset_id=asset_id,
            memory_type="stale_signal_context",
            as_of=as_of,
            half_life_days=30,
            stale_threshold=Decimal("0.250000"),
            archive_threshold=Decimal("0.080000"),
        )
        decayed_old = memory_service.memories.get_memory(old_memory.memory_id)
        if decayed_old is None or decayed_old.confidence >= Decimal("0.250000"):
            raise AssertionError(f"旧记忆必须按半衰期降低置信度，实际={decayed_old}")
        if decayed_old.status not in {"stale", "archived"}:
            raise AssertionError("低置信度旧记忆必须标记为 stale 或 archived。")
        if not decay_result["updated_memories"]:
            raise AssertionError("可信度衰减必须返回被更新的记忆摘要。")

        similar = memory_service.recall_similar_memories(
            owner_id=owner_id,
            asset_id=asset_id,
            query="继续观察 等待突破确认 资金流",
            limit=5,
            as_of=as_of + timedelta(days=2),
        )
        if not similar:
            raise AssertionError("向量召回必须返回相关 Finance Memory。")
        if not {"score", "ranking_score", "effective_confidence", "recency_weight"}.issubset(
            similar[0]
        ):
            raise AssertionError(f"向量召回必须暴露相似度、衰减权重和综合排序分，实际={similar[0]}")
        if decayed_old.memory_id in {item["memory_id"] for item in similar}:
            raise AssertionError("已 stale/archived 的低置信度记忆不应进入默认相似召回。")

        tool_result = memory_service.build_memory_context(
            owner_id=owner_id,
            asset_id=asset_id,
            query="入池后是否应该继续观察",
            limit=5,
            as_of=as_of + timedelta(days=2),
        )
        if not tool_result["similar_memories"] or not tool_result["timeline"]:
            raise AssertionError("Agent 记忆上下文必须同时包含相似召回和资产时间线。")

    print(
        {
            "owner_id": owner_id,
            "asset_id": asset_id,
            "timeline_count": len(timeline),
            "similar_count": len(similar),
            "decayed_status": decayed_old.status if decayed_old else None,
            "decayed_confidence": str(decayed_old.confidence) if decayed_old else None,
        }
    )


if __name__ == "__main__":
    main()
