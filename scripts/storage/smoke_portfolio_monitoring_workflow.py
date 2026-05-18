"""持仓监控最小闭环冒烟验证。

该脚本在 M2 表基础上验证：

- 主 Agent 服务可以读取组合持仓。
- 可以聚合最新信号、风险和 Finance Memory。
- 可以执行规则版 `portfolio_monitoring` 工作流。
- 可以写入提醒、Workflow 审计、决策日志、Finance Memory 和复盘任务。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from finance_agent.agents.personal_assistant import PersonalFinanceAgentService
from finance_agent.application import MemoryService, PortfolioService
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.repositories import RiskRepository, SignalSnapshotRepository


def main() -> None:
    """执行一次持仓监控闭环冒烟验证。"""

    session_factory = create_session_factory()
    owner_id = "local_user"
    as_of = datetime(2026, 5, 17, 14, 30, tzinfo=UTC)
    portfolio_id = "portfolio:local:ashare:monitoring:v2"
    asset_id = "ashare:monitoring:600519"

    with session_scope(session_factory) as session:
        portfolios = PortfolioService(session)
        signals = SignalSnapshotRepository(session)
        risks = RiskRepository(session)
        memory = MemoryService(session)

        portfolios.upsert_portfolio(
            portfolio_id=portfolio_id,
            owner_id=owner_id,
            name="持仓监控冒烟组合",
            portfolio_type="manual",
            base_currency="CNY",
            risk_profile="balanced_growth",
            total_equity=Decimal("120000.00"),
            cash=Decimal("42000.00"),
            market_value=Decimal("78000.00"),
            max_position_weight=Decimal("0.250000"),
            max_drawdown_alert=Decimal("0.080000"),
            as_of=as_of,
            payload={"source": "smoke_portfolio_monitoring_workflow"},
        )
        portfolios.upsert_position(
            position_id=f"position:{portfolio_id}:{asset_id}:long",
            portfolio_id=portfolio_id,
            asset_id=asset_id,
            symbol="600519",
            market="ashare",
            side="long",
            quantity=Decimal("100"),
            avg_cost=Decimal("1680.00"),
            last_price=Decimal("1720.00"),
            market_value=Decimal("172000.00"),
            unrealized_pnl=Decimal("4000.00"),
            unrealized_pnl_pct=Decimal("0.023810"),
            portfolio_weight=Decimal("0.180000"),
            as_of=as_of,
            payload={"source": "smoke_portfolio_monitoring_workflow"},
        )
        signals.upsert_signal_snapshot(
            signal_id=f"signal:{asset_id}:swing:202605171430",
            asset_id=asset_id,
            symbol="600519",
            market="ashare",
            horizon="swing",
            direction="bullish",
            score=Decimal("72.000000"),
            confidence=Decimal("0.780000"),
            rule_version="smoke_signal_v1",
            status="available",
            as_of=as_of,
            payload={"source": "smoke_portfolio_monitoring_workflow"},
        )
        risk = risks.upsert_risk_finding(
            risk_id=f"risk:{asset_id}:valuation:202605171430",
            asset_id=asset_id,
            scope="asset",
            risk_type="valuation",
            severity="medium",
            score=Decimal("0.450000"),
            title="估值弹性一般",
            description="当前估值修复空间有限，加仓需要等待更强确认。",
            as_of=as_of,
            evidence_ids=[],
            payload={"source": "smoke_portfolio_monitoring_workflow"},
        )
        memory.upsert_memory(
            memory_id=f"memory:{asset_id}:prior_watch_note",
            owner_id=owner_id,
            memory_type="risk_note",
            scope="asset",
            asset_id=asset_id,
            content="此前复盘认为该标的适合等待放量突破后再考虑加仓。",
            confidence=Decimal("0.900000"),
            payload={"source": "smoke_portfolio_monitoring_workflow"},
        )

        agent = PersonalFinanceAgentService(session)
        result = agent.monitor_portfolio(
            owner_id=owner_id,
            portfolio_id=portfolio_id,
            as_of=as_of,
            workflow_run_id="workflow:smoke:portfolio_monitoring:v2:202605171430",
        )

        summary = {
            "workflow_run_id": result.workflow_run_id,
            "decision_count": len(result.decision_ids),
            "suggested_actions": [
                decision.suggested_action for decision in result.result.decisions
            ],
            "risk_ids": [risk.risk_id],
            "memory_ids": list(result.memory_ids),
            "review_task_ids": list(result.review_task_ids),
        }

    print(summary)


if __name__ == "__main__":
    main()
