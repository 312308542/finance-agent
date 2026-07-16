"""运行 structural-lite 首轮只读事件研究。"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from sqlalchemy import select

from finance_agent.indicators.structural_methodology_adapters import (
    StructuralMethodologyAdapter,
    StructuralPriceBar,
)
from finance_agent.research.structural_event_study import (
    events_from_structural_payload,
    summarize_events,
)
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.orm import IndicatorFrameORM, MarketBarORM


def parse_dt(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 structural-lite 只读事件研究")
    parser.add_argument("--output", default="artifacts/structural_event_study_20260716.json")
    parser.add_argument("--min-signal-days", type=int, default=10)
    parser.add_argument(
        "--historical",
        action="store_true",
        help="从历史日 K 重算 structural-lite 事件，而不是只读取最新入库帧。",
    )
    parser.add_argument(
        "--historical-bars",
        type=int,
        default=750,
        help="每个资产参与历史重算的最近 K 线数量。",
    )
    args = parser.parse_args()

    with session_scope(create_session_factory()) as session:
        frames = list(
            session.scalars(
                select(IndicatorFrameORM)
                .where(IndicatorFrameORM.library == "structural-lite")
                .order_by(IndicatorFrameORM.input_end_at)
            )
        )
        asset_ids = sorted({frame.asset_id for frame in frames})
        bars = list(
            session.scalars(
                select(MarketBarORM)
                .where(
                    MarketBarORM.asset_id.in_(asset_ids),
                    MarketBarORM.market == "ashare",
                    MarketBarORM.timeframe == "1d",
                    MarketBarORM.source == "canonical:ashare:kline",
                    MarketBarORM.is_closed.is_(True),
                )
                .order_by(MarketBarORM.asset_id, MarketBarORM.timestamp)
            )
        )

    bar_rows: dict[str, list[MarketBarORM]] = defaultdict(list)
    for bar in bars:
        bar_rows[bar.asset_id].append(bar)
    if args.historical:
        bar_rows = {
            asset_id: rows[-max(int(args.historical_bars), 60) :]
            for asset_id, rows in bar_rows.items()
        }
    price_rows: dict[str, list[tuple[date, float]]] = {
        asset_id: [(bar.timestamp.date(), float(bar.close)) for bar in rows]
        for asset_id, rows in bar_rows.items()
    }
    prices = {asset: [close for _, close in rows] for asset, rows in price_rows.items()}
    date_indexes = {asset: {day: index for index, (day, _) in enumerate(rows)} for asset, rows in price_rows.items()}

    events: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    if args.historical:
        adapter = StructuralMethodologyAdapter(
            harmonic_max_bars_since_d=max(int(args.historical_bars), 60),
            fvg_include_mitigated=True,
        )
        for asset_id, rows in bar_rows.items():
            if len(rows) < adapter.minimum_bar_count:
                continue
            structural_bars = [
                StructuralPriceBar(
                    timestamp=bar.timestamp,
                    open=float(bar.open),
                    high=float(bar.high),
                    low=float(bar.low),
                    close=float(bar.close),
                    volume=float(bar.volume),
                )
                for bar in rows
            ]
            first = rows[0]
            payloads = [
                adapter.compute_smc(
                    asset_id=asset_id,
                    symbol=str(first.symbol),
                    market="ashare",
                    timeframe="1d",
                    bars=structural_bars,
                ),
                adapter.compute_harmonic(
                    asset_id=asset_id,
                    symbol=str(first.symbol),
                    market="ashare",
                    timeframe="1d",
                    bars=structural_bars,
                ),
                adapter.compute_elliott(
                    asset_id=asset_id,
                    symbol=str(first.symbol),
                    market="ashare",
                    timeframe="1d",
                    bars=structural_bars,
                ),
            ]
            extracted = events_from_structural_payload(
                asset_id=asset_id,
                payloads=payloads,
                date_indexes={day.isoformat(): index for index, (day, _) in enumerate(price_rows[asset_id])},
            )
            for event in extracted:
                key = (asset_id, str(event["signal"]), str(event["event_date"]))
                if key not in seen:
                    seen.add(key)
                    events.append(event)
    if not args.historical:
        for frame in frames:
            rows = price_rows.get(frame.asset_id, [])
            if not rows:
                continue
            cutoff = frame.input_end_at.date()
            for signal, item_rows, direction_key in _frame_signals(frame):
                for item in item_rows:
                    confirmed = item.get("confirmed_at")
                    if not confirmed:
                        continue
                    event_day = parse_dt(confirmed).date()
                    if (cutoff - event_day).days > args.min_signal_days or event_day > cutoff:
                        continue
                    if event_day not in date_indexes[frame.asset_id]:
                        continue
                    key = (frame.asset_id, signal, event_day.isoformat())
                    if key in seen:
                        continue
                    seen.add(key)
                    events.append(
                        {
                            "asset_id": frame.asset_id,
                            "signal": signal,
                            "direction": str(item.get(direction_key) or item.get("direction") or "bullish"),
                            "event_index": date_indexes[frame.asset_id][event_day],
                            "event_date": event_day,
                        }
                    )

    event_dates = sorted({event["event_date"] for event in events})
    split_date = event_dates[len(event_dates) // 2] if event_dates else None
    period_splits = (
        event_dates[len(event_dates) // 3],
        event_dates[(len(event_dates) * 2) // 3],
    ) if len(event_dates) >= 3 else None
    benchmark_returns: dict[tuple[str, int], float | None] = {}
    for event_day in event_dates:
        for window in (5, 10, 20):
            returns = []
            for asset, rows in price_rows.items():
                index = date_indexes[asset].get(event_day)
                target = index + window if index is not None else None
                if target is not None and target < len(rows) and rows[index][1]:
                    returns.append(rows[target][1] / rows[index][1] - 1.0)
            benchmark_returns[(str(event_day), window)] = sum(returns) / len(returns) if returns else None

    result = {
        "protocol": {
            "market": "ashare",
            "universe": "structural-lite 已入库资产（主板候选池来源）",
            "mode": "historical_recompute" if args.historical else "stored_frames",
            "windows": [5, 10, 20],
            "split_date": split_date.isoformat() if split_date else None,
            "lookahead_control": "历史重算只使用引擎输出的 confirmed_at；已入库帧模式只使用 confirmed_at 不晚于 frame.input_end_at；结果仅作研究，不改评分/动作。",
        },
        "input": {"frame_count": len(frames), "asset_count": len(price_rows), "event_count": len(events), "bar_count": len(bars)},
        "summary": summarize_events(events, prices, benchmark_returns, split_date=split_date),
    }
    if period_splits:
        result["protocol"]["period_splits"] = [value.isoformat() for value in period_splits]
        result["period_summary"] = summarize_events(
            events,
            prices,
            benchmark_returns,
            period_splits=period_splits,
        )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result["input"] | {"split_date": result["protocol"]["split_date"], "output": str(output)}, ensure_ascii=False))


def _frame_signals(frame: IndicatorFrameORM):
    payload = frame.payload or {}
    if frame.horizon == "smc_lite_v2":
        yield from ((str(item.get("name") or "smc_event"), [item], "direction") for item in payload.get("structure_events", []))
        yield from ((str(item.get("name") or "fvg"), [item], "direction") for item in payload.get("fair_value_gaps", []))
    elif frame.horizon == "harmonic_lite_v2":
        yield from ((f"harmonic_{item.get('pattern', 'unknown')}", [item], "direction") for item in payload.get("patterns", []))
    elif frame.horizon == "elliott_lite_v2":
        yield from ((f"elliott_{item.get('pattern', item.get('signal_hint', 'unknown'))}", [item], "direction") for item in payload.get("candidates", []))


if __name__ == "__main__":
    main()
