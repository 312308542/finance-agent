"""Binance 原生公开行情 Provider。

ccxt 适合获取跨交易所通用的市场、K 线和 ticker。资金费率、未平仓量、
多空比这类 Binance U 本位合约专属数据，直接调用 Binance REST API 更清晰。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import requests

from finance_agent.application.data_production_service import BinanceRateLimitPolicy
from finance_agent.data.models import CryptoDerivativeSnapshotResult
from finance_agent.data.normalizers import normalize_binance_derivative_snapshot


class BinanceNativeProvider:
    """Binance 原生公开行情 Provider，不提供账户和下单能力。"""

    provider_name = "binance_native"

    def __init__(
        self,
        *,
        base_url: str = "https://fapi.binance.com",
        base_urls: tuple[str, ...] | None = None,
        timeout_seconds: float = 10.0,
        max_retries: int | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        candidate_urls = base_urls or (
            self.base_url,
            "https://fapi1.binance.com",
            "https://fapi2.binance.com",
            "https://fapi3.binance.com",
        )
        self.rate_limit_policy = BinanceRateLimitPolicy(base_urls=candidate_urls)
        self.base_urls = self.rate_limit_policy.base_urls
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries or len(self.base_urls)
        self.session = requests.Session()
        self.last_request_trace: list[dict[str, Any]] = []

    def fetch_derivative_snapshot(self, *, symbol: str) -> CryptoDerivativeSnapshotResult:
        """获取 U 本位合约资金费率、未平仓量和多空比快照。"""

        compact_symbol = symbol.replace("/", "").upper()
        collected_at = datetime.now(tz=UTC)
        request_trace: list[dict[str, Any]] = []
        try:
            premium_index = self._get_json(
                "/fapi/v1/premiumIndex",
                params={"symbol": compact_symbol},
            )
            request_trace.extend(self.last_request_trace)
            open_interest = self._get_json(
                "/fapi/v1/openInterest",
                params={"symbol": compact_symbol},
            )
            request_trace.extend(self.last_request_trace)
            long_short_ratio_rows = self._get_json(
                "/futures/data/globalLongShortAccountRatio",
                params={"symbol": compact_symbol, "period": "5m", "limit": 1},
            )
            request_trace.extend(self.last_request_trace)
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
                payload={
                    "symbol": compact_symbol,
                    "rate_limited": self.rate_limit_policy.is_rate_limited(exc),
                    "request_trace": request_trace + self.last_request_trace,
                },
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
                "request_trace": request_trace,
            },
        )

    def _get_json(self, path: str, *, params: Mapping[str, Any]) -> Any:
        """调用 Binance REST API 并在限流时切换备用端点。"""

        base_url = self.base_url
        last_error: Exception | None = None
        self.last_request_trace = []
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.get(
                    f"{base_url}{path}",
                    params=params,
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                self.base_url = base_url
                self.last_request_trace.append(
                    {
                        "path": path,
                        "base_url": base_url,
                        "attempt": attempt,
                        "status": "available",
                    }
                )
                return response.json()
            except requests.HTTPError as exc:
                last_error = exc
                retry_after = None
                if exc.response is not None:
                    retry_after = exc.response.headers.get("Retry-After")
                decision = self.rate_limit_policy.plan_retry(
                    exc,
                    current_base_url=base_url,
                    attempt=attempt,
                    retry_after_header=retry_after,
                )
                self.last_request_trace.append(
                    {
                        "path": path,
                        "base_url": base_url,
                        "attempt": attempt,
                        "status": "rate_limited" if decision.is_rate_limited else "error",
                        "error_message": str(exc),
                        "next_base_url": decision.next_base_url,
                    }
                )
                if not decision.should_retry or decision.next_base_url is None:
                    raise
                base_url = decision.next_base_url
            except requests.RequestException as exc:
                last_error = exc
                decision = self.rate_limit_policy.plan_retry(
                    exc,
                    current_base_url=base_url,
                    attempt=attempt,
                )
                self.last_request_trace.append(
                    {
                        "path": path,
                        "base_url": base_url,
                        "attempt": attempt,
                        "status": "rate_limited" if decision.is_rate_limited else "error",
                        "error_message": str(exc),
                        "next_base_url": decision.next_base_url,
                    }
                )
                if not decision.should_retry or decision.next_base_url is None:
                    raise
                base_url = decision.next_base_url
        if last_error is not None:
            raise last_error
        raise RuntimeError("Binance 请求未执行")

    def health_check(self) -> dict[str, Any]:
        """轻量健康检查。"""

        result = self.fetch_derivative_snapshot(symbol="BTCUSDT")
        return {
            "provider_name": self.provider_name,
            "status": result.status,
            "error_message": result.error_message,
        }
