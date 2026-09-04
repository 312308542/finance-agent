from __future__ import annotations

from finance_agent.application.sector_strength_service import (
    SectorStrengthInput,
    SectorStrengthService,
)


def test_sector_strength_ranks_hot_sectors_with_traceable_evidence() -> None:
    service = SectorStrengthService()

    result = service.rank_sectors(
        [
            SectorStrengthInput(
                sector_id="concept:robot",
                sector_name="机器人",
                asset_id="ashare:300001",
                asset_name="机器人A",
                pct_change=12.0,
                net_inflow=900_000_000,
                limit_up=True,
                popularity_rank=3,
                board_hits=2,
                returns_by_horizon={1: 0.12, 3: 0.18, 5: 0.24, 10: 0.30, 20: 0.35},
                above_ma20=True,
                flow_positive_streak=4,
                evidence_ids=["ev:flow:300001", "ev:limit:300001"],
            ),
            SectorStrengthInput(
                sector_id="concept:robot",
                sector_name="机器人",
                asset_id="ashare:300002",
                asset_name="机器人B",
                pct_change=6.0,
                net_inflow=300_000_000,
                limit_up=False,
                popularity_rank=12,
                board_hits=1,
                returns_by_horizon={1: 0.06, 3: 0.12, 5: 0.16, 10: 0.20, 20: 0.22},
                above_ma20=True,
                flow_positive_streak=3,
                evidence_ids=["ev:flow:300002"],
            ),
            SectorStrengthInput(
                sector_id="industry:bank",
                sector_name="银行",
                asset_id="ashare:600001",
                asset_name="银行A",
                pct_change=1.5,
                net_inflow=50_000_000,
                limit_up=False,
                popularity_rank=80,
                board_hits=0,
                evidence_ids=["ev:flow:600001"],
            ),
        ]
    )

    assert [item.sector_id for item in result] == ["concept:robot", "industry:bank"]
    robot = result[0]
    assert robot.sector_name == "机器人"
    assert robot.member_count == 2
    assert robot.limit_up_count == 1
    assert robot.total_net_inflow == 1_200_000_000
    assert robot.continuity == 3
    assert robot.strength_score > result[1].strength_score
    assert robot.evidence_ids == ["ev:flow:300001", "ev:limit:300001", "ev:flow:300002"]
    assert robot.payload["top_assets"][0]["asset_id"] == "ashare:300001"
    assert robot.excess_returns[5] == 0.20
    assert robot.breadth == 1.0
    assert robot.ma20_ratio == 1.0
    assert robot.flow_streak == 4
    assert robot.payload["strength_percentile"] == 100.0


def test_sector_strength_ignores_members_without_sector_id() -> None:
    service = SectorStrengthService()

    result = service.rank_sectors(
        [
            SectorStrengthInput(
                sector_id="",
                sector_name="缺失",
                asset_id="ashare:000001",
                pct_change=20,
                net_inflow=1_000_000_000,
                evidence_ids=["ev:bad"],
            )
        ]
    )

    assert result == []
