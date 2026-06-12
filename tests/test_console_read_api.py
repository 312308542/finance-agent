from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from finance_agent.agents.interfaces import find_report_from_events
from finance_agent.api import routes
from finance_agent.application.dashboard_service import DashboardService


def test_report_detail_appends_async_high_risk_review_results() -> None:
    """报告详情应合并异步高风险复核结果，供前端展示最新结论。"""

    events = (
        SimpleNamespace(
            event_type="report_draft",
            agent_name="report_draft",
            created_at=datetime(2026, 6, 12, 10, 0, tzinfo=UTC),
            payload={
                "output": {
                    "report": {
                        "title": "单标的深度分析报告",
                        "markdown": "# 单标的深度分析报告\n\n## 模型路由与复核\n复核状态：requires_model_review",
                        "review_status": {
                            "status": "requires_model_review",
                            "requires_review_count": 1,
                        },
                    }
                }
            },
        ),
        SimpleNamespace(
            event_type="model_review_result",
            agent_name="model_review_result",
            created_at=datetime(2026, 6, 12, 10, 5, tzinfo=UTC),
            payload={
                "verdict": "approve",
                "review_status": "approved_by_review",
                "confidence": 0.91,
                "reasons": ["复核通过，风险证据已被解释。"],
                "blocking_risks": [],
                "data_gaps": [],
            },
        ),
    )

    report = find_report_from_events(events)

    assert report is not None
    assert report["review_status"]["status"] == "approved_by_review"
    assert report["review_status"]["result_count"] == 1
    assert report["review_results"][0]["review_status"] == "approved_by_review"
    assert report["report_review_appended"]["items"][0]["verdict"] == "approve"
    assert "## 异步高风险复核结果" in report["markdown"]
    assert "复核通过，风险证据已被解释。" in report["markdown"]


def test_dashboard_report_list_returns_recent_workflow_runs() -> None:
    """报告列表应从 Workflow run 生成可展示摘要。"""

    service = DashboardService.__new__(DashboardService)
    service._list_recent_workflow_runs = lambda *, owner_id, limit: [
        SimpleNamespace(
            workflow_run_id="workflow:report:1",
            owner_id=owner_id,
            workflow_type="asset_deep_analysis",
            trigger_type="manual",
            trigger_ref="ashare:600519",
            status="succeeded",
            started_at=datetime(2026, 6, 12, 9, 30, tzinfo=UTC),
            finished_at=datetime(2026, 6, 12, 9, 31, tzinfo=UTC),
            input_ref="asset:ashare:600519",
            output_ref="agent_workflow:workflow:report:1",
            payload={
                "report_title": "贵州茅台深度分析",
                "summary": "建议继续观察，等待复核完成。",
                "review_status": "approved_by_review",
            },
        )
    ]

    payload = service.get_report_list(owner_id="owner:demo", limit=20)

    assert payload["status"] == "ok"
    assert payload["metrics"]["report_count"] == 1
    assert payload["items"][0]["workflow_run_id"] == "workflow:report:1"
    assert payload["items"][0]["title"] == "贵州茅台深度分析"
    assert payload["items"][0]["review_status"] == "approved_by_review"


