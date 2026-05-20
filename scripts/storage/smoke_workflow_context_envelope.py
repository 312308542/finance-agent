"""验证 Workflow 共享上下文 Envelope 与角色视图。"""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from finance_agent.agents.runtime.context_envelope import (
    CONTEXT_ENVELOPE_VERSION,
    build_workflow_context_envelope,
)
from finance_agent.agents.runtime.prompts import build_prompt_bundle


def main() -> None:
    asset_contexts = {
        "asset:sh:600000": {
            "profile": {"asset_id": "asset:sh:600000", "symbol": "600000", "market": "ashare"},
            "factor": {
                "indicator_frame": {"rsi_14": 55.2, "macd_hist": 0.12},
                "factor_frame": {"status": "available", "missing_groups": []},
                "score": {"total_score": 82.5, "score_id": "score:demo"},
            },
            "signal_risk": {
                "signal": {"direction": "bullish", "confidence": 0.81},
                "risks": [{"risk_id": "risk:demo", "severity": "medium"}],
            },
            "memory": {
                "memories": [
                    {
                        "memory_id": "memory:demo",
                        "memory_type": "decision_summary",
                        "content": "历史推荐后两日回撤，后续需等待放量确认。",
                    }
                ]
            },
        }
    }
    envelope = build_workflow_context_envelope(
        workflow_type="asset_deep_analysis",
        market_type="ashare",
        asset_ids=["asset:sh:600000"],
        asset_contexts=asset_contexts,
        portfolio_context={"portfolio": {"portfolio_id": "portfolio:demo"}},
        watchlist_context={"watchlist": {"watchlist_id": "watchlist:demo"}},
        recommendation_context={"run": {"run_id": "recommendation_run:demo"}},
        trigger_event={"trigger_type": "manual"},
        available_tools=["factor.get_asset_factor_context", "memory.recall_asset_memories"],
    )
    data = envelope.to_dict()

    assert data["version"] == CONTEXT_ENVELOPE_VERSION
    assert data["context"]["market_type"] == "ashare"
    assert "memories" not in data["stable"]
    assert data["volatile"]["memory_summary"]["memory_count"] == 1
    assert set(data["role_views"]) == {
        "technical_analyst",
        "factor_analyst",
        "risk_rebuttal",
        "portfolio_manager",
        "memory_manager",
    }
    assert "indicator_frame" in data["role_views"]["technical_analyst"]["visible_sections"]
    assert "factor_frame" in data["role_views"]["factor_analyst"]["visible_sections"]
    assert "risk_items" in data["role_views"]["risk_rebuttal"]["visible_sections"]
    assert "portfolio_context" in data["role_views"]["portfolio_manager"]["visible_sections"]
    assert "memory_items" in data["role_views"]["memory_manager"]["visible_sections"]
    assert data["audit"]["role_view_count"] == 5

    bundle = build_prompt_bundle(
        model_role="primary_financial_analyst",
        context_envelope=data,
        role_name="risk_rebuttal",
    )
    assert "DeepSeek" in bundle["stable"]
    assert "market_type=ashare" in bundle["context"]
    assert "risk_count=1" in bundle["volatile"]
    assert "风险反驳" in bundle["role"]
    assert bundle["template_id"] == "primary_financial_analyst"
    assert bundle["prompt_version"] == "1.0.0"
    assert bundle["market_type"] == "ashare"
    assert bundle["workflow_type"] == "asset_deep_analysis"
    assert bundle["prompt_hash"]
    assert bundle["top_level_output_schema"]["properties"]["status"]["enum"] == ["ready", "need_more_data", "blocked"]
    assert bundle["prompt_hash_stable"] is True
    assert bundle["output_schema"]["properties"]["action"]["enum"]
    assert "FinanceToolRuntime" in bundle["tool_protocol"]
    assert "停复牌" in bundle["market_rules"]
    assert "反驳" in bundle["risk_protocol"]
    assert "risk_rebuttal" in bundle["output_schema"]["properties"]["report_sections"]["properties"]
    assert "summary_zh" in bundle["reporting_constraints"]
    assert any(section["name"] == "tool_protocol" for section in bundle["sections"])
    assert bundle["audit_summary"]["prompt_char_count"] > 0
    assert bundle["audit_summary"]["tool_count"] >= 1
    assert bundle["audit_summary"]["section_lengths"]["tool_protocol"] > 0

    high_risk_bundle = build_prompt_bundle(
        model_role="high_risk_reviewer",
        context_envelope=data,
        role_name="risk_rebuttal",
    )
    assert high_risk_bundle["template_id"] == "high_risk_reviewer"
    assert high_risk_bundle["output_schema"]["properties"]["review_status"]["enum"] == [
        "approve",
        "downgrade",
        "reject",
        "need_more_data",
    ]
    assert "反驳" in high_risk_bundle["risk_protocol"]
    assert "补证据" in high_risk_bundle["stable"]

    dispatcher_bundle = build_prompt_bundle(
        model_role="top_level_dispatcher",
        context_envelope=data,
    )
    assert dispatcher_bundle["output_schema"]["properties"]["status"]["enum"] == [
        "ready",
        "need_more_data",
        "blocked",
    ]

    crypto_envelope = build_workflow_context_envelope(
        workflow_type="swap_decision",
        market_type="crypto",
        asset_ids=["asset:crypto:BTCUSDT"],
        asset_contexts={
            "asset:crypto:BTCUSDT": {
                "profile": {
                    "asset_id": "asset:crypto:BTCUSDT",
                    "symbol": "BTCUSDT",
                    "market": "crypto",
                },
                "factor": {
                    "indicator_frame": {"rsi_14": 63.1},
                    "factor_frame": {"status": "available", "missing_groups": []},
                    "score": {"total_score": 78.0},
                },
                "signal_risk": {
                    "signal": {"direction": "bullish", "confidence": 0.76},
                    "risks": [{"risk_id": "risk:funding", "severity": "high"}],
                },
                "memory": {"memories": []},
            }
        },
        trigger_event={"trigger_type": "manual"},
        available_tools=["factor.get_asset_factor_context"],
    ).to_dict()
    crypto_bundle = build_prompt_bundle(
        model_role="primary_financial_analyst",
        context_envelope=crypto_envelope,
    )
    assert crypto_bundle["market_type"] == "crypto"
    assert "24/7" in crypto_bundle["market_rules"]
    assert "资金费率" in crypto_bundle["market_rules"]

    print("ok")


if __name__ == "__main__":
    main()
