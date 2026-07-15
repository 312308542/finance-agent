"""AKShare A 股 P2 财务和估值 Provider。

P2 负责补齐选股推荐需要的基本面、成长性、估值和红利信息。
Provider 只做采集和归一化，不做因子评分。
"""

from __future__ import annotations

from datetime import UTC, datetime

import akshare as ak

from finance_agent.data.models import FundamentalSnapshotsResult
from finance_agent.data.normalizers import (
    infer_ashare_exchange,
    normalize_ashare_dividend_yield,
    normalize_ashare_financial_indicator,
    normalize_ashare_performance_report,
    normalize_ashare_valuation,
)
from finance_agent.data.providers import eastmoney_curl


class AshareFundamentalProvider:
    """A 股财务数据 Provider。"""

    provider_name = "akshare"

    def fetch_financial_indicators(
        self,
        *,
        symbol: str,
        limit: int | None = None,
    ) -> FundamentalSnapshotsResult:
        """获取个股主要财务指标。"""

        collected_at = datetime.now(tz=UTC)
        ak_symbol = self._with_exchange_suffix(symbol)
        try:
            df = ak.stock_financial_analysis_indicator_em(
                symbol=ak_symbol,
                indicator="按报告期",
            )
            snapshots = normalize_ashare_financial_indicator(
                df,
                symbol=ak_symbol,
                source="akshare:stock_financial_analysis_indicator_em",
                as_of=collected_at,
                limit=limit,
            )
        except Exception as exc:
            return FundamentalSnapshotsResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
                payload={
                    "endpoint": "stock_financial_analysis_indicator_em",
                    "symbol": ak_symbol,
                },
            )
        return FundamentalSnapshotsResult(
            provider_name=self.provider_name,
            status="available" if snapshots else "unavailable",
            collected_at=collected_at,
            snapshots=snapshots,
            payload={
                "endpoint": "stock_financial_analysis_indicator_em",
                "symbol": ak_symbol,
                "row_count": len(snapshots),
            },
        )

    def fetch_performance_report(
        self,
        *,
        date: str,
        report_type: str = "业绩报表",
        limit: int | None = None,
    ) -> FundamentalSnapshotsResult:
        """获取业绩报表、业绩快报或业绩预告。"""

        collected_at = datetime.now(tz=UTC)
        endpoint_map = {
            "业绩报表": ("stock_yjbb_em", ak.stock_yjbb_em),
            "业绩快报": ("stock_yjkb_em", ak.stock_yjkb_em),
            "业绩预告": ("stock_yjyg_em", ak.stock_yjyg_em),
        }
        endpoint, function = endpoint_map[report_type]
        primary_source = f"akshare:{endpoint}"
        fallback_trace: list[dict[str, str]] = []
        try:
            df = function(date=date)
            actual_source = primary_source
        except Exception as exc:
            fallback_trace.append({"source": primary_source, "error_message": str(exc)})
            try:
                df = eastmoney_curl.fetch_performance_report(
                    date,
                    report_type=report_type,
                    limit=limit,
                )
                actual_source = str(
                    df.attrs.get("actual_source", f"eastmoney:curl_cffi:{endpoint}")
                )
            except Exception as fallback_exc:
                fallback_trace.append(
                    {
                        "source": f"eastmoney:curl_cffi:{endpoint}",
                        "error_message": str(fallback_exc),
                    }
                )
                return FundamentalSnapshotsResult(
                    provider_name=self.provider_name,
                    status="error",
                    collected_at=collected_at,
                    error_message=str(fallback_exc),
                    payload={
                        "endpoint": endpoint,
                        "date": date,
                        "report_type": report_type,
                        "primary_source": primary_source,
                        "fallback_trace": fallback_trace,
                    },
                )
        snapshots = normalize_ashare_performance_report(
            df,
            source=actual_source,
            as_of=collected_at,
            limit=limit,
        )
        return FundamentalSnapshotsResult(
            provider_name=self.provider_name,
            status="available" if snapshots else "unavailable",
            collected_at=collected_at,
            snapshots=snapshots,
            payload={
                "endpoint": endpoint,
                "date": date,
                "report_type": report_type,
                "row_count": len(snapshots),
                "primary_source": primary_source,
                "actual_source": actual_source,
                "fallback_used": actual_source != primary_source,
                "fallback_trace": fallback_trace,
            },
        )

    @staticmethod
    def _with_exchange_suffix(symbol: str) -> str:
        """转换为东方财富财务指标接口需要的 `000001.SZ` 格式。"""

        normalized = symbol.strip().upper()
        if "." in normalized:
            return normalized
        exchange = infer_ashare_exchange(normalized)
        if exchange == "SSE":
            return f"{normalized}.SH"
        if exchange == "SZSE":
            return f"{normalized}.SZ"
        if exchange == "BSE":
            return f"{normalized}.BJ"
        return normalized


class AshareValuationProvider:
    """A 股估值和红利数据 Provider。"""

    provider_name = "akshare"

    def fetch_valuation(
        self,
        *,
        symbol: str,
        limit: int | None = None,
    ) -> FundamentalSnapshotsResult:
        """获取个股估值序列。"""

        collected_at = datetime.now(tz=UTC)
        try:
            df = ak.stock_value_em(symbol=symbol)
            snapshots = normalize_ashare_valuation(
                df,
                symbol=symbol,
                source="akshare:stock_value_em",
                as_of=collected_at,
                limit=limit,
            )
        except TypeError as exc:
            if str(exc) != "'NoneType' object is not subscriptable":
                return FundamentalSnapshotsResult(
                    provider_name=self.provider_name,
                    status="error",
                    collected_at=collected_at,
                    error_message=str(exc),
                    payload={"endpoint": "stock_value_em", "symbol": symbol},
                )
            return FundamentalSnapshotsResult(
                provider_name=self.provider_name,
                status="unavailable",
                collected_at=collected_at,
                payload={
                    "endpoint": "stock_value_em",
                    "symbol": symbol,
                    "unavailable_reason": "source_returned_no_data",
                },
            )
        except Exception as exc:
            return FundamentalSnapshotsResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
                payload={"endpoint": "stock_value_em", "symbol": symbol},
            )
        return FundamentalSnapshotsResult(
            provider_name=self.provider_name,
            status="available" if snapshots else "unavailable",
            collected_at=collected_at,
            snapshots=snapshots,
            payload={
                "endpoint": "stock_value_em",
                "symbol": symbol,
                "row_count": len(snapshots),
            },
        )

    def fetch_dividend_yield(
        self,
        *,
        universe: str = "上证A股",
        limit: int | None = None,
    ) -> FundamentalSnapshotsResult:
        """获取 A 股股息率数据。"""

        collected_at = datetime.now(tz=UTC)
        try:
            df = ak.stock_a_gxl_lg(symbol=universe)
            snapshots = normalize_ashare_dividend_yield(
                df,
                source="akshare:stock_a_gxl_lg",
                as_of=collected_at,
                limit=limit,
            )
        except Exception as exc:
            return FundamentalSnapshotsResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
                payload={"endpoint": "stock_a_gxl_lg", "symbol": universe},
            )
        return FundamentalSnapshotsResult(
            provider_name=self.provider_name,
            status="available" if snapshots else "unavailable",
            collected_at=collected_at,
            snapshots=snapshots,
            payload={
                "endpoint": "stock_a_gxl_lg",
                "symbol": universe,
                "row_count": len(snapshots),
            },
        )
