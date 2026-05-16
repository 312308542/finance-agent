"""候选池级推荐流水线冒烟验证。

脚本会按市场写入一个同市场样例候选池，并通过
`UniverseRecommendationPipeline` 执行：

`指标 -> 因子 -> 初筛 -> 评分 -> 信号 -> 推荐排序`

A 股和数字货币需要分别运行本脚本，不能使用 mixed 候选池。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from run_indicator_smoke import seed_sample_bars

from finance_agent.pipelines import UniverseRecommendationPipeline
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.repositories import (
    CapitalFlowRepository,
    DerivativeDataRepository,
    FundamentalDataRepository,
    UniverseRepository,
)

JsonDict = dict[str, Any]


def main() -> None:
    """执行候选池级推荐流水线冒烟验证。"""

    args = parse_args()
    summary = run_universe_recommendation_smoke(args)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="按候选池批量执行推荐基础链路")
    parser.add_argument("--market", default="crypto_spot", help="市场：ashare 或 crypto_spot")
    parser.add_argument("--universe-id", default=None, help="候选池 ID，默认按市场生成")
    parser.add_argument("--timeframe", default=None, help="K 线周期，默认 A 股 1d、数字货币 1h")
    parser.add_argument("--horizon", default="swing", help="推荐周期")
    parser.add_argument("--strategy", default="balanced_swing_v1", help="初筛和评分策略")
    parser.add_argument("--source", default="universe_pipeline_smoke", help="样例 K 线来源")
    parser.add_argument("--bar-count", type=int, default=80, help="样例 K 线数量")
    parser.add_argument("--window", type=int, default=80, help="指标计算窗口")
    parser.add_argument("--limit", type=int, default=10, help="推荐结果数量上限")
    parser.add_argument(
        "--skip-seed",
        action="store_true",
        help="不写入样例资产和 K 线，直接使用数据库内已有候选池",
    )
    return parser.parse_args()


def run_universe_recommendation_smoke(args: argparse.Namespace) -> JsonDict:
    """运行可重复的候选池级推荐流水线冒烟验证。"""

    market = normalize_market(args.market)
    universe_id = args.universe_id or f"universe:smoke:{market}:batch"
    timeframe = args.timeframe or ("1h" if market.startswith("crypto") else "1d")
    session_factory = create_session_factory()
    with session_scope(session_factory) as session:
        members = sample_members(market)
        if not args.skip_seed:
            seed_universe(
                session=session,
                universe_id=universe_id,
                market=market,
                members=members,
                timeframe=timeframe,
                source=args.source,
                bar_count=args.bar_count,
            )

        result = UniverseRecommendationPipeline(session).run_for_universe(
            universe_id=universe_id,
            strategy=args.strategy,
            horizon=args.horizon,
            timeframe=timeframe,
            source=args.source if not args.skip_seed else None,
            window=args.window,
            limit=args.limit,
        )

    return asdict(result)


def seed_universe(
    *,
    session: Any,
    universe_id: str,
    market: str,
    members: list[JsonDict],
    timeframe: str,
    source: str,
    bar_count: int,
) -> None:
    """写入同市场样例候选池和样例 K 线。"""

    as_of = datetime.now(tz=UTC)
    universes = UniverseRepository(session)
    universes.upsert_universe(
        universe_id=universe_id,
        name=f"{market} 批量推荐流水线冒烟候选池",
        source="universe_pipeline_smoke",
        market=market,
        strategy_context="balanced_swing_v1",
        total_before_filter=len(members),
        total_after_filter=len(members),
        as_of=as_of,
        payload={"purpose": "universe_recommendation_pipeline_smoke"},
    )
    universes.replace_members(
        universe_id=universe_id,
        members=[
            {
                "member_id": f"universe_member:{universe_id}:{member['asset_id']}",
                "asset_id": member["asset_id"],
                "symbol": member["symbol"],
                "market": market,
                "rank_hint": index,
                "as_of": as_of,
                "payload": {"source": "universe_pipeline_smoke"},
            }
            for index, member in enumerate(members, start=1)
        ],
    )

    for member in members:
        seed_sample_bars(
            session=session,
            asset_id=str(member["asset_id"]),
            symbol=str(member["symbol"]),
            market=market,
            timeframe=timeframe,
            source=source,
            bar_count=bar_count,
        )
        seed_factor_snapshots(
            session=session,
            market=market,
            asset_id=str(member["asset_id"]),
            symbol=str(member["symbol"]),
            source=source,
            as_of=as_of,
        )


def seed_factor_snapshots(
    *,
    session: Any,
    market: str,
    asset_id: str,
    symbol: str,
    source: str,
    as_of: datetime,
) -> None:
    """写入因子服务需要的样例历史快照。"""

    if market == "ashare":
        seed_ashare_factor_snapshots(
            session=session,
            asset_id=asset_id,
            symbol=symbol,
            source=source,
            as_of=as_of,
        )
        return
    if market.startswith("crypto"):
        seed_crypto_derivative_snapshots(
            session=session,
            asset_id=asset_id,
            symbol=symbol,
            market=market,
            source=source,
            as_of=as_of,
        )


def seed_ashare_factor_snapshots(
    *,
    session: Any,
    asset_id: str,
    symbol: str,
    source: str,
    as_of: datetime,
) -> None:
    """写入 A 股估值历史和资金流历史样例。"""

    fundamentals = FundamentalDataRepository(session)
    capital_flows = CapitalFlowRepository(session)
    for index in range(8):
        item_as_of = as_of - timedelta(days=7 - index)
        pe_ttm = Decimal("18") + Decimal(index) * Decimal("1.6")
        pb = Decimal("2.1") + Decimal(index) * Decimal("0.12")
        fundamentals.upsert_fundamental_snapshot(
            snapshot_id=f"fundamental:{asset_id}:{source}:{item_as_of:%Y%m%d}",
            asset_id=asset_id,
            symbol=symbol,
            source=source,
            status="available",
            as_of=item_as_of,
            report_period="2026Q1",
            pe_ttm=pe_ttm,
            pb=pb,
            roe=Decimal("0.16") + Decimal(index) * Decimal("0.002"),
            revenue_growth_yoy=Decimal("0.08") + Decimal(index) * Decimal("0.005"),
            net_profit_growth_yoy=Decimal("0.10") + Decimal(index) * Decimal("0.004"),
            debt_to_asset=Decimal("0.42"),
            operating_cashflow=Decimal("1200000000") + Decimal(index) * Decimal("10000000"),
            payload={
                "source": "universe_recommendation_pipeline_smoke",
                "dividend_yield": 0.025 + index * 0.001,
            },
        )

        inflow_sign = Decimal("1") if index in {0, 2, 4, 5, 6, 7} else Decimal("-1")
        flow_price_divergence = Decimal("0.01") if index >= 5 else Decimal("-0.005")
        capital_flows.upsert_capital_flow_snapshot(
            snapshot_id=f"capital_flow:{asset_id}:{source}:{item_as_of:%Y%m%d}",
            asset_id=asset_id,
            symbol=symbol,
            market="ashare",
            window="5d",
            source=source,
            status="available",
            as_of=item_as_of,
            main_net_inflow=inflow_sign * (Decimal("18000000") + Decimal(index * 1000000)),
            turnover_rate=Decimal("0.018") + Decimal(index) * Decimal("0.001"),
            amount=Decimal("700000000") + Decimal(index * 12000000),
            payload={
                "source": "universe_recommendation_pipeline_smoke",
                "rank_hint": index + 1,
                "rank_total": 20,
                "flow_price_divergence": str(flow_price_divergence),
            },
        )


def seed_crypto_derivative_snapshots(
    *,
    session: Any,
    asset_id: str,
    symbol: str,
    market: str,
    source: str,
    as_of: datetime,
) -> None:
    """写入数字货币衍生品窗口样例。"""

    derivatives = DerivativeDataRepository(session)
    base_open_interest = Decimal("120000")
    for index in range(30):
        item_as_of = as_of - timedelta(hours=29 - index)
        funding_rate = Decimal("-0.00005") + Decimal(index % 8) * Decimal("0.000015")
        open_interest = base_open_interest + Decimal(index * 850)
        derivatives.upsert_crypto_derivative_snapshot(
            snapshot_id=f"crypto_derivative:{asset_id}:{source}:{item_as_of:%Y%m%dT%H%M%S}",
            asset_id=asset_id,
            symbol=symbol,
            market=market,
            source=source,
            as_of=item_as_of,
            funding_rate=funding_rate,
            open_interest=open_interest,
            open_interest_value=open_interest * Decimal("60000"),
            long_short_ratio=Decimal("1.05") + Decimal(index % 5) * Decimal("0.015"),
            basis_rate=Decimal("0.0008") + Decimal(index % 4) * Decimal("0.0001"),
            status="available",
            payload={"source": "universe_recommendation_pipeline_smoke"},
        )


def sample_members(market: str) -> list[JsonDict]:
    """返回同市场样例成员。"""

    if market.startswith("crypto"):
        return [
            {"asset_id": "crypto_spot:BTCUSDT", "symbol": "BTCUSDT"},
            {"asset_id": "crypto_spot:ETHUSDT", "symbol": "ETHUSDT"},
        ]
    return [
        {"asset_id": "ashare:600519", "symbol": "600519"},
        {"asset_id": "ashare:000001", "symbol": "000001"},
    ]


def normalize_market(value: str) -> str:
    """规范化命令行市场值。"""

    aliases = {
        "crypto": "crypto_spot",
        "spot": "crypto_spot",
        "a": "ashare",
        "a股": "ashare",
    }
    market = aliases.get(value.lower(), value.lower())
    if market not in {"ashare", "crypto_spot", "crypto_future"}:
        raise ValueError("当前冒烟脚本只支持 ashare、crypto_spot、crypto_future")
    if market == "crypto_future":
        raise ValueError("crypto_future 需要衍生品专用样例，当前冒烟脚本先覆盖 crypto_spot")
    return market


if __name__ == "__main__":
    main()
