"""统一快照驱动的自适应推荐决策编排。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from finance_agent.recommendations.decision_snapshot import DecisionSnapshot
from finance_agent.recommendations.lifecycle import (
    LifecycleEvidence,
    RecommendationLifecycleEngine,
    RecommendationState,
    RecommendationTransition,
)
from finance_agent.recommendations.portfolio_construction import (
    BlockedCandidate,
    PortfolioCandidate,
    PortfolioConstructionEngine,
    PortfolioPlan,
    PortfolioPosition,
    PortfolioRiskBudget,
)
from finance_agent.recommendations.structural_decision import (
    StructuralDecisionEngine,
    StructureVerdict,
)
from finance_agent.scoring.adaptive import AdaptiveAlphaEngine, AdaptiveAssetInput, AlphaEstimate

JsonDict = dict[str, Any]
RecommendationAction = Literal["buy_candidate", "hold", "watch", "reduce", "exit"]


@dataclass(frozen=True)
class AdaptiveAssetDecision:
    """单个资产在同一快照中的最终研究动作。"""

    asset_id: str
    symbol: str
    decision_snapshot_id: str
    action: RecommendationAction
    intended_action: RecommendationAction | None
    execution_status: Literal["not_applicable", "planned", "blocked"]
    transition: RecommendationTransition
    alpha: AlphaEstimate
    structure: StructureVerdict
    reason_codes: tuple[str, ...]
    data_quality: str
    payload: JsonDict


@dataclass(frozen=True)
class AdaptiveRecommendationDecisionResult:
    """一次快照内的全部资产决策和组合计划。"""

    status: Literal["available", "unavailable"]
    decision_snapshot_id: str
    decisions: tuple[AdaptiveAssetDecision, ...]
    portfolio_plan: PortfolioPlan
    buy_ready_count: int
    active_count: int
    exit_pending_count: int


class AdaptiveRecommendationDecisionEngine:
    """隐藏 Alpha、结构、生命周期和组合约束的执行顺序。"""

    def __init__(
        self,
        *,
        alpha_engine: AdaptiveAlphaEngine | None = None,
        structure_engine: StructuralDecisionEngine | None = None,
        lifecycle_engine: RecommendationLifecycleEngine | None = None,
        portfolio_engine: PortfolioConstructionEngine | None = None,
    ) -> None:
        self.alpha_engine = alpha_engine or AdaptiveAlphaEngine()
        self.structure_engine = structure_engine or StructuralDecisionEngine()
        self.lifecycle_engine = lifecycle_engine or RecommendationLifecycleEngine()
        self.portfolio_engine = portfolio_engine or PortfolioConstructionEngine()

    def decide(
        self,
        snapshot: DecisionSnapshot,
        *,
        previous_states: Mapping[str, RecommendationState],
        positions: Sequence[PortfolioPosition],
        budget: PortfolioRiskBudget,
        closed_position_events: Mapping[str, datetime] | None = None,
        owner_id: str = "default-owner",
        strategy_id: str = "strategy:ashare:adaptive_v1",
    ) -> AdaptiveRecommendationDecisionResult:
        """从冻结快照一次性产生可持久化决策。"""

        assets = tuple(dict(item) for item in snapshot.assets)
        alpha_inputs = tuple(self._alpha_input(snapshot, item) for item in assets)
        market_regime = str(snapshot.market_regime.get("regime") or "range")
        alpha_results = self.alpha_engine.score(alpha_inputs, market_regime=market_regime)
        market_allows_buys = (
            snapshot.quality_status == "available"
            and _market_allows_buys(snapshot.market_regime)
        )
        position_by_asset = {position.asset_id: position for position in positions}
        closed_events = dict(closed_position_events or {})

        interim: list[AdaptiveAssetDecision] = []
        candidates: list[PortfolioCandidate] = []
        candidate_input_blocks: dict[str, tuple[str, ...]] = {}
        for asset, alpha in zip(assets, alpha_results, strict=True):
            asset_id = str(asset.get("asset_id") or "").strip()
            if not asset_id:
                raise ValueError("决策快照中的 asset_id 不能为空。")
            symbol = str(asset.get("symbol") or asset_id)
            frames = _mapping_sequence(asset.get("structure_frames"))
            current_price = _decimal(asset.get("current_price"))
            structure = self.structure_engine.evaluate(
                frames=frames,
                current_price=current_price,
            )
            data_quality = str(asset.get("data_quality") or "unavailable")
            tradable = bool(asset.get("tradable", False))
            failure_reasons: list[str] = []
            if not alpha.eligible_for_buy:
                failure_reasons.extend(alpha.reason_codes)
            if not structure.buy_allowed:
                failure_reasons.extend(structure.reason_codes)
            if data_quality != "available":
                failure_reasons.append(f"data_quality_{data_quality}")
            if not tradable:
                failure_reasons.extend(_string_tuple(asset.get("tradability_reasons")))
                if not _string_tuple(asset.get("tradability_reasons")):
                    failure_reasons.append("not_tradable")
            if not market_allows_buys:
                failure_reasons.append("market_budget_blocks_new_buys")
            sector_gate_reason = sector_override_reason(
                market_regime=market_regime,
                asset=asset,
                sector_opportunities=snapshot.sector_opportunities,
            )
            if sector_gate_reason is not None:
                failure_reasons.append(sector_gate_reason)

            setup_id = str(
                asset.get("setup_id")
                or _setup_id(
                    strategy_id,
                    asset_id,
                    frames=frames,
                    structure=structure,
                )
            )
            previous = previous_states.get(asset_id)
            transition = self.lifecycle_engine.transition(
                previous,
                LifecycleEvidence(
                    owner_id=owner_id,
                    strategy_id=strategy_id,
                    asset_id=asset_id,
                    setup_id=setup_id,
                    decision_snapshot_id=snapshot.decision_snapshot_id,
                    as_of=snapshot.as_of,
                    trade_date=_date(asset.get("trade_date")) or snapshot.as_of.date(),
                    eligible=not failure_reasons,
                    alpha_score=alpha.alpha_score,
                    entry_threshold=_float(asset.get("entry_threshold"), default=70.0),
                    retention_threshold=_float(
                        asset.get("retention_threshold"),
                        default=58.0,
                    ),
                    structure_invalidated=(
                        bool(asset.get("structure_invalidated"))
                        or structure.status == "invalidated"
                    ),
                    high_quality_intraday_breakout=bool(
                        asset.get("high_quality_intraday_breakout")
                    ),
                    ordinary_volatility=bool(asset.get("ordinary_volatility")),
                    held=asset_id in position_by_asset,
                    data_stale=(
                        data_quality != "available" and previous is not None
                    ),
                    sold=bool(asset.get("sold")) or asset_id in closed_events,
                    cooldown_until=(
                        _date(asset.get("cooldown_until"))
                        or _add_business_days(closed_events[asset_id].date(), 3)
                        if asset_id in closed_events
                        else _date(asset.get("cooldown_until"))
                    ),
                    new_independent_catalyst=bool(
                        asset.get("new_independent_catalyst")
                    ),
                    new_structure_setup=bool(asset.get("new_structure_setup")),
                    reason_codes=tuple(dict.fromkeys(failure_reasons)),
                    payload={
                        "alpha": _alpha_payload(alpha),
                        "structure_verdict": structure.to_dict(),
                        "data_quality": data_quality,
                        "decision_asset": dict(asset),
                        **(
                            {
                                "closed_position_as_of": closed_events[
                                    asset_id
                                ].isoformat()
                            }
                            if asset_id in closed_events
                            else {}
                        ),
                    },
                ),
            )
            decision = AdaptiveAssetDecision(
                asset_id=asset_id,
                symbol=symbol,
                decision_snapshot_id=snapshot.decision_snapshot_id,
                action=_state_action(transition.to_state),
                intended_action=None,
                execution_status="not_applicable",
                transition=transition,
                alpha=alpha,
                structure=structure,
                reason_codes=transition.reason_codes,
                data_quality=data_quality,
                payload=dict(asset),
            )
            interim.append(decision)
            if transition.to_state != "buy_ready":
                continue
            if failure_reasons:
                candidate_input_blocks[asset_id] = tuple(
                    dict.fromkeys(failure_reasons)
                )
                continue
            if current_price is None or structure.invalidation_price is None:
                candidate_input_blocks[asset_id] = ("portfolio_risk_levels_missing",)
                continue
            candidates.append(
                PortfolioCandidate(
                    asset_id=asset_id,
                    setup_id=setup_id,
                    sector_id=str(asset.get("sector_id") or "sector:unknown"),
                    price=current_price,
                    invalidation_price=structure.invalidation_price,
                    expected_net_return=alpha.expected_net_return,
                    downside_risk=alpha.downside_risk,
                    confidence=alpha.confidence,
                    tradable=tradable,
                    tradability_reasons=_string_tuple(asset.get("tradability_reasons")),
                )
            )

        effective_budget = replace(
            budget,
            allow_new_buys=budget.allow_new_buys and market_allows_buys,
        )
        intended_exits = {
            item.asset_id
            for item in interim
            if item.transition.to_state == "exit_pending"
        }
        effective_positions = tuple(
            replace(position, required_action="exit")
            if position.asset_id in intended_exits
            else position
            for position in positions
        )
        plan = self.portfolio_engine.allocate(
            candidates,
            effective_positions,
            effective_budget,
        )
        accepted_buys = {
            order.asset_id for order in plan.orders if order.side == "buy"
        }
        accepted_sells = {
            order.asset_id for order in plan.orders if order.side == "sell"
        }
        portfolio_blocks = _blocks_by_asset(plan.blocked_candidates)
        decisions: list[AdaptiveAssetDecision] = []
        for decision in interim:
            if (
                decision.transition.to_state == "exit_pending"
                and decision.asset_id in position_by_asset
            ):
                if decision.asset_id in accepted_sells:
                    decisions.append(
                        replace(
                            decision,
                            action="exit",
                            intended_action="exit",
                            execution_status="planned",
                        )
                    )
                    continue
                exit_blocks = portfolio_blocks.get(decision.asset_id, ())
                decisions.append(
                    replace(
                        decision,
                        action="watch",
                        intended_action="exit",
                        execution_status="blocked",
                        reason_codes=tuple(
                            dict.fromkeys((*decision.reason_codes, *exit_blocks))
                        ),
                    )
                )
                continue
            if decision.transition.to_state != "buy_ready":
                decisions.append(decision)
                continue
            if decision.asset_id in accepted_buys:
                decisions.append(
                    replace(
                        decision,
                        action="buy_candidate",
                        intended_action="buy_candidate",
                        execution_status="planned",
                    )
                )
                continue
            block_reasons = (
                *candidate_input_blocks.get(decision.asset_id, ()),
                *portfolio_blocks.get(decision.asset_id, ()),
            )
            decisions.append(
                replace(
                    decision,
                    action="watch",
                    intended_action="buy_candidate",
                    execution_status="blocked",
                    reason_codes=tuple(
                        dict.fromkeys((*decision.reason_codes, *block_reasons))
                    ),
                )
            )

        return AdaptiveRecommendationDecisionResult(
            status="available" if decisions else "unavailable",
            decision_snapshot_id=snapshot.decision_snapshot_id,
            decisions=tuple(decisions),
            portfolio_plan=plan,
            buy_ready_count=sum(item.action == "buy_candidate" for item in decisions),
            active_count=sum(item.transition.to_state == "active" for item in decisions),
            exit_pending_count=sum(
                item.transition.to_state == "exit_pending" for item in decisions
            ),
        )

    @staticmethod
    def _alpha_input(
        snapshot: DecisionSnapshot,
        asset: Mapping[str, Any],
    ) -> AdaptiveAssetInput:
        factor_as_of_raw = asset.get("factor_as_of")
        factor_as_of = {
            str(key): parsed
            for key, value in (
                factor_as_of_raw.items() if isinstance(factor_as_of_raw, Mapping) else ()
            )
            if (parsed := _datetime(value)) is not None
        }
        return AdaptiveAssetInput(
            asset_id=str(asset.get("asset_id") or ""),
            as_of=_datetime(asset.get("as_of")) or snapshot.as_of,
            group_scores=(
                dict(asset["group_scores"])
                if isinstance(asset.get("group_scores"), Mapping)
                else {}
            ),
            factor_as_of=factor_as_of,
            data_quality=str(asset.get("data_quality") or "unavailable"),
            missing_groups=_string_tuple(asset.get("missing_groups")),
            partial_groups=_string_tuple(asset.get("partial_groups")),
            expected_return_hint=_optional_float(asset.get("expected_return_hint")),
            downside_risk=_float(asset.get("downside_risk"), default=0.0),
        )


def _state_action(state: str) -> RecommendationAction:
    return {
        "buy_ready": "watch",
        "active": "hold",
        "weakening": "watch",
        "exit_pending": "exit",
    }.get(state, "watch")  # type: ignore[return-value]


def _market_allows_buys(market_regime: Mapping[str, Any]) -> bool:
    budget = market_regime.get("risk_budget")
    if isinstance(budget, Mapping) and "allow_new_buys" in budget:
        return bool(budget["allow_new_buys"])
    return str(market_regime.get("regime") or "") != "risk_off"


def sector_override_reason(
    *,
    market_regime: str,
    asset: Mapping[str, Any],
    sector_opportunities: Sequence[Mapping[str, Any]],
) -> str | None:
    """弱市只允许健康热门板块中的龙头或挑战者覆盖大盘限制。"""

    if market_regime != "trend_down":
        return None
    sector_id = str(asset.get("sector_id") or "")
    asset_id = str(asset.get("asset_id") or "")
    opportunity = next(
        (
            item
            for item in sector_opportunities
            if str(item.get("sector_id") or "") == sector_id
        ),
        None,
    )
    if opportunity is None:
        return "sector_override_not_eligible"
    regime = str(opportunity.get("sector_regime") or opportunity.get("regime") or "")
    if regime not in {"ignition", "diffusion"}:
        return "sector_override_not_eligible"
    if not bool(opportunity.get("override_eligible", False)):
        return "sector_override_not_eligible"
    if bool(opportunity.get("chase_risk", False)):
        return "sector_chase_risk"
    members = {
        str(member)
        for key in ("leader_asset_ids", "challenger_asset_ids")
        for member in (opportunity.get(key) or ())
    }
    if asset_id not in members:
        return "sector_member_not_leader_or_challenger"
    return None


def _alpha_payload(alpha: AlphaEstimate) -> JsonDict:
    return {
        "alpha_score": alpha.alpha_score,
        "expected_net_return": alpha.expected_net_return,
        "downside_risk": alpha.downside_risk,
        "confidence": alpha.confidence,
        "eligible_for_buy": alpha.eligible_for_buy,
        "reason_codes": list(alpha.reason_codes),
        "contributions": list(alpha.contributions),
    }


def _blocks_by_asset(
    blocks: Sequence[BlockedCandidate],
) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for block in blocks:
        result[block.asset_id] = tuple(
            dict.fromkeys((*result.get(block.asset_id, ()), *block.reason_codes))
        )
    return result


def _mapping_sequence(value: Any) -> tuple[JsonDict, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, Mapping))


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def _decimal(value: Any) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any, *, default: float) -> float:
    parsed = _optional_float(value)
    return parsed if parsed is not None else default


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _add_business_days(value: date, days: int) -> date:
    """按工作日推进冷却期；交易日历将在持仓阶段替换该保守适配。"""

    current = value
    remaining = max(0, int(days))
    while remaining:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def _setup_id(
    strategy_id: str,
    asset_id: str,
    *,
    frames: Sequence[JsonDict],
    structure: StructureVerdict,
) -> str:
    """用稳定结构锚点生成设置 ID，忽略每日快照和 evidence ID。"""

    identity = {
        "strategy_id": strategy_id,
        "asset_id": asset_id,
        "direction": structure.direction,
        "entry_zone": (
            [str(structure.entry_zone[0]), str(structure.entry_zone[1])]
            if structure.entry_zone is not None
            else None
        ),
        "invalidation_price": (
            str(structure.invalidation_price)
            if structure.invalidation_price is not None
            else None
        ),
        "primary_structures": sorted(
            {
                f"{frame.get('horizon')}:{frame.get('timeframe')}"
                for frame in frames
                if str(frame.get("horizon") or "")
                in {"structural_swings_v2", "smc_lite_v2", "ichimoku_v1"}
            }
        ),
    }
    raw = json.dumps(identity, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode()).hexdigest()[:32]
    return f"setup:{digest}"
