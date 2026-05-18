"""验证持仓监控和观察池管理已经升级为圆桌式 Workflow。

这两个 Workflow 的规则版决策仍然保留，但 LangGraph 包装必须补齐：
工具调用、圆桌观点、模型路由、高风险复核和完整中文报告。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from finance_agent.agents.interfaces import FinanceAgentInterface
from finance_agent.application import MemoryService, PortfolioService, WatchlistService
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.repositories import (
    AssetScoreRepository,
    DataQualityRepository,
    EventRepository,
    FactorFrameRepository,
    IndicatorFrameRepository,
    RiskRepository,
    SignalSnapshotRepository,
)


def main() -> None:
    """执行持仓监控和观察池管理圆桌冒烟。"""

    session_factory = create_session_factory()
    owner_id = "owner:smoke_o4_roundtable"
    as_of = datetime(2026, 5, 18, 18, 30, tzinfo=UTC)
    portfolio_id = "portfolio:smoke:o4:roundtable"
    watchlist_id = "watchlist:smoke:o4:roundtable"
    position_asset_id = "asset:smoke:o4:portfolio"
    watch_asset_id = "asset:smoke:o4:watchlist"

    with session_scope(session_factory) as session:
        seed_roundtable_operational_data(
            session=session,
            owner_id=owner_id,
            portfolio_id=portfolio_id,
            watchlist_id=watchlist_id,
            position_asset_id=position_asset_id,
            watch_asset_id=watch_asset_id,
            as_of=as_of,
        )

        interface = FinanceAgentInterface(session)
        summaries: dict[str, dict[str, Any]] = {}
        portfolio_result = interface.run_workflow(
            workflow_type="portfolio_monitoring",
            owner_id=owner_id,
            workflow_run_id="workflow:smoke:o4:portfolio_monitoring:202605181830",
            trigger_type="manual",
            started_at=as_of,
            portfolio_id=portfolio_id,
        )
        summaries["portfolio_monitoring"] = assert_roundtable_workflow(
            interface=interface,
            workflow_type="portfolio_monitoring",
            workflow_run_id="workflow:smoke:o4:portfolio_monitoring:202605181830",
            result=portfolio_result.to_dict()["data"],
            required_tool="portfolio.get_snapshot",
        )

        watchlist_result = interface.run_workflow(
            workflow_type="watchlist_management",
            owner_id=owner_id,
            workflow_run_id="workflow:smoke:o4:watchlist_management:202605181830",
            trigger_type="manual",
            started_at=as_of,
            watchlist_id=watchlist_id,
        )
        summaries["watchlist_management"] = assert_roundtable_workflow(
            interface=interface,
            workflow_type="watchlist_management",
            workflow_run_id="workflow:smoke:o4:watchlist_management:202605181830",
            result=watchlist_result.to_dict()["data"],
            required_tool="watchlist.get_active_items",
        )

    print(summaries)


def assert_roundtable_workflow(
    *,
    interface: FinanceAgentInterface,
    workflow_type: str,
    workflow_run_id: str,
    result: dict[str, Any],
    required_tool: str,
) -> dict[str, Any]:
    """断言一个运营类 Workflow 已具备圆桌完整链路。"""

    final_state = result["final_state"]
    required_nodes = [
        "load_context",
        "data_gathering",
        "roundtable_discussion",
        "decision_synthesis",
        "high_risk_review",
        "report_draft",
    ]
    if final_state.get("node_trace") != required_nodes:
        raise AssertionError(f"{workflow_type} 节点流转不完整：{final_state.get('node_trace')}")

    opinions = final_state.get("roundtable_opinions") or []
    roles = {opinion.get("role") for opinion in opinions}
    required_roles = {
        "technical_analyst",
        "factor_analyst",
        "risk_rebuttal",
        "portfolio_manager",
        "memory_manager",
    }
    missing_roles = required_roles - roles
    if missing_roles:
        raise AssertionError(f"{workflow_type} 缺少圆桌角色：{sorted(missing_roles)}")

    tool_names = {call["tool"] for call in final_state.get("tool_calls", [])}
    required_tools = {
        required_tool,
        "factor.get_asset_factor_context",
        "signal_risk.get_asset_context",
        "memory.recall_asset_memories",
    }
    missing_tools = required_tools - tool_names
    if missing_tools:
        raise AssertionError(f"{workflow_type} 缺少工具调用：{sorted(missing_tools)}")

    if not final_state.get("model_routes"):
        raise AssertionError(f"{workflow_type} 必须记录常规模型路由。")
    if not final_state.get("high_risk_reviews"):
        raise AssertionError(f"{workflow_type} 必须记录高风险复核摘要。")

    report = result.get("report") or {}
    if report.get("workflow_type") != workflow_type:
        raise AssertionError(f"{workflow_type} 报告类型错误：{report.get('workflow_type')}")
    if not report.get("markdown") or "## 圆桌观点" not in report["markdown"]:
        raise AssertionError(f"{workflow_type} 必须返回完整中文 Markdown 报告。")

    events = interface.assistant.langgraph_adapter.list_events(workflow_run_id)
    event_types = {event.event_type for event in events}
    required_event_types = {
        "workflow_node_completed",
        "roundtable_opinion",
        "model_route",
        "high_risk_review",
        "report_draft",
    }
    missing_event_types = required_event_types - event_types
    if missing_event_types:
        raise AssertionError(
            f"{workflow_type} 缺少审计事件：{sorted(missing_event_types)}"
        )

    return {
        "workflow_type": workflow_type,
        "tool_names": sorted(tool_names),
        "roles": sorted(role for role in roles if role),
        "decision_count": final_state.get("decision_count"),
        "requires_review_count": report.get("requires_review_count"),
        "report_title": report.get("title"),
    }


def seed_roundtable_operational_data(
    *,
    session: Any,
    owner_id: str,
    portfolio_id: str,
    watchlist_id: str,
    position_asset_id: str,
    watch_asset_id: str,
    as_of: datetime,
) -> None:
    """写入运营类圆桌 Workflow 所需的最小事实数据。"""

    portfolios = PortfolioService(session)
    watchlists = WatchlistService(session)
    memory = MemoryService(session)
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
        name="O4 圆桌持仓组合",
        portfolio_type="manual",
        base_currency="CNY",
        risk_profile="balanced_growth",
        total_equity=Decimal("160000.00"),
        cash=Decimal("30000.00"),
        market_value=Decimal("130000.00"),
        max_position_weight=Decimal("0.300000"),
        max_drawdown_alert=Decimal("0.080000"),
        as_of=as_of,
        payload={"source": "smoke_portfolio_watchlist_roundtable_workflows"},
    )
    portfolios.upsert_position(
        position_id=f"position:{portfolio_id}:{position_asset_id}",
        portfolio_id=portfolio_id,
        asset_id=position_asset_id,
        symbol="PFT",
        market="ashare",
        side="long",
        quantity=Decimal("100"),
        avg_cost=Decimal("100.00"),
        last_price=Decimal("92.00"),
        market_value=Decimal("9200.00"),
        unrealized_pnl=Decimal("-800.00"),
        unrealized_pnl_pct=Decimal("-0.080000"),
        portfolio_weight=Decimal("0.180000"),
        as_of=as_of,
        payload={"source": "smoke_portfolio_watchlist_roundtable_workflows"},
    )
    watchlists.upsert_watchlist(
        watchlist_id=watchlist_id,
        owner_id=owner_id,
        name="O4 圆桌观察池",
        market="ashare",
        purpose="workflow_roundtable",
        payload={"source": "smoke_portfolio_watchlist_roundtable_workflows"},
    )
    item = watchlists.add_or_update_item(
        watchlist_item_id=f"watchlist_item:{watchlist_id}:{watch_asset_id}",
        watchlist_id=watchlist_id,
        asset_id=watch_asset_id,
        symbol="WLT",
        market="ashare",
        source_type="agent_decision",
        source_id="recommendation:o4:roundtable",
        reason="资金流和趋势同步修复，等待 Agent 决策是否升级为买入前候选。",
        watch_conditions={"trend": "维持 20 日均线上方"},
        trigger_conditions={"signal": "swing bullish 且评分大于 70"},
        invalid_conditions={"risk": "出现 high 风险或趋势转弱"},
        risk_level="medium",
        status="active",
        next_review_at=as_of,
        payload={"source": "smoke_portfolio_watchlist_roundtable_workflows"},
    )
    watchlists.record_thesis(
        thesis_id=f"thesis:{watch_asset_id}:o4_roundtable",
        owner_id=owner_id,
        asset_id=watch_asset_id,
        source_type="watchlist",
        source_id=item.watchlist_item_id,
        thesis="如果趋势修复和资金流改善同步出现，可升级为买入前候选。",
        supporting_points=[{"type": "technical", "text": "TA 趋势结构修复"}],
        risk_points=[{"type": "valuation", "text": "估值修复后追高风险上升"}],
        invalid_if={"signal": "转为 bearish"},
    )

    seed_asset_context(
        indicators=indicators,
        factors=factors,
        scores=scores,
        signals=signals,
        risks=risks,
        events=events,
        qualities=qualities,
        asset_id=position_asset_id,
        symbol="PFT",
        market="ashare",
        signal_direction="bearish",
        signal_score=Decimal("36.000000"),
        total_score=Decimal("44.000000"),
        risk_severity="high",
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
        asset_id=watch_asset_id,
        symbol="WLT",
        market="ashare",
        signal_direction="bullish",
        signal_score=Decimal("78.000000"),
        total_score=Decimal("82.000000"),
        risk_severity=None,
        as_of=as_of,
    )
    memory.upsert_memory(
        memory_id=f"memory:{position_asset_id}:o4_roundtable",
        owner_id=owner_id,
        memory_type="decision_summary",
        scope="asset",
        asset_id=position_asset_id,
        content="历史复盘提示：该持仓若趋势转弱，应优先降低组合波动。",
        confidence=Decimal("0.880000"),
        payload={"source": "smoke_portfolio_watchlist_roundtable_workflows"},
    )
    memory.upsert_memory(
        memory_id=f"memory:{watch_asset_id}:o4_roundtable",
        owner_id=owner_id,
        memory_type="watchlist_daily_reason",
        scope="asset",
        asset_id=watch_asset_id,
        content="昨日继续关注原因：趋势修复但买点仍需确认。",
        confidence=Decimal("0.860000"),
        payload={"source": "smoke_portfolio_watchlist_roundtable_workflows"},
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
    """写入单标的 TA、因子、评分、信号、风险和证据上下文。"""

    indicator_frame_id = f"indicator:{asset_id}:1d:swing:o4"
    factor_frame_id = f"factor:{asset_id}:swing:o4"
    score_id = f"score:{asset_id}:swing:o4"
    evidence_id = f"evidence:{asset_id}:o4"

    indicators.upsert_indicator_frame(
        indicator_frame_id=indicator_frame_id,
        asset_id=asset_id,
        symbol=symbol,
        market=market,
        timeframe="1d",
        horizon="swing",
        library="TA-Lib",
        input_start_at=as_of - timedelta(days=80),
        input_end_at=as_of,
        bar_count=80,
        status="available",
        as_of=as_of,
        rsi_14=Decimal("63.000000"),
        macd=Decimal("1.180000"),
        macd_signal=Decimal("0.820000"),
        macd_hist=Decimal("0.360000"),
        atr_14=Decimal("2.300000"),
        bb_percent_b=Decimal("0.710000"),
        ma_20=Decimal("31.500000"),
        ma_60=Decimal("29.400000"),
        payload={"source": "smoke_portfolio_watchlist_roundtable_workflows"},
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
        source_ids=[indicator_frame_id, f"akshare:fund_flow:{asset_id}:o4"],
        indicator_frame_id=indicator_frame_id,
        as_of=as_of,
        payload={
            "factor_groups": {
                "technical": {"score": "82.0", "source": "TA-Lib"},
                "flow": {"score": "76.0", "source": "AKShare"},
            }
        },
    )
    scores.upsert_asset_score(
        score_id=score_id,
        asset_id=asset_id,
        symbol=symbol,
        market=market,
        universe_id="universe:o4_roundtable",
        screening_id="screening:o4_roundtable",
        factor_frame_id=factor_frame_id,
        horizon="swing",
        total_score=total_score,
        rank=1 if total_score >= Decimal("80") else 30,
        confidence=Decimal("0.820000"),
        rule_version="o4_roundtable_smoke",
        status="available",
        as_of=as_of,
        technical_score=Decimal("82.000000"),
        fundamental_score=Decimal("70.000000"),
        valuation_score=Decimal("66.000000"),
        flow_score=Decimal("76.000000"),
        event_score=Decimal("72.000000"),
        risk_penalty=Decimal("3.000000"),
        missing_penalty=Decimal("0.000000"),
        rank_in_universe=1 if total_score >= Decimal("80") else 30,
        payload={"source": "smoke_portfolio_watchlist_roundtable_workflows"},
    )
    signals.upsert_signal_snapshot(
        signal_id=f"signal:{asset_id}:swing:o4",
        asset_id=asset_id,
        symbol=symbol,
        market=market,
        horizon="swing",
        direction=signal_direction,
        score=signal_score,
        confidence=Decimal("0.720000"),
        rule_version="o4_roundtable_smoke",
        status="available",
        as_of=as_of,
        payload={"source": "smoke_portfolio_watchlist_roundtable_workflows"},
    )
    events.upsert_evidence(
        evidence_id=evidence_id,
        evidence_type="factor_snapshot",
        asset_id=asset_id,
        source="AKShare/TA",
        title=f"{symbol} O4 因子证据",
        summary="TA 指标、AKShare 资金流和评分结果共同进入圆桌讨论。",
        data_ref="factor_frames",
        reliability="medium",
        as_of=as_of,
        collected_at=as_of,
        payload={"source": "smoke_portfolio_watchlist_roundtable_workflows"},
    )
    qualities.upsert_quality_snapshot(
        quality_id=f"quality:{asset_id}:o4",
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
        payload={"source": "smoke_portfolio_watchlist_roundtable_workflows"},
    )
    if risk_severity:
        risks.upsert_risk_finding(
            risk_id=f"risk:{asset_id}:o4",
            asset_id=asset_id,
            scope="asset",
            risk_type="trend_break",
            severity=risk_severity,
            score=Decimal("0.820000"),
            title=f"{symbol} 趋势转弱风险",
            description="持仓趋势转弱且出现组合波动放大迹象。",
            as_of=as_of,
            evidence_ids=[evidence_id],
            payload={"source": "smoke_portfolio_watchlist_roundtable_workflows"},
        )


if __name__ == "__main__":
    main()
