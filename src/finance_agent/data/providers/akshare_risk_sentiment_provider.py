"""AKShare A 股风险和情绪 Provider。

这一层只负责把停复牌、龙虎榜、大宗交易、两融、热度榜和涨停池
采集为结构化结果，不做推荐打分，也不直接生成买卖建议。
"""

from __future__ import annotations

from datetime import UTC, datetime

import akshare as ak

from finance_agent.data.models import RiskFindingsResult, SentimentSignalsResult
from finance_agent.data.normalizers import (
    normalize_ashare_block_trades,
    normalize_ashare_hot_rank,
    normalize_ashare_lhb_detail,
    normalize_ashare_margin_summary,
    normalize_ashare_stop_list,
    normalize_ashare_zt_pool,
)
from finance_agent.data.providers import eastmoney_curl


class AshareRiskProvider:
    """A 股风险数据 Provider。"""

    provider_name = "akshare"

    def fetch_stop_list(self, *, limit: int | None = None) -> RiskFindingsResult:
        """获取停牌列表，用于不可交易风险过滤。"""

        collected_at = datetime.now(tz=UTC)
        endpoint = "stock_zh_a_stop_em"
        primary_source = f"akshare:{endpoint}"
        fallback_trace: list[dict[str, str]] = []
        try:
            df = ak.stock_zh_a_stop_em()
            actual_source = primary_source
        except Exception as exc:
            fallback_trace.append({"source": primary_source, "error_message": str(exc)})
            try:
                df = eastmoney_curl.fetch_stop_list(limit=limit)
                actual_source = str(
                    df.attrs.get("actual_source", "eastmoney:curl_cffi:stock_zh_a_stop_em")
                )
            except Exception as fallback_exc:
                fallback_trace.append(
                    {
                        "source": "eastmoney:curl_cffi:stock_zh_a_stop_em",
                        "error_message": str(fallback_exc),
                    }
                )
                return RiskFindingsResult(
                    provider_name=self.provider_name,
                    status="error",
                    collected_at=collected_at,
                    error_message=str(fallback_exc),
                    payload={
                        "endpoint": endpoint,
                        "primary_source": primary_source,
                        "fallback_trace": fallback_trace,
                    },
                )
        try:
            risks, events = normalize_ashare_stop_list(
                df,
                source=actual_source,
                collected_at=collected_at,
                limit=limit,
            )
        except Exception as exc:
            return RiskFindingsResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
                payload={
                    "endpoint": endpoint,
                    "primary_source": primary_source,
                    "actual_source": actual_source,
                    "fallback_trace": fallback_trace,
                },
            )
        return RiskFindingsResult(
            provider_name=self.provider_name,
            status="available" if risks or events else "unavailable",
            collected_at=collected_at,
            risks=risks,
            events=events,
            payload={
                "endpoint": endpoint,
                "row_count": len(risks),
                "primary_source": primary_source,
                "actual_source": actual_source,
                "fallback_used": actual_source != primary_source,
                "fallback_trace": fallback_trace,
                "source_coverage": df.attrs.get("source_coverage"),
            },
        )

    def fetch_lhb_detail(
        self,
        *,
        start_date: str,
        end_date: str,
        limit: int | None = None,
    ) -> RiskFindingsResult:
        """获取龙虎榜明细，用于识别短线交易活跃和异常成交风险。"""

        collected_at = datetime.now(tz=UTC)
        endpoint = "stock_lhb_detail_em"
        source = f"akshare:{endpoint}"
        try:
            df = ak.stock_lhb_detail_em(start_date=start_date, end_date=end_date)
            risks, evidence = normalize_ashare_lhb_detail(
                df,
                source=source,
                collected_at=collected_at,
                limit=limit,
            )
        except Exception as exc:
            return RiskFindingsResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
                payload={"endpoint": endpoint, "start_date": start_date, "end_date": end_date},
            )
        return RiskFindingsResult(
            provider_name=self.provider_name,
            status="available" if risks else "unavailable",
            collected_at=collected_at,
            risks=risks,
            evidence=evidence,
            payload={
                "endpoint": endpoint,
                "start_date": start_date,
                "end_date": end_date,
                "row_count": len(risks),
                "actual_source": source,
            },
        )

    def fetch_block_trades(
        self,
        *,
        start_date: str,
        end_date: str,
        symbol: str = "A股",
        limit: int | None = None,
    ) -> RiskFindingsResult:
        """获取大宗交易明细，用于折溢价和机构交易异动风险。"""

        collected_at = datetime.now(tz=UTC)
        endpoint = "stock_dzjy_mrmx"
        source = f"akshare:{endpoint}"
        try:
            df = ak.stock_dzjy_mrmx(symbol=symbol, start_date=start_date, end_date=end_date)
            risks, evidence = normalize_ashare_block_trades(
                df,
                source=source,
                collected_at=collected_at,
                limit=limit,
            )
        except Exception as exc:
            return RiskFindingsResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
                payload={
                    "endpoint": endpoint,
                    "symbol": symbol,
                    "start_date": start_date,
                    "end_date": end_date,
                },
            )
        return RiskFindingsResult(
            provider_name=self.provider_name,
            status="available" if risks else "unavailable",
            collected_at=collected_at,
            risks=risks,
            evidence=evidence,
            payload={
                "endpoint": endpoint,
                "symbol": symbol,
                "start_date": start_date,
                "end_date": end_date,
                "row_count": len(risks),
                "actual_source": source,
            },
        )

    def fetch_margin_sse(
        self,
        *,
        start_date: str,
        end_date: str,
        limit: int | None = None,
    ) -> RiskFindingsResult:
        """获取上交所融资融券汇总，用于市场杠杆情绪风险。"""

        collected_at = datetime.now(tz=UTC)
        endpoint = "stock_margin_sse"
        source = f"akshare:{endpoint}"
        try:
            df = ak.stock_margin_sse(start_date=start_date, end_date=end_date)
            risks = normalize_ashare_margin_summary(
                df,
                source=source,
                market_scope="上交所",
                collected_at=collected_at,
                limit=limit,
            )
        except Exception as exc:
            return RiskFindingsResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
                payload={"endpoint": endpoint, "start_date": start_date, "end_date": end_date},
            )
        return RiskFindingsResult(
            provider_name=self.provider_name,
            status="available" if risks else "unavailable",
            collected_at=collected_at,
            risks=risks,
            payload={
                "endpoint": endpoint,
                "start_date": start_date,
                "end_date": end_date,
                "row_count": len(risks),
                "actual_source": source,
            },
        )

    def fetch_margin_szse(self, *, date: str, limit: int | None = None) -> RiskFindingsResult:
        """获取深交所融资融券汇总，用于市场杠杆情绪风险。"""

        collected_at = datetime.now(tz=UTC)
        endpoint = "stock_margin_szse"
        source = f"akshare:{endpoint}"
        try:
            df = ak.stock_margin_szse(date=date)
            risks = normalize_ashare_margin_summary(
                df,
                source=source,
                market_scope="深交所",
                collected_at=collected_at,
                limit=limit,
            )
        except Exception as exc:
            return RiskFindingsResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
                payload={"endpoint": endpoint, "date": date},
            )
        return RiskFindingsResult(
            provider_name=self.provider_name,
            status="available" if risks else "unavailable",
            collected_at=collected_at,
            risks=risks,
            payload={
                "endpoint": endpoint,
                "date": date,
                "row_count": len(risks),
                "actual_source": source,
            },
        )


