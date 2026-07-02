from __future__ import annotations

from finance_agent.application.theme_context_service import (
    ThemeContextInput,
    ThemeContextService,
)


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
    )

    leader_payload = contexts["ashare:300001"].to_member_payload()
    groups = {item["group"]: item for item in leader_payload["theme_context"]["factor_groups"]}
    assert groups["sector_strength"]["factors"]["sector_id"] == "concept:robot"
    assert groups["leadership"]["factors"]["role"] == "leader"
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
