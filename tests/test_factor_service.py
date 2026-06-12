from decimal import Decimal
from types import SimpleNamespace

from finance_agent.factors.service import build_capital_flow_group, infer_symbol_market


def test_build_capital_flow_group_consumes_northbound_flow_window() -> None:
    """资金流因子应消费北向净流入，并给出多窗口连续性。"""

    history = [
        SimpleNamespace(
            snapshot_id="flow:1",
            main_net_inflow=Decimal("5"),
            northbound_net_inflow=Decimal("10"),
            amount=Decimal("100"),
            payload={},
            window="1d",
        ),
        SimpleNamespace(
            snapshot_id="flow:2",
            main_net_inflow=Decimal("6"),
            northbound_net_inflow=Decimal("-5"),
            amount=Decimal("100"),
            payload={},
            window="1d",
        ),
        SimpleNamespace(
            snapshot_id="flow:3",
            main_net_inflow=Decimal("7"),
            northbound_net_inflow=Decimal("20"),
            amount=Decimal("100"),
            payload={},
            window="1d",
        ),
    ]

    group = build_capital_flow_group(history[-1], history=history)

    assert group["factors"]["northbound_net_inflow_strength"] == 0.2
    assert group["factors"]["northbound_flow_continuity"] == 2 / 3
    assert "northbound_net_inflow_strength" not in group["missing_factors"]
    assert "northbound_flow_continuity" not in group["missing_factors"]


def test_infer_symbol_market_uses_fallback_market_for_fundamental_snapshot() -> None:
    """财务快照表不保存 market，因子计算应回退到候选池成员市场。"""

    fundamental = SimpleNamespace(
        asset_id="ashare:000001",
        symbol="000001",
    )

    symbol, market = infer_symbol_market(
        indicator=None,
        fundamental=fundamental,
        capital_flow=None,
        derivative=None,
        fallback_symbol="000001",
        fallback_market="ashare",
    )

    assert symbol == "000001"
    assert market == "ashare"
