from __future__ import annotations

from finance_agent.agents.workflows.langgraph_graphs import resolve_roundtable_model_roles
from finance_agent.agents.workflows.roundtable_prompts import role_prompt


def test_roundtable_prompts_include_event_and_flow_roles_with_data_gap_self_check() -> None:
    event_prompt = role_prompt("event_analyst")
    flow_prompt = role_prompt("flow_analyst")
    risk_prompt = role_prompt("risk_rebuttal")

    assert "市场事件分析师" in event_prompt
    assert "新闻、公告、舆情、监管、题材" in event_prompt
    assert "数据维度自检" in event_prompt
    assert "资金流分析师" in flow_prompt
    assert "个股资金流、北向、板块资金、龙虎榜席位" in flow_prompt
    assert "数据维度自检" in flow_prompt
    assert "数据维度缺失本身就是一条反方观点" in risk_prompt


def test_roundtable_model_roles_all_includes_event_and_flow_roles() -> None:
    roles = resolve_roundtable_model_roles(
        {"roundtable_model_roles": ["all"]},
        workflow_type="asset_deep_analysis",
    )

    assert roles == [
        "technical_analyst",
        "factor_analyst",
        "event_analyst",
        "flow_analyst",
        "risk_rebuttal",
        "portfolio_manager",
        "memory_manager",
    ]
