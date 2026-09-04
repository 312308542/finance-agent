from __future__ import annotations

from types import SimpleNamespace

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
