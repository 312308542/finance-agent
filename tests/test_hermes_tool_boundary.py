"""Hermes 可见工具和 Workflow 的权限边界防回归测试。"""

from __future__ import annotations

import pytest

from finance_agent.agents.interfaces import FinanceAgentInterface


ALLOWED_WORKFLOWS = {
    "portfolio_monitoring",
    "watchlist_management",
    "recommendation_decision",
    "asset_deep_analysis",
    "swap_decision",
    "daily_review",
}

ALLOWED_TOOL_PREFIXES = (
    "portfolio.",
    "watchlist.",
    "factor.",
    "signal_risk.",
    "memory.",
    "profile.",
    "advice.",
    "graph.",
    "data_quality.",
    "recommendation.",
    "workflow.",
)

FORBIDDEN_TOOL_KEYWORDS = ("order", "trade", "execute", "broker", "place")
CONTROLLED_WRITE_TOOLS = {"profile.upsert": "investment_profile"}


@pytest.fixture()
def interface() -> FinanceAgentInterface:
    """构造不访问真实数据库的门面实例。"""

    return FinanceAgentInterface(object())  # type: ignore[arg-type]


def test_hermes_visible_tools_are_read_only_or_controlled_profile_writes(
    interface: FinanceAgentInterface,
) -> None:
    """Hermes 可见工具必须只读；画像 upsert 是唯一受控写白名单。"""

    result = interface.list_tools().to_dict()

    assert result["status"] == "ok"
    tools = result["data"]["tools"]
    assert tools

    for tool in tools:
        name = tool["name"]
        assert name.startswith(ALLOWED_TOOL_PREFIXES)
        assert not any(keyword in name.lower() for keyword in FORBIDDEN_TOOL_KEYWORDS)
        if name in CONTROLLED_WRITE_TOOLS:
            assert tool.get("read_only") is False
            assert tool.get("requires_review") is True
            assert tool.get("write_scope") == CONTROLLED_WRITE_TOOLS[name]
        else:
            assert tool.get("read_only") is True
            assert tool.get("requires_review") is False
            assert tool.get("write_scope") is None


def test_hermes_workflow_whitelist_is_fixed(interface: FinanceAgentInterface) -> None:
    """Hermes 只能看到 6 个受控金融团队 Workflow。"""

    result = interface.list_workflows().to_dict()

    assert result["status"] == "ok"
    workflow_types = {item["workflow_type"] for item in result["data"]["workflows"]}
    assert workflow_types == ALLOWED_WORKFLOWS


def test_run_workflow_rejects_unknown_workflow(interface: FinanceAgentInterface) -> None:
    """未知 Workflow 名称必须明确拒绝，避免 Hermes 临时拼接入口。"""

    with pytest.raises(ValueError, match="未知 Workflow 类型"):
        interface.run_workflow(
            workflow_type="trade_execution",
            owner_id="owner:hermes-boundary-test",
        )
