from __future__ import annotations

from typing import Any

from finance_agent.agents.reports.templates import build_chinese_decision_report


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


def build_report(*, roundtable_opinions: list[dict[str, Any]]) -> dict[str, Any]:
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
            }
        },
        model_routes=[],
        review_model_routes=[],
    )
