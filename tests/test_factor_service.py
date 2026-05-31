from types import SimpleNamespace

from finance_agent.factors.service import infer_symbol_market


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
