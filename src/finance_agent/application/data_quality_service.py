"""数据质量应用服务。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finance_agent.storage.orm import (
    AssetORM,
    DataQualitySnapshotORM,
    FactorFrameORM,
    IndicatorFrameORM,
    MarketBarORM,
    RealtimeQuoteSnapshotORM,
    RecommendationRunORM,
)
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

    def refresh_quality_snapshots(
        self,
        *,
        market: str,
        timeframe: str = "1d",
        horizon: str = "swing",
        limit: int = 200,
        min_bars: int = 60,
        stale_after_seconds: int = 24 * 60 * 60,
        data_domains: list[str] | None = None,
    ) -> JsonDict:
        """按市场资产刷新数据质量快照，供 Agent 和触发器读取。"""

        checked_at = datetime.now(tz=UTC)
        domains = data_domains or [
            "market_bars",
            "realtime_quotes",
            "indicator_frames",
            "factor_frames",
            "recommendations",
        ]
        assets = list_market_assets(self.repository.session, market=market, limit=limit)
        summaries = [self._refresh_domain_summary(domain, market, checked_at) for domain in domains]
        snapshots: list[DataQualitySnapshotORM] = []
        for asset in assets:
            for domain in domains:
                snapshot = self._refresh_asset_domain(
                    asset=asset,
                    data_domain=domain,
                    timeframe=timeframe,
                    horizon=horizon,
                    min_bars=min_bars,
                    stale_after=timedelta(seconds=stale_after_seconds),
                    checked_at=checked_at,
                )
                if snapshot is not None:
                    snapshots.append(snapshot)
        return {
            "status": "available",
            "market": market,
            "checked_at": checked_at.isoformat(),
            "asset_count": len(assets),
            "snapshot_count": len(snapshots) + len(summaries),
            "summary_count": len(summaries),
            "data_domains": domains,
        }

    def _refresh_domain_summary(
        self,
        data_domain: str,
        market: str,
        checked_at: datetime,
    ) -> DataQualitySnapshotORM:
        """写入市场级质量摘要，保证仪表盘即使无资产也可见。"""

        return self.upsert_quality_snapshot(
            quality_id=f"quality:{market}:{data_domain}:summary",
            market=market,
            data_domain=data_domain,
            provider="internal:data_quality_refresh",
            status="available",
            freshness_status="fresh",
            checked_at=checked_at,
            issue_count=0,
            payload={"scope": "market_summary"},
        )

    def _refresh_asset_domain(
        self,
        *,
        asset: AssetORM,
        data_domain: str,
        timeframe: str,
        horizon: str,
        min_bars: int,
        stale_after: timedelta,
        checked_at: datetime,
    ) -> DataQualitySnapshotORM | None:
        """刷新单资产单领域质量快照。"""

        latest_data_at: datetime | None = None
        missing_items: list[str] = []
        payload: JsonDict = {}
        status = "available"

        if data_domain == "market_bars":
            bar_count, latest_data_at = market_bar_coverage(
                self.repository.session,
                asset_id=asset.asset_id,
                timeframe=timeframe,
            )
            payload["bar_count"] = bar_count
            payload["min_bars"] = min_bars
            if bar_count <= 0:
                status = "missing"
                missing_items.append("market_bars")
            elif bar_count < min_bars:
                status = "partial"
                missing_items.append(f"market_bars<{min_bars}")
        elif data_domain == "realtime_quotes":
            latest_data_at = latest_realtime_quote_at(self.repository.session, asset.asset_id)
            if latest_data_at is None:
                status = "missing"
                missing_items.append("realtime_quotes")
        elif data_domain == "indicator_frames":
            latest_data_at, frame_status = latest_indicator_frame(
                self.repository.session,
                asset_id=asset.asset_id,
                timeframe=timeframe,
                horizon=horizon,
            )
            payload["frame_status"] = frame_status
            if latest_data_at is None:
                status = "missing"
                missing_items.append("indicator_frames")
            elif frame_status not in {"available", "partial"}:
                status = "partial"
                missing_items.append(f"indicator_status:{frame_status}")
        elif data_domain == "factor_frames":
            latest_data_at, frame_status, missing_groups = latest_factor_frame(
                self.repository.session,
                asset_id=asset.asset_id,
                horizon=horizon,
            )
            payload["frame_status"] = frame_status
            payload["missing_groups"] = missing_groups
            if latest_data_at is None:
                status = "missing"
                missing_items.append("factor_frames")
            elif frame_status not in {"available", "partial"}:
                status = "partial"
                missing_items.append(f"factor_status:{frame_status}")
            elif missing_groups:
                status = "partial"
                missing_items.extend(f"factor_group:{group}" for group in missing_groups)
        elif data_domain == "recommendations":
            latest_data_at = latest_recommendation_at(
                self.repository.session,
                market=asset.market,
                horizon=horizon,
            )
            if latest_data_at is None:
                status = "missing"
                missing_items.append("recommendations")
        else:
            return None

        freshness_status = freshness_for(latest_data_at, checked_at, stale_after)
        if freshness_status in {"missing", "stale"} and status == "available":
            status = "partial"
        return self.upsert_quality_snapshot(
            quality_id=f"quality:{asset.asset_id}:{data_domain}",
            asset_id=asset.asset_id,
            symbol=asset.symbol,
            market=asset.market,
            data_domain=data_domain,
            provider="internal:data_quality_refresh",
            status=status,
            freshness_status=freshness_status,
            latest_data_at=latest_data_at,
            checked_at=checked_at,
            missing_items=missing_items,
            issue_count=len(missing_items) + (1 if freshness_status in {"missing", "stale"} else 0),
            payload=payload,
        )


def list_market_assets(session: Session, *, market: str, limit: int) -> list[AssetORM]:
    """读取本次质量刷新要覆盖的市场资产。"""

    statement = (
        select(AssetORM)
        .where(AssetORM.market == market, AssetORM.tradable.is_(True))
        .order_by(AssetORM.symbol)
        .limit(limit)
    )
    return list(session.scalars(statement))


def market_bar_coverage(
    session: Session,
    *,
    asset_id: str,
    timeframe: str,
) -> tuple[int, datetime | None]:
    """查询资产 K 线覆盖数量和最新时间。"""

    row = session.execute(
        select(func.count(MarketBarORM.timestamp), func.max(MarketBarORM.timestamp)).where(
            MarketBarORM.asset_id == asset_id,
            MarketBarORM.timeframe == timeframe,
            MarketBarORM.is_closed.is_(True),
            MarketBarORM.status.in_(("available", "revised")),
        )
    ).one()
    return int(row[0] or 0), row[1]


def latest_realtime_quote_at(session: Session, asset_id: str) -> datetime | None:
    """查询最新实时行情快照时间。"""

    return session.scalar(
        select(func.max(RealtimeQuoteSnapshotORM.as_of)).where(
            RealtimeQuoteSnapshotORM.asset_id == asset_id
        )
    )


def latest_indicator_frame(
    session: Session,
    *,
    asset_id: str,
    timeframe: str,
    horizon: str,
) -> tuple[datetime | None, str | None]:
    """查询最新指标帧时间和状态。"""

    row = session.execute(
        select(IndicatorFrameORM.as_of, IndicatorFrameORM.status)
        .where(
            IndicatorFrameORM.asset_id == asset_id,
            IndicatorFrameORM.timeframe == timeframe,
            IndicatorFrameORM.horizon == horizon,
        )
        .order_by(IndicatorFrameORM.as_of.desc())
        .limit(1)
    ).first()
    return (row[0], row[1]) if row else (None, None)


def latest_factor_frame(
    session: Session,
    *,
    asset_id: str,
    horizon: str,
) -> tuple[datetime | None, str | None, list[str]]:
    """查询最新因子帧时间、状态和缺失组。"""

    row = session.execute(
        select(FactorFrameORM.as_of, FactorFrameORM.status, FactorFrameORM.missing_groups)
        .where(FactorFrameORM.asset_id == asset_id, FactorFrameORM.horizon == horizon)
        .order_by(FactorFrameORM.as_of.desc())
        .limit(1)
    ).first()
    return (row[0], row[1], list(row[2] or [])) if row else (None, None, [])


def latest_recommendation_at(session: Session, *, market: str, horizon: str) -> datetime | None:
    """查询市场最新可用推荐运行完成时间。"""

    return session.scalar(
        select(func.max(RecommendationRunORM.finished_at)).where(
            RecommendationRunORM.market == market,
            RecommendationRunORM.horizon == horizon,
            RecommendationRunORM.status == "available",
        )
    )


def freshness_for(
    latest_data_at: datetime | None,
    checked_at: datetime,
    stale_after: timedelta,
) -> str:
    """根据最新数据时间计算新鲜度。"""

    if latest_data_at is None:
        return "missing"
    normalized = latest_data_at if latest_data_at.tzinfo else latest_data_at.replace(tzinfo=UTC)
    return "stale" if checked_at - normalized > stale_after else "fresh"
