"""ccxt Binance 数字货币数据 Provider。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import ccxt

from finance_agent.application.data_production_service import BinanceRateLimitPolicy
from finance_agent.data.models import AssetListResult, MarketBarsResult
from finance_agent.data.normalizers import normalize_crypto_markets, normalize_crypto_ohlcv


class CcxtBinanceProvider:
    """Binance 行情 Provider。

    本类只读取行情和市场信息，不提供下单能力。
    """

    provider_name = "ccxt_binance"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        default_type: str = "spot",
    ) -> None:
        self.default_type = default_type
        exchange_factory = ccxt.binanceusdm if default_type in {"future", "swap"} else ccxt.binance
        self.exchange = exchange_factory(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": True,
                "options": {"defaultType": default_type},
            }
        )
        self.rate_limit_policy = BinanceRateLimitPolicy(
            base_urls=("https://api.binance.com", "https://api1.binance.com", "https://api2.binance.com")
        )

    def fetch_assets(self, *, limit: int | None = None) -> AssetListResult:
        """获取 Binance 交易对列表。"""

        collected_at = datetime.now(tz=UTC)
        try:
            markets = self.exchange.load_markets()
            assets = normalize_crypto_markets(
                markets,
                limit=limit,
                market_type=self.default_type,
            )
        except Exception as exc:
            return AssetListResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
                payload={
                    "default_type": self.default_type,
                    "rate_limited": self.rate_limit_policy.is_rate_limited(exc),
                },
            )
        return AssetListResult(
            provider_name=self.provider_name,
            status="available" if assets else "unavailable",
            collected_at=collected_at,
            assets=assets,
            payload={"row_count": len(assets), "default_type": self.default_type},
        )

    def fetch_ohlcv(
        self,
        *,
        symbol: str,
        timeframe: str,
        start: str | None = None,
        end: str | None = None,
        limit: int | None = None,
    ) -> MarketBarsResult:
        """获取 Binance K 线。"""

        collected_at = datetime.now(tz=UTC)
        try:
            since_ms = self._to_milliseconds(start) if start else None
            rows = self.exchange.fetch_ohlcv(
                self._to_ccxt_symbol(symbol),
                timeframe=timeframe,
                since=since_ms,
                limit=limit,
            )
            if end:
                end_ms = self._to_milliseconds(end)
                rows = [row for row in rows if row[0] < end_ms]
            bars = normalize_crypto_ohlcv(
                rows,
                symbol=symbol,
                timeframe=timeframe,
                source=self.provider_name,
                market_type=self.default_type,
            )
        except Exception as exc:
            return MarketBarsResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
                payload={
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "default_type": self.default_type,
                    "rate_limited": self.rate_limit_policy.is_rate_limited(exc),
                },
            )
        return MarketBarsResult(
            provider_name=self.provider_name,
            status="available" if bars else "unavailable",
            collected_at=collected_at,
            bars=bars,
            payload={"symbol": symbol, "timeframe": timeframe, "default_type": self.default_type},
        )

    @staticmethod
    def _to_milliseconds(value: str) -> int:
        """将 ISO 时间或日期字符串转换为毫秒时间戳。"""

        normalized = value.replace("Z", "+00:00")
        if len(normalized) == 8 and normalized.isdigit():
            parsed = datetime.strptime(normalized, "%Y%m%d").replace(tzinfo=UTC)
        else:
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp() * 1000)

    def _to_ccxt_symbol(self, symbol: str) -> str:
        """把 BTCUSDT 这类紧凑写法转换为 ccxt 常用写法。"""

        if "/" in symbol:
            return symbol
        delivery_suffix = None
        core_symbol = symbol
        if "-" in symbol:
            core_symbol, delivery_suffix = symbol.split("-", 1)
        common_quotes = ["USDT", "USDC", "BUSD", "BTC", "ETH", "USD"]
        for quote in common_quotes:
            if core_symbol.endswith(quote) and len(core_symbol) > len(quote):
                base = core_symbol[: -len(quote)]
                if self.default_type in {"future", "swap"} and quote in {"USDT", "USDC", "USD"}:
                    suffix = f"-{delivery_suffix}" if delivery_suffix else ""
                    return f"{base}/{quote}:{quote}{suffix}"
                return f"{base}/{quote}"
        return symbol

    def health_check(self) -> dict[str, Any]:
        """轻量健康检查。"""

        result = self.fetch_ohlcv(symbol="BTCUSDT", timeframe="1h", limit=1)
        return {
            "provider_name": self.provider_name,
            "status": result.status,
            "error_message": result.error_message,
        }
