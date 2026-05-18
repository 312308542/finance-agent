"""观察池管理闭环冒烟验证。

该脚本先作为 TDD 红灯脚本：在 `manage_watchlist` 实现前应失败；
实现后用于验证观察池状态流转、提醒、决策日志、Finance Memory 和复盘任务。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from finance_agent.agents.personal_assistant import PersonalFinanceAgentService
from finance_agent.application import MemoryService, WatchlistService
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.repositories import RiskRepository, SignalSnapshotRepository


def main() -> None:
    """执行一次观察池管理闭环冒烟验证。"""

    session_factory = create_session_factory()
    owner_id = "local_user"
    as_of = datetime(2026, 5, 17, 15, 0, tzinfo=UTC)
    watchlist_id = "watchlist:local:ashare:management"
    asset_id = "ashare:300750"

    with session_scope(session_factory) as session:
        watchlists = WatchlistService(session)
        signals = SignalSnapshotRepository(session)
        risks = RiskRepository(session)
        memory = MemoryService(session)

        watchlist = watchlists.upsert_watchlist(
            watchlist_id=watchlist_id,
            owner_id=owner_id,
            name="观察池管理冒烟池",
            market="ashare",
            purpose="swing_candidate",
            payload={"source": "smoke_watchlist_management_workflow"},
        )
        item = watchlists.add_or_update_item(
            watchlist_item_id=f"watchlist_item:{watchlist_id}:{asset_id}",
            watchlist_id=watchlist.watchlist_id,
            asset_id=asset_id,
            symbol="300750",
            market="ashare",
            source_type="recommendation",
            source_id="recommendation:ashare:300750:202605171500",
            reason="新能源龙头回调后出现趋势修复，需要观察是否进入可买入候选。",
            watch_conditions={"trend": "站回 20 日均线", "flow": "资金流改善"},
            trigger_conditions={"signal": "swing 信号 bullish 且评分大于 70"},
            invalid_conditions={"risk": "出现 high 级别风险或趋势转弱"},
            risk_level="medium",
            status="active",
            next_review_at=as_of,
            payload={"source": "smoke_watchlist_management_workflow"},
        )
        watchlists.record_thesis(
            thesis_id=f"thesis:{asset_id}:watchlist_management",
            owner_id=owner_id,
            asset_id=asset_id,
            source_type="watchlist",
            source_id=item.watchlist_item_id,
            thesis="如果趋势修复和资金流改善同步出现，可从观察池升级为买入前候选。",
            supporting_points=[
                {"type": "technical", "text": "swing 信号转强"},
                {"type": "liquidity", "text": "龙头资产成交活跃"},
            ],
            risk_points=[
                {"type": "sector", "text": "新能源板块波动仍高"},
            ],
            invalid_if={"signal": "swing 信号转 bearish", "risk": "出现 high 风险"},
        )
        signals.upsert_signal_snapshot(
            signal_id=f"signal:{asset_id}:swing:202605171500",
            asset_id=asset_id,
            symbol="300750",
            market="ashare",
            horizon="swing",
            direction="mixed",
            score=Decimal("68.000000"),
            confidence=Decimal("0.620000"),
            rule_version="smoke_signal_v1",
            status="available",
            as_of=as_of,
            payload={"source": "smoke_watchlist_management_workflow"},
        )
        risk = risks.upsert_risk_finding(
            risk_id=f"risk:{asset_id}:sector_volatility:202605171500",
            asset_id=asset_id,
            scope="asset",
            risk_type="sector_volatility",
            severity="medium",
            score=Decimal("0.420000"),
            title="板块波动仍高",
            description="板块波动尚未完全收敛，升级为候选后仍需等待买入确认。",
            as_of=as_of,
            evidence_ids=[],
            payload={"source": "smoke_watchlist_management_workflow"},
        )
        memory.upsert_memory(
            memory_id=f"memory:{asset_id}:prior_watchlist_note",
            owner_id=owner_id,
            memory_type="watchlist_note",
            scope="asset",
            asset_id=asset_id,
            content="历史复盘认为该标的适合等待趋势修复后再进入买入前候选。",
            confidence=Decimal("0.860000"),
            payload={"source": "smoke_watchlist_management_workflow"},
        )

        agent = PersonalFinanceAgentService(session)
        result = agent.manage_watchlist(
            owner_id=owner_id,
            watchlist_id=watchlist_id,
            as_of=as_of,
            workflow_run_id="workflow:smoke:watchlist_management:202605171500",
        )

        refreshed_items = watchlists.repository.list_active_items(
            owner_id=owner_id,
            watchlist_id=watchlist_id,
        )
        events = watchlists.list_events(watchlist_id=watchlist_id, limit=10)
        daily_events = [
            event for event in events if event.event_type == "daily_watch_reason"
        ]
        if not daily_events:
            raise AssertionError("观察池每日复核必须写入 daily_watch_reason 事件。")
        daily_payload = daily_events[0].payload
        if not daily_payload.get("daily_watch_reason"):
            raise AssertionError("daily_watch_reason 事件必须保存每日继续关注原因。")
        if not daily_payload.get("original_intake_reason"):
            raise AssertionError("daily_watch_reason 事件必须保留原始入池原因。")
        daily_memories = memory.memories.list_active_memories(
            owner_id=owner_id,
            asset_id=asset_id,
            memory_type="watchlist_daily_reason",
            limit=5,
        )
        if not daily_memories:
            raise AssertionError("观察池每日复核必须沉淀 watchlist_daily_reason 记忆。")
        summary = {
            "workflow_run_id": result.workflow_run_id,
            "decision_count": len(result.decision_ids),
            "suggested_actions": [
                decision.suggested_action for decision in result.result.decisions
            ],
            "next_statuses": [
                decision.next_status for decision in result.result.decisions
            ],
            "active_item_count": len(refreshed_items),
            "risk_ids": [risk.risk_id],
            "memory_ids": list(result.memory_ids),
            "daily_reason_memory_ids": [
                memory.memory_id for memory in daily_memories
            ],
            "daily_watch_reason": daily_payload.get("daily_watch_reason"),
            "review_task_ids": list(result.review_task_ids),
        }

    print(summary)


if __name__ == "__main__":
    main()
