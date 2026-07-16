"""结构信号历史事件研究的纯函数工具。

研究只消费已确认事件和历史收盘价，不修改推荐、评分或交易动作。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from statistics import median
from typing import Any

JsonDict = dict[str, Any]


def forward_return(
    closes: Sequence[float],
    event_index: int,
    window: int,
    *,
    direction: str = "bullish",
) -> float | None:
    """计算事件确认后指定交易日的方向收益。"""

    target = event_index + int(window)
    if event_index < 0 or target >= len(closes) or closes[event_index] == 0:
        return None
    raw = float(closes[target]) / float(closes[event_index]) - 1.0
    return raw if direction == "bullish" else -raw


def maximum_adverse_excursion(
    closes: Sequence[float],
    event_index: int,
    window: int,
    *,
    direction: str = "bullish",
) -> float | None:
    """计算事件后窗口内最大不利收盘波动。"""

    target = event_index + int(window)
    if event_index < 0 or target >= len(closes) or closes[event_index] == 0:
        return None
    path = [float(value) / float(closes[event_index]) - 1.0 for value in closes[event_index : target + 1]]
    if direction != "bullish":
        path = [-value for value in path]
    # MAE 统一以事件方向的收益表示，最不利点始终是最小值。
    return min(path)


def events_from_structural_payload(
    *,
    asset_id: str,
    payloads: Iterable[Mapping[str, Any]],
    date_indexes: Mapping[str, int],
) -> list[JsonDict]:
    """从历史重算 payload 提取已确认事件，严格使用 confirmed_at。"""

    events: list[JsonDict] = []
    for payload in payloads:
        horizon = str(payload.get("horizon") or payload.get("schema_version") or "")
        if horizon == "smc_lite_v2":
            groups = (payload.get("structure_events", []), payload.get("fair_value_gaps", []))
        elif horizon == "harmonic_lite_v2":
            groups = (payload.get("patterns", []),)
        elif horizon == "elliott_lite_v2":
            groups = (payload.get("candidates", []),)
        else:
            continue
        for rows in groups:
            for item in rows:
                confirmed_at = item.get("confirmed_at")
                if not confirmed_at:
                    continue
                event_date = str(confirmed_at)[:10]
                if event_date not in date_indexes:
                    continue
                signal = str(item.get("name") or item.get("pattern") or item.get("signal_hint") or horizon)
                direction = str(item.get("direction") or "bullish")
                events.append(
                    {
                        "asset_id": asset_id,
                        "signal": signal,
                        "direction": direction,
                        "event_index": date_indexes[event_date],
                        "event_date": date.fromisoformat(event_date),
                    }
                )
    return events


def summarize_events(
    events: Iterable[Mapping[str, Any]],
    prices: Mapping[str, Sequence[float]],
    benchmark_returns: Mapping[tuple[str, int], float | None],
    *,
    windows: Sequence[int] = (5, 10, 20),
    split_date: date | None = None,
    period_splits: Sequence[date] | None = None,
) -> JsonDict:
    """汇总全样本、样本内和样本外的收益、胜率与不利波动。

    ``period_splits`` 用于把时间序列切成样本内和多个连续样本外周期，
    便于验证信号是否在至少两个独立时间段保持方向一致。
    """

    buckets: dict[tuple[str, str, int], list[JsonDict]] = defaultdict(list)
    for event in events:
        asset_id = str(event["asset_id"])
        signal = str(event["signal"])
        direction = str(event.get("direction") or "bullish")
        event_index = int(event["event_index"])
        event_date = event.get("event_date")
        if isinstance(event_date, datetime):
            event_date = event_date.date()
        if period_splits and event_date:
            ordered_splits = sorted(period_splits)
            if event_date < ordered_splits[0]:
                scope = "in_sample"
            else:
                period_index = next(
                    (index for index, boundary in enumerate(ordered_splits[1:], start=1) if event_date < boundary),
                    len(ordered_splits),
                )
                scope = f"out_of_sample_period_{period_index}"
        else:
            scope = "out_of_sample" if split_date and event_date and event_date >= split_date else "in_sample"
        closes = prices.get(asset_id)
        if closes is None:
            continue
        for window in windows:
            result = forward_return(closes, event_index, window, direction=direction)
            mae = maximum_adverse_excursion(closes, event_index, window, direction=direction)
            benchmark = benchmark_returns.get((str(event.get("event_date")), int(window)))
            if result is None or mae is None:
                continue
            buckets[(signal, scope, int(window))].append(
                {"return": result, "mae": mae, "benchmark": benchmark}
            )

    output: JsonDict = {"windows": list(windows), "signals": {}}
    for (signal, scope, window), rows in sorted(buckets.items()):
        target = output["signals"].setdefault(signal, {}).setdefault(scope, {})
        returns = [float(row["return"]) for row in rows]
        excess = [
            float(row["return"]) - float(row["benchmark"])
            for row in rows
            if row["benchmark"] is not None
        ]
        target[str(window)] = {
            "sample_count": len(rows),
            "win_rate": sum(value > 0 for value in returns) / len(returns),
            "median_return": median(returns),
            "median_excess_return": median(excess) if excess else None,
            "max_adverse_excursion": min(float(row["mae"]) for row in rows),
        }
    return output
