"""Binance 原生公开行情 Provider。

ccxt 适合获取跨交易所通用的市场、K 线和 ticker。资金费率、未平仓量、
多空比这类 Binance U 本位合约专属数据，直接调用 Binance REST API 更清晰。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import requests

from finance_agent.data.models import CryptoDerivativeSnapshotResult
from finance_agent.data.normalizers import normalize_binance_derivative_snapshot


class BinanceNativeProvider:
    """Binance 原生公开行情 Provider，不提供账户和下单能力。"""

    provider_name = "binance_native"

    def __init__(
        self,
        *,
        base_url: str = "https://fapi.binance.com",
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()

    def fetch_derivative_snapshot(self, *, symbol: str) -> CryptoDerivativeSnapshotResult:
        """获取 U 本位合约资金费率、未平仓量和多空比快照。"""

        compact_symbol = symbol.replace("/", "").upper()
        collected_at = datetime.now(tz=UTC)
        try:
            premium_index = self._get_json(
                "/fapi/v1/premiumIndex",
                params={"symbol": compact_symbol},
            )
            open_interest = self._get_json(
                "/fapi/v1/openInterest",
                params={"symbol": compact_symbol},
            )
            long_short_ratio_rows = self._get_json(
                "/futures/data/globalLongShortAccountRatio",
                params={"symbol": compact_symbol, "period": "5m", "limit": 1},
            )
            long_short_ratio = (
                long_short_ratio_rows[-1]
                if isinstance(long_short_ratio_rows, list) and long_short_ratio_rows
                else {}
            )
            snapshot = normalize_binance_derivative_snapshot(
                symbol=compact_symbol,
                source=self.provider_name,
                premium_index=premium_index if isinstance(premium_index, dict) else {},
                open_interest=open_interest if isinstance(open_interest, dict) else {},
                long_short_ratio=long_short_ratio
                if isinstance(long_short_ratio, dict)
                else {},
                collected_at=collected_at,
            )
        except Exception as exc:
            return CryptoDerivativeSnapshotResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
                payload={"symbol": compact_symbol},
            )

        return CryptoDerivativeSnapshotResult(
            provider_name=self.provider_name,
            status=snapshot.status,
            collected_at=collected_at,
            snapshot=snapshot,
            payload={
                "symbol": compact_symbol,
                "source": self.provider_name,
                "endpoints": [
                    "/fapi/v1/premiumIndex",
                    "/fapi/v1/openInterest",
                    "/futures/data/globalLongShortAccountRatio",
                ],
            },
        )

    def _get_json(self, path: str, *, params: Mapping[str, Any]) -> Any:
        """调用 Binance REST API 并返回 JSON。"""

        response = self.session.get(
            f"{self.base_url}{path}",
            params=params,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def health_check(self) -> dict[str, Any]:
        """轻量健康检查。"""

        result = self.fetch_derivative_snapshot(symbol="BTCUSDT")
        return {
            "provider_name": self.provider_name,
            "status": result.status,
            "error_message": result.error_message,
        }
