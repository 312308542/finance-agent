"""组合和持仓应用服务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from finance_agent.storage.orm import (
    PortfolioORM,
    PortfolioSnapshotORM,
    PositionORM,
    PositionSnapshotORM,
)
from finance_agent.storage.repositories import PortfolioRepository

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class PortfolioSnapshot:
    """私人金融助手读取组合时返回的结构化快照。"""

    portfolio: PortfolioORM
    positions: tuple[PositionORM, ...]


class PortfolioService:
    """组合和持仓服务。

    本服务只负责当前组合事实的写入和读取。持仓动作建议、风险反驳和换股判断
    交给 Workflow 层处理。
    """

    def __init__(self, session: Session) -> None:
        self.repository = PortfolioRepository(session)

    def upsert_portfolio(
        self,
        *,
        portfolio_id: str,
        owner_id: str,
        name: str,
        portfolio_type: str,
        base_currency: str,
        risk_profile: str,
        as_of: datetime,
        total_equity: Decimal | None = None,
        cash: Decimal | None = None,
        market_value: Decimal | None = None,
        max_position_weight: Decimal | None = None,
        max_drawdown_alert: Decimal | None = None,
        status: str = "active",
        snapshot_source: str = "portfolio_service",
        payload: JsonDict | None = None,
    ) -> PortfolioORM:
        """新增或更新组合定义。"""

        portfolio = self.repository.upsert_portfolio(
            portfolio_id=portfolio_id,
            owner_id=owner_id,
            name=name,
            portfolio_type=portfolio_type,
            base_currency=base_currency,
            risk_profile=risk_profile,
            total_equity=total_equity,
            cash=cash,
            market_value=market_value,
            max_position_weight=max_position_weight,
            max_drawdown_alert=max_drawdown_alert,
            status=status,
            as_of=as_of,
            payload=payload,
        )
        position_count = len(self.repository.list_positions(portfolio_id, status=None))
        self.repository.insert_portfolio_snapshot(
            snapshot_id=build_portfolio_snapshot_id(
                portfolio_id=portfolio_id,
                captured_at=as_of,
                source=snapshot_source,
            ),
            portfolio_id=portfolio_id,
            owner_id=owner_id,
            total_equity=total_equity,
            cash=cash,
            market_value=market_value,
            position_count=position_count,
            source=snapshot_source,
            captured_at=as_of,
            payload=payload,
        )
        return portfolio

    def upsert_position(
        self,
        *,
        position_id: str,
        portfolio_id: str,
        asset_id: str,
        symbol: str,
        market: str,
        side: str,
        quantity: Decimal,
        as_of: datetime,
        avg_cost: Decimal | None = None,
        last_price: Decimal | None = None,
        market_value: Decimal | None = None,
        unrealized_pnl: Decimal | None = None,
        unrealized_pnl_pct: Decimal | None = None,
        portfolio_weight: Decimal | None = None,
        leverage: Decimal | None = None,
        liquidation_price: Decimal | None = None,
        status: str = "active",
        snapshot_source: str = "portfolio_service",
        payload: JsonDict | None = None,
    ) -> PositionORM:
        """新增或更新组合当前持仓。"""

        position = self.repository.upsert_position(
            position_id=position_id,
            portfolio_id=portfolio_id,
            asset_id=asset_id,
            symbol=symbol,
            market=market,
            side=side,
            quantity=quantity,
            avg_cost=avg_cost,
            last_price=last_price,
            market_value=market_value,
            unrealized_pnl=unrealized_pnl,
            unrealized_pnl_pct=unrealized_pnl_pct,
            portfolio_weight=portfolio_weight,
            leverage=leverage,
            liquidation_price=liquidation_price,
            status=status,
            as_of=as_of,
            payload=payload,
        )
        self.repository.insert_position_snapshot(
            snapshot_id=build_position_snapshot_id(
                position_id=position.position_id,
                captured_at=as_of,
                source=snapshot_source,
            ),
            position_id=position.position_id,
            portfolio_id=portfolio_id,
            asset_id=asset_id,
            symbol=symbol,
            market=market,
            side=side,
            quantity=quantity,
            avg_cost=avg_cost,
            last_price=last_price,
            market_value=market_value,
            unrealized_pnl=unrealized_pnl,
            unrealized_pnl_pct=unrealized_pnl_pct,
            portfolio_weight=portfolio_weight,
            leverage=leverage,
            liquidation_price=liquidation_price,
            source=snapshot_source,
            captured_at=as_of,
            payload=payload,
        )
        return position

    def load_portfolio_snapshot(self, portfolio_id: str) -> PortfolioSnapshot:
        """读取组合和当前活跃持仓。"""

        portfolio = self.repository.get_portfolio(portfolio_id)
        positions = tuple(self.repository.list_positions(portfolio_id))
        return PortfolioSnapshot(portfolio=portfolio, positions=positions)

    def list_portfolio_snapshots(
        self,
        *,
        portfolio_id: str,
        limit: int = 20,
    ) -> tuple[PortfolioSnapshotORM, ...]:
        """查询组合历史快照。"""

        return tuple(
            self.repository.list_portfolio_snapshots(
                portfolio_id=portfolio_id,
                limit=limit,
            )
        )

    def list_position_snapshots(
        self,
        *,
        portfolio_id: str,
        asset_id: str | None = None,
        limit: int = 20,
    ) -> tuple[PositionSnapshotORM, ...]:
        """查询持仓历史快照。"""

        return tuple(
            self.repository.list_position_snapshots(
                portfolio_id=portfolio_id,
                asset_id=asset_id,
                limit=limit,
            )
        )


def build_portfolio_snapshot_id(
    *,
    portfolio_id: str,
    captured_at: datetime,
    source: str,
) -> str:
    """生成组合快照 ID。"""

    return f"portfolio_snapshot:{portfolio_id}:{source}:{captured_at:%Y%m%d%H%M%S}"


def build_position_snapshot_id(
    *,
    position_id: str,
    captured_at: datetime,
    source: str,
) -> str:
    """生成持仓快照 ID。"""

    return f"position_snapshot:{position_id}:{source}:{captured_at:%Y%m%d%H%M%S}"
