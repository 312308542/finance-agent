"""从已入库日 K 中抽取弱市/震荡市样本。

该脚本用于方案 19/10 的真实样本验收材料生成：读取指数日 K 与全市场日 K，
按现有 `MarketRegimeService` 的确定性规则抽取 bear/range/bull 窗口，并输出
Markdown 或 JSON。脚本只读数据库，不写入业务表。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import pstdev
from typing import Any

from sqlalchemy import text

from finance_agent.application.market_context_service import (
    MarketRegimeInput,
    MarketRegimeResult,
    MarketRegimeService,
)
from finance_agent.storage.db import create_session_factory, session_scope

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class DailyBarSample:
    """用于大盘样本计算的简化日 K。"""

    asset_id: str
    trade_date: date
    close: Decimal
    prev_close: Decimal | None = None


def main() -> None:
    """生成真实库大盘环境样本报告。"""

    args = parse_args()
    report = build_report_from_database(
        index_asset_id=args.index_asset_id,
        start_date=parse_date(args.start_date) if args.start_date else None,
        end_date=parse_date(args.end_date) if args.end_date else None,
        market=args.market,
        timeframe=args.timeframe,
        limit=args.limit,
    )
    content = (
        json.dumps(report, ensure_ascii=False, indent=2, default=str)
        if args.format == "json"
        else render_markdown_report(report)
    )
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
    else:
        print(content)


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="从 market_bars 生成弱市/震荡市验收样本")
    parser.add_argument("--index-asset-id", default="ashare:index:000001", help="作为大盘代理的指数资产 ID")
    parser.add_argument("--market", default="ashare", help="市场，默认 ashare")
    parser.add_argument("--timeframe", default="1d", help="K 线周期，默认 1d")
    parser.add_argument("--start-date", default=None, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end-date", default=None, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--limit", type=int, default=3, help="每类样本最多输出几条")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown", help="输出格式")
    parser.add_argument("--output", default=None, help="输出文件路径；不传则打印到 stdout")
    return parser.parse_args()


def build_report_from_database(
    *,
    index_asset_id: str,
    start_date: date | None,
    end_date: date | None,
    market: str,
    timeframe: str,
    limit: int,
) -> JsonDict:
    """从真实数据库读取 K 线并生成样本报告。"""

    session_factory = create_session_factory()
    with session_scope(session_factory) as session:
        index_bars = load_index_daily_bars(
            session=session,
            index_asset_id=index_asset_id,
            timeframe=timeframe,
            start_date=start_date,
            end_date=end_date,
        )
        market_return_bars = load_market_return_bars(
            session=session,
            market=market,
            timeframe=timeframe,
            start_date=start_date,
            end_date=end_date,
        )
        index_source = "explicit_index"
        if not index_bars:
            candidates = discover_index_candidates(session=session, market=market, timeframe=timeframe)
            index_bars = build_equal_weight_proxy_index_bars(
                market_return_bars,
                proxy_asset_id=f"{market}:proxy:equal_weight",
            )
            index_source = "equal_weight_market_proxy"
            if not index_bars:
                raise RuntimeError(
                    "没有读取到指数 K 线，也无法用个股 K 线合成等权大盘代理。"
                    f"可选指数候选：{', '.join(candidates[:10]) or '无'}"
                )
        breadth_by_date = build_breadth_metrics_by_date(market_return_bars)

    samples = build_regime_samples(index_bars, breadth_by_date=breadth_by_date, limit=limit)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "index_asset_id": index_asset_id,
        "index_source": index_source,
        "market": market,
        "timeframe": timeframe,
        "date_range": {
            "start": index_bars[0].trade_date.isoformat(),
            "end": index_bars[-1].trade_date.isoformat(),
            "index_bar_count": len(index_bars),
            "breadth_day_count": len(breadth_by_date),
        },
        "samples": samples,
    }


def load_index_daily_bars(
    *,
    session: Any,
    index_asset_id: str,
    timeframe: str,
    start_date: date | None,
    end_date: date | None,
) -> list[DailyBarSample]:
    """读取指数日 K。"""

    params: JsonDict = {"asset_id": index_asset_id, "timeframe": timeframe}
    filters = [
        "asset_id = :asset_id",
        "timeframe = :timeframe",
        "is_closed is true",
        "status in ('available', 'partial')",
    ]
    if start_date is not None:
        filters.append("date(timestamp) >= :start_date")
        params["start_date"] = start_date
    if end_date is not None:
        filters.append("date(timestamp) <= :end_date")
        params["end_date"] = end_date
    rows = session.execute(
        text(
            f"""
            select asset_id, date(timestamp) as trade_date, close
            from market_bars
            where {' and '.join(filters)}
            order by timestamp asc
            """
        ),
        params,
    ).all()
    return [
        DailyBarSample(
            asset_id=str(row.asset_id),
            trade_date=row.trade_date,
            close=Decimal(str(row.close)),
        )
        for row in rows
    ]


def load_breadth_metrics(
    *,
    session: Any,
    market: str,
    timeframe: str,
    start_date: date,
    end_date: date,
) -> dict[date, JsonDict]:
    """读取全市场日 K 并按日生成涨跌家数、涨跌停比等宽度指标。"""

    return build_breadth_metrics_by_date(
        load_market_return_bars(
            session=session,
            market=market,
            timeframe=timeframe,
            start_date=start_date,
            end_date=end_date,
        )
    )


def load_market_return_bars(
    *,
    session: Any,
    market: str,
    timeframe: str,
    start_date: date | None,
    end_date: date | None,
) -> list[DailyBarSample]:
    """读取用于市场宽度和等权代理指数的个股收盘收益序列。"""

    effective_end = end_date or date.today()
    effective_start = start_date or (effective_end - timedelta(days=420))
    lag_start = effective_start - timedelta(days=14)
    rows = session.execute(
        text(
            """
            with daily_bars as (
                select
                    date(timestamp) as trade_date,
                    asset_id,
                    close,
                    lag(close) over (partition by asset_id order by timestamp) as prev_close
                from market_bars
                where market = :market
                  and timeframe = :timeframe
                  and is_closed is true
                  and status in ('available', 'partial')
                  and date(timestamp) between :lag_start and :end_date
                  and asset_id like 'ashare:%'
                  and asset_id not like 'ashare:index:%'
            )
            select trade_date, asset_id, close, prev_close
            from daily_bars
            where trade_date between :start_date and :end_date
              and prev_close is not null
            order by trade_date asc
            """
        ),
        {
            "market": market,
            "timeframe": timeframe,
            "lag_start": lag_start,
            "start_date": effective_start,
            "end_date": effective_end,
        },
    ).all()
    return [
        DailyBarSample(
            asset_id=str(row.asset_id),
            trade_date=row.trade_date,
            close=Decimal(str(row.close)),
            prev_close=Decimal(str(row.prev_close)),
        )
        for row in rows
    ]


def build_breadth_metrics_by_date(bars: Sequence[DailyBarSample]) -> dict[date, JsonDict]:
    """按交易日聚合市场宽度指标。"""

    bars_by_date: dict[date, list[DailyBarSample]] = {}
    for bar in bars:
        bars_by_date.setdefault(bar.trade_date, []).append(bar)
    return {trade_date: compute_breadth_metrics(items) for trade_date, items in bars_by_date.items()}


def build_equal_weight_proxy_index_bars(
    bars: Sequence[DailyBarSample],
    *,
    proxy_asset_id: str,
) -> list[DailyBarSample]:
    """用全市场等权日收益合成一个大盘代理指数。"""

    bars_by_date: dict[date, list[DailyBarSample]] = {}
    for bar in bars:
        if bar.prev_close is None or bar.prev_close == 0:
            continue
        bars_by_date.setdefault(bar.trade_date, []).append(bar)
    proxy_close = Decimal("100.000000")
    result: list[DailyBarSample] = []
    for trade_date in sorted(bars_by_date):
        returns = [
            Decimal(str(ratio_change(float(bar.close), float(bar.prev_close))))
            for bar in bars_by_date[trade_date]
        ]
        if not returns:
            continue
        avg_return = sum(returns, Decimal("0")) / Decimal(len(returns))
        proxy_close = (proxy_close * (Decimal("1") + avg_return)).quantize(Decimal("0.000001"))
        result.append(
            DailyBarSample(
                asset_id=proxy_asset_id,
                trade_date=trade_date,
                close=proxy_close,
            )
        )
    return result


def discover_index_candidates(*, session: Any, market: str, timeframe: str) -> list[str]:
    """尽量从库中找出可能的指数资产 ID，便于命令行报错时提示。"""

    rows = session.execute(
        text(
            """
            select asset_id, count(*) as bar_count
            from market_bars
            where market = :market
              and timeframe = :timeframe
              and asset_id like '%index%'
            group by asset_id
            order by bar_count desc
            limit 20
            """
        ),
        {"market": market, "timeframe": timeframe},
    ).all()
    return [str(row.asset_id) for row in rows]


def build_regime_samples(
    index_bars: Sequence[DailyBarSample],
    *,
    breadth_by_date: Mapping[date, Mapping[str, float | int]],
    limit: int = 3,
) -> dict[str, list[JsonDict]]:
    """按现有大盘规则从指数序列中抽取各类市场样本。"""

    service = MarketRegimeService()
    samples: dict[str, list[JsonDict]] = {"bear": [], "range": [], "bull": []}
    for trade_date in sorted(breadth_by_date):
        if all(len(items) >= limit for items in samples.values()):
            break
        metrics = breadth_by_date[trade_date]
        try:
            regime_input = compute_index_regime_input(
                index_bars,
                as_of=trade_date,
                advance_decline_ratio=float(metrics.get("advance_decline_ratio", 1.0)),
                limit_up_down_ratio=float(metrics.get("limit_up_down_ratio", 1.0)),
            )
        except ValueError:
            continue
        result = service.evaluate(regime_input)
        if len(samples[result.regime]) >= limit:
            continue
        samples[result.regime].append(sample_to_dict(trade_date, result, regime_input, metrics))
    return samples


def compute_index_regime_input(
    index_bars: Sequence[DailyBarSample],
    *,
    as_of: date,
    advance_decline_ratio: float,
    limit_up_down_ratio: float,
) -> MarketRegimeInput:
    """把指数 K 线窗口转换为大盘环境规则输入。"""

    bars = sorted((bar for bar in index_bars if bar.trade_date <= as_of), key=lambda item: item.trade_date)
    if len(bars) < 40:
        raise ValueError("至少需要 40 个交易日指数 K 线才能生成大盘样本")
    closes = [float(bar.close) for bar in bars]
    recent_20 = closes[-20:]
    previous_20 = closes[-40:-20]
    trend_20d = ratio_change(mean(recent_20), mean(previous_20))
    long_window = closes[-60:] if len(closes) >= 60 else closes
    trend_60d = ratio_change(mean(long_window[-20:]), mean(long_window[:20]))
    returns_20d = [
        ratio_change(current, previous)
        for previous, current in zip(recent_20, recent_20[1:], strict=False)
        if previous != 0
    ]
    volatility_20d = pstdev(returns_20d) * (252**0.5) if len(returns_20d) >= 2 else 0.0
    return MarketRegimeInput(
        index_trend_20d=round(trend_20d, 6),
        index_trend_60d=round(trend_60d, 6),
        volatility_20d=round(volatility_20d, 6),
        advance_decline_ratio=round(float(advance_decline_ratio), 6),
        limit_up_down_ratio=round(float(limit_up_down_ratio), 6),
        evidence_ids=(f"market_bars:{bars[-1].asset_id}:{as_of.isoformat()}",),
    )


def compute_breadth_metrics(bars: Sequence[DailyBarSample]) -> JsonDict:
    """按单日股票 K 线估算市场宽度指标。"""

    advancers = 0
    decliners = 0
    limit_up_count = 0
    limit_down_count = 0
    for bar in bars:
        previous = bar.prev_close
        if previous is None or previous == 0:
            continue
        change = ratio_change(float(bar.close), float(previous))
        if change > 0:
            advancers += 1
        elif change < 0:
            decliners += 1
        if change >= 0.098:
            limit_up_count += 1
        elif change <= -0.098:
            limit_down_count += 1
    return {
        "sample_size": len(bars),
        "advancers": advancers,
        "decliners": decliners,
        "limit_up_count": limit_up_count,
        "limit_down_count": limit_down_count,
        "advance_decline_ratio": safe_ratio(advancers, decliners),
        "limit_up_down_ratio": safe_ratio(limit_up_count, limit_down_count),
    }


def sample_to_dict(
    trade_date: date,
    result: MarketRegimeResult,
    regime_input: MarketRegimeInput,
    breadth_metrics: Mapping[str, Any],
) -> JsonDict:
    """把单个样本转换为报告友好的结构。"""

    return {
        "as_of": trade_date.isoformat(),
        "regime": result.regime,
        "strength": result.strength,
        "risk_multiplier": result.risk_multiplier,
        "index_trend_20d": regime_input.index_trend_20d,
        "index_trend_60d": regime_input.index_trend_60d,
        "volatility_20d": regime_input.volatility_20d,
        "advance_decline_ratio": regime_input.advance_decline_ratio,
        "limit_up_down_ratio": regime_input.limit_up_down_ratio,
        "sample_size": int(breadth_metrics.get("sample_size") or 0),
        "evidence_ids": list(result.evidence_ids),
        "reasons": list(result.reasons),
    }


def render_markdown_report(report: Mapping[str, Any]) -> str:
    """渲染 Markdown 验收材料。"""

    samples = report.get("samples") or {}
    lines = [
        "# A 股大盘环境样本验收材料",
        "",
        f"- 生成时间：{report.get('generated_at')}",
        f"- 指数资产：`{report.get('index_asset_id')}`",
        f"- 指数口径：`{report.get('index_source')}`",
        f"- 市场/周期：`{report.get('market')}` / `{report.get('timeframe')}`",
        f"- 日期范围：{(report.get('date_range') or {}).get('start')} ~ {(report.get('date_range') or {}).get('end')}",
        "",
    ]
    for regime, title in (("bear", "弱市样本"), ("range", "震荡市样本"), ("bull", "强市样本")):
        lines.extend([f"## {title}", ""])
        items = list((samples or {}).get(regime) or [])
        if not items:
            lines.extend(["暂无满足条件的样本。", ""])
            continue
        lines.append(
            "| 日期 | 状态 | 强度 | 20日趋势 | 60日趋势 | 20日波动 | 涨跌家数比 | 涨跌停比 | 样本数 |"
        )
        lines.append("| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
        for item in items:
            lines.append(
                (
                    "| {as_of} | {regime} | {strength} | {t20:.2%} | {t60:.2%} | {vol:.2%} "
                    "| {adr:.2f} | {lud:.2f} | {size} |"
                ).format(
                    as_of=item["as_of"],
                    regime=item["regime"],
                    strength=item["strength"],
                    t20=float(item["index_trend_20d"]),
                    t60=float(item["index_trend_60d"]),
                    vol=float(item["volatility_20d"]),
                    adr=float(item["advance_decline_ratio"]),
                    lud=float(item["limit_up_down_ratio"]),
                    size=int(item["sample_size"]),
                )
            )
        lines.append("")
    return "\n".join(lines)


def mean(values: Sequence[float]) -> float:
    """计算均值。"""

    if not values:
        raise ValueError("均值输入不能为空")
    return sum(values) / len(values)


def ratio_change(current: float, previous: float) -> float:
    """计算相对变化率。"""

    if previous == 0:
        return 0.0
    return current / previous - 1


def safe_ratio(numerator: int, denominator: int) -> float:
    """计算计数比率，避免零除。"""

    if denominator == 0:
        return float(numerator) if numerator else 1.0
    return round(numerator / denominator, 6)


def parse_date(value: str) -> date:
    """解析 YYYY-MM-DD 日期。"""

    return datetime.strptime(value, "%Y-%m-%d").date()


if __name__ == "__main__":
    main()
