"""V1.2 触发事件层冒烟验证。

验证目标：
- 触发层只读取已入库数据，不直接抓行情或计算因子。
- 持仓回撤、信号转弱、观察池条件、推荐运行、风险和数据质量均能生成触发事件。
- 观察池触发条件能使用 TA 指标、因子快照和多维评分。
- 触发事件具备冷却去重，并能派发到 Agent 唤醒队列。
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from finance_agent.application import PortfolioService, WatchlistService
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.orm import AgentWorkflowRunORM
from finance_agent.storage.repositories import (
    AssetRepository,
    AssetScoreRepository,
    DataQualityRepository,
    FactorFrameRepository,
    IndicatorFrameRepository,
    RecommendationRepository,
    RiskRepository,
    SignalSnapshotRepository,
)
from finance_agent.triggers import TriggerEvaluationRequest, TriggerService


def main() -> None:
    """执行一次 V1.2 触发事件层冒烟验证。"""

    session_factory = create_session_factory()
    as_of = datetime.now(UTC).replace(microsecond=0)
    stamp = as_of.strftime("%Y%m%d%H%M%S")
    owner_id = f"owner:smoke:v12:{stamp}"
    portfolio_id = f"portfolio:smoke:v12:{stamp}"
    watchlist_id = f"watchlist:smoke:v12:{stamp}"
    recommendation_run_id = f"run:smoke:v12:{stamp}"
    weak_asset_id = f"asset:smoke:v12:{stamp}:weak"
    candidate_asset_id = f"asset:smoke:v12:{stamp}:candidate"
    weak_symbol = f"V12W{stamp[-6:]}"
    candidate_symbol = f"V12C{stamp[-6:]}"
    indicator_frame_id = f"indicator:{candidate_asset_id}:1d:swing"
    factor_frame_id = f"factor:{candidate_asset_id}:swing"
    score_id = f"score:{candidate_asset_id}:swing"

    with session_scope(session_factory) as session:
        assets = AssetRepository(session)
        portfolios = PortfolioService(session)
        watchlists = WatchlistService(session)
        recommendations = RecommendationRepository(session)
        signals = SignalSnapshotRepository(session)
        risks = RiskRepository(session)
        indicators = IndicatorFrameRepository(session)
        factors = FactorFrameRepository(session)
        scores = AssetScoreRepository(session)
        quality = DataQualityRepository(session)

        assets.upsert_asset(
            asset_id=weak_asset_id,
            symbol=weak_symbol,
            name="V1.2 弱持仓",
            market="ashare",
            asset_type="stock",
            exchange="SSE",
            currency="CNY",
            payload={"source": "smoke_v12_trigger_events"},
        )
        assets.upsert_asset(
            asset_id=candidate_asset_id,
            symbol=candidate_symbol,
            name="V1.2 强候选",
            market="ashare",
            asset_type="stock",
            exchange="SZSE",
            currency="CNY",
            payload={"source": "smoke_v12_trigger_events"},
        )
        portfolios.upsert_portfolio(
            portfolio_id=portfolio_id,
            owner_id=owner_id,
            name="V1.2 触发冒烟组合",
            portfolio_type="manual",
            base_currency="CNY",
            risk_profile="balanced_growth",
            total_equity=Decimal("100000.00"),
            cash=Decimal("20000.00"),
            market_value=Decimal("80000.00"),
            max_drawdown_alert=Decimal("0.050000"),
            as_of=as_of,
            payload={"source": "smoke_v12_trigger_events"},
        )
        portfolios.upsert_position(
            position_id=f"position:{portfolio_id}:{weak_asset_id}:long",
            portfolio_id=portfolio_id,
            asset_id=weak_asset_id,
            symbol=weak_symbol,
            market="ashare",
            side="long",
            quantity=Decimal("100"),
            avg_cost=Decimal("100.00"),
            last_price=Decimal("92.00"),
            market_value=Decimal("9200.00"),
            unrealized_pnl=Decimal("-800.00"),
            unrealized_pnl_pct=Decimal("-0.080000"),
            portfolio_weight=Decimal("0.200000"),
            as_of=as_of,
            payload={"source": "smoke_v12_trigger_events"},
        )
        watchlists.upsert_watchlist(
            watchlist_id=watchlist_id,
            owner_id=owner_id,
            name="V1.2 触发冒烟观察池",
            market="ashare",
            purpose="trigger_smoke",
            payload={"source": "smoke_v12_trigger_events"},
        )
        watchlists.add_or_update_item(
            watchlist_item_id=f"watchlist_item:{watchlist_id}:{candidate_asset_id}",
            watchlist_id=watchlist_id,
            asset_id=candidate_asset_id,
            symbol=candidate_symbol,
            market="ashare",
            source_type="manual",
            reason="观察 TA、因子、评分和信号共振。",
            trigger_conditions={
                "signal_direction": "bullish",
                "min_signal_score": "80",
                "min_signal_confidence": "0.700000",
                "min_total_score": "80",
                "min_factor_groups": 3,
                "factor_status": "available",
                "min_rsi_14": "50",
                "require_macd_positive": True,
            },
            payload={"source": "smoke_v12_trigger_events"},
        )
        signals.upsert_signal_snapshot(
            signal_id=f"signal:{weak_asset_id}:previous",
            asset_id=weak_asset_id,
            symbol=weak_symbol,
            market="ashare",
            horizon="swing",
            direction="bullish",
            score=Decimal("72.000000"),
            confidence=Decimal("0.720000"),
            rule_version="smoke_v12",
            status="available",
            as_of=as_of - timedelta(minutes=20),
            payload={"source": "smoke_v12_trigger_events"},
        )
        signals.upsert_signal_snapshot(
            signal_id=f"signal:{weak_asset_id}:latest",
            asset_id=weak_asset_id,
            symbol=weak_symbol,
            market="ashare",
            horizon="swing",
            direction="bearish",
            score=Decimal("36.000000"),
            confidence=Decimal("0.780000"),
            rule_version="smoke_v12",
            status="available",
            as_of=as_of - timedelta(minutes=5),
            payload={"source": "smoke_v12_trigger_events"},
        )
        signals.upsert_signal_snapshot(
            signal_id=f"signal:{candidate_asset_id}:latest",
            asset_id=candidate_asset_id,
            symbol=candidate_symbol,
            market="ashare",
            horizon="swing",
            direction="bullish",
            score=Decimal("86.000000"),
            confidence=Decimal("0.820000"),
            rule_version="smoke_v12",
            status="available",
            as_of=as_of - timedelta(minutes=4),
            payload={"source": "smoke_v12_trigger_events"},
        )
        indicators.upsert_indicator_frame(
            indicator_frame_id=indicator_frame_id,
            asset_id=candidate_asset_id,
            symbol=candidate_symbol,
            market="ashare",
            timeframe="1d",
            horizon="swing",
            library="TA-Lib",
            input_start_at=as_of - timedelta(days=60),
            input_end_at=as_of,
            bar_count=60,
            status="available",
            as_of=as_of,
            rsi_14=Decimal("62.000000"),
            macd=Decimal("1.200000"),
            macd_signal=Decimal("0.800000"),
            macd_hist=Decimal("0.400000"),
            atr_14=Decimal("2.100000"),
            bb_percent_b=Decimal("0.780000"),
            ma_20=Decimal("28.000000"),
            ma_60=Decimal("25.000000"),
            payload={"source": "smoke_v12_trigger_events"},
        )
        factors.upsert_factor_frame(
            factor_frame_id=factor_frame_id,
            asset_id=candidate_asset_id,
            symbol=candidate_symbol,
            market="ashare",
            horizon="swing",
            status="available",
            total_available_groups=4,
            missing_groups=[],
            source_ids=[indicator_frame_id, "akshare:fund_flow:v12"],
            indicator_frame_id=indicator_frame_id,
            as_of=as_of,
            payload={"factor_groups": {"technical": 86, "flow": 78, "event": 70}},
        )
        scores.upsert_asset_score(
            score_id=score_id,
            asset_id=candidate_asset_id,
            symbol=candidate_symbol,
            market="ashare",
            universe_id="universe:smoke:v12",
            screening_id="screening:smoke:v12",
            factor_frame_id=factor_frame_id,
            horizon="swing",
            total_score=Decimal("88.000000"),
            technical_score=Decimal("86.000000"),
            flow_score=Decimal("78.000000"),
            event_score=Decimal("70.000000"),
            rank=1,
            rank_in_universe=1,
            confidence=Decimal("0.820000"),
            rule_version="smoke_v12",
            status="available",
            as_of=as_of,
            risk_penalty=Decimal("1.000000"),
            missing_penalty=Decimal("0.000000"),
            payload={"source": "smoke_v12_trigger_events"},
        )
        recommendations.upsert_run(
            run_id=recommendation_run_id,
            strategy="trigger_smoke",
            market="ashare",
            horizon="swing",
            limit=3,
            status="available",
            started_at=as_of - timedelta(minutes=3),
            finished_at=as_of - timedelta(minutes=2),
            summary="V1.2 触发冒烟推荐运行。",
            payload={"source": "smoke_v12_trigger_events"},
        )
        recommendations.upsert_asset_recommendation(
            recommendation_id=f"asset_rec:{candidate_asset_id}:v12",
            run_id=recommendation_run_id,
            asset_id=candidate_asset_id,
            symbol=candidate_symbol,
            name="V1.2 强候选",
            market="ashare",
            horizon="swing",
            action="buy_candidate",
            rank=1,
            total_score=Decimal("88.000000"),
            confidence=Decimal("0.820000"),
            conviction="high",
            score_id=score_id,
            factor_frame_id=factor_frame_id,
            signal_ids=[f"signal:{candidate_asset_id}:latest"],
            risk_ids=[],
            evidence_ids=[],
            watch_conditions={"conditions": ["TA 和因子共振"]},
            invalid_if={"conditions": ["信号转 bearish", "数据质量恶化"]},
            summary="V1.2 强候选满足推荐决策输入。",
            payload={"source": "smoke_v12_trigger_events"},
        )
        risks.upsert_risk_finding(
            risk_id=f"risk:{weak_asset_id}:v12",
            asset_id=weak_asset_id,
            scope="asset",
            risk_type="trend_break",
            severity="high",
            title="弱持仓趋势破位",
            description="信号转弱且持仓回撤超过阈值。",
            as_of=as_of - timedelta(minutes=1),
            evidence_ids=[],
            score=Decimal("0.850000"),
            payload={"source": "smoke_v12_trigger_events"},
        )
        quality.upsert_quality_snapshot(
            quality_id=f"quality:{candidate_asset_id}:factor:v12",
            asset_id=candidate_asset_id,
            symbol=candidate_symbol,
            market="ashare",
            data_domain="factor_frame",
            provider="factor_service",
            status="partial",
            freshness_status="stale",
            checked_at=as_of - timedelta(minutes=1),
            latest_data_at=as_of - timedelta(hours=3),
            missing_items=["valuation_score"],
            issue_count=1,
            payload={"source": "smoke_v12_trigger_events"},
        )

        service = TriggerService(session)
        request = TriggerEvaluationRequest(
            owner_id=owner_id,
            as_of=as_of,
            portfolio_id=portfolio_id,
            watchlist_id=watchlist_id,
            recommendation_run_id=recommendation_run_id,
            horizon="swing",
            timeframe="1d",
            since_minutes=120,
            cooldown_minutes=30,
        )
        evaluation = service.evaluate(request)
        created_types = {event.trigger_type for event in evaluation.created_events}
        required_types = {
            "position_drawdown",
            "signal_flip",
            "watchlist_condition_hit",
            "recommendation_run_ready",
            "risk_event_detected",
            "data_quality_degraded",
        }
        missing = required_types - created_types
        if missing:
            raise AssertionError(f"V1.2 触发事件缺失: {sorted(missing)}")
        repeat = service.evaluate(request)
        if repeat.created_events:
            raise AssertionError("触发事件冷却期内不应重复创建。")
        if len(repeat.suppressed_dedup_keys) < len(required_types):
            raise AssertionError("重复评估必须返回被冷却抑制的 dedup_key。")

        dispatch = service.dispatch_pending(owner_id=owner_id, limit=20, as_of=as_of)
        if len(dispatch.dispatched_events) < len(required_types):
            skipped = [event.payload for event in dispatch.skipped_events]
            raise AssertionError(f"触发事件必须能派发到 Agent 唤醒队列，skipped={skipped}")
        for event in dispatch.dispatched_events:
            if not event.agent_task_id:
                raise AssertionError("触发事件派发后必须生成 Agent 任务 ID。")
            if event.payload.get("dispatch_status") != "agent_wakeup_queued":
                raise AssertionError("触发事件派发后必须标记为 Agent 唤醒任务。")
            if not event.requested_workflow_type:
                raise AssertionError(
                    "触发事件必须保留建议的内部 Workflow 类型，供 Agent 按需调用。"
                )
        workflow_run_count = session.scalar(
            select(func.count())
            .select_from(AgentWorkflowRunORM)
            .where(AgentWorkflowRunORM.owner_id == owner_id)
        )
        if workflow_run_count:
            raise AssertionError("触发派发不应直接创建 Workflow run，应只唤醒 Agent。")

    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "finance_agent.cli",
            "triggers",
            "evaluate",
            "--owner-id",
            owner_id,
            "--portfolio-id",
            portfolio_id,
            "--watchlist-id",
            watchlist_id,
            "--recommendation-run-id",
            recommendation_run_id,
            "--as-of",
            as_of.isoformat(),
            "--since-minutes",
            "120",
            "--cooldown-minutes",
            "30",
        ],
        check=True,
        capture_output=True,
        encoding="utf-8",
        text=True,
    )
    cli_payload = json.loads(process.stdout)
    if cli_payload["status"] != "ok":
        raise AssertionError("触发层 CLI 必须返回 ok。")
    if cli_payload["data"]["created_count"] != 0:
        raise AssertionError("CLI 重复评估应受冷却期保护。")

    print(
        {
            "owner_id": owner_id,
            "created_types": sorted(created_types),
            "dispatched_count": len(dispatch.dispatched_events),
            "suppressed_count": len(repeat.suppressed_dedup_keys),
            "cli_status": cli_payload["status"],
        }
    )


if __name__ == "__main__":
    main()
