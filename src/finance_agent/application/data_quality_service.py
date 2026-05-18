"""数据质量应用服务。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.storage.orm import DataQualitySnapshotORM
from finance_agent.storage.repositories import DataQualityRepository

JsonDict = dict[str, Any]


class DataQualityService:
    """记录和查询资产级数据新鲜度、缺口和质量状态。"""

    def __init__(self, session: Session) -> None:
        self.repository = DataQualityRepository(session)

    def upsert_quality_snapshot(
        self,
        *,
        quality_id: str,
        market: str,
        data_domain: str,
        provider: str,
        status: str,
        freshness_status: str,
        checked_at: datetime,
        issue_count: int,
        asset_id: str | None = None,
        symbol: str | None = None,
        latest_data_at: datetime | None = None,
        missing_items: list[str] | None = None,
        payload: JsonDict | None = None,
    ) -> DataQualitySnapshotORM:
        """新增或更新一条数据质量快照。"""

        return self.repository.upsert_quality_snapshot(
            quality_id=quality_id,
            asset_id=asset_id,
            symbol=symbol,
            market=market,
            data_domain=data_domain,
            provider=provider,
            status=status,
            freshness_status=freshness_status,
            latest_data_at=latest_data_at,
            checked_at=checked_at,
            missing_items=missing_items,
            issue_count=issue_count,
            payload=payload,
        )

    def list_latest_quality(
        self,
        *,
        asset_id: str | None = None,
        market: str | None = None,
        data_domain: str | None = None,
        limit: int = 20,
    ) -> tuple[DataQualitySnapshotORM, ...]:
        """查询最近数据质量快照。"""

        return tuple(
            self.repository.list_latest_quality(
                asset_id=asset_id,
                market=market,
                data_domain=data_domain,
                limit=limit,
            )
        )
