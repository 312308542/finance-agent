from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from sqlalchemy.dialects import postgresql

from finance_agent.application.theme_context_service import (
    ThemeContextInput,
    ThemeContextService,
)


class _PointInTimeSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    def scalars(self, statement: Any) -> list[Any]:
        self.statements.append(statement)
        return []


def test_theme_fact_queries_are_bounded_by_snapshot_as_of() -> None:
    session = _PointInTimeSession()
    service = ThemeContextService(session)  # type: ignore[arg-type]
    as_of = datetime(2026, 9, 8, 7, 0, tzinfo=UTC)

    service._latest_daily_bars(["ashare:600519"], as_of=as_of)
    service._latest_capital_flows(["ashare:600519"], as_of=as_of)
    service._latest_asset_status(["ashare:600519"], as_of=as_of)

    sql = [
        str(statement.compile(dialect=postgresql.dialect()))
        for statement in session.statements
    ]
    assert "market_bars.timestamp <=" in sql[0]
    assert "capital_flow_snapshots.as_of <=" in sql[1]
    assert "asset_status_snapshots.as_of <=" in sql[2]


def test_theme_context_service_builds_factor_groups_per_asset() -> None:
    service = ThemeContextService()

    contexts = service.build_contexts(
        [
            ThemeContextInput(
                sector_id="concept:robot",
                sector_name="机器人",
                asset_id="ashare:300001",
                asset_name="机器人A",
                pct_change=12.0,
                net_inflow=900_000_000,
                limit_up=True,
                popularity_rank=3,
                board_hits=2,
                consecutive_limit_up=2,
                limit_up_time="09:35:00",
                returns_by_horizon={1: 0.02, 3: 0.04, 5: 0.07, 10: 0.12, 20: 0.15},
                above_ma20=True,
                flow_positive_streak=4,
                breadth_change=0.05,
                valid_cross_sections=2,
                evidence_ids=["ev:flow:300001", "ev:bar:300001"],
            ),
            ThemeContextInput(
                sector_id="concept:robot",
                sector_name="机器人",
                asset_id="ashare:300002",
                asset_name="机器人B",
                pct_change=6.0,
                net_inflow=300_000_000,
                popularity_rank=12,
                board_hits=1,
                returns_by_horizon={1: 0.01, 3: 0.04, 5: 0.07, 10: 0.09, 20: 0.11},
                above_ma20=True,
                flow_positive_streak=3,
                breadth_change=0.05,
                valid_cross_sections=2,
                evidence_ids=["ev:flow:300002"],
            ),
            ThemeContextInput(
                sector_id="industry:bank",
                sector_name="银行",
                asset_id="ashare:600001",
                asset_name="银行A",
                pct_change=1.0,
                net_inflow=20_000_000,
                evidence_ids=["ev:flow:600001"],
            ),
        ],
        strong_sector_limit=1,
        market_regime="trend_down",
    )

    leader_payload = contexts["ashare:300001"].to_member_payload()
    groups = {item["group"]: item for item in leader_payload["theme_context"]["factor_groups"]}
    assert groups["sector_strength"]["factors"]["sector_id"] == "concept:robot"
    assert groups["leadership"]["factors"]["role"] == "leader"
    assert groups["sector_strength"]["factors"]["sector_regime"] == "diffusion"
    assert groups["sector_strength"]["factors"]["override_eligible"] is True
    assert leader_payload["theme_context"]["source"] == "deterministic_theme_context_v1"
    assert leader_payload["theme_context"]["evidence_ids"] == [
        "ev:flow:300001",
        "ev:bar:300001",
        "ev:flow:300002",
    ]

    follower_groups = {
        item["group"]: item
        for item in contexts["ashare:300002"].to_member_payload()["theme_context"]["factor_groups"]
    }
    assert follower_groups["leadership"]["factors"]["role"] == "challenger"
    assert "ashare:600001" not in contexts


def test_membership_adapter_reads_persisted_multiday_sector_metrics() -> None:
    service = ThemeContextService()

    item = service._theme_input_from_membership(
        membership=SimpleNamespace(
            id="member:1",
            asset_id="ashare:600001",
            rank_hint=1,
            payload={
                "returns_by_horizon": {"1": "0.01", "3": "0.04", "5": "0.07"},
                "above_ma20": True,
                "breadth_change": "0.08",
                "valid_cross_sections": 2,
                "previous_sector_regime": "diffusion",
            },
        ),
        universe=SimpleNamespace(universe_id="industry:bank", name="银行"),
        asset_name="银行A",
        bar=SimpleNamespace(open=10, close=11, raw_record_id="raw:bar"),
        flow=SimpleNamespace(
            main_net_inflow=100,
            snapshot_id="flow:1",
            payload={"positive_streak": 4},
        ),
        status=SimpleNamespace(trading_status="available"),
    )

    assert item.returns_by_horizon == {1: 0.01, 3: 0.04, 5: 0.07}
    assert item.above_ma20 is True
    assert item.flow_positive_streak == 4
    assert item.breadth_change == 0.08
    assert item.valid_cross_sections == 2
    assert item.previous_sector_regime == "diffusion"
