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

    print("ok")


if __name__ == "__main__":
    main()