def test_dashboard_alert_center_merges_alerts_and_trigger_events() -> None:
    """提醒中心应合并监控提醒和 Agent 触发事件并按时间倒序展示。"""

    service = DashboardService.__new__(DashboardService)
    service._list_recent_alerts = lambda *, owner_id, limit: [
        SimpleNamespace(
            alert_id="alert:1",
            owner_id=owner_id,
            portfolio_id="portfolio:demo",
            asset_id="ashare:600519",
            alert_type="risk_warning",
            severity="high",
            triggered_by="risk_engine",
            trigger_condition="风险等级升高",
            current_value=Decimal("0.85"),
            threshold_value=Decimal("0.80"),
            status="open",
            as_of=datetime(2026, 6, 12, 10, 0, tzinfo=UTC),
            payload={"summary": "风险升高。"},
        )
    ]
    service._list_recent_trigger_events = lambda *, owner_id, limit: [
        SimpleNamespace(
            trigger_event_id="trigger:1",
            owner_id=owner_id,
            trigger_type="intraday_drop",
            trigger_ref="ashare:600519",
            dedup_key="drop:600519",
            severity="medium",
            status="dispatched",
            agent_runtime="hermes_agent",
            agent_task_id="task:1",
            requested_workflow_type="asset_deep_analysis",
            portfolio_id=None,
            watchlist_id=None,
            recommendation_run_id=None,
            asset_id="ashare:600519",
            triggered_at=datetime(2026, 6, 12, 10, 5, tzinfo=UTC),
            dispatched_at=datetime(2026, 6, 12, 10, 6, tzinfo=UTC),
            payload={"summary": "盘中急跌触发。"},
        )
    ]

    payload = service.get_alert_center(owner_id="owner:demo", status=None, limit=20)

    assert payload["status"] == "ok"
    assert payload["metrics"]["alert_count"] == 1
    assert payload["metrics"]["trigger_count"] == 1
    assert payload["metrics"]["high_severity_count"] == 1
    assert [item["item_type"] for item in payload["items"]] == [
        "trigger_event",
        "monitoring_alert",
    ]


def test_dashboard_recent_memories_returns_cross_asset_flow() -> None:
    """最近记忆流应跨资产返回 active/stale Finance Memory。"""

    service = DashboardService.__new__(DashboardService)
    service.memories = SimpleNamespace(
        list_memories=lambda **kwargs: [
            SimpleNamespace(
                memory_id="memory:1",
                owner_id=kwargs["owner_id"],
                memory_type="decision_summary",
                scope="asset",
                asset_id="ashare:600519",
                source_decision_id="decision:1",
                source_review_task_id=None,
                content="用户确认继续观察贵州茅台。",
                confidence=Decimal("0.8"),
                status="active",
                created_at=datetime(2026, 6, 12, 10, 0, tzinfo=UTC),
                updated_at=datetime(2026, 6, 12, 10, 5, tzinfo=UTC),
                payload={"workflow_run_id": "workflow:1"},
            )
        ]
    )

    payload = service.get_recent_memories(owner_id="owner:demo", limit=20)

    assert payload["status"] == "ok"
    assert payload["metrics"]["memory_count"] == 1
    assert payload["items"][0]["memory_id"] == "memory:1"
    assert payload["items"][0]["asset_id"] == "ashare:600519"


def test_console_read_routes_delegate_to_dashboard_service(monkeypatch: Any) -> None:
    """新增只读端点应复用 DashboardService，不在路由中拼业务逻辑。"""

    class FakeDashboardService:
        def __init__(self, session: object) -> None:
            self.session = session

        def get_report_list(self, *, owner_id: str, limit: int) -> dict[str, Any]:
            return {"status": "ok", "owner_id": owner_id, "limit": limit, "items": []}

        def get_alert_center(
            self,
            *,
            owner_id: str,
            status: str | None,
            limit: int,
        ) -> dict[str, Any]:
            return {"status": "ok", "owner_id": owner_id, "filter_status": status, "items": []}

        def get_recent_memories(self, *, owner_id: str, limit: int) -> dict[str, Any]:
            return {"status": "ok", "owner_id": owner_id, "limit": limit, "items": []}

    monkeypatch.setattr(routes, "DashboardService", FakeDashboardService)

    assert routes.reports(owner_id="owner:demo", limit=20, session=object())["status"] == "ok"
    assert (
        routes.alerts(owner_id="owner:demo", status="open", limit=20, session=object())[
            "filter_status"
        ]
        == "open"
    )
    assert (
        routes.recent_memories(owner_id="owner:demo", limit=20, session=object())[
            "owner_id"
        ]
        == "owner:demo"
    )
