from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from finance_agent.agents.interfaces import find_report_from_events


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
