"""验证深度分析、换股/换币、每日复盘 Workflow 具备工具调用和统一审计。

这三个 Workflow 第一阶段不接 Hermes 和真实 LLM，但必须已经形成同一条链路：
读取已入库金融事实工具 -> 受控圆桌 -> 主席裁决 -> 高风险复核 -> 中文报告审计落库。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from finance_agent.agents import FinanceAssistantService
from finance_agent.application import MemoryService, PortfolioService, WatchlistService
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.repositories import (
    AssetScoreRepository,
    DataQualityRepository,
    EventRepository,
    FactorFrameRepository,
    IndicatorFrameRepository,
    RecommendationRepository,
    RiskRepository,
    SignalSnapshotRepository,
)


def main() -> None:
    """执行三个圆桌报告 Workflow 冒烟。"""

    session_factory = create_session_factory()
    owner_id = "owner:smoke_roundtable_reports"
    as_of = datetime(2026, 5, 18, 16, 0, tzinfo=UTC)
    portfolio_id = "portfolio:smoke:roundtable_reports"
    watchlist_id = "watchlist:smoke:roundtable_reports"
    run_id = "run:smoke:roundtable_reports:202605181600"
    candidate_asset_id = "asset:smoke:roundtable_reports:candidate"
    weak_asset_id = "asset:smoke:roundtable_reports:weak"

    with session_scope(session_factory) as session:
        seed_roundtable_report_data(
            session=session,
            owner_id=owner_id,
            portfolio_id=portfolio_id,
            watchlist_id=watchlist_id,
            run_id=run_id,
            candidate_asset_id=candidate_asset_id,
            weak_asset_id=weak_asset_id,
            as_of=as_of,
        )

        assistant = FinanceAssistantService(session)
        cases = {
            "asset_deep_analysis": {
                "asset_id": candidate_asset_id,
                "owner_id": owner_id,
                "portfolio_id": portfolio_id,
                "watchlist_id": watchlist_id,
                "recommendation_run_id": run_id,
                "session": session,
            },
            "swap_decision": {
                "owner_id": owner_id,
                "source_asset_id": weak_asset_id,
                "candidate_asset_id": candidate_asset_id,
                "asset_ids": [weak_asset_id, candidate_asset_id],
                "portfolio_id": portfolio_id,
                "watchlist_id": watchlist_id,
                "recommendation_run_id": run_id,
                "session": session,
            },
            "daily_review": {
                "owner_id": owner_id,
                "portfolio_id": portfolio_id,
                "watchlist_id": watchlist_id,
                "recommendation_run_id": run_id,
                "session": session,
            },
        }

        summaries: dict[str, dict[str, Any]] = {}
        for workflow_type, initial_state in cases.items():
            workflow_run_id = f"workflow:smoke:roundtable_reports:{workflow_type}:202605181600"
            result = assistant.run_workflow(
                workflow_type=workflow_type,
                owner_id=owner_id,
                workflow_run_id=workflow_run_id,
                trigger_type="manual",
                started_at=as_of,
                initial_state=initial_state,
            )
            events = assistant.langgraph_adapter.list_events(workflow_run_id)
            event_types = {event.event_type for event in events}
            required = {
                "workflow_node_completed",
                "roundtable_opinion",
                "high_risk_review",
                "report_draft",
            }
            missing = required - event_types
            if missing:
                raise AssertionError(f"{workflow_type} 缺少统一审计事件: {sorted(missing)}")

            data_events = [
                event for event in events if event.agent_name == "data_gathering"
            ]
            if not data_events:
                raise AssertionError(f"{workflow_type} 缺少数据工具调用节点。")
            tool_names = {
                call["tool"]
                for event in data_events
                for call in event.payload["output"].get("tool_calls", [])
            }
            if "factor.get_asset_factor_context" not in tool_names:
                raise AssertionError(f"{workflow_type} 未调用因子/TA 工具。")
            if "signal_risk.get_asset_context" not in tool_names:
                raise AssertionError(f"{workflow_type} 未调用信号风险工具。")

            report = result.report or {}
            if not report.get("title") or not report.get("decision_actions"):
                raise AssertionError(f"{workflow_type} 未返回中文报告摘要。")
            if not result.final_state.get("roundtable_opinions"):
                raise AssertionError(f"{workflow_type} 未生成圆桌观点。")
            if not result.final_state.get("high_risk_reviews"):
                raise AssertionError(f"{workflow_type} 未生成高风险复核摘要。")

            summaries[workflow_type] = {
                "report_title": report["title"],
                "event_types": sorted(event_types),
                "tool_names": sorted(tool_names),
                "roundtable_count": len(result.final_state["roundtable_opinions"]),
                "requires_review_count": sum(
                    1 for item in result.final_state["high_risk_reviews"]
                    if item["requires_review"]
                ),
            }

        print(summaries)


def seed_roundtable_report_data(
    *,
    session: Any,
    owner_id: str,
    portfolio_id: str,
    watchlist_id: str,
    run_id: str,
    candidate_asset_id: str,
    weak_asset_id: str,
    as_of: datetime,
) -> None:
    """写入圆桌报告 Workflow 所需的最小事实数据。"""

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
    qualities = DataQualityRepository(session)

    portfolios.upsert_portfolio(
        portfolio_id=portfolio_id,
        owner_id=owner_id,
        name="圆桌报告冒烟组合",
        portfolio_type="manual",
        base_currency="CNY",
        risk_profile="balanced_growth",
        total_equity=Decimal("100000.00"),
        cash=Decimal("20000.00"),
        market_value=Decimal("80000.00"),
        max_position_weight=Decimal("0.300000"),
        max_drawdown_alert=Decimal("0.080000"),
        as_of=as_of,
        payload={"source": "smoke_roundtable_report_workflows"},
    )
    portfolios.upsert_position(
        position_id=f"position:{portfolio_id}:{weak_asset_id}",
        portfolio_id=portfolio_id,
        asset_id=weak_asset_id,
        symbol="RWEAK",
        market="ashare",
        side="long",
        quantity=Decimal("100"),
        avg_cost=Decimal("50.00"),
        last_price=Decimal("45.00"),
        market_value=Decimal("4500.00"),
        unrealized_pnl=Decimal("-500.00"),
        unrealized_pnl_pct=Decimal("-0.100000"),
        portfolio_weight=Decimal("0.550000"),
        as_of=as_of,
        payload={"source": "smoke_roundtable_report_workflows"},
    )
    watchlists.upsert_watchlist(
        watchlist_id=watchlist_id,
        owner_id=owner_id,
        name="圆桌报告观察池",
        market="ashare",
        purpose="workflow_roundtable_report",
        payload={"source": "smoke_roundtable_report_workflows"},
    )
    watchlists.add_or_update_item(
        watchlist_item_id=f"watchlist_item:{watchlist_id}:{candidate_asset_id}",
        watchlist_id=watchlist_id,
        asset_id=candidate_asset_id,
        symbol="RPT",
        market="ashare",
        reason="技术、资金流和基本面因子共振，等待圆桌确认。",
        source_type="agent_decision",
        source_id=run_id,
        watch_conditions={"conditions": ["RSI 不过热", "资金流维持"]},
        trigger_conditions={"conditions": ["突破前高"]},
        invalid_conditions={"conditions": ["MACD 死叉"]},
        payload={"source": "smoke_roundtable_report_workflows"},
        next_review_at=as_of + timedelta(days=1),
    )
    recommendations.upsert_run(
        run_id=run_id,
        strategy="roundtable_report_smoke",
        market="ashare",
        horizon="swing",
        limit=5,
        status="available",
        started_at=as_of - timedelta(minutes=10),
        finished_at=as_of - timedelta(minutes=5),
        summary="圆桌报告冒烟推荐运行。",
        payload={"source": "smoke_roundtable_report_workflows"},
    )
    recommendations.upsert_asset_recommendation(
        recommendation_id=f"asset_rec:{candidate_asset_id}:roundtable_report",
        run_id=run_id,
        asset_id=candidate_asset_id,
        symbol="RPT",
        name="圆桌报告标的",
        market="ashare",
        horizon="swing",
        action="buy_candidate",
        rank=1,
        total_score=Decimal("91.000000"),
        confidence=Decimal("0.860000"),
        conviction="high",
        score_id=f"score:{candidate_asset_id}:swing",
        factor_frame_id=f"factor:{candidate_asset_id}:swing",
        signal_ids=[f"signal:{candidate_asset_id}:swing"],
        risk_ids=[],
        evidence_ids=[f"evidence:{candidate_asset_id}:akshare"],
        watch_conditions={"conditions": ["资金流继续为正"]},
        invalid_if={"conditions": ["信号转弱"]},
        summary="评分、信号和 AKShare 资金流证据较强。",
        payload={"source": "smoke_roundtable_report_workflows"},
    )
    seed_asset_context(
        indicators=indicators,
        factors=factors,
        scores=scores,
        signals=signals,
        risks=risks,
        events=events,
        qualities=qualities,
        asset_id=candidate_asset_id,
        symbol="RPT",
        market="ashare",
        signal_direction="bullish",
        signal_score=Decimal("88.000000"),
        total_score=Decimal("91.000000"),
        risk_severity=None,
        as_of=as_of,
    )
    seed_asset_context(
        indicators=indicators,
        factors=factors,
        scores=scores,
        signals=signals,
        risks=risks,
        events=events,
        qualities=qualities,
        asset_id=weak_asset_id,
        symbol="RWEAK",
        market="ashare",
        signal_direction="bearish",
        signal_score=Decimal("35.000000"),
        total_score=Decimal("42.000000"),
        risk_severity="high",
        as_of=as_of,
    )
    memory.upsert_memory(
        memory_id=f"memory:{candidate_asset_id}:roundtable_report",
        owner_id=owner_id,
        memory_type="candidate_intake_reason",
        scope="asset",
        asset_id=candidate_asset_id,
        content="历史入池原因：技术趋势、资金流和业绩预期同时改善。",
        confidence=Decimal("0.900000"),
        payload={"source": "smoke_roundtable_report_workflows"},
    )


def seed_asset_context(
    *,
    indicators: IndicatorFrameRepository,
    factors: FactorFrameRepository,
    scores: AssetScoreRepository,
    signals: SignalSnapshotRepository,
    risks: RiskRepository,
    events: EventRepository,
    qualities: DataQualityRepository,
    asset_id: str,
    symbol: str,
    market: str,
    signal_direction: str,
    signal_score: Decimal,
    total_score: Decimal,
    risk_severity: str | None,
    as_of: datetime,
) -> None:
    """写入单标的工具上下文。"""

    indicator_frame_id = f"indicator:{asset_id}:1d:swing"
    factor_frame_id = f"factor:{asset_id}:swing"
    score_id = f"score:{asset_id}:swing"
    evidence_id = f"evidence:{asset_id}:akshare"

    indicators.upsert_indicator_frame(
        indicator_frame_id=indicator_frame_id,
        asset_id=asset_id,
        symbol=symbol,
        market=market,
        timeframe="1d",
        horizon="swing",
        library="TA-Lib",
        input_start_at=as_of - timedelta(days=60),
        input_end_at=as_of,
        bar_count=60,
        status="available",
        as_of=as_of,
        rsi_14=Decimal("67.000000"),
        macd=Decimal("1.250000"),
        macd_signal=Decimal("0.900000"),
        macd_hist=Decimal("0.350000"),
        atr_14=Decimal("2.000000"),
        bb_percent_b=Decimal("0.780000"),
        ma_20=Decimal("32.000000"),
        ma_60=Decimal("29.000000"),
        payload={"source": "smoke_roundtable_report_workflows"},
    )
    factors.upsert_factor_frame(
        factor_frame_id=factor_frame_id,
        asset_id=asset_id,
        symbol=symbol,
        market=market,
        horizon="swing",
        status="available",
        total_available_groups=5,
        missing_groups=[],
        source_ids=[indicator_frame_id, f"akshare:fund_flow:{asset_id}"],
        indicator_frame_id=indicator_frame_id,
        as_of=as_of,
        payload={"factor_groups": {"technical": 85, "flow": 82, "fundamental": 74}},
    )
    scores.upsert_asset_score(
        score_id=score_id,
        asset_id=asset_id,
        symbol=symbol,
        market=market,
        universe_id="universe:roundtable_report",
        screening_id="screening:roundtable_report",
        factor_frame_id=factor_frame_id,
        horizon="swing",
        total_score=total_score,
        rank=1 if total_score >= Decimal("80") else 20,
        confidence=Decimal("0.820000"),
        rule_version="roundtable_report_smoke",
        status="available",
        as_of=as_of,
        risk_penalty=Decimal("2.000000"),
        missing_penalty=Decimal("0.000000"),
        technical_score=Decimal("85.000000"),
        fundamental_score=Decimal("74.000000"),
        flow_score=Decimal("82.000000"),
        event_score=Decimal("70.000000"),
        rank_in_universe=1 if total_score >= Decimal("80") else 20,
        payload={"source": "smoke_roundtable_report_workflows"},
    )
    signals.upsert_signal_snapshot(
        signal_id=f"signal:{asset_id}:swing",
        asset_id=asset_id,
        symbol=symbol,
        market=market,
        horizon="swing",
        direction=signal_direction,
        score=signal_score,
        confidence=Decimal("0.820000"),
        rule_version="roundtable_report_smoke",
        status="available",
        as_of=as_of,
        payload={"source": "smoke_roundtable_report_workflows"},
    )
    events.upsert_evidence(
        evidence_id=evidence_id,
        evidence_type="akshare_factor",
        asset_id=asset_id,
        source="AKShare",
        title=f"{symbol} 因子证据",
        summary="AKShare 清洗数据与 TA 指标共同支撑本次圆桌讨论。",
        data_ref="factor_frames",
        reliability="medium",
        as_of=as_of,
        collected_at=as_of,
        payload={"source": "smoke_roundtable_report_workflows"},
    )
    qualities.upsert_quality_snapshot(
        quality_id=f"quality:{asset_id}:factor",
        asset_id=asset_id,
        symbol=symbol,
        market=market,
        data_domain="factor",
        provider="finance-agent",
        status="available",
        freshness_status="fresh",
        latest_data_at=as_of,
        checked_at=as_of,
        issue_count=0,
        payload={"source": "smoke_roundtable_report_workflows"},
    )
    if risk_severity:
        risks.upsert_risk_finding(
            risk_id=f"risk:{asset_id}:roundtable_report",
            asset_id=asset_id,
            scope="asset",
            risk_type="trend_break",
            severity=risk_severity,
            score=Decimal("0.800000"),
            title=f"{symbol} 趋势风险",
            description="信号转弱且组合中存在替换候选。",
            as_of=as_of,
            evidence_ids=[evidence_id],
            payload={"source": "smoke_roundtable_report_workflows"},
        )


if __name__ == "__main__":
    main()
