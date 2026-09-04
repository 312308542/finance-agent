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
        levels = _risk_levels(primary, current_price=current_price)
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
        payload = dict(frame)
        payload.setdefault("horizon", payload.get("schema_version"))
        return payload
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
    if str(frame.get("horizon") or "") == "structural_swings_v2":
        swings = _mapping_rows(frame.get("swings"))
        highs = [row for row in swings if str(row.get("type")) == "H"]
        lows = [row for row in swings if str(row.get("type")) == "L"]
        if len(highs) >= 2 and len(lows) >= 2:
            last_high = _decimal(highs[-1].get("price"))
            previous_high = _decimal(highs[-2].get("price"))
            last_low = _decimal(lows[-1].get("price"))
            previous_low = _decimal(lows[-2].get("price"))
            if None not in {last_high, previous_high, last_low, previous_low}:
                if last_high > previous_high and last_low > previous_low:
                    return "bullish"
                if last_high < previous_high and last_low < previous_low:
                    return "bearish"
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
    if setup in {"retest_holds", "breakout_confirmed", "pullback_holds"}:
        return True
    if str(frame.get("horizon") or "") != "smc_lite_v2":
        return False
    events = _mapping_rows(frame.get("structure_events"))
    if not events:
        return False
    latest = events[-1]
    name = str(latest.get("name") or "")
    direction = _normalize_direction(latest.get("direction"))
    confidence = float(latest.get("confidence") or 0)
    break_pct = abs(float(latest.get("break_pct") or 0))
    confirmed_at = str(latest.get("confirmed_at") or latest.get("timestamp") or "")
    input_end_at = str(frame.get("input_end_at") or "")
    latest_bar = frame.get("latest_bar")
    continuation_holds = False
    if isinstance(latest_bar, Mapping):
        latest_close = _decimal(latest_bar.get("close"))
        latest_low = _decimal(latest_bar.get("low"))
        break_level = _decimal(latest.get("break_level"))
        confirmed_at_bar = int(latest.get("confirmed_at_bar") or -1)
        bar_count = int(frame.get("bar_count") or 0)
        bars_since_break = bar_count - 1 - confirmed_at_bar
        continuation_holds = (
            latest_close is not None
            and latest_low is not None
            and break_level is not None
            and 1 <= bars_since_break <= 3
            and latest_close >= break_level
            and latest_low >= break_level * Decimal("0.99")
        )
    return (
        name in {"bos_bullish", "choch_bullish"}
        and direction == "bullish"
        and confidence >= 0.55
        and break_pct <= 0.03
        and bool(confirmed_at)
        and (confirmed_at == input_end_at or continuation_holds)
    )


def _risk_levels(
    frames: Sequence[JsonDict],
    *,
    current_price: Decimal | None,
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
    if entry_zone is None:
        entry_zone = _smc_entry_zone(frames)
    if invalidation is None:
        invalidation = _swing_invalidation(frames, current_price=current_price)
    if invalidation is None:
        invalidation = _ichimoku_invalidation(frames, current_price=current_price)
    if target is None:
        target = _measured_move_target(
            frames,
            current_price=current_price,
            invalidation_price=invalidation,
        )
    return entry_zone, invalidation, target


def _smc_entry_zone(frames: Sequence[JsonDict]) -> tuple[Decimal, Decimal] | None:
    for frame in frames:
        if str(frame.get("horizon") or "") != "smc_lite_v2":
            continue
        if str(frame.get("timeframe") or "").lower() not in {"60m", "1h", "hourly"}:
            continue
        events = _mapping_rows(frame.get("structure_events"))
        if not events:
            continue
        latest = events[-1]
        if not _entry_confirmed(frame):
            continue
        break_level = _decimal(latest.get("break_level"))
        close = _decimal(latest.get("close"))
        if break_level is None or close is None:
            continue
        upper = min(close, break_level * Decimal("1.01"))
        return min(break_level, upper), max(break_level, upper)
    return None


def _swing_invalidation(
    frames: Sequence[JsonDict],
    *,
    current_price: Decimal | None,
) -> Decimal | None:
    if current_price is None:
        return None
    lows: list[tuple[str, Decimal]] = []
    for frame in frames:
        if str(frame.get("horizon") or "") != "structural_swings_v2":
            continue
        if str(frame.get("timeframe") or "").lower() != "1d":
            continue
        for swing in _mapping_rows(frame.get("swings")):
            price = _decimal(swing.get("price"))
            if str(swing.get("type")) == "L" and price is not None and price < current_price:
                lows.append((str(swing.get("confirmed_at") or ""), price))
    return max(lows, key=lambda item: item[0])[1] if lows else None


def _ichimoku_invalidation(
    frames: Sequence[JsonDict],
    *,
    current_price: Decimal | None,
) -> Decimal | None:
    if current_price is None:
        return None
    for frame in frames:
        if str(frame.get("horizon") or "") != "ichimoku_v1":
            continue
        lines = frame.get("lines")
        kijun = _decimal(lines.get("kijun_sen")) if isinstance(lines, Mapping) else None
        if kijun is not None and kijun < current_price:
            return kijun
    return None


def _measured_move_target(
    frames: Sequence[JsonDict],
    *,
    current_price: Decimal | None,
    invalidation_price: Decimal | None,
) -> Decimal | None:
    if current_price is None or invalidation_price is None:
        return None
    amplitudes = [
        amplitude
        for frame in frames
        if str(frame.get("horizon") or "") == "structural_swings_v2"
        and str(frame.get("timeframe") or "").lower() == "1d"
        for segment in _mapping_rows(frame.get("segments"))
        if _normalize_direction(segment.get("direction")) == "bullish"
        and (amplitude := _decimal(segment.get("amplitude"))) is not None
    ]
    if not amplitudes:
        return None
    target = current_price + max(amplitudes[-3:])
    minimum_target = current_price + (current_price - invalidation_price) * 2
    return target if target >= minimum_target else None


def _mapping_rows(value: Any) -> list[JsonDict]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


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
