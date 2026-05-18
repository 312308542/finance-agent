"""私人观察池应用服务。"""

from __future__ import annotations

from datetime import datetime
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
