"""AKShare A 股数据 Provider。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import akshare as ak

from finance_agent.data.models import AssetListResult, MarketBarsResult
from finance_agent.data.normalizers import normalize_ashare_hist, normalize_ashare_spot


class AkshareProvider:
    """A 股数据 Provider。"""

    provider_name = "akshare"

    def fetch_assets(self, *, limit: int | None = None) -> AssetListResult:
        """获取 A 股可交易资产列表。"""

        collected_at = datetime.now(tz=UTC)
        try:
            df = ak.stock_zh_a_spot_em()
            assets = normalize_ashare_spot(df, limit=limit)
        except Exception as exc:
            return AssetListResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
            )
        return AssetListResult(
            provider_name=self.provider_name,
            status="available" if assets else "unavailable",
            collected_at=collected_at,
            assets=assets,
            payload={"row_count": len(assets)},
        )

    def fetch_ohlcv(
        self,
        *,
        symbol: str,
        timeframe: str,
        start: str | None = None,
        end: str | None = None,
        limit: int | None = None,
        adjust: str = "qfq",
    ) -> MarketBarsResult:
        """获取 A 股历史 K 线。"""

        collected_at = datetime.now(tz=UTC)
        try:
            df = ak.stock_zh_a_hist(
                symbol=symbol,
                period=self._to_ak_period(timeframe),
                start_date=start or "20000101",
                end_date=end or "20991231",
                adjust=adjust,
            )
            if limit:
                df = df.tail(limit)
            bars = normalize_ashare_hist(
                df,
                symbol=symbol,
                timeframe=timeframe,
                source=self.provider_name,
                adjustment=adjust,
            )
        except Exception as exc:
            return MarketBarsResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
            )
        return MarketBarsResult(
            provider_name=self.provider_name,
            status="available" if bars else "unavailable",
            collected_at=collected_at,
            bars=bars,
            payload={"symbol": symbol, "timeframe": timeframe, "adjust": adjust},
        )

    @staticmethod
    def _to_ak_period(timeframe: str) -> str:
        """转换为 AKShare 支持的 period。"""

        mapping: dict[str, str] = {
            "1d": "daily",
            "1w": "weekly",
            "1M": "monthly",
        }
        if timeframe not in mapping:
            raise ValueError(f"AKShare A 股历史行情暂不支持周期: {timeframe}")
        return mapping[timeframe]

    def health_check(self) -> dict[str, Any]:
        """轻量健康检查。"""

        result = self.fetch_ohlcv(symbol="000001", timeframe="1d", limit=1)
        return {
            "provider_name": self.provider_name,
            "status": result.status,
            "error_message": result.error_message,
        }
