from decimal import Decimal
from types import SimpleNamespace

from finance_agent.factors import service as factor_service_module
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


def test_factor_service_uses_default_event_signal_window(monkeypatch) -> None:
    """因子计算消费事件时应走默认时效窗口，而不是无时间限制 latest N。"""

    captured: dict[str, object] = {}

    class _Indicators:
        def __init__(self, _session):
            pass

        def get_latest_indicator_frame(self, **_kwargs):
            return None

    class _Frames:
        def __init__(self, _session):
            pass

        def upsert_factor_frame(self, **kwargs):
            return SimpleNamespace(
                status=kwargs["status"],
                factor_frame_id=kwargs["factor_frame_id"],
                asset_id=kwargs["asset_id"],
                symbol=kwargs["symbol"],
                market=kwargs["market"],
                horizon=kwargs["horizon"],
                total_available_groups=kwargs["total_available_groups"],
                missing_groups=kwargs["missing_groups"],
            )

    class _Fundamentals:
        def __init__(self, _session):
            pass

        def list_recent_snapshots(self, **_kwargs):
            return []

    class _Flows(_Fundamentals):
        pass

    class _Derivatives(_Fundamentals):
        pass

    class _Events:
        def __init__(self, _session):
            pass

        def list_recent_events(self, **kwargs):
            captured.update(kwargs)
            return []

    class _Risks:
        def __init__(self, _session):
            pass

        def list_recent_risks(self, **_kwargs):
            return []

    monkeypatch.setattr(factor_service_module, "IndicatorFrameRepository", _Indicators)
    monkeypatch.setattr(factor_service_module, "FactorFrameRepository", _Frames)
    monkeypatch.setattr(factor_service_module, "FundamentalDataRepository", _Fundamentals)
    monkeypatch.setattr(factor_service_module, "CapitalFlowRepository", _Flows)
    monkeypatch.setattr(factor_service_module, "DerivativeDataRepository", _Derivatives)
    monkeypatch.setattr(factor_service_module, "EventRepository", _Events)
    monkeypatch.setattr(factor_service_module, "RiskRepository", _Risks)

    factor_service_module.FactorService(object()).compute_for_asset(
        asset_id="ashare:600519",
        fallback_symbol="600519",
        fallback_market="ashare",
    )

    assert captured["asset_id"] == "ashare:600519"
    assert captured["limit"] == 20
    assert captured["max_age_days"] == 90
