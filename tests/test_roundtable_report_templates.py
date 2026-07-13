from __future__ import annotations

from decimal import Decimal
from typing import Any

from finance_agent.agents.reports.templates import build_chinese_decision_report
from finance_agent.storage.orm import AssistantMemoryORM


def test_report_roundtable_section_renders_model_key_points_and_rebuttals() -> None:
    report = build_report(
        roundtable_opinions=[
            {
                "role": "risk_rebuttal",
                "asset_id": "ashare:600519",
                "stance": "bearish",
                "summary": "模型认为风险证据强于原建议。",
                "key_points": ["证据 ev:risk 显示风险未解除。"],
                "rebuttals": ["若公告澄清，反驳强度会下降。"],
                "data_gaps": ["缺少最新公告原文。"],
                "generated_by": "model",
                "model_instance_id": "deepseek-v4-pro",
                "evidence_ids": ["ev:risk"],
            }
        ]
    )

    opinion = report["roundtable_opinions"][0]
    assert opinion["generated_by"] == "model"
    assert opinion["model_instance_id"] == "deepseek-v4-pro"
    assert opinion["key_points"] == ["证据 ev:risk 显示风险未解除。"]
    assert opinion["rebuttals"] == ["若公告澄清，反驳强度会下降。"]
    assert "risk_rebuttal（model）: 模型认为风险证据强于原建议。" in report["markdown"]
    assert "要点：证据 ev:risk 显示风险未解除。" in report["markdown"]
    assert "反方：若公告澄清，反驳强度会下降。" in report["markdown"]
    assert "缺口：缺少最新公告原文。" in report["markdown"]


def test_report_roundtable_section_keeps_rule_based_summary_compatible() -> None:
    report = build_report(
        roundtable_opinions=[
            {
                "role": "technical_analyst",
                "asset_id": "ashare:600519",
                "stance": "support",
                "summary": "规则版技术观点。",
                "evidence_ids": ["ev:tech"],
            }
        ]
    )

    opinion = report["roundtable_opinions"][0]
    assert opinion["generated_by"] == "fallback"
    assert opinion["key_points"] == []
    assert opinion["rebuttals"] == []
    assert "- technical_analyst（fallback）: 规则版技术观点。" in report["markdown"]


def test_report_key_evidence_renders_backtest_summary() -> None:
    report = build_report(
        roundtable_opinions=[],
        backtest={
            "status": "available",
            "backtest_id": "bt:factor_score_topn:ashare:1",
            "summary": "近 5 年模拟回放：年化收益 12.34%，最大回撤 -18.20%，夏普 1.21。",
            "metrics": {"cagr": 0.1234},
        },
    )

    backtest_items = [
        item for item in report["key_evidence"] if item.get("source") == "backtest"
    ]
    assert backtest_items[0]["evidence_id"] == "bt:factor_score_topn:ashare:1"
    assert backtest_items[0]["title"] == "策略回测证据"
    assert "年化收益 12.34%" in report["markdown"]


def test_report_key_evidence_marks_missing_backtest() -> None:
    report = build_report(
        roundtable_opinions=[],
        backtest={
            "status": "missing",
            "reason": "暂无同策略回测证据",
            "certainty_adjustment": "lower",
        },
    )

    backtest_items = [
        item for item in report["key_evidence"] if item.get("source") == "backtest"
    ]
    assert backtest_items[0]["title"] == "暂无回测证据"
    assert "暂无同策略回测证据" in report["markdown"]
    assert "仅基于当前评分、信号和风险事实" in report["markdown"]


def test_report_memory_references_accept_sqlalchemy_memory_rows() -> None:
    """真实数据库返回 ORM 记忆时，报告模板也应生成可序列化引用。"""

    memory = AssistantMemoryORM(
        memory_id="memory:ashare:600519:review",
        owner_id="owner:test",
        memory_type="review_result",
        scope="asset",
        asset_id="ashare:600519",
        source_decision_id="decision:test",
        source_review_task_id="review:test",
        content="复盘确认原建议风险偏高。",
        embedding_ref=None,
        confidence=Decimal("0.900000"),
        status="active",
        payload={},
    )

    report = build_report(roundtable_opinions=[], memory_items=[memory])

    assert report["memory_references"] == [
        {
            "asset_id": "ashare:600519",
            "symbol": "600519",
            "memory_id": "memory:ashare:600519:review",
            "memory_type": "review_result",
            "content": "复盘确认原建议风险偏高。",
            "confidence": Decimal("0.900000"),
        }
    ]


def build_report(
    *,
    roundtable_opinions: list[dict[str, Any]],
    backtest: dict[str, Any] | None = None,
    memory_items: list[Any] | None = None,
) -> dict[str, Any]:
    return build_chinese_decision_report(
        title="圆桌模型报告",
        summary="圆桌模型报告摘要。",
        workflow_type="asset_deep_analysis",
        asset_symbols=["600519"],
        decisions=[
            {
                "asset_id": "ashare:600519",
                "action": "watch",
                "decision_type": "asset_deep_analysis_watch",
                "severity": "medium",
                "confidence": 0.6,
                "data_quality_status": "available",
                "summary": "继续观察。",
            }
        ],
        roundtable_opinions=roundtable_opinions,
        high_risk_reviews=[],
        asset_contexts={
            "ashare:600519": {
                "profile": {"symbol": "600519"},
                "factor": {
                    "score": {"total_score": 72},
                    "indicator_frame": {"indicator_frame_id": "indicator:1"},
                    "evidence": [
                        {
                            "evidence_id": "ev:risk",
                            "source": "risk",
                            "title": "风险证据",
                            "summary": "风险未解除。",
                        }
                    ],
                },
                "signal_risk": {"data_quality": []},
                "backtest": backtest,
                "memory": {"memories": memory_items or []},
            }
        },
        model_routes=[],
        review_model_routes=[],
    )
