"""AKShare 基金数据 Provider。"""

from __future__ import annotations

from datetime import UTC, datetime

import akshare as ak

from finance_agent.data.models import AssetListResult, FundNavSnapshotsResult, MarketBarsResult
from finance_agent.data.normalizers import (
    infer_fund_exchange,
    normalize_fund_etf_hist_em,
    normalize_fund_etf_spot_em,
    normalize_fund_lof_hist_em,
    normalize_fund_lof_spot_em,
    normalize_fund_open_fund_daily_em,
    normalize_fund_open_nav_em,
)
from finance_agent.data.providers.ashare_kline_sources import fetch_tencent_kline_direct
from finance_agent.scheduler.task_queue_model import (
    ProviderChain,
    ProviderChainResult,
    ProviderFetchResult,
    ProviderSource,
)


class AkshareFundProvider:
    """基金资产池、场内基金日 K 和开放式基金净值 Provider。"""

    provider_name = "akshare"

    def __init__(self, *, request_timeout_seconds: float = 15.0) -> None:
        """设置基金数据源单次 HTTP 请求超时。"""

        self.request_timeout_seconds = request_timeout_seconds

    @staticmethod
    def _sina_fund_symbol(symbol: str) -> str:
        """把基金代码转换为新浪历史接口要求的带交易所前缀格式。"""

        exchange = infer_fund_exchange(symbol)
        prefix = "sh" if exchange == "SSE" else "sz"
        return f"{prefix}{symbol}"

    @staticmethod
    def _tencent_fund_symbol(symbol: str) -> str:
        """把基金代码转换为腾讯 K 线接口要求的带交易所前缀格式。"""

        exchange = infer_fund_exchange(symbol)
        if exchange == "SSE":
            return f"sh{symbol}"
        if exchange == "SZSE":
            return f"sz{symbol}"
        return symbol

    def fetch_etf_assets(self, *, limit: int | None = None) -> AssetListResult:
        """获取 ETF 实时列表。"""

        collected_at = datetime.now(tz=UTC)
        try:
            df = ak.fund_etf_spot_em()
            assets = normalize_fund_etf_spot_em(df, limit=limit)
        except Exception as exc:
            return AssetListResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
                payload={"endpoint": "fund_etf_spot_em"},
            )
        return AssetListResult(
            provider_name=self.provider_name,
            status="available" if assets else "unavailable",
            collected_at=collected_at,
            assets=assets,
            payload={"endpoint": "fund_etf_spot_em", "row_count": len(assets)},
        )

    def fetch_lof_assets(self, *, limit: int | None = None) -> AssetListResult:
        """获取 LOF 实时列表。"""

        collected_at = datetime.now(tz=UTC)
        try:
            df = ak.fund_lof_spot_em()
            assets = normalize_fund_lof_spot_em(df, limit=limit)
        except Exception as exc:
            return AssetListResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
                payload={"endpoint": "fund_lof_spot_em"},
            )
        return AssetListResult(
            provider_name=self.provider_name,
            status="available" if assets else "unavailable",
            collected_at=collected_at,
            assets=assets,
            payload={"endpoint": "fund_lof_spot_em", "row_count": len(assets)},
        )

    def fetch_open_fund_assets(self, *, limit: int | None = None) -> AssetListResult:
        """获取开放式基金列表及最新净值。"""

        collected_at = datetime.now(tz=UTC)
        try:
            df = ak.fund_open_fund_daily_em()
            assets = normalize_fund_open_fund_daily_em(df, limit=limit)
        except Exception as exc:
            return AssetListResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
                payload={"endpoint": "fund_open_fund_daily_em"},
            )
        return AssetListResult(
            provider_name=self.provider_name,
            status="available" if assets else "unavailable",
            collected_at=collected_at,
            assets=assets,
            payload={"endpoint": "fund_open_fund_daily_em", "row_count": len(assets)},
        )

    def fetch_etf_ohlcv(
        self,
        *,
        symbol: str,
        period: str = "daily",
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int | None = None,
        is_closed: bool = True,
        status: str = "available",
    ) -> MarketBarsResult:
        """获取 ETF 历史日 K。"""

        collected_at = datetime.now(tz=UTC)
        try:
            df = ak.fund_etf_hist_em(symbol=symbol, period=period, start_date=start_date, end_date=end_date)
            source = "akshare:fund_etf_hist_em"
            payload = {"endpoint": "fund_etf_hist_em", "symbol": symbol, "fallback_used": False}
        except Exception as primary_exc:
            try:
                df = ak.fund_etf_hist_sina(symbol=self._sina_fund_symbol(symbol))
                source = "akshare:fund_etf_hist_sina"
                payload = {
                    "endpoint": "fund_etf_hist_sina",
                    "symbol": symbol,
                    "fallback_used": True,
                    "primary_error": str(primary_exc),
                }
            except Exception as fallback_exc:
                return MarketBarsResult(
                    provider_name=self.provider_name,
                    status="error",
                    collected_at=collected_at,
                    error_message=f"{primary_exc}; fallback={fallback_exc}",
                    payload={
                        "endpoint": "fund_etf_hist_em",
                        "symbol": symbol,
                        "fallback_endpoint": "fund_etf_hist_sina",
                    },
                )
        try:
            if limit:
                df = df.tail(limit)
            bars = normalize_fund_etf_hist_em(
                df,
                symbol=symbol,
                timeframe="1d",
                source=source,
                is_closed=is_closed,
                status=status,
            )
        except Exception as exc:
            return MarketBarsResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
                payload=payload,
            )
        return MarketBarsResult(
            provider_name=self.provider_name,
            status="available" if bars else "unavailable",
            collected_at=collected_at,
            bars=bars,
            payload={**payload, "row_count": len(bars)},
        )

    def fetch_lof_ohlcv(
        self,
        *,
        symbol: str,
        period: str = "daily",
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int | None = None,
        is_closed: bool = True,
        status: str = "available",
    ) -> MarketBarsResult:
        """获取 LOF 历史日 K。"""

        collected_at = datetime.now(tz=UTC)
        primary_source = "akshare:fund_lof_hist_em"
        fallback_source = "tencent:direct:kline"
        chain = ProviderChain(
            [
                ProviderSource(name=primary_source, rate_key="eastmoney_fund_kline", complexity="fund_lof_kline"),
                ProviderSource(name=fallback_source, rate_key="tencent_kline", complexity="fund_lof_kline"),
            ]
        )

        def fetch(source: ProviderSource) -> ProviderFetchResult:
            if source.name == primary_source:
                frame = ak.fund_lof_hist_em(symbol=symbol, period=period, start_date=start_date, end_date=end_date)
            elif source.name == fallback_source:
                frame = fetch_tencent_kline_direct(
                    symbol=self._tencent_fund_symbol(symbol),
                    start=start_date,
                    end=end_date,
                    adjust="qfq",
                    timeout=self.request_timeout_seconds,
                )
            else:
                return ProviderFetchResult.error(f"未知 LOF K 线数据源: {source.name}")
            if limit:
                frame = frame.tail(limit)
            return ProviderFetchResult.ok(payload=frame, row_count=len(frame))

        chain_result = chain.run(fetch)
        provider_chain_payload = _provider_chain_payload(chain_result)
        if chain_result.status != "ok":
            return MarketBarsResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=chain_result.error_message,
                payload={
                    "endpoint": primary_source,
                    "symbol": symbol,
                    "fallback_endpoint": fallback_source,
                    "provider_chain": provider_chain_payload,
                },
            )
        try:
            bars = normalize_fund_lof_hist_em(
                chain_result.payload,
                symbol=symbol,
                timeframe="1d",
                source=chain_result.source or primary_source,
                is_closed=is_closed,
                status=status,
            )
        except Exception as exc:
            return MarketBarsResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
                payload={
                    "endpoint": chain_result.source or primary_source,
                    "symbol": symbol,
                    "provider_chain": provider_chain_payload,
                },
            )
        return MarketBarsResult(
            provider_name=self.provider_name,
            status="available" if bars else "unavailable",
            collected_at=collected_at,
            bars=bars,
            payload={
                "endpoint": chain_result.source or primary_source,
                "symbol": symbol,
                "fallback_used": chain_result.source != primary_source,
                "fallback_endpoint": fallback_source,
                "adjust": "qfq" if chain_result.source == fallback_source else "",
                "provider_chain": provider_chain_payload,
                "row_count": len(bars),
            },
        )

    def fetch_open_fund_nav(
        self,
        *,
        symbol: str,
        indicator: str = "累计净值走势",
        limit: int | None = None,
    ) -> FundNavSnapshotsResult:
        """获取开放式基金净值历史。"""

        collected_at = datetime.now(tz=UTC)
        try:
            df = ak.fund_open_fund_info_em(symbol=symbol, indicator=indicator)
            snapshots = normalize_fund_open_nav_em(
                df,
                symbol=symbol,
                source="akshare:fund_open_fund_info_em",
                limit=limit,
            )
        except Exception as exc:
            return FundNavSnapshotsResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
                payload={
                    "endpoint": "fund_open_fund_info_em",
                    "symbol": symbol,
                    "indicator": indicator,
                },
            )
        return FundNavSnapshotsResult(
            provider_name=self.provider_name,
            status="available" if snapshots else "unavailable",
            collected_at=collected_at,
            snapshots=snapshots,
            payload={
                "endpoint": "fund_open_fund_info_em",
                "symbol": symbol,
                "indicator": indicator,
                "row_count": len(snapshots),
            },
        )


def _provider_chain_payload(result: ProviderChainResult) -> dict:
    """把 ProviderChainResult 转为可归档的轻量审计结构。"""

    return {
        "status": result.status,
        "source": result.source,
        "row_count": result.row_count,
        "error_message": result.error_message,
        "attempts": [attempt.to_dict() for attempt in result.attempts],
    }
