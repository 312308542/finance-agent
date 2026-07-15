from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from finance_agent.factors import service as factor_service_module
from finance_agent.factors.service import (
    build_capital_flow_group,
    infer_symbol_market,
    normalize_supplemental_factor_groups,
)


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


def test_factor_service_merges_historical_and_spot_valuation_snapshots(monkeypatch) -> None:
    """估值因子应使用最新 spot 值，同时保留历史估值来源链。"""

    requested_sources: list[str | None] = []
    saved_payloads: list[dict[str, object]] = []
    historical = SimpleNamespace(
        snapshot_id="valuation:historical:600519:20260714",
        asset_id="ashare:600519",
        symbol="600519",
        as_of=datetime(2026, 7, 14, tzinfo=UTC),
        pe_ttm=Decimal("18.20"),
        pb=Decimal("6.30"),
        payload={},
    )
    spot = SimpleNamespace(
        snapshot_id="valuation:spot:600519:20260715",
        asset_id="ashare:600519",
        symbol="600519",
        as_of=datetime(2026, 7, 15, tzinfo=UTC),
        pe_ttm=Decimal("16.80"),
        pb=Decimal("5.90"),
        payload={"valuation_kind": "spot_snapshot"},
    )

    class _Indicators:
        def __init__(self, _session):
            pass

        def get_latest_indicator_frame(self, **_kwargs):
            return None

    class _Frames:
        def __init__(self, _session):
            pass

        def upsert_factor_frame(self, **kwargs):
            saved_payloads.append(kwargs)
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

        def list_recent_snapshots(self, **kwargs):
            source = kwargs.get("source")
            requested_sources.append(source)
            if source == "akshare:stock_value_em":
                return [historical]
            if source == "akshare:stock_zh_a_spot":
                return [spot]
            return []

    class _EmptySnapshots:
        def __init__(self, _session):
            pass

        def list_recent_snapshots(self, **_kwargs):
            return []

    class _EmptyEvents:
        def __init__(self, _session):
            pass

        def list_recent_events(self, **_kwargs):
            return []

    class _EmptyRisks:
        def __init__(self, _session):
            pass

        def list_recent_risks(self, **_kwargs):
            return []

    monkeypatch.setattr(factor_service_module, "IndicatorFrameRepository", _Indicators)
    monkeypatch.setattr(factor_service_module, "FactorFrameRepository", _Frames)
    monkeypatch.setattr(factor_service_module, "FundamentalDataRepository", _Fundamentals)
    monkeypatch.setattr(factor_service_module, "CapitalFlowRepository", _EmptySnapshots)
    monkeypatch.setattr(factor_service_module, "DerivativeDataRepository", _EmptySnapshots)
    monkeypatch.setattr(factor_service_module, "EventRepository", _EmptyEvents)
    monkeypatch.setattr(factor_service_module, "RiskRepository", _EmptyRisks)

    factor_service_module.FactorService(object()).compute_for_asset(
        asset_id="ashare:600519",
        fallback_symbol="600519",
        fallback_market="ashare",
    )

    assert "akshare:stock_zh_a_spot" in requested_sources
    valuation_group = next(
        item
        for item in saved_payloads[0]["payload"]["factor_groups"]
        if item["group"] == "valuation"
    )
    assert valuation_group["factors"]["pe_ttm"] == 16.8
    assert valuation_group["factors"]["pb"] == 5.9
    assert valuation_group["source_ids"] == [
        historical.snapshot_id,
        spot.snapshot_id,
    ]
    assert saved_payloads[0]["source_ids"][-2:] == [
        historical.snapshot_id,
        spot.snapshot_id,
    ]


def test_factor_service_persists_supplemental_theme_factor_groups(monkeypatch) -> None:
    """因子服务应把确定性题材/龙头上下文写入 factor_frames。"""

    saved_payloads: list[dict[str, object]] = []

    class _Indicators:
        def __init__(self, _session):
            pass

        def get_latest_indicator_frame(self, **_kwargs):
            return SimpleNamespace(
                indicator_frame_id="indicator:ashare:600519",
                asset_id="ashare:600519",
                symbol="600519",
                market="ashare",
                input_end_at=SimpleNamespace(astimezone=lambda _tz: SimpleNamespace(strftime=lambda _fmt: "20260630T070000Z")),
                ma_20=None,
                ma_60=None,
                rsi_14=None,
                macd=None,
                macd_hist=None,
                atr_14=None,
                bb_percent_b=None,
                payload={"computed_values": {}},
            )

    class _Frames:
        def __init__(self, _session):
            pass

        def upsert_factor_frame(self, **kwargs):
            saved_payloads.append(kwargs)
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

    class _EmptySnapshots:
        def __init__(self, _session):
            pass

        def list_recent_snapshots(self, **_kwargs):
            return []

    class _EmptyEvents:
        def __init__(self, _session):
            pass

        def list_recent_events(self, **_kwargs):
            return []

    class _EmptyRisks:
        def __init__(self, _session):
            pass

        def list_recent_risks(self, **_kwargs):
            return []

    monkeypatch.setattr(factor_service_module, "IndicatorFrameRepository", _Indicators)
    monkeypatch.setattr(factor_service_module, "FactorFrameRepository", _Frames)
    monkeypatch.setattr(factor_service_module, "FundamentalDataRepository", _EmptySnapshots)
    monkeypatch.setattr(factor_service_module, "CapitalFlowRepository", _EmptySnapshots)
    monkeypatch.setattr(factor_service_module, "DerivativeDataRepository", _EmptySnapshots)
    monkeypatch.setattr(factor_service_module, "EventRepository", _EmptyEvents)
    monkeypatch.setattr(factor_service_module, "RiskRepository", _EmptyRisks)

    factor_service_module.FactorService(object()).compute_for_asset(
        asset_id="ashare:600519",
        fallback_symbol="600519",
        fallback_market="ashare",
        supplemental_factor_groups=[
            {
                "group": "sector_strength",
                "status": "available",
                "score": 91,
                "factors": {"sector_id": "concept:ai"},
                "evidence_ids": ["ev:sector"],
            },
            {
                "group": "leadership",
                "status": "available",
                "score": 87,
                "factors": {"leader_rank": 1},
                "source_ids": ["leader:600519"],
            },
        ],
    )

    payload = saved_payloads[0]["payload"]
    groups = {item["group"]: item for item in payload["factor_groups"]}
    assert groups["sector_strength"]["score"] == 91.0
    assert groups["leadership"]["score"] == 87.0
    assert saved_payloads[0]["source_ids"][-2:] == ["ev:sector", "leader:600519"]


def test_normalize_supplemental_factor_groups_keeps_only_theme_groups() -> None:
    """外部注入的因子组只能进入允许的题材组，避免污染评分链路。"""

    groups = normalize_supplemental_factor_groups(
        [
            {"group": "sector_strength", "status": "available", "score": "90"},
            {"group": "leadership", "status": "available", "score": 80},
            {"group": "technical", "status": "available", "score": 100},
            {"group": "leadership", "status": "available", "score": 70},
        ]
    )

    assert [item["group"] for item in groups] == ["sector_strength", "leadership"]
    assert groups[0]["score"] == 90.0
    assert groups[1]["score"] == 80.0
