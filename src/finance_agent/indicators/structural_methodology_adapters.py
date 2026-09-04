"""结构类方法论的轻量确定性引擎。

本模块只基于已入库 OHLCV K 线计算结构证据，供方法论 skill 解读。
LLM 不参与 swing、谐波、SMC 或波浪结构计算。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

JsonDict = dict[str, Any]

ENGINE_NAME = "finance-agent-structural-lite"
ENGINE_VERSION = "2026.07.04"


@dataclass(frozen=True)
class StructuralPriceBar:
    """结构方法论使用的 OHLCV K 线输入。"""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class SwingPoint:
    """交替高低点。"""

    timestamp: datetime
    bar_index: int
    type: str
    price: float
    confirmed_bar_index: int
    confirmed_at: datetime


class StructuralMethodologyAdapter:
    """A 股优先的结构方法论轻量适配器。"""

    def __init__(
        self,
        *,
        swing_window: int = 10,
        harmonic_tolerance: float = 0.12,
        harmonic_max_bars_since_d: int = 10,
        fvg_min_atr_ratio: float = 0.3,
        fvg_include_mitigated: bool = False,
        elliott_confidence_threshold: float = 0.6,
        min_bars_per_wave: int = 3,
    ) -> None:
        if swing_window < 1:
            raise ValueError("swing_window 必须大于等于 1。")
        self.swing_window = swing_window
        self.harmonic_tolerance = harmonic_tolerance
        self.harmonic_max_bars_since_d = harmonic_max_bars_since_d
        self.fvg_min_atr_ratio = fvg_min_atr_ratio
        self.fvg_include_mitigated = fvg_include_mitigated
        self.elliott_confidence_threshold = elliott_confidence_threshold
        self.min_bars_per_wave = min_bars_per_wave

    def compute_swings(
        self,
        *,
        asset_id: str,
        symbol: str,
        market: str,
        timeframe: str,
        bars: list[StructuralPriceBar],
    ) -> JsonDict:
        """计算交替 swing 结构。"""

        normalized = normalize_bars(bars)
        if len(normalized) < self.minimum_bar_count:
            input_start_at, input_end_at = input_bounds(normalized)
            return {
                **base_payload(
                    schema_version="structural_swings_v2",
                    status="insufficient_data",
                    asset_id=asset_id,
                    symbol=symbol,
                    market=market,
                    timeframe=timeframe,
                    bars=normalized,
                    raw_bar_count=len(bars),
                    evidence_prefix="structural_swings",
                ),
                "input_start_at": input_start_at.isoformat(),
                "input_end_at": input_end_at.isoformat(),
                "swings": [],
                "segments": [],
                "confidence": 0.0,
                "caveats": [
                    f"结构方法论至少需要 {self.minimum_bar_count} 根 K 线，当前仅 {len(normalized)} 根。",
                ],
                "red_lines": red_lines("Swing 结构"),
            }
        swings = detect_swings(normalized, window=self.swing_window)
        input_start_at, input_end_at = input_bounds(normalized)
        status = "available" if swings else "insufficient_structure"
        return {
            **base_payload(
                schema_version="structural_swings_v2",
                status=status,
                asset_id=asset_id,
                symbol=symbol,
                market=market,
                timeframe=timeframe,
                bars=normalized,
                raw_bar_count=len(bars),
                evidence_prefix="structural_swings",
            ),
            "input_start_at": input_start_at.isoformat(),
            "input_end_at": input_end_at.isoformat(),
            "swings": [serialize_swing(point) for point in swings],
            "segments": build_segments(swings),
            "confidence": swing_structure_confidence(swings, bar_count=len(normalized)),
            "caveats": [
                "Swing 点来自固定窗口局部极值，窗口过小会更敏感，窗口过大会漏掉短期结构。",
            ],
            "red_lines": red_lines("Swing 结构"),
        }

    def compute_harmonic(
        self,
        *,
        asset_id: str,
        symbol: str,
        market: str,
        timeframe: str,
        bars: list[StructuralPriceBar],
    ) -> JsonDict:
        """基于 XABCD 五点识别轻量谐波候选。"""

        normalized = normalize_bars(bars)
        if len(normalized) < self.minimum_bar_count:
            input_start_at, input_end_at = input_bounds(normalized)
            return {
                **base_payload(
                    schema_version="harmonic_lite_v2",
                    status="insufficient_data",
                    asset_id=asset_id,
                    symbol=symbol,
                    market=market,
                    timeframe=timeframe,
                    bars=normalized,
                    raw_bar_count=len(bars),
                    evidence_prefix="harmonic_lite",
                ),
                "input_start_at": input_start_at.isoformat(),
                "input_end_at": input_end_at.isoformat(),
                "swing_count": 0,
                "patterns": [],
                "caveats": [
                    f"谐波形态至少需要 {self.minimum_bar_count} 根 K 线，当前仅 {len(normalized)} 根。",
                ],
                "red_lines": red_lines("谐波形态"),
            }
        swings = detect_swings(normalized, window=self.swing_window)
        patterns = detect_harmonic_patterns(
            swings,
            rel_tolerance=self.harmonic_tolerance,
            bar_count=len(normalized),
            max_bars_since_d=self.harmonic_max_bars_since_d,
        )
        status = "available" if patterns else "no_pattern"
        input_start_at, input_end_at = input_bounds(normalized)
        return {
            **base_payload(
                schema_version="harmonic_lite_v2",
                status=status,
                asset_id=asset_id,
                symbol=symbol,
                market=market,
                timeframe=timeframe,
                bars=normalized,
                raw_bar_count=len(bars),
                evidence_prefix="harmonic_lite",
            ),
            "input_start_at": input_start_at.isoformat(),
            "input_end_at": input_end_at.isoformat(),
            "swing_count": len(swings),
            "patterns": patterns,
            "caveats": [
                "第一版只做 XABCD Fibonacci 候选识别，不声称已接入 pyharmonics。",
                "谐波形态只能作为潜在反转结构证据，不能直接覆盖系统动作。",
            ],
            "red_lines": red_lines("谐波形态"),
        }

    def compute_smc(
        self,
        *,
        asset_id: str,
        symbol: str,
        market: str,
        timeframe: str,
        bars: list[StructuralPriceBar],
    ) -> JsonDict:
        """计算 SMC-lite 的 BOS/CHoCH/FVG 结构事件。"""

        normalized = normalize_bars(bars)
        if len(normalized) < self.minimum_bar_count:
            input_start_at, input_end_at = input_bounds(normalized)
            return {
                **base_payload(
                    schema_version="smc_lite_v2",
                    status="insufficient_data",
                    asset_id=asset_id,
                    symbol=symbol,
                    market=market,
                    timeframe=timeframe,
                    bars=normalized,
                    raw_bar_count=len(bars),
                    evidence_prefix="smc_lite",
                ),
                "input_start_at": input_start_at.isoformat(),
                "input_end_at": input_end_at.isoformat(),
                "swing_count": 0,
                "structure_events": [],
                "fair_value_gaps": [],
                "latest_bar": serialize_latest_bar(normalized),
                "confidence": 0.0,
                "caveats": [
                    f"SMC 结构至少需要 {self.minimum_bar_count} 根 K 线，当前仅 {len(normalized)} 根。",
                ],
                "red_lines": red_lines("SMC 结构"),
            }
        swings = detect_swings(normalized, window=self.swing_window)
        structure_events = detect_structure_breaks(normalized, swings)
        fair_value_gaps = detect_fair_value_gaps(
            normalized,
            min_atr_ratio=self.fvg_min_atr_ratio,
            include_mitigated=self.fvg_include_mitigated,
        )
        confidence = max(
            (
                float(item["confidence"])
                for item in structure_events + fair_value_gaps
            ),
            default=0.0,
        )
        status = "available" if structure_events or fair_value_gaps else "no_structure_event"
        input_start_at, input_end_at = input_bounds(normalized)
        return {
            **base_payload(
                schema_version="smc_lite_v2",
                status=status,
                asset_id=asset_id,
                symbol=symbol,
                market=market,
                timeframe=timeframe,
                bars=normalized,
                raw_bar_count=len(bars),
                evidence_prefix="smc_lite",
            ),
            "input_start_at": input_start_at.isoformat(),
            "input_end_at": input_end_at.isoformat(),
            "swing_count": len(swings),
            "structure_events": structure_events,
            "fair_value_gaps": fair_value_gaps,
            "latest_bar": serialize_latest_bar(normalized),
            "confidence": round(confidence, 6),
            "confidence_method": "max_event_or_gap_confidence",
            "caveats": [
                "SMC-lite 只覆盖 BOS、CHoCH 和三根 K 线 FVG。",
                "订单块、流动性池和机构意图不能由第一版轻量引擎强结论化。",
            ],
            "red_lines": red_lines("SMC 结构"),
        }

    def compute_elliott(
        self,
        *,
        asset_id: str,
        symbol: str,
        market: str,
        timeframe: str,
        bars: list[StructuralPriceBar],
    ) -> JsonDict:
        """计算 Elliott-lite 波浪候选。"""

        normalized = normalize_bars(bars)
        if len(normalized) < self.minimum_bar_count:
            input_start_at, input_end_at = input_bounds(normalized)
            return {
                **base_payload(
                    schema_version="elliott_lite_v2",
                    status="insufficient_data",
                    asset_id=asset_id,
                    symbol=symbol,
                    market=market,
                    timeframe=timeframe,
                    bars=normalized,
                    raw_bar_count=len(bars),
                    evidence_prefix="elliott_lite",
                ),
                "input_start_at": input_start_at.isoformat(),
                "input_end_at": input_end_at.isoformat(),
                "swing_count": 0,
                "candidates": [],
                "confidence": 0.0,
                "caveats": [
                    f"Elliott 结构至少需要 {self.minimum_bar_count} 根 K 线，当前仅 {len(normalized)} 根。",
                ],
                "red_lines": red_lines("艾略特波浪"),
            }
        swings = detect_swings(normalized, window=self.swing_window)
        raw_candidates = detect_elliott_candidates(
            swings,
            min_bars_per_wave=self.min_bars_per_wave,
        )
        candidates = [
            candidate
            for candidate in raw_candidates
            if float(candidate["confidence"]) >= self.elliott_confidence_threshold
        ]
        max_confidence = max(
            (float(candidate["confidence"]) for candidate in candidates or raw_candidates),
            default=0.0,
        )
        status = "available" if candidates else "insufficient_structure"
        input_start_at, input_end_at = input_bounds(normalized)
        return {
            **base_payload(
                schema_version="elliott_lite_v2",
                status=status,
                asset_id=asset_id,
                symbol=symbol,
                market=market,
                timeframe=timeframe,
                bars=normalized,
                raw_bar_count=len(bars),
                evidence_prefix="elliott_lite",
            ),
            "input_start_at": input_start_at.isoformat(),
            "input_end_at": input_end_at.isoformat(),
            "swing_count": len(swings),
            "candidates": candidates,
            "confidence": round(max_confidence, 6),
            "caveats": [
                "Elliott-lite 只输出有限候选，不声称唯一正确浪型。",
                "低置信度时必须按结构不清晰处理，不得强行数浪。",
            ],
            "red_lines": red_lines("艾略特波浪"),
        }

    @property
    def minimum_bar_count(self) -> int:
        """生产默认下结构方法论所需的最小 K 线数量。"""

        return self.swing_window * 4


def normalize_bars(bars: list[StructuralPriceBar]) -> list[StructuralPriceBar]:
    """按时间排序并校验 K 线。"""

    if not bars:
        raise ValueError("结构方法论至少需要 1 根 K 线。")
    deduped: dict[datetime, StructuralPriceBar] = {}
    for bar in bars:
        normalized_timestamp = normalize_datetime(bar.timestamp)
        deduped[normalized_timestamp] = StructuralPriceBar(
            timestamp=normalized_timestamp,
            open=float(bar.open),
            high=float(bar.high),
            low=float(bar.low),
            close=float(bar.close),
            volume=float(bar.volume),
        )
    normalized = sorted(deduped.values(), key=lambda bar: bar.timestamp)
    for bar in normalized:
        values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
        if any(not math.isfinite(float(value)) for value in values):
            raise ValueError("K 线包含非有限数值。")
        if float(bar.high) < float(bar.low):
            raise ValueError("K 线最高价不能低于最低价。")
    return normalized


def detect_swings(bars: list[StructuralPriceBar], *, window: int) -> list[SwingPoint]:
    """使用固定窗口局部极值提取交替 swing 点。"""

    if len(bars) < window * 2 + 1:
        return []
    candidates: list[SwingPoint] = []
    for index in range(window, len(bars) - window):
        current = bars[index]
        section = bars[index - window : index + window + 1]
        max_high = max(bar.high for bar in section)
        min_low = min(bar.low for bar in section)
        is_high = current.high == max_high and first_value_offset(
            [bar.high for bar in section],
            max_high,
        ) == window
        is_low = current.low == min_low and first_value_offset(
            [bar.low for bar in section],
            min_low,
        ) == window
        if is_high and is_low:
            high_prominence = current.high - max(bar.high for bar in section if bar is not current)
            low_prominence = min(bar.low for bar in section if bar is not current) - current.low
            point_type = "H" if high_prominence >= low_prominence else "L"
            price = current.high if point_type == "H" else current.low
            candidates.append(make_swing_point(bars, index, window, point_type, price))
        elif is_high:
            candidates.append(make_swing_point(bars, index, window, "H", current.high))
        elif is_low:
            candidates.append(make_swing_point(bars, index, window, "L", current.low))
    return merge_alternating_swings(candidates)


def make_swing_point(
    bars: list[StructuralPriceBar],
    index: int,
    window: int,
    point_type: str,
    price: float,
) -> SwingPoint:
    """构造带确认时间的 swing 点。"""

    confirmed_bar_index = min(len(bars) - 1, index + window)
    return SwingPoint(
        timestamp=bars[index].timestamp,
        bar_index=index,
        type=point_type,
        price=price,
        confirmed_bar_index=confirmed_bar_index,
        confirmed_at=bars[confirmed_bar_index].timestamp,
    )


def merge_alternating_swings(points: list[SwingPoint]) -> list[SwingPoint]:
    """合并连续同类 swing 点，只保留更极端值。"""

    merged: list[SwingPoint] = []
    for point in sorted(points, key=lambda item: (item.timestamp, item.bar_index)):
        if not merged or merged[-1].type != point.type:
            merged.append(point)
            continue
        previous = merged[-1]
        should_replace = (
            point.type == "H"
            and point.price > previous.price
            or point.type == "L"
            and point.price < previous.price
        )
        if should_replace:
            merged[-1] = point
    return merged


HARMONIC_RULES: dict[str, JsonDict] = {
    "Gartley": {
        "b_retrace": (0.55, 0.68),
        "d_retrace": (0.72, 0.84),
        "bc_ratio": (0.382, 0.886),
        "cd_ratio": (1.27, 1.618),
    },
    "Bat": {
        "b_retrace": (0.33, 0.55),
        "d_retrace": (0.82, 0.94),
        "bc_ratio": (0.382, 0.886),
        "cd_ratio": (1.618, 2.618),
    },
    "Butterfly": {
        "b_retrace": (0.72, 0.84),
        "d_retrace": (1.20, 1.38),
        "bc_ratio": (0.382, 0.886),
        "cd_ratio": (1.618, 2.618),
    },
    "Crab": {
        "b_retrace": (0.33, 0.68),
        "d_retrace": (1.52, 1.72),
        "bc_ratio": (0.382, 0.886),
        "cd_ratio": (2.24, 3.618),
    },
}


def detect_harmonic_patterns(
    swings: list[SwingPoint],
    *,
    rel_tolerance: float,
    bar_count: int,
    max_bars_since_d: int,
) -> list[JsonDict]:
    """检测轻量谐波 XABCD 候选。"""

    found: list[JsonDict] = []
    for start in range(0, max(0, len(swings) - 4)):
        points = swings[start : start + 5]
        if len(points) < 5:
            continue
        if not all(points[index].type != points[index + 1].type for index in range(4)):
            continue
        pattern = classify_harmonic(
            points,
            rel_tolerance=rel_tolerance,
            bar_count=bar_count,
            max_bars_since_d=max_bars_since_d,
        )
        if pattern is not None:
            found.append(pattern)
    return deduplicate_harmonic_patterns(found)


def classify_harmonic(
    points: list[SwingPoint],
    *,
    rel_tolerance: float,
    bar_count: int,
    max_bars_since_d: int,
) -> JsonDict | None:
    """按常见 Fibonacci 区间分类谐波候选。"""

    x, a, b, c, d = points
    bars_since_d = bar_count - 1 - d.bar_index
    if bars_since_d > max_bars_since_d:
        return None
    xa = abs(a.price - x.price)
    ab = abs(b.price - a.price)
    bc = abs(c.price - b.price)
    cd = abs(d.price - c.price)
    if xa == 0 or ab == 0 or bc == 0:
        return None
    ratios = {
        "b_retrace": ab / xa,
        "d_retrace": abs(d.price - a.price) / xa,
        "bc_ratio": bc / ab,
        "cd_ratio": cd / bc,
    }
    candidates: list[tuple[str, JsonDict, float]] = []
    for pattern_name, pattern_rules in HARMONIC_RULES.items():
        if all(
            in_range(
                float(ratios[key]),
                pattern_rules[key],
                rel_tolerance=rel_tolerance,
            )
            for key in ("b_retrace", "d_retrace", "bc_ratio", "cd_ratio")
        ):
            fit_score = harmonic_fit_score(
                ratios,
                pattern_rules,
                rel_tolerance=rel_tolerance,
            )
            candidates.append((pattern_name, pattern_rules, fit_score))
    if not candidates:
        return None
    pattern_name, _pattern_rules, fit_score = max(candidates, key=lambda item: item[2])
    direction = "bullish" if x.type == "L" else "bearish"
    invalidation = d.price - xa * 0.02 if direction == "bullish" else d.price + xa * 0.02
    confirmed_bar_index = max(point.confirmed_bar_index for point in points)
    confirmed_at = max(point.confirmed_at for point in points)
    return {
        "pattern": pattern_name,
        "direction": direction,
        "points": {
            label: serialize_swing(point)
            for label, point in zip(("X", "A", "B", "C", "D"), points, strict=True)
        },
        "ratios": {key: round(value, 6) for key, value in ratios.items()},
        "confidence": round(min(0.95, max(0.55, fit_score)), 6),
        "completion": "forming_or_just_completed" if bars_since_d == 0 else "complete",
        "bars_since_d": bars_since_d,
        "confirmed_at_bar": confirmed_bar_index,
        "confirmed_at": confirmed_at.isoformat(),
        "invalidation_price": round(invalidation, 6),
    }


def deduplicate_harmonic_patterns(patterns: list[JsonDict]) -> list[JsonDict]:
    """对重叠时间窗的同名谐波形态去重，保留 fit 最高者。"""

    deduped: dict[tuple[str, str, str], JsonDict] = {}
    for pattern in patterns:
        points = pattern["points"]
        key = (
            str(pattern["pattern"]),
            str(points["X"]["timestamp"]),
            str(points["D"]["timestamp"]),
        )
        existing = deduped.get(key)
        if existing is None or float(pattern["confidence"]) > float(existing["confidence"]):
            deduped[key] = pattern
    return sorted(
        deduped.values(),
        key=lambda item: (int(item.get("bars_since_d", 0)), -float(item["confidence"])),
    )


def detect_structure_breaks(
    bars: list[StructuralPriceBar],
    swings: list[SwingPoint],
) -> list[JsonDict]:
    """识别轻量 BOS/CHoCH 事件。"""

    events: list[JsonDict] = []
    trend: str | None = None
    last_break_high_index: int | None = None
    last_break_low_index: int | None = None
    for index, bar in enumerate(bars):
        prior_highs = [
            point
            for point in swings
            if point.type == "H" and point.confirmed_bar_index <= index
        ]
        prior_lows = [
            point
            for point in swings
            if point.type == "L" and point.confirmed_bar_index <= index
        ]
        latest_high = prior_highs[-1] if prior_highs else None
        latest_low = prior_lows[-1] if prior_lows else None
        if latest_high is not None and bar.close > latest_high.price and latest_high.bar_index != last_break_high_index:
            name = "bos_bullish" if trend in {None, "bullish"} else "choch_bullish"
            events.append(structure_event(name, "bullish", bar, index, latest_high))
            trend = "bullish"
            last_break_high_index = latest_high.bar_index
        if latest_low is not None and bar.close < latest_low.price and latest_low.bar_index != last_break_low_index:
            name = "bos_bearish" if trend in {None, "bearish"} else "choch_bearish"
            events.append(structure_event(name, "bearish", bar, index, latest_low))
            trend = "bearish"
            last_break_low_index = latest_low.bar_index
    return events


def detect_fair_value_gaps(
    bars: list[StructuralPriceBar],
    *,
    min_atr_ratio: float,
    include_mitigated: bool,
) -> list[JsonDict]:
    """识别三根 K 线公允价值缺口。"""

    gaps: list[JsonDict] = []
    for index in range(2, len(bars)):
        left = bars[index - 2]
        right = bars[index]
        atr = average_true_range_at(bars, index=index, window=14)
        if right.low > left.high:
            gap = build_fvg_gap(
                bars,
                index=index,
                name="fvg_bullish",
                direction="bullish",
                lower_bound=left.high,
                upper_bound=right.low,
                atr=atr,
            )
            if should_keep_fvg(gap, min_atr_ratio=min_atr_ratio, include_mitigated=include_mitigated):
                gaps.append(gap)
        elif right.high < left.low:
            gap = build_fvg_gap(
                bars,
                index=index,
                name="fvg_bearish",
                direction="bearish",
                lower_bound=right.high,
                upper_bound=left.low,
                atr=atr,
            )
            if should_keep_fvg(gap, min_atr_ratio=min_atr_ratio, include_mitigated=include_mitigated):
                gaps.append(gap)
    return gaps


def build_fvg_gap(
    bars: list[StructuralPriceBar],
    *,
    index: int,
    name: str,
    direction: str,
    lower_bound: float,
    upper_bound: float,
    atr: float,
) -> JsonDict:
    """构造带 ATR 与回补状态的 FVG。"""

    left = bars[index - 2]
    right = bars[index]
    width = upper_bound - lower_bound
    ratio = width / atr if atr > 0 else 0.0
    mitigated_at = find_fvg_mitigation(
        bars,
        start_index=index + 1,
        lower_bound=lower_bound,
        upper_bound=upper_bound,
    )
    return {
        "name": name,
        "direction": direction,
        "start_timestamp": left.timestamp.isoformat(),
        "end_timestamp": right.timestamp.isoformat(),
        "confirmed_at_bar": index,
        "confirmed_at": right.timestamp.isoformat(),
        "lower_bound": round(lower_bound, 6),
        "upper_bound": round(upper_bound, 6),
        "width": round(width, 6),
        "atr": round(atr, 6),
        "width_atr_ratio": round(ratio, 6),
        "mitigated": mitigated_at is not None,
        "mitigated_at": mitigated_at.isoformat() if mitigated_at is not None else None,
        "confidence": round(min(0.85, 0.5 + ratio * 0.2), 6),
    }


def should_keep_fvg(
    gap: JsonDict,
    *,
    min_atr_ratio: float,
    include_mitigated: bool,
) -> bool:
    """判断 FVG 是否达到输出门槛。"""

    if float(gap["width_atr_ratio"]) < min_atr_ratio:
        return False
    if bool(gap["mitigated"]) and not include_mitigated:
        return False
    return True


def find_fvg_mitigation(
    bars: list[StructuralPriceBar],
    *,
    start_index: int,
    lower_bound: float,
    upper_bound: float,
) -> datetime | None:
    """向后扫描缺口是否被后续 K 线触及。"""

    for bar in bars[start_index:]:
        if bar.low <= upper_bound and bar.high >= lower_bound:
            return bar.timestamp
    return None


def average_true_range_at(
    bars: list[StructuralPriceBar],
    *,
    index: int,
    window: int,
) -> float:
    """计算截至指定 bar 的简单 ATR。"""

    start = max(0, index - window + 1)
    ranges: list[float] = []
    for current_index in range(start, index + 1):
        bar = bars[current_index]
        if current_index == 0:
            ranges.append(bar.high - bar.low)
            continue
        previous_close = bars[current_index - 1].close
        ranges.append(
            max(
                bar.high - bar.low,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            )
        )
    return sum(ranges) / len(ranges) if ranges else 0.0


def detect_elliott_candidates(
    swings: list[SwingPoint],
    *,
    min_bars_per_wave: int,
) -> list[JsonDict]:
    """识别有限 Elliott-lite 候选。"""

    candidates: list[JsonDict] = []
    for start in range(0, max(0, len(swings) - 5)):
        points = swings[start : start + 6]
        impulse = classify_impulse(points, min_bars_per_wave=min_bars_per_wave)
        if impulse is not None:
            candidates.append(impulse)
    for start in range(0, max(0, len(swings) - 3)):
        points = swings[start : start + 4]
        abc = classify_abc(points, min_bars_per_wave=min_bars_per_wave)
        if abc is not None:
            candidates.append(abc)
    return sorted(candidates, key=lambda item: float(item["confidence"]), reverse=True)


def classify_impulse(points: list[SwingPoint], *, min_bars_per_wave: int) -> JsonDict | None:
    """识别 5 浪推动完成候选。"""

    types = [point.type for point in points]
    if not check_min_bars(points, min_bars_per_wave=min_bars_per_wave):
        return None
    if types == ["L", "H", "L", "H", "L", "H"]:
        x, p1, p2, p3, p4, p5 = points
        wave1 = p1.price - x.price
        wave2 = p1.price - p2.price
        wave3 = p3.price - p2.price
        wave4 = p3.price - p4.price
        wave5 = p5.price - p4.price
        if not valid_bullish_impulse(x, p1, p2, p3, p4, p5, (wave1, wave2, wave3, wave4, wave5)):
            return None
        confidence = impulse_confidence(wave1, wave2, wave3, wave4, wave5)
        return elliott_candidate(
            pattern="bullish_impulse_complete",
            signal_hint="uptrend_exhaustion_risk",
            points=points,
            confidence=confidence,
        )
    if types == ["H", "L", "H", "L", "H", "L"]:
        x, p1, p2, p3, p4, p5 = points
        wave1 = x.price - p1.price
        wave2 = p2.price - p1.price
        wave3 = p2.price - p3.price
        wave4 = p4.price - p3.price
        wave5 = p4.price - p5.price
        if not valid_bearish_impulse(x, p1, p2, p3, p4, p5, (wave1, wave2, wave3, wave4, wave5)):
            return None
        confidence = impulse_confidence(wave1, wave2, wave3, wave4, wave5)
        return elliott_candidate(
            pattern="bearish_impulse_complete",
            signal_hint="downtrend_exhaustion_rebound_watch",
            points=points,
            confidence=confidence,
        )
    return None


def classify_abc(points: list[SwingPoint], *, min_bars_per_wave: int) -> JsonDict | None:
    """识别 ABC 调整候选。"""

    types = [point.type for point in points]
    if not check_min_bars(points, min_bars_per_wave=min_bars_per_wave):
        return None
    if types == ["H", "L", "H", "L"]:
        start, pa, pb, pc = points
        wave_a = start.price - pa.price
        wave_b = pb.price - pa.price
        wave_c = pb.price - pc.price
        if wave_a <= 0 or wave_b <= 0 or wave_c <= 0:
            return None
        if pb.price >= start.price:
            return None
        if not (0.25 <= wave_b / wave_a <= 0.75 and 0.5 <= wave_c / wave_a <= 1.8):
            return None
        confidence = abc_confidence(wave_b / wave_a, wave_c / wave_a)
        return elliott_candidate(
            pattern="bearish_abc_correction_complete",
            signal_hint="pullback_exhaustion_watch",
            points=points,
            confidence=confidence,
        )
    if types == ["L", "H", "L", "H"]:
        start, pa, pb, pc = points
        wave_a = pa.price - start.price
        wave_b = pa.price - pb.price
        wave_c = pc.price - pb.price
        if wave_a <= 0 or wave_b <= 0 or wave_c <= 0:
            return None
        if pb.price <= start.price:
            return None
        if not (0.25 <= wave_b / wave_a <= 0.75 and 0.5 <= wave_c / wave_a <= 1.8):
            return None
        confidence = abc_confidence(wave_b / wave_a, wave_c / wave_a)
        return elliott_candidate(
            pattern="bullish_abc_correction_complete",
            signal_hint="rebound_exhaustion_watch",
            points=points,
            confidence=confidence,
        )
    return None


def check_min_bars(points: list[SwingPoint], *, min_bars_per_wave: int) -> bool:
    """检查每段浪至少跨指定数量的 bar。"""

    return all(
        abs(right.bar_index - left.bar_index) >= min_bars_per_wave
        for left, right in zip(points, points[1:], strict=False)
    )


def abc_confidence(wave_b_ratio: float, wave_c_ratio: float) -> float:
    """根据 ABC 浪间比例靠近经典比例的程度打分。"""

    b_fit = max(
        0.0,
        1 - min(abs(wave_b_ratio - 0.5), abs(wave_b_ratio - 0.618)) / 0.35,
    )
    c_fit = max(
        0.0,
        1 - min(abs(wave_c_ratio - 1.0), abs(wave_c_ratio - 1.618)) / 0.8,
    )
    return min(0.58, max(0.45, 0.45 + 0.13 * ((b_fit + c_fit) / 2)))


def valid_bullish_impulse(
    x: SwingPoint,
    p1: SwingPoint,
    p2: SwingPoint,
    p3: SwingPoint,
    p4: SwingPoint,
    p5: SwingPoint,
    waves: tuple[float, float, float, float, float],
) -> bool:
    """校验上升推动浪的基本铁律。"""

    wave1, wave2, wave3, wave4, wave5 = waves
    if min(wave1, wave2, wave3, wave4, wave5) <= 0:
        return False
    if p2.price <= x.price:
        return False
    if wave3 < wave1 and wave3 < wave5:
        return False
    if p4.price <= p1.price:
        return False
    if not p5.price > p3.price:
        return False
    return valid_impulse_ratios(wave1, wave2, wave3, wave4)


def valid_bearish_impulse(
    x: SwingPoint,
    p1: SwingPoint,
    p2: SwingPoint,
    p3: SwingPoint,
    p4: SwingPoint,
    p5: SwingPoint,
    waves: tuple[float, float, float, float, float],
) -> bool:
    """校验下降推动浪的基本铁律。"""

    wave1, wave2, wave3, wave4, wave5 = waves
    if min(wave1, wave2, wave3, wave4, wave5) <= 0:
        return False
    if p2.price >= x.price:
        return False
    if wave3 < wave1 and wave3 < wave5:
        return False
    if p4.price >= p1.price:
        return False
    if not p5.price < p3.price:
        return False
    return valid_impulse_ratios(wave1, wave2, wave3, wave4)


def valid_impulse_ratios(wave1: float, wave2: float, wave3: float, wave4: float) -> bool:
    """Elliott-lite 推动浪硬比例门槛。"""

    if wave1 == 0 or wave3 == 0:
        return False
    r2 = wave2 / wave1
    r3 = wave3 / wave1
    r4 = wave4 / wave3
    return 0.236 <= r2 <= 0.786 and r3 >= 1.0 and 0.236 <= r4 <= 0.5


def impulse_confidence(
    wave1: float,
    wave2: float,
    wave3: float,
    wave4: float,
    wave5: float,
) -> float:
    """根据浪间关系给推动浪候选打置信度。"""

    if wave1 == 0 or wave3 == 0:
        return 0.0
    r2 = wave2 / wave1
    r3 = wave3 / wave1
    r4 = wave4 / wave3
    score = 0.4
    if 0.35 <= r2 <= 0.75:
        score += 0.08
    if 1.0 <= r3 <= 2.618:
        score += 0.1
    if 0.2 <= r4 <= 0.65:
        score += 0.08
    if wave3 >= wave1 and wave3 >= wave5:
        score += 0.04
    return min(0.9, score)


def elliott_candidate(
    *,
    pattern: str,
    signal_hint: str,
    points: list[SwingPoint],
    confidence: float,
) -> JsonDict:
    """构造 Elliott-lite 候选。"""

    return {
        "pattern": pattern,
        "signal_hint": signal_hint,
        "points": [serialize_swing(point) for point in points],
        "confidence": round(confidence, 6),
        "confirmed_at_bar": max(point.confirmed_bar_index for point in points),
        "confirmed_at": max(point.confirmed_at for point in points).isoformat(),
        "thesis_confirmation_price": round(points[-2].price, 6),
        "thesis_invalidation_price": round(points[-1].price, 6),
    }


def structure_event(
    name: str,
    direction: str,
    bar: StructuralPriceBar,
    bar_index: int,
    reference: SwingPoint,
) -> JsonDict:
    """构造 BOS/CHoCH 事件。"""

    confirmed_bar_index = max(bar_index, reference.confirmed_bar_index)
    confirmed_at = max(bar.timestamp, reference.confirmed_at)
    break_pct = abs(bar.close - reference.price) / abs(reference.price) if reference.price else 0.0
    body_ratio = abs(bar.close - bar.open) / (bar.high - bar.low) if bar.high > bar.low else 0.0
    confidence = min(0.9, 0.5 + 4 * break_pct + 0.2 * body_ratio)
    return {
        "name": name,
        "direction": direction,
        "timestamp": bar.timestamp.isoformat(),
        "confirmed_at_bar": confirmed_bar_index,
        "confirmed_at": confirmed_at.isoformat(),
        "close": round(bar.close, 6),
        "reference_swing": serialize_swing(reference),
        "break_level": round(reference.price, 6),
        "break_pct": round(break_pct, 6),
        "body_ratio": round(body_ratio, 6),
        "confidence": round(confidence, 6),
    }


def build_segments(swings: list[SwingPoint]) -> list[JsonDict]:
    """把 swing 点转换为趋势段。"""

    segments: list[JsonDict] = []
    for left, right in zip(swings, swings[1:], strict=False):
        direction = "up" if right.price > left.price else "down"
        segments.append(
            {
                "from": serialize_swing(left),
                "to": serialize_swing(right),
                "direction": direction,
                "amplitude": round(abs(right.price - left.price), 6),
            }
        )
    return segments


def swing_structure_confidence(swings: list[SwingPoint], *, bar_count: int) -> float:
    """根据 swing 密度给结构清晰度一个保守置信度。"""

    if not swings or bar_count <= 0:
        return 0.0
    density = len(swings) / bar_count
    return round(min(0.85, max(0.15, density * 5)), 6)


def serialize_swing(point: SwingPoint) -> JsonDict:
    """把 swing 点转为 JSON 结构。"""

    return {
        "timestamp": point.timestamp.isoformat(),
        "bar_index": point.bar_index,
        "type": point.type,
        "price": round(point.price, 6),
        "confirmed_bar_index": point.confirmed_bar_index,
        "confirmed_at": point.confirmed_at.isoformat(),
    }


def serialize_latest_bar(bars: list[StructuralPriceBar]) -> JsonDict | None:
    """输出延续确认所需的最后一根闭合 K，不泄露未来数据。"""

    if not bars:
        return None
    bar = bars[-1]
    return {
        "timestamp": bar.timestamp.isoformat(),
        "open": round(bar.open, 6),
        "high": round(bar.high, 6),
        "low": round(bar.low, 6),
        "close": round(bar.close, 6),
        "volume": round(bar.volume, 6),
    }


def base_payload(
    *,
    schema_version: str,
    status: str,
    asset_id: str,
    symbol: str,
    market: str,
    timeframe: str,
    bars: list[StructuralPriceBar],
    evidence_prefix: str,
    raw_bar_count: int | None = None,
) -> JsonDict:
    """构造统一方法论 payload 基础字段。"""

    _input_start_at, input_end_at = input_bounds(bars)
    return {
        "schema_version": schema_version,
        "status": status,
        "asset_id": asset_id,
        "symbol": symbol,
        "market": market,
        "timeframe": timeframe,
        "engine": ENGINE_NAME,
        "engine_version": ENGINE_VERSION,
        "bar_count": len(bars),
        "as_of_semantics": "confirmed_only",
        "data_warnings": {
            "duplicate_timestamp_count": max(0, (raw_bar_count or len(bars)) - len(bars)),
        },
        "evidence_id": build_evidence_id(evidence_prefix, asset_id, timeframe, input_end_at),
    }


def red_lines(method_name: str) -> list[str]:
    """统一红线说明。"""

    return [
        f"{method_name}由确定性结构引擎计算，LLM 只能解读。",
        "不得用模型自行补算结构、修改系统分数、信号方向、风险标记或动作枚举。",
        "无引擎输出时不得声称结构已经成立。",
    ]


def harmonic_fit_score(
    ratios: JsonDict,
    rules: JsonDict,
    *,
    rel_tolerance: float,
) -> float:
    """根据关键比例靠近区间中点的程度给谐波候选打分。"""

    scores = []
    for key in ("b_retrace", "d_retrace", "bc_ratio", "cd_ratio"):
        low, high = rules[key]
        midpoint = (low + high) / 2
        half_width = (high - low) / 2 + midpoint * rel_tolerance
        distance = abs(float(ratios[key]) - midpoint)
        scores.append(max(0.0, 1 - distance / half_width))
    return 0.6 + 0.3 * (sum(scores) / len(scores))


def in_range(value: float, bounds: tuple[float, float], *, rel_tolerance: float) -> bool:
    """判断数值是否落在带容差区间内。"""

    low, high = bounds
    return low * (1 - rel_tolerance) <= value <= high * (1 + rel_tolerance)


def count_value(values: list[float], target: float) -> int:
    """统计浮点值在局部窗口中的出现次数。"""

    return sum(1 for value in values if value == target)


def first_value_offset(values: list[float], target: float) -> int:
    """返回目标值在窗口中的首个位置。"""

    for index, value in enumerate(values):
        if value == target:
            return index
    return -1


def input_bounds(bars: list[StructuralPriceBar]) -> tuple[datetime, datetime]:
    """返回输入起止时间。"""

    return bars[0].timestamp, bars[-1].timestamp


def build_evidence_id(prefix: str, asset_id: str, timeframe: str, input_end_at: datetime) -> str:
    """生成方法论证据 ID。"""

    normalized = input_end_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}:{asset_id}:{timeframe}:{normalized}"


def normalize_datetime(value: datetime) -> datetime:
    """统一时间为 UTC aware datetime。"""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
