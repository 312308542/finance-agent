"""把多种结构帧压缩为买入方向、确认、风险收益和失效裁决。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

JsonDict = dict[str, Any]
PRIMARY_STRUCTURE_HORIZONS = frozenset(
    {"structural_swings_v2", "smc_lite_v2", "ichimoku_v1"}
)
AUXILIARY_STRUCTURE_HORIZONS = frozenset({"harmonic_lite_v2", "elliott_lite_v2"})


@dataclass(frozen=True)
class StructureVerdict:
    """单股结构买入门槛的确定性结果。"""

    status: Literal["confirmed", "waiting", "blocked", "invalidated"]
    direction: Literal["bullish", "range", "bearish", "unknown"]
    buy_allowed: bool
    entry_zone: tuple[Decimal, Decimal] | None
    invalidation_price: Decimal | None
    target_price: Decimal | None
    reward_risk_ratio: float | None
    primary_evidence_ids: tuple[str, ...]
    auxiliary_evidence_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]

    def to_dict(self) -> JsonDict:
        """转换为推荐 payload 可保存的结构。"""

        return {
            "status": self.status,
            "direction": self.direction,
            "buy_allowed": self.buy_allowed,
            "entry_zone": (
                {"low": str(self.entry_zone[0]), "high": str(self.entry_zone[1])}
                if self.entry_zone
                else None
            ),
            "invalidation_price": (
                str(self.invalidation_price) if self.invalidation_price is not None else None
            ),
            "target_price": str(self.target_price) if self.target_price is not None else None,
            "reward_risk_ratio": self.reward_risk_ratio,
            "primary_evidence_ids": list(self.primary_evidence_ids),
            "auxiliary_evidence_ids": list(self.auxiliary_evidence_ids),
            "reason_codes": list(self.reason_codes),
        }


class StructuralDecisionEngine:
    """按主结构优先级执行无副作用的买入结构裁决。"""

    def evaluate(
        self,
        *,
        frames: Sequence[Any],
        current_price: Decimal | None,
    ) -> StructureVerdict:
        """主结构缺失或未确认时 fail-closed。"""

        normalized = tuple(_frame_mapping(frame) for frame in frames)
        available = tuple(frame for frame in normalized if _available(frame))
        primary = tuple(
            frame
            for frame in available
            if str(frame.get("horizon") or "") in PRIMARY_STRUCTURE_HORIZONS
        )
        auxiliary = tuple(
            frame
            for frame in available
            if str(frame.get("horizon") or "") in AUXILIARY_STRUCTURE_HORIZONS
        )
        primary_evidence = _evidence_ids(primary)
        auxiliary_evidence = _evidence_ids(auxiliary)
        if not primary:
            return _verdict(
                status="blocked",
                direction="unknown",
                primary_evidence=primary_evidence,
                auxiliary_evidence=auxiliary_evidence,
                reasons=("primary_structure_missing",),
            )

        directions = tuple(
            direction
            for frame in primary
            if (direction := _frame_direction(frame)) != "unknown"
        )
        distinct_directions = set(directions)
        if "bullish" in distinct_directions and "bearish" in distinct_directions:
            return _verdict(
                status="waiting",
                direction="unknown",
                primary_evidence=primary_evidence,
                auxiliary_evidence=auxiliary_evidence,
                reasons=("primary_direction_conflict",),
            )
        direction = directions[-1] if directions else "unknown"
        levels = _risk_levels(primary)
        entry_zone, invalidation_price, target_price = levels
        if (
            current_price is not None
            and invalidation_price is not None
            and current_price <= invalidation_price
        ):
            return _verdict(
                status="invalidated",
                direction=direction,
                entry_zone=entry_zone,
                invalidation_price=invalidation_price,
                target_price=target_price,
                primary_evidence=primary_evidence,
                auxiliary_evidence=auxiliary_evidence,
                reasons=("invalidation_price_breached",),
            )
        if direction != "bullish":
            return _verdict(
                status="blocked",
                direction=direction,
                entry_zone=entry_zone,
                invalidation_price=invalidation_price,
                target_price=target_price,
                primary_evidence=primary_evidence,
                auxiliary_evidence=auxiliary_evidence,
                reasons=("primary_direction_not_bullish",),
            )
        if not any(_entry_confirmed(frame) for frame in primary):
            return _verdict(
                status="waiting",
                direction=direction,
                entry_zone=entry_zone,
                invalidation_price=invalidation_price,
                target_price=target_price,
                primary_evidence=primary_evidence,
                auxiliary_evidence=auxiliary_evidence,
                reasons=("entry_confirmation_missing",),
            )
        reward_risk = _reward_risk(
            current_price=current_price,
            invalidation_price=invalidation_price,
            target_price=target_price,
        )
        if reward_risk is None:
            return _verdict(
                status="waiting",
                direction=direction,
                entry_zone=entry_zone,
                invalidation_price=invalidation_price,
                target_price=target_price,
                primary_evidence=primary_evidence,
                auxiliary_evidence=auxiliary_evidence,
                reasons=("risk_levels_missing",),
            )
        if reward_risk < 2:
            return _verdict(
                status="blocked",
                direction=direction,
                entry_zone=entry_zone,
                invalidation_price=invalidation_price,
                target_price=target_price,
                reward_risk=reward_risk,
                primary_evidence=primary_evidence,
                auxiliary_evidence=auxiliary_evidence,
                reasons=("reward_risk_below_two",),
            )
        return _verdict(
            status="confirmed",
            direction=direction,
            entry_zone=entry_zone,
            invalidation_price=invalidation_price,
            target_price=target_price,
            reward_risk=reward_risk,
            primary_evidence=primary_evidence,
            auxiliary_evidence=auxiliary_evidence,
            reasons=("primary_structure_confirmed",),
        )


def _verdict(
    *,
    status: Literal["confirmed", "waiting", "blocked", "invalidated"],
    direction: str,
    primary_evidence: tuple[str, ...],
    auxiliary_evidence: tuple[str, ...],
    reasons: tuple[str, ...],
    entry_zone: tuple[Decimal, Decimal] | None = None,
    invalidation_price: Decimal | None = None,
    target_price: Decimal | None = None,
    reward_risk: float | None = None,
) -> StructureVerdict:
    return StructureVerdict(
        status=status,
        direction=direction,  # type: ignore[arg-type]
        buy_allowed=status == "confirmed",
        entry_zone=entry_zone,
        invalidation_price=invalidation_price,
        target_price=target_price,
        reward_risk_ratio=reward_risk,
        primary_evidence_ids=primary_evidence,
        auxiliary_evidence_ids=auxiliary_evidence,
        reason_codes=reasons,
    )


def _frame_mapping(frame: Any) -> JsonDict:
    if isinstance(frame, Mapping):
        return dict(frame)
    payload = dict(getattr(frame, "payload", {}) or {})
    for field in ("horizon", "timeframe", "status", "confidence", "evidence_id", "as_of"):
        value = getattr(frame, field, None)
        if value is not None:
            payload[field] = value
    return payload


def _available(frame: JsonDict) -> bool:
    return str(frame.get("status") or "") in {"available", "confirmed", "revised"}


def _frame_direction(frame: JsonDict) -> str:
    direct = _normalize_direction(frame.get("direction"))
    if direct != "unknown":
        return direct
    candidates = frame.get("items") or frame.get("signals") or frame.get("structure_events")
    if isinstance(candidates, list):
        for item in reversed(candidates):
            if isinstance(item, Mapping):
                direction = _normalize_direction(item.get("direction"))
                if direction != "unknown":
                    return direction
    segments = frame.get("segments")
    if isinstance(segments, list) and segments and isinstance(segments[-1], Mapping):
        return _normalize_direction(segments[-1].get("direction"))
    return "unknown"


def _normalize_direction(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"bullish", "up", "long", "above_cloud"}:
        return "bullish"
    if normalized in {"bearish", "down", "short", "below_cloud"}:
        return "bearish"
    if normalized in {"range", "neutral", "sideways", "inside_cloud"}:
        return "range"
    return "unknown"


def _entry_confirmed(frame: JsonDict) -> bool:
    timeframe = str(frame.get("timeframe") or "").lower()
    if timeframe not in {"60m", "1h", "hourly"}:
        return False
    setup = str(frame.get("setup") or frame.get("entry_setup") or "").lower()
    return setup in {"retest_holds", "breakout_confirmed", "pullback_holds"}


def _risk_levels(
    frames: Sequence[JsonDict],
) -> tuple[tuple[Decimal, Decimal] | None, Decimal | None, Decimal | None]:
    entry_zone = None
    invalidation = None
    target = None
    for frame in frames:
        if entry_zone is None and isinstance(frame.get("entry_zone"), Mapping):
            low = _decimal(frame["entry_zone"].get("low"))
            high = _decimal(frame["entry_zone"].get("high"))
            if low is not None and high is not None:
                entry_zone = (min(low, high), max(low, high))
        invalidation = invalidation or _decimal(frame.get("invalidation_price"))
        target = target or _decimal(frame.get("target_price"))
    return entry_zone, invalidation, target


def _reward_risk(
    *,
    current_price: Decimal | None,
    invalidation_price: Decimal | None,
    target_price: Decimal | None,
) -> float | None:
    if current_price is None or invalidation_price is None or target_price is None:
        return None
    risk = current_price - invalidation_price
    if risk <= 0:
        return None
    return round(float((target_price - current_price) / risk), 6)


def _decimal(value: Any) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _evidence_ids(frames: Sequence[JsonDict]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(frame.get("evidence_id") or "").strip()
            for frame in frames
            if str(frame.get("evidence_id") or "").strip()
        )
    )
