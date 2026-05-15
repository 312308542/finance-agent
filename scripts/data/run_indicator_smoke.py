"""指标计算服务冒烟验证。

默认写入一组确定性的 BTCUSDT 1h 样例 K 线，然后调用 IndicatorService
计算技术指标并写入 `indicator_frames`。
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from finance_agent.indicators import IndicatorService
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.repositories import AssetRepository, MarketDataRepository

JsonDict = dict[str, Any]


def main() -> None:
    """执行指标计算冒烟验证。"""

    args = parse_args()
    summary = run_indicator_smoke(args)
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="计算单标的技术指标并写入 indicator_frames")
    parser.add_argument("--asset-id", default="crypto_spot:BTCUSDT", help="资产 ID")
    parser.add_argument("--symbol", default="BTCUSDT", help="交易代码")
    parser.add_argument("--market", default="crypto_spot", help="市场")
    parser.add_argument("--timeframe", default="1h", help="K 线周期")
    parser.add_argument("--horizon", default="swing", help="推荐周期")
    parser.add_argument("--source", default="indicator_smoke", help="K 线来源")
    parser.add_argument("--bar-count", type=int, default=80, help="样例 K 线数量")
    parser.add_argument(
        "--skip-seed",
        action="store_true",
        help="不写入样例 K 线，直接使用库中已有数据计算",
    )
    return parser.parse_args()


def run_indicator_smoke(args: argparse.Namespace) -> JsonDict:
    """运行可重复的指标服务冒烟验证。"""

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

        result = IndicatorService(session).compute_for_asset(
            asset_id=args.asset_id,
            timeframe=args.timeframe,
            horizon=args.horizon,
            source=args.source,
            window=args.bar_count,
        )

    return {
        "status": result.status,
        "indicator_frame_id": result.indicator_frame_id,
        "asset_id": result.asset_id,
        "symbol": result.symbol,
        "market": result.market,
        "timeframe": result.timeframe,
        "horizon": result.horizon,
        "bar_count": result.bar_count,
        "missing_indicators": list(result.missing_indicators),
        "error_message": result.error_message,
    }


def seed_sample_bars(
    *,
    session: Any,
    asset_id: str,
    symbol: str,
    market: str,
    timeframe: str,
    source: str,
    bar_count: int,
) -> None:
    """写入确定性样例 K 线，确保 TA-Lib 指标有足够窗口。"""

    if bar_count < 60:
        raise ValueError("样例 K 线至少需要 60 根，才能覆盖 MA60 和 MACD")

    assets = AssetRepository(session)
    market_data = MarketDataRepository(session)
    assets.upsert_asset(
        asset_id=asset_id,
        symbol=symbol,
        name="Bitcoin / USDT" if symbol == "BTCUSDT" else symbol,
        market=market,
        asset_type="crypto" if market.startswith("crypto") else "stock",
        exchange="Binance" if market.startswith("crypto") else None,
        currency="USDT" if market.startswith("crypto") else "CNY",
        base_asset="BTC" if symbol == "BTCUSDT" else None,
        quote_asset="USDT" if symbol == "BTCUSDT" else None,
        payload={"source": source, "purpose": "indicator_smoke"},
    )

    start_at = datetime(2026, 5, 12, 0, 0, tzinfo=UTC)
    for index in range(bar_count):
        timestamp = start_at + timedelta(hours=index)
        close = Decimal("60000") + Decimal(index * 35) + Decimal((index % 7) * 9)
        open_price = close - Decimal("18")
        high = close + Decimal("45")
        low = close - Decimal("55")
        volume = Decimal("1000") + Decimal(index * 3)
        amount = close * volume
        market_data.upsert_bar(
            asset_id=asset_id,
            symbol=symbol,
            market=market,
            timeframe=timeframe,
            timestamp=timestamp,
            end_timestamp=timestamp + timedelta(hours=1),
            open_price=open_price,
            high=high,
            low=low,
            close=close,
            volume=volume,
            amount=amount,
            source=source,
            adjustment="",
            is_closed=True,
            status="available",
        )


if __name__ == "__main__":
    main()
