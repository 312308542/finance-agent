from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from finance_agent.application.dashboard_service import DashboardService

NOW = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)


def test_dashboard_watchlists_split_research_and_manual_pools() -> None:
    """Dashboard 应区分系统研究跟踪池和用户观察池。"""

    class FakeWatchlistRepository:
        def list_active_items(self, *, owner_id: str, watchlist_id: str | None = None) -> list[Any]:
            assert owner_id == "default-owner"
            assert watchlist_id is None
            return [
                watchlist_item(
                    watchlist_id="watchlist:default-owner:ashare:technical",
                    symbol="600036",
                    source_type="technical_screening",
                    payload={"source_type": "technical_screening"},
                ),
                watchlist_item(
                    watchlist_id="watchlist:default-owner:ashare:research",
                    symbol="600519",
                    source_type="recommendation",
                    payload={"promotion_status": "system_research"},
                ),
                watchlist_item(
                    watchlist_id="watchlist:default-owner:ashare:manual",
                    symbol="000001",
                    source_type="manual",
                    payload={},
                ),
            ]

    service = DashboardService.__new__(DashboardService)
    service.watchlists = FakeWatchlistRepository()

    payload = service.get_watchlists(owner_id="default-owner", limit=10)

    assert [item["pool"] for item in payload["items"]] == [
        "technical_screening_pool",
        "system_research_pool",
        "manual_watchlist",
    ]
    assert payload["pools"] == [
        {
            "key": "technical_screening_pool",
            "label": "技术初筛池",
            "count": 1,
            "description": "历史行情完成后的技术粗筛结果，只表示后续优先补齐，不代表买入建议。",
        },
        {
            "key": "system_research_pool",
            "label": "系统研究跟踪",
            "count": 1,
            "description": "系统推荐后自动跟踪，尚未代表用户确认关注。",
        },
        {
            "key": "manual_watchlist",
            "label": "用户观察池",
            "count": 1,
            "description": "用户手动加入或确认关注的资产。",
        },
        {
            "key": "other_watchlist",
            "label": "其他观察项",
            "count": 0,
            "description": "暂未归类到研究池或用户观察池的有效条目。",
        },
    ]


def test_latest_recommendations_returns_lifecycle_groups_and_zero_buy_message() -> None:
    """推荐接口应按生命周期分组，并在零买入时返回明确说明。"""

    service = DashboardService.__new__(DashboardService)
    service.recommendations = SimpleNamespace(
        list_available_runs_since=lambda **_: [
            SimpleNamespace(
                run_id="run:latest",
                universe_id="universe:merged:ashare:recommendation",
                screening_id="screening:1",
                strategy="strategy:ashare:adaptive_v1",
                market="ashare",
                horizon="swing",
                limit=80,
                status="available",
                started_at=datetime(2026, 9, 4, 15, 0, tzinfo=UTC),
                finished_at=datetime(2026, 9, 4, 15, 1, tzinfo=UTC),
                summary="本次没有满足新增买入门槛的标的。",
                payload={"buy_ready_count": 0},
            )
        ],
        list_top_recommendations=lambda **_: [
            SimpleNamespace(
                recommendation_id="rec:1",
                run_id="run:latest",
                asset_id="ashare:600519",
                symbol="600519",
                name="贵州茅台",
                market="ashare",
                horizon="swing",
                action="watch",
                rank=1,
                total_score=Decimal("62"),
                confidence=Decimal("0.62"),
                conviction="medium",
                score_id=None,
                factor_frame_id=None,
                signal_ids=[],
                risk_ids=[],
                evidence_ids=[],
                summary="等待结构确认。",
                payload={
                    "recommendation_state": "setup_confirming",
                    "previous_state": "watch",
                    "state_changed_at": "2026-09-04T15:00:00+08:00",
                    "decision_snapshot_id": "decision:ashare:2026-09-04:abc",
                    "planned_horizon_days": 10,
                    "sector_regime": "diffusion",
                    "structure_verdict": {"status": "waiting"},
                    "entry_zone": {"low": 100, "high": 102},
                    "invalidation_price": 96,
                    "expected_net_return": 0.04,
                    "downside_risk": 0.02,
                    "replacement_reason": "暂无新增买入门槛",
                    "data_quality": "available",
                },
            )
        ],
    )
    service._list_lifecycle_states = lambda **_: [
        SimpleNamespace(
            owner_id="owner-a",
            strategy_id="strategy:ashare:adaptive_v1",
            asset_id="ashare:600519",
            current_state="setup_confirming",
            previous_state="watch",
            state_changed_at=datetime(2026, 9, 4, 15, 0, tzinfo=UTC),
            decision_snapshot_id="decision:ashare:2026-09-04:abc",
            payload={"sector_regime": "diffusion"},
        )
    ]

    payload = service.get_latest_recommendations(owner_id="owner-a", market="ashare", limit=80)

    assert set(payload["groups"]) == {
        "new_opportunities",
        "continuing",
        "waiting_entry",
        "positions",
        "weakening_or_exit",
    }
    assert payload["metrics"]["buy_ready_count"] == 0
    assert payload["message"] == "今日没有满足新增买入门槛的机会。"
    assert payload["groups"]["waiting_entry"][0]["recommendation_state"] == "setup_confirming"


