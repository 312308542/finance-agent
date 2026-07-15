from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from finance_agent.data.collectors import AshareP1Collector
from finance_agent.data.models import CapitalFlowSnapshotData, CapitalFlowSnapshotsResult
from finance_agent.data import normalizers
from finance_agent.data.providers.akshare_p1_provider import AshareCapitalFlowProvider


class _FakeRawRecords:
    def insert_raw_record(self, **_kwargs: Any) -> Any:
        return type("RawRecord", (), {"raw_record_id": "raw:northbound"})()


def test_normalize_ashare_northbound_market_flow_outputs_market_snapshot() -> None:
    """沪深港通市场级历史资金应落为市场级北向资金快照。"""

    as_of = datetime(2026, 6, 12, tzinfo=UTC)
    df = pd.DataFrame(
        [
            {
                "日期": "2026-06-11",
                "当日资金流入": "12.30",
                "北向资金": "12.30",
                "沪股通": "4.10",
                "深股通": "8.20",
            }
        ]
    )

    assert hasattr(normalizers, "normalize_ashare_northbound_market_flow")
    snapshots = normalizers.normalize_ashare_northbound_market_flow(
        df,
        source="akshare:stock_hsgt_hist_em",
        symbol="北向资金",
        as_of=as_of,
    )

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.asset_id == "market:ashare:northbound"
    assert snapshot.symbol == "northbound"
    assert snapshot.window == "daily"
    assert snapshot.as_of == datetime(2026, 6, 11, tzinfo=UTC)
    assert snapshot.northbound_net_inflow == Decimal("12.30")
    assert snapshot.payload["raw"]["沪股通"] == "4.10"


def test_normalize_ashare_northbound_individual_flow_filters_non_main_board() -> None:
    """北向个股资金只保留用户可交易的主板股票。"""

    as_of = datetime(2026, 6, 12, tzinfo=UTC)
    df = pd.DataFrame(
        [
            {
                "持股日期": "2026-06-11",
                "股票代码": "000001",
                "持股市值": "1000.50",
                "今日增持资金": "12.30",
            },
            {"持股日期": "2026-06-11", "股票代码": "688001", "持股市值": "2000.00"},
        ]
    )

    assert hasattr(normalizers, "normalize_ashare_northbound_individual_flow")
    snapshots = normalizers.normalize_ashare_northbound_individual_flow(
        df,
        source="akshare:stock_hsgt_individual_em",
        symbol="000001",
        as_of=as_of,
    )

    assert [snapshot.symbol for snapshot in snapshots] == ["000001"]
    assert snapshots[0].asset_id == "ashare:000001"
    assert snapshots[0].amount == Decimal("1000.50")
    assert snapshots[0].northbound_net_inflow == Decimal("12.30")
    assert snapshots[0].as_of == datetime(2026, 6, 11, tzinfo=UTC)


def test_northbound_individual_none_payload_is_unavailable(monkeypatch) -> None:
    """非互联互通标的返回空对象时应标记不可用，不能触发全局熔断。"""

    def raise_no_data(*, symbol: str) -> None:
        assert symbol == "000004"
        raise TypeError("'NoneType' object is not subscriptable")

    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p1_provider.ak.stock_hsgt_individual_em",
        raise_no_data,
    )

    result = AshareCapitalFlowProvider().fetch_northbound_individual_flow(symbol="000004")

    assert result.status == "unavailable"
    assert result.error_message is None
    assert result.snapshots == []
    assert result.payload["unavailable_reason"] == "source_returned_no_data"


def test_ashare_p1_collector_collects_northbound_flow_in_batch(
    monkeypatch,
) -> None:
    """北向资金采集应批量写入资金流快照，并归档原始返回。"""

    calls: dict[str, list[list[dict[str, Any]]]] = {"flows": []}

    class FakeCapitalFlowRepository:
        def __init__(self, _session: Any) -> None:
            pass

        def upsert_capital_flow_snapshots(self, rows: list[dict[str, Any]]) -> int:
            calls["flows"].append(rows)
            return len(rows)

        def upsert_capital_flow_snapshot(self, **_kwargs: Any) -> None:
            raise AssertionError("北向资金不应逐条写入资金流快照")

    class FakeFlowProvider:
        def fetch_northbound_flow(
            self,
            *,
            symbol: str = "北向资金",
            limit: int | None = None,
        ) -> CapitalFlowSnapshotsResult:
            return CapitalFlowSnapshotsResult(
                provider_name="akshare",
                status="available",
                collected_at=datetime(2026, 6, 12, tzinfo=UTC),
                snapshots=[
                    CapitalFlowSnapshotData(
                        snapshot_id="northbound:market:20260611",
                        asset_id="market:ashare:northbound",
                        symbol="northbound",
                        market="ashare",
                        window="daily",
                        source="akshare:stock_hsgt_hist_em",
                        as_of=datetime(2026, 6, 11, tzinfo=UTC),
                        northbound_net_inflow=Decimal("12.30"),
                    )
                ],
            )

    monkeypatch.setattr(
        "finance_agent.data.collectors.CapitalFlowRepository",
        FakeCapitalFlowRepository,
    )
    monkeypatch.setattr(
        "finance_agent.data.collectors.RawRecordRepository",
        lambda _session: _FakeRawRecords(),
    )

    result = AshareP1Collector(
        object(),
        flow_provider=FakeFlowProvider(),
    ).collect_northbound_flow(symbol="北向资金")

    assert result.raw_record_id == "raw:northbound"
    assert len(calls["flows"]) == 1
    assert calls["flows"][0][0]["asset_id"] == "market:ashare:northbound"
    assert calls["flows"][0][0]["northbound_net_inflow"] == Decimal("12.30")
