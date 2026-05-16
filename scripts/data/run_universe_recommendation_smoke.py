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
from datetime import UTC, datetime
from typing import Any

from run_indicator_smoke import seed_sample_bars

from finance_agent.pipelines import UniverseRecommendationPipeline
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.repositories import UniverseRepository

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
