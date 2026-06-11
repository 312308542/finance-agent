from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from finance_agent.application.dashboard_service import DashboardService


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
