"""因子计算服务冒烟验证。

脚本先确保 `indicator_frames` 有可用技术指标，再调用 FactorService 合并
指标和基础快照，写入 `factor_frames`。
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from run_indicator_smoke import seed_sample_bars

from finance_agent.factors import FactorService
from finance_agent.indicators import IndicatorService
from finance_agent.storage.db import create_session_factory, session_scope

JsonDict = dict[str, Any]


def main() -> None:
    """执行因子计算冒烟验证。"""

    args = parse_args()
    summary = run_factor_smoke(args)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="计算单标的推荐因子并写入 factor_frames")
    parser.add_argument("--asset-id", default="crypto_spot:BTCUSDT", help="资产 ID")
    parser.add_argument("--symbol", default="BTCUSDT", help="交易代码")
    parser.add_argument("--market", default="crypto_spot", help="市场")
    parser.add_argument("--timeframe", default="1h", help="K 线周期")
    parser.add_argument("--horizon", default="swing", help="推荐周期")
    parser.add_argument("--source", default="indicator_smoke", help="K 线来源")
    parser.add_argument("--bar-count", type=int, default=80, help="样例 K 线数量")
    parser.add_argument("--skip-seed", action="store_true", help="不写入样例 K 线")
    return parser.parse_args()


def run_factor_smoke(args: argparse.Namespace) -> JsonDict:
    """运行可重复的因子服务冒烟验证。"""

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
        factor_frame = FactorService(session).factors.get_latest_factor_frame(
            asset_id=args.asset_id,
            horizon=args.horizon,
        )
        factor_groups = (
            list(factor_frame.payload.get("factor_groups") or []) if factor_frame else []
        )

    return {
        "indicator": indicator.__dict__,
        "factor": factor.__dict__,
        "factor_groups": factor_groups,
    }


if __name__ == "__main__":
    main()
