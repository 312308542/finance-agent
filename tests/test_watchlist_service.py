from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from finance_agent.application.watchlist_service import (
    DEFAULT_ASHARE_RESEARCH_WATCHLIST_ID,
    LEGACY_ASHARE_RECOMMENDATION_WATCHLIST_ID,
    WatchlistService,
)


def test_migrate_recommendation_watchlist_to_research_pool() -> None:
    """旧推荐观察池 active 项应可一次性迁移到系统研究跟踪池。"""

    class FakeRepository:
        def __init__(self) -> None:
            self.watchlists: list[dict[str, Any]] = []
            self.items: list[dict[str, Any]] = []
            self.events: list[dict[str, Any]] = []

        def upsert_watchlist(self, **kwargs: Any) -> SimpleNamespace:
            self.watchlists.append(kwargs)
            return SimpleNamespace(**kwargs)

        def list_active_items(self, *, owner_id: str, watchlist_id: str | None = None) -> list[Any]:
            assert owner_id == "default-owner"
            assert watchlist_id == LEGACY_ASHARE_RECOMMENDATION_WATCHLIST_ID
            return [
                SimpleNamespace(
                    watchlist_item_id=f"watchlist_item:{LEGACY_ASHARE_RECOMMENDATION_WATCHLIST_ID}:ashare:600519",
                    watchlist_id=LEGACY_ASHARE_RECOMMENDATION_WATCHLIST_ID,
                    asset_id="ashare:600519",
                    symbol="600519",
                    market="ashare",
                    source_type="recommendation",
                    source_id="asset_rec:ashare:600519",
                    reason="旧推荐观察池条目。",
                    watch_conditions={"conditions": ["趋势保持"]},
                    trigger_conditions={"conditions": ["趋势保持"]},
                    invalid_conditions={"conditions": ["信号转弱"]},
                    risk_level="medium",
                    status="active",
                    next_review_at=None,
                    removed_at=None,
                    removed_reason=None,
                    payload={"recommendation_run_id": "run:legacy"},
                )
            ]

        def upsert_watchlist_item(self, **kwargs: Any) -> SimpleNamespace:
            self.items.append(kwargs)
            return SimpleNamespace(**kwargs)

        def insert_watchlist_event(self, **kwargs: Any) -> SimpleNamespace:
            self.events.append(kwargs)
            return SimpleNamespace(**kwargs)

    service = WatchlistService.__new__(WatchlistService)
    service.repository = FakeRepository()
    as_of = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)

    result = service.migrate_recommendation_watchlist_to_research_pool(
        owner_id="default-owner",
        market="ashare",
        as_of=as_of,
    )

    assert result == {
        "status": "executed",
        "migrated_count": 1,
        "source_watchlist_id": LEGACY_ASHARE_RECOMMENDATION_WATCHLIST_ID,
        "target_watchlist_id": DEFAULT_ASHARE_RESEARCH_WATCHLIST_ID,
    }
    assert service.repository.watchlists[0]["purpose"] == "system_research_pool"
    assert service.repository.items[0]["watchlist_id"] == DEFAULT_ASHARE_RESEARCH_WATCHLIST_ID
    assert service.repository.items[0]["payload"]["migrated_from_watchlist_id"] == (
        LEGACY_ASHARE_RECOMMENDATION_WATCHLIST_ID
    )
    assert service.repository.items[0]["payload"]["promotion_status"] == "system_research"
    assert service.repository.events[0]["event_type"] == "migrated_to_research_pool"