class AshareSentimentProvider:
    """A 股短线情绪数据 Provider。"""

    provider_name = "akshare"

    def fetch_hot_rank(self, *, limit: int | None = None) -> SentimentSignalsResult:
        """获取东方财富人气榜，用于热度候选种子。"""

        collected_at = datetime.now(tz=UTC)
        endpoint = "stock_hot_rank_em"
        primary_source = f"akshare:{endpoint}"
        fallback_trace: list[dict[str, str]] = []
        try:
            df = ak.stock_hot_rank_em()
            actual_source = primary_source
        except Exception as exc:
            fallback_trace.append({"source": primary_source, "error_message": str(exc)})
            try:
                df = eastmoney_curl.fetch_hot_rank(limit=limit)
                actual_source = str(
                    df.attrs.get("actual_source", "eastmoney:curl_cffi:stock_hot_rank_em")
                )
            except Exception as fallback_exc:
                fallback_trace.append(
                    {
                        "source": "eastmoney:curl_cffi:stock_hot_rank_em",
                        "error_message": str(fallback_exc),
                    }
                )
                return SentimentSignalsResult(
                    provider_name=self.provider_name,
                    status="error",
                    collected_at=collected_at,
                    error_message=str(fallback_exc),
                    payload={
                        "endpoint": endpoint,
                        "primary_source": primary_source,
                        "fallback_trace": fallback_trace,
                    },
                )
        try:
            seeds, events = normalize_ashare_hot_rank(
                df,
                source=actual_source,
                as_of=collected_at,
                limit=limit,
            )
        except Exception as exc:
            return SentimentSignalsResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
                payload={
                    "endpoint": endpoint,
                    "primary_source": primary_source,
                    "actual_source": actual_source,
                    "fallback_trace": fallback_trace,
                },
            )
        return SentimentSignalsResult(
            provider_name=self.provider_name,
            status="available" if seeds or events else "unavailable",
            collected_at=collected_at,
            seeds=seeds,
            events=events,
            payload={
                "endpoint": endpoint,
                "row_count": len(seeds),
                "primary_source": primary_source,
                "actual_source": actual_source,
                "fallback_used": actual_source != primary_source,
                "fallback_trace": fallback_trace,
                "source_coverage": df.attrs.get("source_coverage"),
            },
        )

    def fetch_zt_pool(self, *, date: str, limit: int | None = None) -> SentimentSignalsResult:
        """获取涨停池，用于强势候选种子和情绪过热风险。"""

        collected_at = datetime.now(tz=UTC)
        endpoint = "stock_zt_pool_em"
        source = f"akshare:{endpoint}"
        try:
            df = ak.stock_zt_pool_em(date=date)
            seeds, events, risks = normalize_ashare_zt_pool(
                df,
                date=date,
                source=source,
                collected_at=collected_at,
                limit=limit,
            )
        except Exception as exc:
            return SentimentSignalsResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
                payload={"endpoint": endpoint, "date": date},
            )
        return SentimentSignalsResult(
            provider_name=self.provider_name,
            status="available" if seeds or events or risks else "unavailable",
            collected_at=collected_at,
            seeds=seeds,
            events=events,
            risks=risks,
            payload={
                "endpoint": endpoint,
                "date": date,
                "row_count": len(seeds),
                "actual_source": source,
            },
        )
