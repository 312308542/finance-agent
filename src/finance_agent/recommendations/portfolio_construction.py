"""A 股组合风险分配、集中度和换股缓冲。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Literal

PositionAction = Literal["hold", "reduce", "exit"]
OrderSide = Literal["buy", "sell"]


@dataclass(frozen=True)
class PortfolioCandidate:
    """已经通过评分、结构和生命周期门控的候选。"""

    asset_id: str
    setup_id: str
    sector_id: str
    price: Decimal
    invalidation_price: Decimal
    expected_net_return: float
    downside_risk: float
    confidence: float
    tradable: bool = True
    tradability_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class PortfolioPosition:
    """组合构建所需的最小持仓事实。"""

    position_id: str
    asset_id: str
    sector_id: str
    quantity: Decimal
    sellable_quantity: Decimal
    price: Decimal
    expected_net_return: float
    required_action: PositionAction = "hold"
    tradable: bool = True
    tradability_reasons: tuple[str, ...] = ()

    @property
    def market_value(self) -> Decimal:
        return self.quantity * self.price


@dataclass(frozen=True)
class PortfolioRiskBudget:
    """一次组合构建使用的账户与市场风险预算。"""

    equity: Decimal
    total_exposure: float
    per_position_risk: float
    allow_new_buys: bool = True
    max_sector_exposure: float = 0.30
    minimum_positions: int = 6
    maximum_positions: int = 10
    weekly_turnover_ratio: float = 0.0
    maximum_weekly_turnover: float = 0.35
    weak_market_sector_override: bool = False


@dataclass(frozen=True)
class PlannedAction:
    """尚未写入订单系统的组合动作。"""

    asset_id: str
    side: OrderSide
    maximum_quantity: int
    price: Decimal
    notional: Decimal
    risk_amount: Decimal
    sector_id: str
    setup_id: str | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class BlockedCandidate:
    """因组合或交易约束未生成动作的标的。"""

    asset_id: str
    intended_action: OrderSide
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class PortfolioPlan:
    """组合目标、动作和所有阻止原因。"""

    target_exposure: float
    orders: tuple[PlannedAction, ...]
    retained_position_ids: tuple[str, ...]
    blocked_candidates: tuple[BlockedCandidate, ...]
    turnover_ratio: float
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class ReplacementDecision:
    """候选替换现有持仓的净改善判断。"""

    allowed: bool
    expected_improvement: float
    required_improvement: float
    reason_codes: tuple[str, ...]


def should_replace(
    *,
    held_expected_return: float,
    candidate_expected_return: float,
    round_trip_cost: float,
    uncertainty_buffer: float,
) -> ReplacementDecision:
    """只有候选净改善严格覆盖交易成本和不确定性时才换股。"""

    improvement = candidate_expected_return - held_expected_return
    required = round_trip_cost + uncertainty_buffer
    allowed = improvement > required
    return ReplacementDecision(
        allowed=allowed,
        expected_improvement=improvement,
        required_improvement=required,
        reason_codes=("replacement_buffer_met",) if allowed else ("replacement_buffer_not_met",),
    )


class PortfolioConstructionEngine:
    """在不产生副作用的前提下构建组合动作计划。"""

    def allocate(
        self,
        candidates: Sequence[PortfolioCandidate],
        positions: Sequence[PortfolioPosition],
        budget: PortfolioRiskBudget,
    ) -> PortfolioPlan:
        """应用风险预算、集中度、换手和交易限制。"""

        self._validate_budget(budget)
        exposure_multiplier = 0.5 if budget.weak_market_sector_override else 1.0
        target_exposure = max(0.0, min(1.0, budget.total_exposure * exposure_multiplier))
        target_notional = budget.equity * _decimal(target_exposure)
        maximum_sector_notional = budget.equity * _decimal(budget.max_sector_exposure)
        risk_budget = budget.equity * _decimal(budget.per_position_risk)

        orders: list[PlannedAction] = []
        blocked: list[BlockedCandidate] = []
        retained: list[str] = []
        position_assets = {position.asset_id for position in positions}
        sector_notional: dict[str, Decimal] = {}
        current_notional = Decimal("0")
        active_position_count = 0

        for position in positions:
            current_notional += position.market_value
            sector_notional[position.sector_id] = (
                sector_notional.get(position.sector_id, Decimal("0")) + position.market_value
            )
            if position.required_action not in {"reduce", "exit"}:
                retained.append(position.position_id)
                active_position_count += 1
                continue
            sell = self._plan_sell(position)
            if isinstance(sell, BlockedCandidate):
                blocked.append(sell)
                retained.append(position.position_id)
                active_position_count += 1
                continue
            orders.append(sell)
            current_notional -= sell.notional
            sector_notional[position.sector_id] = max(
                Decimal("0"),
                sector_notional[position.sector_id] - sell.notional,
            )
            if sell.maximum_quantity < int(position.quantity):
                retained.append(position.position_id)
                active_position_count += 1

        buys_allowed = (
            budget.allow_new_buys
            and budget.weekly_turnover_ratio < budget.maximum_weekly_turnover
            and target_notional > current_notional
        )
        ordered_candidates = sorted(
            candidates,
            key=lambda item: (item.expected_net_return, item.confidence, item.asset_id),
            reverse=True,
        )
        for candidate in ordered_candidates:
            if candidate.asset_id in position_assets:
                blocked.append(_blocked(candidate.asset_id, "buy", "already_held"))
                continue
            if not candidate.tradable:
                blocked.append(
                    BlockedCandidate(
                        asset_id=candidate.asset_id,
                        intended_action="buy",
                        reason_codes=candidate.tradability_reasons or ("not_tradable",),
                    )
                )
                continue
            if not budget.allow_new_buys:
                blocked.append(_blocked(candidate.asset_id, "buy", "new_buys_disabled"))
                continue
            if budget.weekly_turnover_ratio >= budget.maximum_weekly_turnover:
                blocked.append(_blocked(candidate.asset_id, "buy", "turnover_limit_reached"))
                continue
            if active_position_count >= budget.maximum_positions:
                blocked.append(_blocked(candidate.asset_id, "buy", "maximum_position_count_reached"))
                continue
            if not buys_allowed or current_notional >= target_notional:
                blocked.append(_blocked(candidate.asset_id, "buy", "total_exposure_limit"))
                continue

            planned = self._plan_buy(
                candidate,
                risk_budget=risk_budget,
                available_notional=target_notional - current_notional,
                available_sector_notional=max(
                    Decimal("0"),
                    maximum_sector_notional
                    - sector_notional.get(candidate.sector_id, Decimal("0")),
                ),
            )
            if isinstance(planned, BlockedCandidate):
                blocked.append(planned)
                continue
            orders.append(planned)
            current_notional += planned.notional
            sector_notional[candidate.sector_id] = (
                sector_notional.get(candidate.sector_id, Decimal("0")) + planned.notional
            )
            active_position_count += 1

        planned_turnover = sum((order.notional for order in orders), Decimal("0"))
        turnover_ratio = budget.weekly_turnover_ratio + float(planned_turnover / budget.equity)
        reasons: list[str] = []
        if active_position_count < budget.minimum_positions:
            reasons.append("target_position_count_below_minimum")
        if budget.weak_market_sector_override:
            reasons.append("weak_market_sector_override_reduced")
        if budget.weekly_turnover_ratio >= budget.maximum_weekly_turnover:
            reasons.append("turnover_exit_only")
        return PortfolioPlan(
            target_exposure=target_exposure,
            orders=tuple(orders),
            retained_position_ids=tuple(retained),
            blocked_candidates=tuple(blocked),
            turnover_ratio=turnover_ratio,
            reason_codes=tuple(reasons),
        )

    @staticmethod
    def _plan_buy(
        candidate: PortfolioCandidate,
        *,
        risk_budget: Decimal,
        available_notional: Decimal,
        available_sector_notional: Decimal,
    ) -> PlannedAction | BlockedCandidate:
        risk_per_share = candidate.price - candidate.invalidation_price
        if candidate.price <= 0 or risk_per_share <= 0:
            return _blocked(candidate.asset_id, "buy", "invalid_invalidation_distance")
        risk_quantity = _floor_board_lot(risk_budget / risk_per_share)
        exposure_quantity = _floor_board_lot(available_notional / candidate.price)
        sector_quantity = _floor_board_lot(available_sector_notional / candidate.price)
        quantity = min(risk_quantity, exposure_quantity, sector_quantity)
        if quantity <= 0:
            reason = (
                "sector_concentration_limit"
                if sector_quantity <= 0
                else "total_exposure_limit"
            )
            return _blocked(candidate.asset_id, "buy", reason)

        reasons = ["risk_sized_by_invalidation"]
        if quantity < risk_quantity and quantity == sector_quantity:
            reasons.append("sector_concentration_capped")
        if quantity < risk_quantity and quantity == exposure_quantity:
            reasons.append("total_exposure_capped")
        quantity_decimal = Decimal(quantity)
        return PlannedAction(
            asset_id=candidate.asset_id,
            side="buy",
            maximum_quantity=quantity,
            price=candidate.price,
            notional=candidate.price * quantity_decimal,
            risk_amount=(risk_per_share * quantity_decimal).quantize(Decimal("0.01")),
            sector_id=candidate.sector_id,
            setup_id=candidate.setup_id,
            reason_codes=tuple(reasons),
        )

    @staticmethod
    def _plan_sell(position: PortfolioPosition) -> PlannedAction | BlockedCandidate:
        if not position.tradable:
            return BlockedCandidate(
                asset_id=position.asset_id,
                intended_action="sell",
                reason_codes=position.tradability_reasons or ("not_tradable",),
            )
        if position.sellable_quantity <= 0:
            return _blocked(position.asset_id, "sell", "t1_not_sellable")
        desired = position.quantity if position.required_action == "exit" else position.quantity / 2
        quantity = int(min(desired, position.sellable_quantity).to_integral_value(rounding=ROUND_DOWN))
        if quantity <= 0:
            return _blocked(position.asset_id, "sell", "sellable_quantity_below_one_share")
        quantity_decimal = Decimal(quantity)
        return PlannedAction(
            asset_id=position.asset_id,
            side="sell",
            maximum_quantity=quantity,
            price=position.price,
            notional=position.price * quantity_decimal,
            risk_amount=Decimal("0.00"),
            sector_id=position.sector_id,
            setup_id=None,
            reason_codes=(f"position_{position.required_action}",),
        )

    @staticmethod
    def _validate_budget(budget: PortfolioRiskBudget) -> None:
        if budget.equity <= 0:
            raise ValueError("账户权益必须大于零。")
        if not 0 <= budget.total_exposure <= 1:
            raise ValueError("总敞口预算必须位于 0 到 1 之间。")
        if not 0 <= budget.per_position_risk <= 1:
            raise ValueError("单票风险预算必须位于 0 到 1 之间。")
        if not 0 <= budget.max_sector_exposure <= 1:
            raise ValueError("板块敞口上限必须位于 0 到 1 之间。")
        if budget.minimum_positions < 0 or budget.maximum_positions < budget.minimum_positions:
            raise ValueError("目标持仓数量边界无效。")


def _blocked(asset_id: str, side: OrderSide, reason: str) -> BlockedCandidate:
    return BlockedCandidate(asset_id=asset_id, intended_action=side, reason_codes=(reason,))


def _floor_board_lot(quantity: Decimal) -> int:
    shares = int(quantity.to_integral_value(rounding=ROUND_DOWN))
    return max(0, shares // 100 * 100)


def _decimal(value: float) -> Decimal:
    return Decimal(str(value))
