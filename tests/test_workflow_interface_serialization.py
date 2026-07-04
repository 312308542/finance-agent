from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from finance_agent.agents.interfaces import serialize_workflow_summary
from finance_agent.storage.orm import RiskFindingORM


def test_serialize_workflow_summary_sanitizes_nested_orm_objects() -> None:
    """Workflow 摘要输出前应深度清理 ORM，避免 CLI JSON 序列化失败。"""

    risk = RiskFindingORM(
        risk_id="risk:ashare:600519:demo",
        asset_id="ashare:600519",
        scope="asset",
        risk_type="event",
        severity="high",
        score=Decimal("0.820000"),
        title="事件风险未解除",
        description="仍需等待公告确认。",
        as_of=datetime(2026, 7, 3, tzinfo=UTC),
        evidence_ids=["ev:risk"],
        payload={"source": "test"},
    )
    summary = SimpleNamespace(
        workflow_run_id="workflow:test",
        workflow_type="recommendation_decision",
        final_state={
            "asset_contexts": {
                "ashare:600519": {
                    "signal_risk": {
                        "risks": (risk,),
                    }
                }
            },
            "session": object(),
            "result": object(),
        },
        report={"summary": "测试报告"},
    )

    payload = serialize_workflow_summary(summary)

    json.dumps(payload, ensure_ascii=False)
    risks = payload["final_state"]["asset_contexts"]["ashare:600519"]["signal_risk"]["risks"]
    assert risks == [
        {
            "risk_id": "risk:ashare:600519:demo",
            "asset_id": "ashare:600519",
            "scope": "asset",
            "risk_type": "event",
            "severity": "high",
            "score": "0.820000",
            "title": "事件风险未解除",
            "description": "仍需等待公告确认。",
            "as_of": "2026-07-03T00:00:00+00:00",
            "evidence_ids": ["ev:risk"],
            "payload": {"source": "test"},
        }
    ]
    assert "session" not in payload["final_state"]
    assert "result" not in payload["final_state"]
