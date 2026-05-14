"""Binance 原生衍生品数据冒烟验证脚本。

该脚本只读取公开行情，不需要密钥，不包含账户或下单逻辑。
公网接口可能因为网络、地区或限流失败；失败时 Provider 应返回结构化
`error`，不能让异常穿透到推荐链路。
"""

from __future__ import annotations

from finance_agent.data.providers import BinanceNativeProvider
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.repositories import AssetRepository, DerivativeDataRepository


def main() -> None:
    """拉取 BTCUSDT U 本位合约衍生品快照并写入数据库。"""

    provider = BinanceNativeProvider()
    result = provider.fetch_derivative_snapshot(symbol="BTCUSDT")

    saved = False
    latest_as_of = None
    if result.snapshot:
        snapshot = result.snapshot
        session_factory = create_session_factory()
        with session_scope(session_factory) as session:
            assets = AssetRepository(session)
            derivatives = DerivativeDataRepository(session)

            assets.upsert_asset(
                asset_id=snapshot.asset_id,
                symbol=snapshot.symbol,
                name="Bitcoin / USDT Perpetual",
                market=snapshot.market,
                asset_type="crypto",
                exchange="Binance",
                currency="USDT",
                base_asset="BTC",
                quote_asset="USDT",
                payload={"source": "binance_native_smoke"},
            )
            derivatives.upsert_crypto_derivative_snapshot(
                snapshot_id=snapshot.snapshot_id,
                asset_id=snapshot.asset_id,
                symbol=snapshot.symbol,
                market=snapshot.market,
                source=snapshot.source,
                as_of=snapshot.as_of,
                funding_rate=snapshot.funding_rate,
                next_funding_time=snapshot.next_funding_time,
                open_interest=snapshot.open_interest,
                open_interest_value=snapshot.open_interest_value,
                long_short_ratio=snapshot.long_short_ratio,
                basis_rate=snapshot.basis_rate,
                liquidation_risk_score=snapshot.liquidation_risk_score,
                status=snapshot.status,
                payload=snapshot.payload,
            )
            latest = derivatives.get_latest_snapshot(
                asset_id=snapshot.asset_id,
                source=snapshot.source,
            )
            saved = latest is not None
            latest_as_of = latest.as_of.isoformat() if latest else None

    print(
        {
            "binance_native_status": result.status,
            "binance_native_error": result.error_message,
            "snapshot_saved": saved,
            "latest_as_of": latest_as_of,
            "symbol": result.payload.get("symbol"),
        }
    )


if __name__ == "__main__":
    main()
