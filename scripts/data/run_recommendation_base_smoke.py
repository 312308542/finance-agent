"""推荐基础链路冒烟验证。

脚本串起第一版确定性链路：

`market_bars -> indicator_frames -> factor_frames -> screening_results
-> asset_scores -> signal_snapshots`
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from typing import Any

from run_indicator_smoke import seed_sample_bars

from finance_agent.factors import FactorService
from finance_agent.indicators import IndicatorService
from finance_agent.scoring import ScoringService
from finance_agent.screening import ScreeningService
from finance_agent.signals import SignalService
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.repositories import UniverseRepository

JsonDict = dict[str, Any]


def main() -> None:
    """执行推荐基础链路冒烟验证。"""

    args = parse_args()
    summary = run_recommendation_base_smoke(args)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="串联初筛、评分和信号基础链路")
    parser.add_argument("--asset-id", default="crypto_spot:BTCUSDT", help="资产 ID")
    parser.add_argument("--symbol", default="BTCUSDT", help="交易代码")
    parser.add_argument("--market", default="crypto_spot", help="市场")
    parser.add_argument("--timeframe", default="1h", help="K 线周期")
    parser.add_argument("--horizon", default="swing", help="推荐周期")
    parser.add_argument("--source", default="indicator_smoke", help="K 线来源")
    parser.add_argument("--bar-count", type=int, default=80, help="样例 K 线数量")
    parser.add_argument(
        "--universe-id",
        default="universe:smoke:crypto_spot",
        help="冒烟候选池 ID",
    )
    parser.add_argument("--strategy", default="balanced_swing_v1", help="初筛和评分策略")
    parser.add_argument("--skip-seed", action="store_true", help="不写入样例 K 线")
    return parser.parse_args()


def run_recommendation_base_smoke(args: argparse.Namespace) -> JsonDict:
    """运行可重复的推荐基础链路冒烟验证。"""

    session_factory = create_session_factory()
    with session_scope(session_factory) as session:
        if not args.skip_seed:
            seed_sample_bars(
                session=session,
                asset_id=args.asset_id,
                symbol=args.symbol,
                market=args.market,
                timeframe=args.timeframe,
                source=args.source,
                bar_count=args.bar_count,
            )
        seed_smoke_universe(
            session=session,
            universe_id=args.universe_id,
            asset_id=args.asset_id,
            symbol=args.symbol,
            market=args.market,
        )

        indicator = IndicatorService(session).compute_for_asset(
            asset_id=args.asset_id,
            timeframe=args.timeframe,
            horizon=args.horizon,
            source=args.source,
            window=args.bar_count,
        )
        factor = FactorService(session).compute_for_asset(
            asset_id=args.asset_id,
            timeframe=args.timeframe,
            horizon=args.horizon,
        )
        screening = ScreeningService(session).apply_rules(
            universe_id=args.universe_id,
            strategy=args.strategy,
            horizon=args.horizon,
        )
        scoring = ScoringService(session).score_screening(
            screening_id=screening.screening_id,
            horizon=args.horizon,
        )
        signal = SignalService(session).compute_for_asset(
            asset_id=args.asset_id,
            horizon=args.horizon,
        )

    return {
        "indicator": indicator.__dict__,
        "factor": factor.__dict__,
        "screening": screening.__dict__,
        "scoring": scoring.__dict__,
        "signal": signal.__dict__,
    }


def seed_smoke_universe(
    *,
    session: Any,
    universe_id: str,
    asset_id: str,
    symbol: str,
    market: str,
) -> None:
    """写入冒烟候选池和成员。"""

    universes = UniverseRepository(session)
    as_of = datetime.now(tz=UTC)
    universes.upsert_universe(
        universe_id=universe_id,
        name="推荐基础链路冒烟候选池",
        source="recommendation_base_smoke",
        market=market,
        strategy_context="balanced_swing_v1",
        total_before_filter=1,
        total_after_filter=1,
        as_of=as_of,
        payload={"purpose": "recommendation_base_smoke"},
    )
    universes.upsert_member(
        member_id=f"{universe_id}:{asset_id}",
        universe_id=universe_id,
        asset_id=asset_id,
        symbol=symbol,
        market=market,
        rank_hint=1,
        as_of=as_of,
        payload={"source": "recommendation_base_smoke"},
    )


if __name__ == "__main__":
    main()
