"""私人观察池应用服务。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.storage.orm import (
    AssetThesisORM,
    WatchlistItemEventORM,
    WatchlistItemORM,
    WatchlistORM,
)
from finance_agent.storage.repositories import WatchlistRepository

JsonDict = dict[str, Any]
DEFAULT_OWNER_ID = "default-owner"
DEFAULT_ASHARE_RESEARCH_WATCHLIST_ID = "watchlist:default-owner:ashare:research"
LEGACY_ASHARE_RECOMMENDATION_WATCHLIST_ID = (
    "watchlist:default-owner:ashare:recommendations"
)
RESEARCH_EXIT_EVENT_TYPES = frozenset(
    {"research_expired", "research_removed", "expired", "removed"}
)
RESEARCH_EXIT_STATUSES = frozenset({"expired", "removed"})


class WatchlistService:
    """观察池服务。

    观察池表示“需要持续跟踪”，不等于立即买入。服务层只维护状态和理由，
    是否升级为买入候选由 Workflow 再结合评分、信号、风险和持仓判断。
    """

    def __init__(self, session: Session) -> None:
        self.repository = WatchlistRepository(session)

    def upsert_watchlist(
        self,
        *,
        watchlist_id: str,
        owner_id: str,
        name: str,
        purpose: str,
        market: str | None = None,
        status: str = "active",
        payload: JsonDict | None = None,
    ) -> WatchlistORM:
        """新增或更新私人观察池。"""

        return self.repository.upsert_watchlist(
            watchlist_id=watchlist_id,
            owner_id=owner_id,
            name=name,
            market=market,
            purpose=purpose,
            status=status,
            payload=payload,
        )

    def add_or_update_item(
        self,
        *,
        watchlist_item_id: str,
        watchlist_id: str,
        asset_id: str,
        symbol: str,
        market: str,
        source_type: str,
        reason: str,
        source_id: str | None = None,
        watch_conditions: JsonDict | None = None,
        trigger_conditions: JsonDict | None = None,
        invalid_conditions: JsonDict | None = None,
        risk_level: str | None = None,
        status: str = "active",
        next_review_at: datetime | None = None,
        removed_at: datetime | None = None,
        removed_reason: str | None = None,
        payload: JsonDict | None = None,
    ) -> WatchlistItemORM:
        """新增或更新观察项。"""

        return self.repository.upsert_watchlist_item(
            watchlist_item_id=watchlist_item_id,
            watchlist_id=watchlist_id,
            asset_id=asset_id,
            symbol=symbol,
            market=market,
            source_type=source_type,
            source_id=source_id,
            reason=reason,
            watch_conditions=watch_conditions,
            trigger_conditions=trigger_conditions,
            invalid_conditions=invalid_conditions,
            risk_level=risk_level,
            status=status,
            next_review_at=next_review_at,
            removed_at=removed_at,
            removed_reason=removed_reason,
            payload=payload,
        )

    def get_watchlist(self, watchlist_id: str) -> WatchlistORM:
        """查询观察池定义。"""

        return self.repository.get_watchlist(watchlist_id)

    def transition_item(
        self,
        *,
        item: WatchlistItemORM,
        status: str,
        next_review_at: datetime | None,
        removed_at: datetime | None = None,
        removed_reason: str | None = None,
        owner_id: str | None = None,
        event_type: str | None = None,
        reason: str | None = None,
        source_decision_id: str | None = None,
        event_at: datetime | None = None,
        payload: JsonDict | None = None,
    ) -> WatchlistItemORM:
        """在保留原观察条件的前提下更新观察项状态。"""

        from_status = item.status
        merged_payload = dict(item.payload or {})
        merged_payload.update(payload or {})
        updated = self.add_or_update_item(
            watchlist_item_id=item.watchlist_item_id,
            watchlist_id=item.watchlist_id,
            asset_id=item.asset_id,
            symbol=item.symbol,
            market=item.market,
            source_type=item.source_type,
            source_id=item.source_id,
            reason=item.reason,
            watch_conditions=item.watch_conditions,
            trigger_conditions=item.trigger_conditions,
            invalid_conditions=item.invalid_conditions,
            risk_level=item.risk_level,
            status=status,
            next_review_at=next_review_at,
            removed_at=removed_at,
            removed_reason=removed_reason,
            payload=merged_payload,
        )
        if owner_id is not None and event_type is not None:
            created_at = event_at or datetime.now().astimezone()
            self.record_event(
                event_id=build_watchlist_event_id(
                    watchlist_item_id=item.watchlist_item_id,
                    event_type=event_type,
                    created_at=created_at,
                ),
                owner_id=owner_id,
                watchlist_id=item.watchlist_id,
                watchlist_item_id=item.watchlist_item_id,
                asset_id=item.asset_id,
                event_type=event_type,
                from_status=from_status,
                to_status=status,
                reason=reason or removed_reason,
                source_decision_id=source_decision_id,
                created_at=created_at,
                payload=payload,
            )
        return updated

    def list_active_items(
        self,
        *,
        owner_id: str,
        watchlist_id: str | None = None,
    ) -> tuple[WatchlistItemORM, ...]:
        """查询活跃观察项。"""

        return tuple(
            self.repository.list_active_items(owner_id=owner_id, watchlist_id=watchlist_id)
        )

    def migrate_recommendation_watchlist_to_research_pool(
        self,
        *,
        owner_id: str = DEFAULT_OWNER_ID,
        market: str = "ashare",
        source_watchlist_id: str | None = None,
        target_watchlist_id: str | None = None,
        as_of: datetime | None = None,
    ) -> JsonDict:
        """将旧推荐观察池 active 条目迁移到系统研究跟踪池。"""

        migrated_at = as_of or datetime.now(UTC)
        source_id = source_watchlist_id or default_legacy_recommendation_watchlist_id(
            owner_id=owner_id,
            market=market,
        )
        target_id = target_watchlist_id or default_research_watchlist_id(
            owner_id=owner_id,
            market=market,
        )
        self.upsert_watchlist(
            watchlist_id=target_id,
            owner_id=owner_id,
            name=default_research_watchlist_name(market),
            market=market,
            purpose="system_research_pool",
            status="active",
            payload={
                "source": "watchlist_migration",
                "migrated_from_watchlist_id": source_id,
                "migrated_at": migrated_at.isoformat(),
            },
        )

        migrated_count = 0
        for item in self.list_active_items(owner_id=owner_id, watchlist_id=source_id):
            payload = dict(item.payload or {})
            payload.update(
                {
                    "promotion_status": "system_research",
                    "migrated_from_watchlist_id": source_id,
                    "migrated_from_watchlist_item_id": item.watchlist_item_id,
                    "migrated_at": migrated_at.isoformat(),
                }
            )
            new_item_id = build_watchlist_item_id(
                watchlist_id=target_id,
                asset_id=item.asset_id,
            )
            migrated_item = self.add_or_update_item(
                watchlist_item_id=new_item_id,
                watchlist_id=target_id,
                asset_id=item.asset_id,
                symbol=item.symbol,
                market=item.market,
                source_type=item.source_type,
                source_id=item.source_id,
                reason=item.reason,
                watch_conditions=item.watch_conditions,
                trigger_conditions=item.trigger_conditions,
                invalid_conditions=item.invalid_conditions,
                risk_level=item.risk_level,
                status=item.status,
                next_review_at=item.next_review_at,
                removed_at=item.removed_at,
                removed_reason=item.removed_reason,
                payload=payload,
            )
            self.record_event(
                event_id=build_watchlist_event_id(
                    watchlist_item_id=migrated_item.watchlist_item_id,
                    event_type="migrated_to_research_pool",
                    created_at=migrated_at,
                ),
                owner_id=owner_id,
                watchlist_id=target_id,
                watchlist_item_id=migrated_item.watchlist_item_id,
                asset_id=item.asset_id,
                event_type="migrated_to_research_pool",
                from_status=None,
                to_status=migrated_item.status,
                reason="旧推荐观察池条目迁移到系统研究跟踪池。",
                created_at=migrated_at,
                payload={
                    "source_watchlist_id": source_id,
                    "source_watchlist_item_id": item.watchlist_item_id,
                    "target_watchlist_id": target_id,
                },
            )
            migrated_count += 1

        return {
            "status": "executed",
            "migrated_count": migrated_count,
            "source_watchlist_id": source_id,
            "target_watchlist_id": target_id,
        }

    def expire_research_pool_items(
        self,
        *,
        owner_id: str = DEFAULT_OWNER_ID,
        watchlist_id: str = DEFAULT_ASHARE_RESEARCH_WATCHLIST_ID,
        as_of: datetime | None = None,
    ) -> JsonDict:
        """将系统研究跟踪池中已到期的 active 条目标记为 expired。"""

        expired_at = as_of or datetime.now(UTC)
        expired_count = 0
        removed_reason = "系统研究跟踪有效期已到期。"
        for item in self.list_active_items(owner_id=owner_id, watchlist_id=watchlist_id):
            expires_at = parse_payload_datetime((item.payload or {}).get("expires_at"))
            if expires_at is None or normalize_datetime(expires_at) > normalize_datetime(expired_at):
                continue

            payload = {
                "expired_at": expired_at.isoformat(),
                "expires_at": expires_at.isoformat(),
            }
            self.transition_item(
                item=item,
                status="expired",
                next_review_at=None,
                removed_at=expired_at,
                removed_reason=removed_reason,
                owner_id=owner_id,
                event_type="research_expired",
                reason=removed_reason,
                event_at=expired_at,
                payload=payload,
            )
            expired_count += 1

        return {"status": "executed", "expired_count": expired_count}

    def list_asset_theses(
        self,
        *,
        owner_id: str,
        asset_id: str,
        status: str | None = "active",
    ) -> tuple[AssetThesisORM, ...]:
        """查询观察池相关投资假设。"""

        return tuple(
            self.repository.list_asset_theses(
                owner_id=owner_id,
                asset_id=asset_id,
                status=status,
            )
        )

    def record_event(
        self,
        *,
        event_id: str,
        owner_id: str,
        watchlist_id: str,
        watchlist_item_id: str,
        asset_id: str,
        event_type: str,
        to_status: str,
        created_at: datetime,
        from_status: str | None = None,
        reason: str | None = None,
        source_decision_id: str | None = None,
        payload: JsonDict | None = None,
    ) -> WatchlistItemEventORM:
        """记录观察池成员事件。"""

        return self.repository.insert_watchlist_event(
            event_id=event_id,
            owner_id=owner_id,
            watchlist_id=watchlist_id,
            watchlist_item_id=watchlist_item_id,
            asset_id=asset_id,
            event_type=event_type,
            from_status=from_status,
            to_status=to_status,
            reason=reason,
            source_decision_id=source_decision_id,
            created_at=created_at,
            payload=payload,
        )

    def list_events(
        self,
        *,
        watchlist_id: str,
        limit: int = 50,
    ) -> tuple[WatchlistItemEventORM, ...]:
        """查询观察池事件。"""

        return tuple(
            self.repository.list_watchlist_events(watchlist_id=watchlist_id, limit=limit)
        )

    def get_research_intake_cooldown(
        self,
        *,
        watchlist_id: str,
        asset_id: str,
        as_of: datetime,
        cooldown_days: int = 7,
    ) -> JsonDict | None:
        """查询研究池自动入池冷却信息，未命中冷却窗口时返回 None。"""

        if cooldown_days <= 0:
            return None
        current_at = normalize_datetime(as_of)
        for event in self.repository.list_recent_watchlist_item_events(
            watchlist_id=watchlist_id,
            asset_id=asset_id,
            limit=10,
        ):
            event_type = str(getattr(event, "event_type", "") or "")
            to_status = str(getattr(event, "to_status", "") or "")
            if (
                event_type not in RESEARCH_EXIT_EVENT_TYPES
                and to_status not in RESEARCH_EXIT_STATUSES
            ):
                continue
            exit_at = normalize_datetime(event.created_at)
            cooldown_until = exit_at + timedelta(days=cooldown_days)
            if current_at >= cooldown_until:
                return None
            return {
                "reason": "cooldown",
                "event_id": event.event_id,
                "event_type": event_type,
                "last_exit_at": exit_at.isoformat(),
                "cooldown_until": cooldown_until.isoformat(),
                "cooldown_days": cooldown_days,
                "last_exit_reason": event.reason,
            }
        return None

    def record_thesis(
        self,
        *,
        thesis_id: str,
        owner_id: str,
        asset_id: str,
        source_type: str,
        thesis: str,
        source_id: str | None = None,
        supporting_points: list[JsonDict] | None = None,
        risk_points: list[JsonDict] | None = None,
        invalid_if: JsonDict | None = None,
        status: str = "active",
        payload: JsonDict | None = None,
    ) -> AssetThesisORM:
        """记录投资假设，供后续风险反驳和复盘引用。"""

        return self.repository.upsert_asset_thesis(
            thesis_id=thesis_id,
            owner_id=owner_id,
            asset_id=asset_id,
            source_type=source_type,
            source_id=source_id,
            thesis=thesis,
            supporting_points=supporting_points,
            risk_points=risk_points,
            invalid_if=invalid_if,
            status=status,
            payload=payload,
        )


def build_watchlist_event_id(
    *,
    watchlist_item_id: str,
    event_type: str,
    created_at: datetime,
) -> str:
    """生成观察池事件 ID。"""

    return f"watchlist_event:{watchlist_item_id}:{event_type}:{created_at:%Y%m%d%H%M%S}"


def parse_payload_datetime(value: Any) -> datetime | None:
    """从 payload 字段解析 ISO 时间，解析失败时返回 None。"""

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_datetime(value: datetime) -> datetime:
    """统一时间比较口径，缺少时区时按 UTC 处理。"""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def build_watchlist_item_id(*, watchlist_id: str, asset_id: str) -> str:
    """生成观察池条目 ID。"""

    return f"watchlist_item:{watchlist_id}:{asset_id}"


def default_research_watchlist_id(*, owner_id: str, market: str) -> str:
    """返回系统研究跟踪池 ID。"""

    if owner_id == DEFAULT_OWNER_ID and market == "ashare":
        return DEFAULT_ASHARE_RESEARCH_WATCHLIST_ID
    return f"watchlist:{owner_id}:{market}:research"


def default_legacy_recommendation_watchlist_id(*, owner_id: str, market: str) -> str:
    """返回旧推荐观察池 ID。"""

    if owner_id == DEFAULT_OWNER_ID and market == "ashare":
        return LEGACY_ASHARE_RECOMMENDATION_WATCHLIST_ID
    return f"watchlist:{owner_id}:{market}:recommendations"


def default_research_watchlist_name(market: str) -> str:
    """返回系统研究跟踪池名称。"""

    return {
        "ashare": "A 股系统研究跟踪池",
        "crypto_spot": "数字货币现货系统研究跟踪池",
        "crypto_future": "数字货币合约系统研究跟踪池",
    }.get(market, f"{market} 系统研究跟踪池")
