"""验证推荐决策 LangGraph 具备圆桌会议流转。

本脚本不调用 Hermes，也不调用外部数据源。它只验证 LangGraph Workflow 能把
已入库的推荐、TA 指标、AKShare 因子、评分、风险和记忆组织成受控圆桌。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from finance_agent.agents.workflows import build_recommendation_decision_graph
from finance_agent.agents.workflows.recommendation_decision import (
    RecommendationDecisionInput,
)
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
    """执行圆桌 Workflow 冒烟。"""

    session_factory = create_session_factory()
    owner_id = "owner:smoke_roundtable"
    as_of = datetime(2026, 5, 18, 14, 30, tzinfo=UTC)
    portfolio_id = "portfolio:smoke:roundtable"
    watchlist_id = "watchlist:smoke:roundtable"
    run_id = "run:smoke:roundtable:202605181430"
    asset_id = "asset:smoke:roundtable:decision"
    weak_asset_id = "asset:smoke:roundtable:weak"
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
            name="圆桌冒烟组合",
            portfolio_type="manual",
            base_currency="CNY",
            risk_profile="balanced_growth",
            total_equity=Decimal("100000.00"),
            cash=Decimal("20000.00"),
            market_value=Decimal("80000.00"),
            max_position_weight=Decimal("0.300000"),
            max_drawdown_alert=Decimal("0.080000"),
            as_of=as_of,
            payload={"source": "smoke_roundtable_workflow"},
        )
        portfolios.upsert_position(
            position_id=f"position:{portfolio_id}:{weak_asset_id}",
            portfolio_id=portfolio_id,
            asset_id=weak_asset_id,
            symbol="WEAK",
            market="ashare",
            side="long",
            quantity=Decimal("100"),
            avg_cost=Decimal("50.00"),
            last_price=Decimal("46.00"),
            market_value=Decimal("4600.00"),
            unrealized_pnl=Decimal("-400.00"),
            unrealized_pnl_pct=Decimal("-0.080000"),
            portfolio_weight=Decimal("0.600000"),
            as_of=as_of,
            payload={"source": "smoke_roundtable_workflow"},
        )
        watchlists.upsert_watchlist(
            watchlist_id=watchlist_id,
            owner_id=owner_id,
            name="圆桌冒烟观察池",
            market="ashare",
            purpose="roundtable",
            payload={"source": "smoke_roundtable_workflow"},
        )
        recommendations.upsert_run(
            run_id=run_id,
            strategy="roundtable_smoke",
            market="ashare",
            horizon="swing",
            limit=3,
            status="available",
            started_at=as_of - timedelta(minutes=5),
            finished_at=as_of - timedelta(minutes=2),
            summary="圆桌冒烟推荐运行。",
            payload={"source": "smoke_roundtable_workflow"},
        )
        recommendations.upsert_asset_recommendation(
            recommendation_id=f"asset_rec:{asset_id}:roundtable",
            run_id=run_id,
            asset_id=asset_id,
            symbol="RTD",
            name="圆桌标的",
            market="ashare",
            horizon="swing",
            action="buy_candidate",
            rank=1,
            total_score=Decimal("88.000000"),
            confidence=Decimal("0.820000"),
            conviction="high",
            score_id=score_id,
            factor_frame_id=factor_frame_id,
            signal_ids=[f"signal:{asset_id}:swing"],
            risk_ids=[],
            evidence_ids=[f"evidence:{asset_id}:akshare"],
            watch_conditions={"conditions": ["趋势维持 bullish"]},
            invalid_if={"conditions": ["信号转弱"]},
            summary="评分、信号和资金流均较强。",
            payload={"source": "smoke_roundtable_workflow"},
        )
        signals.upsert_signal_snapshot(
            signal_id=f"signal:{asset_id}:swing",
            asset_id=asset_id,
            symbol="RTD",
            market="ashare",
            horizon="swing",
            direction="bullish",
            score=Decimal("86.000000"),
            confidence=Decimal("0.800000"),
            rule_version="roundtable_smoke",
            status="available",
            as_of=as_of,
            payload={"source": "smoke_roundtable_workflow"},
        )
        signals.upsert_signal_snapshot(
            signal_id=f"signal:{weak_asset_id}:swing",
            asset_id=weak_asset_id,
            symbol="WEAK",
            market="ashare",
            horizon="swing",
            direction="bearish",
            score=Decimal("35.000000"),
            confidence=Decimal("0.720000"),
            rule_version="roundtable_smoke",
            status="available",
            as_of=as_of,
            payload={"source": "smoke_roundtable_workflow"},
        )
        risks.upsert_risk_finding(
            risk_id=f"risk:{weak_asset_id}:roundtable",
            asset_id=weak_asset_id,
            scope="asset",
            risk_type="trend_break",
            severity="high",
            score=Decimal("0.800000"),
            title="弱持仓趋势破位",
            description="弱持仓信号转弱且仓位偏高。",
            as_of=as_of,
            evidence_ids=[],
            payload={"source": "smoke_roundtable_workflow"},
        )
        indicators.upsert_indicator_frame(
            indicator_frame_id=indicator_frame_id,
            asset_id=asset_id,
            symbol="RTD",
            market="ashare",
            timeframe="1d",
            horizon="swing",
            library="TA-Lib",
            input_start_at=as_of - timedelta(days=60),
            input_end_at=as_of,
            bar_count=60,
            status="available",
            as_of=as_of,
            rsi_14=Decimal("67.000000"),
            macd=Decimal("1.100000"),
            macd_signal=Decimal("0.800000"),
            macd_hist=Decimal("0.300000"),
            atr_14=Decimal("2.000000"),
            bb_percent_b=Decimal("0.790000"),
            ma_20=Decimal("31.000000"),
            ma_60=Decimal("28.500000"),
            payload={"source": "smoke_roundtable_workflow"},
        )
        factors.upsert_factor_frame(
            factor_frame_id=factor_frame_id,
            asset_id=asset_id,
            symbol="RTD",
            market="ashare",
            horizon="swing",
            status="available",
            total_available_groups=5,
            missing_groups=[],
            source_ids=[indicator_frame_id, "akshare:fund_flow:roundtable"],
            indicator_frame_id=indicator_frame_id,
            as_of=as_of,
            payload={"factor_groups": {"technical": 82, "flow": 78, "fundamental": 70}},
        )
        scores.upsert_asset_score(
            score_id=score_id,
            asset_id=asset_id,
            symbol="RTD",
            market="ashare",
            universe_id="universe:roundtable",
            screening_id="screening:roundtable",
            factor_frame_id=factor_frame_id,
            horizon="swing",
            total_score=Decimal("88.000000"),
            rank=1,
            confidence=Decimal("0.820000"),
            rule_version="roundtable_smoke",
            status="available",
            as_of=as_of,
            risk_penalty=Decimal("2.000000"),
            missing_penalty=Decimal("0.000000"),
            technical_score=Decimal("82.000000"),
            fundamental_score=Decimal("70.000000"),
            flow_score=Decimal("78.000000"),
            event_score=Decimal("72.000000"),
            rank_in_universe=1,
            payload={"source": "smoke_roundtable_workflow"},
        )
        events.upsert_evidence(
            evidence_id=f"evidence:{asset_id}:akshare",
            evidence_type="fund_flow",
            asset_id=asset_id,
            source="AKShare",
            title="资金流支持",
            summary="AKShare 资金流和热度数据支持继续观察。",
            data_ref="capital_flow_snapshots",
            reliability="medium",
            as_of=as_of,
            collected_at=as_of,
            payload={"source": "smoke_roundtable_workflow"},
        )
        memory.upsert_memory(
            memory_id=f"memory:{asset_id}:roundtable",
            owner_id=owner_id,
            memory_type="candidate_intake_reason",
            scope="asset",
            asset_id=asset_id,
            content="历史入池理由：资金流和趋势共振时可持续跟踪。",
            confidence=Decimal("0.900000"),
            payload={"source": "smoke_roundtable_workflow"},
        )

        workflow_input = RecommendationDecisionInput(
            owner_id=owner_id,
            recommendation_run_id=run_id,
            portfolio_id=portfolio_id,
            watchlist=watchlists.get_watchlist(watchlist_id),
            recommendations=tuple(recommendations.list_top_recommendations(run_id=run_id, limit=3)),
            positions=tuple(portfolios.load_portfolio_snapshot(portfolio_id).positions),
            watchlist_items=tuple(
                watchlists.list_active_items(
                    owner_id=owner_id,
                    watchlist_id=watchlist_id,
                )
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
        graph = build_recommendation_decision_graph()
        final_state = graph.invoke({"workflow_input": workflow_input, "session": session})

        roles = {opinion["role"] for opinion in final_state["roundtable_opinions"]}
        required_roles = {
            "technical_analyst",
            "factor_analyst",
            "risk_rebuttal",
            "portfolio_manager",
            "memory_manager",
        }
        missing = required_roles - roles
        if missing:
            raise AssertionError(f"圆桌缺少角色观点: {sorted(missing)}")
        if not final_state["roundtable_opinions"][0].get("tool_calls"):
            raise AssertionError("圆桌角色必须记录工具调用。")
        report = final_state.get("report")
        if not report or "RTD" not in report["title"]:
            raise AssertionError("Workflow 必须生成中文报告摘要。")

        print(
            {
                "workflow": "recommendation_decision",
                "node_trace": final_state["node_trace"],
                "roles": sorted(roles),
                "decision_count": final_state["decision_count"],
                "report_title": report["title"],
            }
        )


if __name__ == "__main__":
    main()
