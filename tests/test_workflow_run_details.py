from types import SimpleNamespace

from finance_agent.agents.interfaces import extract_model_routes, extract_node_trace


def test_extract_node_trace_preserves_workflow_node_order() -> None:
    events = (
        SimpleNamespace(
            event_type="workflow_node_completed",
            agent_name="load_context",
            payload={"output": {"state_keys": ["a"]}},
        ),
        SimpleNamespace(
            event_type="workflow_node_completed",
            agent_name="data_gathering",
            payload={"output": {"state_keys": ["b"]}},
        ),
        SimpleNamespace(
            event_type="roundtable_opinion",
            agent_name="roundtable:portfolio_manager",
            payload={"output": {"role": "portfolio_manager"}},
        ),
        SimpleNamespace(
            event_type="high_risk_review",
            agent_name="high_risk_review",
            payload={"output": {"review_count": 2}},
        ),
        SimpleNamespace(
            event_type="report_draft",
            agent_name="report_draft",
            payload={"output": {"report": {"title": "demo"}}},
        ),
    )

    assert extract_node_trace(events) == [
        "load_context",
        "data_gathering",
        "high_risk_review",
        "report_draft",
    ]


def test_extract_model_routes_returns_route_payloads() -> None:
    events = (
        SimpleNamespace(
            event_type="model_route",
            agent_name="model_route:primary:1",
            payload={
                "output": {
                    "task": "roundtable_discussion",
                    "model_key": "deepseek-v4-pro",
                    "model_name": "DeepSeek V4 Pro",
                    "role": "primary_financial_analyst",
                }
            },
        ),
        SimpleNamespace(
            event_type="model_review",
            agent_name="model_review:1",
            payload={
                "output": {
                    "review_status": "requires_model_review",
                    "route": {
                        "task": "high_risk_review",
                        "model_key": "gpt-5.5-pro",
                        "model_name": "GPT-5.5 Pro",
                        "role": "high_risk_reviewer",
                    },
                }
            },
        ),
    )

    assert extract_model_routes(events, event_type="model_route") == [
        {
            "task": "roundtable_discussion",
            "model_key": "deepseek-v4-pro",
            "model_name": "DeepSeek V4 Pro",
            "role": "primary_financial_analyst",
        }
    ]
    assert extract_model_routes(events, event_type="model_review") == [
        {
            "task": "high_risk_review",
            "model_key": "gpt-5.5-pro",
            "model_name": "GPT-5.5 Pro",
            "role": "high_risk_reviewer",
        }
    ]
