"""数据源端口定义。

应用服务只依赖这些 Protocol，不直接依赖 AKShare、ccxt 或交易所 SDK。
"""

from __future__ import annotations

from typing import Protocol

from finance_agent.data.models import AssetListResult, MarketBarsResult


class UniverseProvider(Protocol):
    """候选池 Provider 接口。"""

    provider_name: str

    def fetch_assets(self, *, limit: int | None = None) -> AssetListResult:
        """获取可参与推荐的资产列表。"""


class MarketDataProvider(Protocol):
    """行情 Provider 接口。"""

    provider_name: str

    def fetch_ohlcv(
        self,
        *,
        symbol: str,
        timeframe: str,
        start: str | None = None,
        end: str | None = None,
        limit: int | None = None,
    ) -> MarketBarsResult:
        """获取标准 OHLCV K 线。"""