def test_latest_recommendations_isolates_lifecycle_by_owner() -> None:
    """生命周期推荐不能把其他用户的资产泄露到当前用户。"""

    service = DashboardService.__new__(DashboardService)
    service.recommendations = SimpleNamespace(
        list_available_runs_since=lambda **_: [
            SimpleNamespace(
                run_id="run:latest",
                universe_id=None,
                screening_id=None,
                strategy="strategy:ashare:adaptive_v1",
                market="ashare",
                horizon="swing",
                limit=80,
                status="available",
                started_at=datetime(2026, 9, 4, 15, 0, tzinfo=UTC),
                finished_at=None,
                summary=None,
                payload={"buy_ready_count": 0},
            )
        ],
        list_top_recommendations=lambda **_: [],
    )
    service._list_lifecycle_states = lambda **_: []

    payload = service.get_latest_recommendations(owner_id="owner-a", market="ashare", limit=80)

    assert payload["recommendations"] == []


def test_portfolio_overview_exposes_monitoring_state_and_execution_boundary() -> None:
    """持仓页面应区分监控建议和真实执行状态，T+1 不可卖不能伪装为已卖出。"""

    position = SimpleNamespace(
        position_id="position:1",
        portfolio_id="portfolio:1",
        asset_id="ashare:600519",
        symbol="600519",
        market="ashare",
        side="long",
        quantity=Decimal("1000"),
        avg_cost=Decimal("10"),
        last_price=Decimal("9.2"),
        market_value=Decimal("9200"),
        unrealized_pnl=Decimal("-800"),
        unrealized_pnl_pct=Decimal("-0.08"),
        portfolio_weight=Decimal("0.10"),
        status="active",
        as_of=NOW,
        payload={},
    )
    portfolio = SimpleNamespace(
        portfolio_id="portfolio:1",
        owner_id="owner-a",
        name="测试组合",
        portfolio_type="manual",
        base_currency="CNY",
        risk_profile="balanced",
        total_equity=Decimal("100000"),
        cash=Decimal("90000"),
        market_value=Decimal("10000"),
        max_position_weight=Decimal("0.25"),
        max_drawdown_alert=Decimal("0.12"),
        status="active",
        as_of=NOW,
        payload={},
    )
    monitoring_state = SimpleNamespace(
        monitoring_state_id="monitoring:position:1",
        position_id="position:1",
        owner_id="owner-a",
        current_action="unexecutable",
        previous_valid_action="hold",
        total_quantity=Decimal("1000"),
        sellable_quantity=Decimal("0"),
        planned_horizon_days=10,
        invalidation_price=Decimal("9.4"),
        protective_price=Decimal("9.5"),
        sector_regime="cooling",
        last_evaluated_at=NOW,
        payload={"structure_direction": "bearish", "reason_codes": ["t1_not_sellable"]},
    )
    service = DashboardService.__new__(DashboardService)
    service.portfolios = SimpleNamespace(
        list_portfolios=lambda **_: [portfolio],
        list_positions=lambda *_: [position],
    )
    service.assets = SimpleNamespace(list_intraday_quote_latest=lambda **_: [])
    service.monitoring = SimpleNamespace(
        list_states_by_position_ids=lambda *_args, **_kwargs: [monitoring_state],
        list_events=lambda **_: [
            SimpleNamespace(
                event_id="event:1",
                action="unexecutable",
                severity="critical",
                reason_codes=["t1_not_sellable"],
                occurred_at=NOW,
                payload={},
            )
        ],
    )

    payload = service.get_portfolio_overview(owner_id="owner-a")

    item = payload["positions"][0]["monitoring"]
    assert item["action"] == "unexecutable"
    assert item["intended_action"] == "exit"
    assert item["sellable_quantity"] == "0"
    assert item["reason_codes"] == ["t1_not_sellable"]
    assert item["execution_status"] == "blocked"


def watchlist_item(
    *,
    watchlist_id: str,
    symbol: str,
    source_type: str,
    payload: dict[str, Any],
) -> SimpleNamespace:
    return SimpleNamespace(
        watchlist_item_id=f"watchlist_item:{watchlist_id}:ashare:{symbol}",
        watchlist_id=watchlist_id,
        asset_id=f"ashare:{symbol}",
        symbol=symbol,
        market="ashare",
        source_type=source_type,
        source_id=None,
        reason=f"{symbol} 观察原因",
        status="active",
        risk_level=None,
        next_review_at=datetime(2026, 6, 9, tzinfo=UTC),
        watch_conditions={},
        trigger_conditions={},
        invalid_conditions={},
        payload=payload,
    )
