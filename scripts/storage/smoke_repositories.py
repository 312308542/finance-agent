"""仓储层数据库冒烟验证脚本。

用途：
- 写入 A 股和数字货币样例资产。
- 写入一个混合候选池。
- 写入 A 股日线和数字货币 1h K 线。
- 查询候选池成员和最近 K 线，验证 Repository 层和 TimescaleDB 表可用。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.repositories import (
    AssetRepository,
    MarketDataRepository,
    UniverseRepository,
)


def main() -> None:
    """执行一次可重复运行的仓储冒烟验证。"""

    session_factory = create_session_factory()
    as_of = datetime(2026, 5, 14, 15, 0, tzinfo=UTC)

    with session_scope(session_factory) as session:
        assets = AssetRepository(session)
        universes = UniverseRepository(session)
        market_data = MarketDataRepository(session)

        assets.upsert_asset(
            asset_id="ashare:600519",
            symbol="600519",
            name="贵州茅台",
            market="ashare",
            asset_type="stock",
            exchange="SSE",
            currency="CNY",
            sector="食品饮料",
            payload={"source": "smoke"},
        )
        assets.upsert_asset(
            asset_id="crypto_spot:BTCUSDT",
            symbol="BTCUSDT",
            name="Bitcoin / USDT",
            market="crypto_spot",
            asset_type="crypto",
            exchange="Binance",
            currency="USDT",
            base_asset="BTC",
            quote_asset="USDT",
            payload={"source": "smoke"},
        )

        universes.upsert_universe(
            universe_id="universe:smoke:mixed:20260514",
            name="冒烟验证混合候选池",
            source="manual:smoke",
            market="mixed",
            strategy_context="smoke_test",
            as_of=as_of,
            total_before_filter=2,
            total_after_filter=2,
            payload={"note": "用于验证仓储层写入和查询"},
        )
        universes.replace_members(
            universe_id="universe:smoke:mixed:20260514",
            members=[
                {
                    "member_id": "universe_member:smoke:ashare:600519",
                    "asset_id": "ashare:600519",
                    "symbol": "600519",
                    "market": "ashare",
                    "as_of": as_of,
                    "rank_hint": 1,
                },
                {
                    "member_id": "universe_member:smoke:crypto_spot:BTCUSDT",
                    "asset_id": "crypto_spot:BTCUSDT",
                    "symbol": "BTCUSDT",
                    "market": "crypto_spot",
                    "as_of": as_of,
                    "rank_hint": 2,
                },
            ],
        )

        market_data.upsert_bar(
            asset_id="ashare:600519",
            symbol="600519",
            market="ashare",
            timeframe="1d",
            timestamp=datetime(2026, 5, 14, 0, 0, tzinfo=UTC),
            open_price=Decimal("1700.00"),
            high=Decimal("1728.50"),
            low=Decimal("1690.20"),
            close=Decimal("1715.60"),
            volume=Decimal("2600000"),
            amount=Decimal("4450000000"),
            source="smoke",
            adjustment="qfq",
        )
        market_data.upsert_bar(
            asset_id="crypto_spot:BTCUSDT",
            symbol="BTCUSDT",
            market="crypto_spot",
            timeframe="1h",
            timestamp=datetime(2026, 5, 14, 7, 0, tzinfo=UTC),
            end_timestamp=datetime(2026, 5, 14, 8, 0, tzinfo=UTC),
            open_price=Decimal("64200.00"),
            high=Decimal("64850.00"),
            low=Decimal("63920.00"),
            close=Decimal("64680.00"),
            volume=Decimal("1280.50000000"),
            amount=Decimal("82600000.00"),
            source="smoke",
        )
        market_data.upsert_bar(
            asset_id="crypto_spot:BTCUSDT",
            symbol="BTCUSDT",
            market="crypto_spot",
            timeframe="1h",
            timestamp=datetime(2026, 5, 14, 8, 0, tzinfo=UTC),
            end_timestamp=datetime(2026, 5, 14, 9, 0, tzinfo=UTC),
            open_price=Decimal("64680.00"),
            high=Decimal("65120.00"),
            low=Decimal("64510.00"),
            close=Decimal("65010.00"),
            volume=Decimal("960.25000000"),
            amount=Decimal("62300000.00"),
            source="smoke",
        )

        members = universes.list_members("universe:smoke:mixed:20260514")
        btc_bars = market_data.list_recent_bars(
            asset_id="crypto_spot:BTCUSDT",
            timeframe="1h",
            source="smoke",
            limit=2,
        )
        window_bars = market_data.list_window_bars(
            asset_ids=[member.asset_id for member in members],
            timeframe="1h",
            start_at=as_of - timedelta(days=1),
            end_at=as_of + timedelta(days=1),
            source="smoke",
        )

    print(
        {
            "members": [member.asset_id for member in members],
            "btc_recent_closes": [str(bar.close) for bar in btc_bars],
            "window_bar_count": len(window_bars),
        }
    )


if __name__ == "__main__":
    main()