def test_expire_research_pool_items_marks_only_expired_entries() -> None:
    """系统研究跟踪池应能按 expires_at 清理到期条目。"""

    class FakeRepository:
        def __init__(self) -> None:
            self.updated_items: list[dict[str, Any]] = []
            self.events: list[dict[str, Any]] = []

        def list_active_items(self, *, owner_id: str, watchlist_id: str | None = None) -> list[Any]:
            assert owner_id == "default-owner"
            assert watchlist_id == DEFAULT_ASHARE_RESEARCH_WATCHLIST_ID
            return [
                research_item("600519", "2026-06-08T09:30:00+00:00"),
                research_item("000001", "2026-06-10T09:30:00+00:00"),
                research_item("600036", None),
            ]

        def upsert_watchlist_item(self, **kwargs: Any) -> SimpleNamespace:
            self.updated_items.append(kwargs)
            return SimpleNamespace(**kwargs)

        def insert_watchlist_event(self, **kwargs: Any) -> SimpleNamespace:
            self.events.append(kwargs)
            return SimpleNamespace(**kwargs)

    service = WatchlistService.__new__(WatchlistService)
    service.repository = FakeRepository()
    as_of = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)

    result = service.expire_research_pool_items(
        owner_id="default-owner",
        watchlist_id=DEFAULT_ASHARE_RESEARCH_WATCHLIST_ID,
        as_of=as_of,
    )

    assert result == {"status": "executed", "expired_count": 1}
    assert service.repository.updated_items[0]["symbol"] == "600519"
    assert service.repository.updated_items[0]["status"] == "expired"
    assert service.repository.updated_items[0]["removed_reason"] == "系统研究跟踪有效期已到期。"
    assert service.repository.events[0]["event_type"] == "research_expired"


def test_research_intake_cooldown_blocks_recent_exit_events() -> None:
    """最近被移出或过期的研究池标的，应在冷却窗口内阻止自动重新入池。"""

    class FakeRepository:
        def list_recent_watchlist_item_events(
            self,
            *,
            watchlist_id: str,
            asset_id: str,
            limit: int = 10,
        ) -> list[Any]:
            assert watchlist_id == DEFAULT_ASHARE_RESEARCH_WATCHLIST_ID
            assert asset_id == "ashare:600519"
            assert limit == 10
            return [
                SimpleNamespace(
                    event_id="watchlist_event:expired",
                    event_type="research_expired",
                    to_status="expired",
                    created_at=datetime(2026, 6, 10, 9, 30, tzinfo=UTC),
                    reason="系统研究跟踪有效期已到期。",
                    payload={},
                )
            ]

    service = WatchlistService.__new__(WatchlistService)
    service.repository = FakeRepository()

    cooldown = service.get_research_intake_cooldown(
        watchlist_id=DEFAULT_ASHARE_RESEARCH_WATCHLIST_ID,
        asset_id="ashare:600519",
        as_of=datetime(2026, 6, 12, 9, 30, tzinfo=UTC),
        cooldown_days=7,
    )

    assert cooldown is not None
    assert cooldown["reason"] == "cooldown"
    assert cooldown["event_type"] == "research_expired"
    assert cooldown["last_exit_at"] == "2026-06-10T09:30:00+00:00"
    assert cooldown["cooldown_until"] == "2026-06-17T09:30:00+00:00"


def test_research_intake_cooldown_allows_after_window() -> None:
    """超过冷却窗口后，同一标的可以再次由推荐链路自动进入研究池。"""

    class FakeRepository:
        def list_recent_watchlist_item_events(
            self,
            *,
            watchlist_id: str,
            asset_id: str,
            limit: int = 10,
        ) -> list[Any]:
            return [
                SimpleNamespace(
                    event_id="watchlist_event:removed",
                    event_type="research_removed",
                    to_status="removed",
                    created_at=datetime(2026, 6, 1, 9, 30, tzinfo=UTC),
                    reason="人工移出研究池。",
                    payload={},
                )
            ]

    service = WatchlistService.__new__(WatchlistService)
    service.repository = FakeRepository()

    cooldown = service.get_research_intake_cooldown(
        watchlist_id=DEFAULT_ASHARE_RESEARCH_WATCHLIST_ID,
        asset_id="ashare:600519",
        as_of=datetime(2026, 6, 12, 9, 30, tzinfo=UTC),
        cooldown_days=7,
    )

    assert cooldown is None


def research_item(symbol: str, expires_at: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        watchlist_item_id=f"watchlist_item:{DEFAULT_ASHARE_RESEARCH_WATCHLIST_ID}:ashare:{symbol}",
        watchlist_id=DEFAULT_ASHARE_RESEARCH_WATCHLIST_ID,
        asset_id=f"ashare:{symbol}",
        symbol=symbol,
        market="ashare",
        source_type="recommendation",
        source_id=f"asset_rec:ashare:{symbol}",
        reason=f"{symbol} 系统研究跟踪。",
        watch_conditions={},
        trigger_conditions={},
        invalid_conditions={},
        risk_level=None,
        status="active",
        next_review_at=None,
        removed_at=None,
        removed_reason=None,
        payload={"expires_at": expires_at} if expires_at else {},
    )
