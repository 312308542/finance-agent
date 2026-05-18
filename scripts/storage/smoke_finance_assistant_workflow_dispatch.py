"""验证 FinanceAssistantService 可调度圆桌 Workflow 并写入统一审计事件。

本脚本复用 `smoke_roundtable_workflow` 的样例构造逻辑，额外验证业务内核统一调度
`recommendation_decision` 后会把圆桌观点、高风险复核和中文报告摘要落到
`agent_workflow_events`。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from finance_agent.agents import FinanceAssistantService
from finance_agent.agents.workflows.recommendation_decision import RecommendationDecisionInput
from finance_agent.application import MemoryService, PortfolioService, WatchlistService
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.repositories import (
    AssetScoreRepository,
    EventRepository,
    FactorFrameRepository,
    IndicatorFrameRepository,
    RecommendationRepository,
    RiskRepository,
    SignalSnapshotRepository,
)


def main() -> None:
    """执行业务内核 Workflow 调度冒烟。"""

    session_factory = create_session_factory()
    owner_id = "owner:smoke_dispatch"
    as_of = datetime(2026, 5, 18, 15, 0, tzinfo=UTC)
    workflow_run_id = "workflow:smoke:dispatch:recommendation_decision:202605181500"
    portfolio_id = "portfolio:smoke:dispatch"
    watchlist_id = "watchlist:smoke:dispatch"
    run_id = "run:smoke:dispatch:202605181500"
    asset_id = "asset:smoke:dispatch:candidate"
    weak_asset_id = "asset:smoke:dispatch:weak"
    indicator_frame_id = f"indicator:{asset_id}:1d:swing"
    factor_frame_id = f"factor:{asset_id}:swing"
    score_id = f"score:{asset_id}:swing"

    with session_scope(session_factory) as session:
        portfolios = PortfolioService(session)
        watchlists = WatchlistService(session)
        memory = MemoryService(session)
        recommendations = RecommendationRepository(session)
        signals = SignalSnapshotRepository(session)
        risks = RiskRepository(session)
        indicators = IndicatorFrameRepository(session)
        factors = FactorFrameRepository(session)
        scores = AssetScoreRepository(session)
        events = EventRepository(session)

        portfolios.upsert_portfolio(
            portfolio_id=portfolio_id,
            owner_id=owner_id,
            name="统一调度冒烟组合",
            portfolio_type="manual",
            base_currency="CNY",
            risk_profile="balanced_growth",
            total_equity=Decimal("120000.00"),
            cash=Decimal("30000.00"),
            market_value=Decimal("90000.00"),
            max_position_weight=Decimal("0.300000"),
            max_drawdown_alert=Decimal("0.080000"),
            as_of=as_of,
            payload={"source": "smoke_finance_assistant_workflow_dispatch"},
        )
        portfolios.upsert_position(
            position_id=f"position:{portfolio_id}:{weak_asset_id}",
            portfolio_id=portfolio_id,
            asset_id=weak_asset_id,
            symbol="DWEAK",
            market="ashare",
            side="long",
            quantity=Decimal("100"),
            avg_cost=Decimal("60.00"),
            last_price=Decimal("55.00"),
            market_value=Decimal("5500.00"),
            unrealized_pnl=Decimal("-500.00"),
            unrealized_pnl_pct=Decimal("-0.083333"),
            portfolio_weight=Decimal("0.600000"),
            as_of=as_of,
            payload={"source": "smoke_finance_assistant_workflow_dispatch"},
        )
        watchlists.upsert_watchlist(
            watchlist_id=watchlist_id,
            owner_id=owner_id,
            name="统一调度冒烟观察池",
            market="ashare",
            purpose="workflow_dispatch",
            payload={"source": "smoke_finance_assistant_workflow_dispatch"},
        )
        recommendations.upsert_run(
            run_id=run_id,
            strategy="dispatch_smoke",
            market="ashare",
            horizon="swing",
            limit=3,
            status="available",
            started_at=as_of - timedelta(minutes=5),
            finished_at=as_of - timedelta(minutes=2),
            summary="统一调度冒烟推荐运行。",
            payload={"source": "smoke_finance_assistant_workflow_dispatch"},
        )
        recommendations.upsert_asset_recommendation(
            recommendation_id=f"asset_rec:{asset_id}:dispatch",
            run_id=run_id,
            asset_id=asset_id,
            symbol="DSP",
            name="调度标的",
            market="ashare",
            horizon="swing",
            action="buy_candidate",
            rank=1,
            total_score=Decimal("89.000000"),
            confidence=Decimal("0.830000"),
            conviction="high",
            score_id=score_id,
            factor_frame_id=factor_frame_id,
            signal_ids=[f"signal:{asset_id}:swing"],
            risk_ids=[],
            evidence_ids=[f"evidence:{asset_id}:akshare"],
            watch_conditions={"conditions": ["趋势维持 bullish"]},
            invalid_if={"conditions": ["信号转弱"]},
            summary="评分、信号和资金流均较强。",
            payload={"source": "smoke_finance_assistant_workflow_dispatch"},
        )
        signals.upsert_signal_snapshot(
            signal_id=f"signal:{asset_id}:swing",
            asset_id=asset_id,
            symbol="DSP",
            market="ashare",
            horizon="swing",
            direction="bullish",
            score=Decimal("87.000000"),
            confidence=Decimal("0.810000"),
            rule_version="dispatch_smoke",
            status="available",
            as_of=as_of,
            payload={"source": "smoke_finance_assistant_workflow_dispatch"},
        )
        signals.upsert_signal_snapshot(
            signal_id=f"signal:{weak_asset_id}:swing",
            asset_id=weak_asset_id,
            symbol="DWEAK",
            market="ashare",
            horizon="swing",
            direction="bearish",
            score=Decimal("34.000000"),
            confidence=Decimal("0.720000"),
            rule_version="dispatch_smoke",
            status="available",
            as_of=as_of,
            payload={"source": "smoke_finance_assistant_workflow_dispatch"},
        )
        risks.upsert_risk_finding(
            risk_id=f"risk:{weak_asset_id}:dispatch",
            asset_id=weak_asset_id,
            scope="asset",
            risk_type="trend_break",
            severity="high",
            score=Decimal("0.800000"),
            title="弱持仓趋势破位",
            description="弱持仓信号转弱且仓位偏高。",
            as_of=as_of,
            evidence_ids=[],
            payload={"source": "smoke_finance_assistant_workflow_dispatch"},
        )
        indicators.upsert_indicator_frame(
            indicator_frame_id=indicator_frame_id,
            asset_id=asset_id,
            symbol="DSP",
            market="ashare",
            timeframe="1d",
            horizon="swing",
            library="TA-Lib",
            input_start_at=as_of - timedelta(days=60),
            input_end_at=as_of,
            bar_count=60,
            status="available",
            as_of=as_of,
            rsi_14=Decimal("68.000000"),
            macd=Decimal("1.150000"),
            macd_signal=Decimal("0.850000"),
            macd_hist=Decimal("0.300000"),
            atr_14=Decimal("2.100000"),
            bb_percent_b=Decimal("0.800000"),
            ma_20=Decimal("32.000000"),
            ma_60=Decimal("29.000000"),
            payload={"source": "smoke_finance_assistant_workflow_dispatch"},
        )
        factors.upsert_factor_frame(
            factor_frame_id=factor_frame_id,
            asset_id=asset_id,
            symbol="DSP",
            market="ashare",
            horizon="swing",
            status="available",
            total_available_groups=5,
            missing_groups=[],
            source_ids=[indicator_frame_id, "akshare:fund_flow:dispatch"],
            indicator_frame_id=indicator_frame_id,
            as_of=as_of,
            payload={"factor_groups": {"technical": 84, "flow": 79, "fundamental": 71}},
        )
        scores.upsert_asset_score(
            score_id=score_id,
            asset_id=asset_id,
            symbol="DSP",
            market="ashare",
            universe_id="universe:dispatch",
            screening_id="screening:dispatch",
            factor_frame_id=factor_frame_id,
            horizon="swing",
            total_score=Decimal("89.000000"),
            rank=1,
            confidence=Decimal("0.830000"),
            rule_version="dispatch_smoke",
            status="available",
            as_of=as_of,
            risk_penalty=Decimal("2.000000"),
            missing_penalty=Decimal("0.000000"),
            technical_score=Decimal("84.000000"),
            fundamental_score=Decimal("71.000000"),
            flow_score=Decimal("79.000000"),
            event_score=Decimal("73.000000"),
            rank_in_universe=1,
            payload={"source": "smoke_finance_assistant_workflow_dispatch"},
        )
        events.upsert_evidence(
            evidence_id=f"evidence:{asset_id}:akshare",
            evidence_type="fund_flow",
            asset_id=asset_id,
            source="AKShare",
            title="资金流支持",
            summary="AKShare 资金流数据支持继续观察。",
            data_ref="capital_flow_snapshots",
            reliability="medium",
            as_of=as_of,
            collected_at=as_of,
            payload={"source": "smoke_finance_assistant_workflow_dispatch"},
        )
        memory.upsert_memory(
            memory_id=f"memory:{asset_id}:dispatch",
            owner_id=owner_id,
            memory_type="candidate_intake_reason",
            scope="asset",
            asset_id=asset_id,
            content="历史入池理由：趋势和资金流共振时可持续跟踪。",
            confidence=Decimal("0.900000"),
            payload={"source": "smoke_finance_assistant_workflow_dispatch"},
        )

        workflow_input = RecommendationDecisionInput(
            owner_id=owner_id,
            recommendation_run_id=run_id,
            portfolio_id=portfolio_id,
            watchlist=watchlists.get_watchlist(watchlist_id),
            recommendations=tuple(recommendations.list_top_recommendations(run_id=run_id, limit=3)),
            positions=tuple(portfolios.load_portfolio_snapshot(portfolio_id).positions),
            watchlist_items=tuple(
                watchlists.list_active_items(owner_id=owner_id, watchlist_id=watchlist_id)
            ),
            signals_by_asset={
                asset_id: signals.get_latest_signal(asset_id=asset_id, horizon="swing"),
                weak_asset_id: signals.get_latest_signal(asset_id=weak_asset_id, horizon="swing"),
            },
            risks_by_asset={
                asset_id: tuple(risks.list_recent_risks(asset_id=asset_id, limit=5)),
                weak_asset_id: tuple(risks.list_recent_risks(asset_id=weak_asset_id, limit=5)),
            },
            memories_by_asset={
                asset_id: tuple(
                    memory.memories.list_active_memories(
                        owner_id=owner_id,
                        asset_id=asset_id,
                        limit=5,
                    )
                )
            },
            as_of=as_of,
        )

        assistant = FinanceAssistantService(session)
        result = assistant.run_workflow(
            workflow_type="recommendation_decision",
            owner_id=owner_id,
            workflow_run_id=workflow_run_id,
            trigger_type="manual",
            started_at=as_of,
            initial_state={"workflow_input": workflow_input, "session": session},
        )

        events_written = assistant.workflow_audit.repository.list_events(workflow_run_id)
        event_types = {event.event_type for event in events_written}
        required_events = {
            "workflow_node_completed",
            "roundtable_opinion",
            "high_risk_review",
            "report_draft",
        }
        missing = required_events - event_types
        if missing:
            raise AssertionError(f"缺少统一审计事件: {sorted(missing)}")
        if result.report is None or "DSP" not in result.report["title"]:
            raise AssertionError("统一调度结果必须返回中文报告摘要。")
        if not result.final_state.get("roundtable_opinions"):
            raise AssertionError("统一调度必须保留圆桌观点。")

        print(
            {
                "workflow_run_id": result.workflow_run_id,
                "workflow_type": result.workflow_type,
                "event_types": sorted(event_types),
                "report_title": result.report["title"],
                "roundtable_count": len(result.final_state["roundtable_opinions"]),
            }
        )


if __name__ == "__main__":
    main()
