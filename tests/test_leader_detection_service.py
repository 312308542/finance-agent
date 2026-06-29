from __future__ import annotations

from finance_agent.application.leader_detection_service import (
    LeaderCandidateInput,
    LeaderDetectionService,
)


def test_leader_detection_ranks_leaders_and_marks_unbuyable() -> None:
    service = LeaderDetectionService()

    result = service.rank_leaders(
        [
            LeaderCandidateInput(
                sector_id="concept:robot",
                asset_id="ashare:300001",
                asset_name="龙头A",
                pct_change=20.0,
                net_inflow=800_000_000,
                limit_up_time="09:32:00",
                consecutive_limit_up=3,
                one_word_limit=True,
                evidence_ids=["ev:leader:a", "ev:limit:a"],
            ),
            LeaderCandidateInput(
                sector_id="concept:robot",
                asset_id="ashare:300002",
                asset_name="龙头B",
                pct_change=13.0,
                net_inflow=700_000_000,
                limit_up_time="10:10:00",
                consecutive_limit_up=2,
                evidence_ids=["ev:leader:b"],
            ),
            LeaderCandidateInput(
                sector_id="concept:robot",
                asset_id="ashare:300003",
                asset_name="跟风C",
                pct_change=6.0,
                net_inflow=120_000_000,
                consecutive_limit_up=0,
                evidence_ids=["ev:leader:c"],
            ),
        ],
        strong_sector_ids=["concept:robot"],
    )

    assert [item.asset_id for item in result] == [
        "ashare:300001",
        "ashare:300002",
        "ashare:300003",
    ]
    assert [item.leader_rank for item in result] == [1, 2, 3]
    assert result[0].role == "leader"
    assert result[1].role == "challenger"
    assert result[2].role == "follower"
    assert result[0].buyability_warning == "unbuyable"
    assert result[0].payload["unbuyable_reasons"] == ["one_word_limit"]
    assert result[1].buyability_warning == "tradable"
    assert result[0].evidence_ids == ["ev:leader:a", "ev:limit:a"]


def test_leader_detection_filters_non_strong_sectors() -> None:
    service = LeaderDetectionService()

    result = service.rank_leaders(
        [
            LeaderCandidateInput(
                sector_id="industry:bank",
                asset_id="ashare:600001",
                pct_change=7.0,
                net_inflow=300_000_000,
                evidence_ids=["ev:bank"],
            )
        ],
        strong_sector_ids=["concept:robot"],
    )

    assert result == []
