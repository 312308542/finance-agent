"""AKShare A 股 P1 数据 Provider。

P1 侧重让选股推荐具备行业/主题、资金流和事件证据。
这些 Provider 只负责采集和归一化，不负责因子打分或推荐决策。
"""

from __future__ import annotations

from datetime import UTC, datetime

import akshare as ak

from finance_agent.data.models import (
    CapitalFlowSnapshotsResult,
    EventRecordsResult,
    UniverseSeedsResult,
)
from finance_agent.data.normalizers import (
    normalize_ashare_board_members,
    normalize_ashare_fund_flow_rank,
    normalize_ashare_notice_reports,
    normalize_ashare_stock_news,
)
from finance_agent.data.providers import eastmoney_curl, ths_curl


class AshareSectorProvider:
    """A 股行业/概念种子 Provider。"""

    provider_name = "akshare"

    def fetch_industry_members(
        self,
        *,
        industry_name: str,
        limit: int | None = None,
    ) -> UniverseSeedsResult:
        """获取行业板块成分作为候选池种子。"""

        collected_at = datetime.now(tz=UTC)
        fallback_trace: list[dict[str, str]] = []
        primary_source = "akshare:stock_board_industry_cons_em"
        try:
            df = ak.stock_board_industry_cons_em(symbol=industry_name)
            actual_source = primary_source
        except Exception as exc:
            fallback_trace.append({"source": primary_source, "error_message": str(exc)})
            try:
                df = eastmoney_curl.fetch_industry_members(industry_name)
                actual_source = str(
                    df.attrs.get(
                        "actual_source",
                        "eastmoney:curl_cffi:stock_board_industry_cons_em",
                    )
                )
            except Exception as fallback_exc:
                fallback_trace.append(
                    {
                        "source": "eastmoney:curl_cffi:stock_board_industry_cons_em",
                        "error_message": str(fallback_exc),
                    }
                )
                try:
                    df = ths_curl.fetch_industry_members(industry_name, limit=limit)
                    actual_source = str(
                        df.attrs.get(
                            "actual_source",
                            "ths:curl_cffi:stock_board_industry_detail_first_page",
                        )
                    )
                except Exception as ths_exc:
                    fallback_trace.append(
                        {
                            "source": "ths:curl_cffi:stock_board_industry_detail_first_page",
                            "error_message": str(ths_exc),
                        }
                    )
                    return UniverseSeedsResult(
                        provider_name=self.provider_name,
                        status="error",
                        collected_at=collected_at,
                        error_message=str(ths_exc),
                        payload={
                            "endpoint": "stock_board_industry_cons_em",
                            "symbol": industry_name,
                            "primary_source": primary_source,
                            "fallback_trace": fallback_trace,
                        },
                    )
        seeds = normalize_ashare_board_members(
            df,
            source_name=industry_name,
            source_type="industry",
            as_of=collected_at,
            limit=limit,
        )
        return UniverseSeedsResult(
            provider_name=self.provider_name,
            status="available" if seeds else "unavailable",
            collected_at=collected_at,
            seeds=seeds,
            payload={
                "endpoint": "stock_board_industry_cons_em",
                "symbol": industry_name,
                "row_count": len(seeds),
                "primary_source": primary_source,
                "actual_source": actual_source,
                "fallback_used": actual_source != primary_source,
                "fallback_trace": fallback_trace,
                "source_coverage": df.attrs.get("source_coverage"),
                "source_board_code": df.attrs.get("board_code"),
            },
        )

    def fetch_concept_members(
        self,
        *,
        concept_name: str,
        limit: int | None = None,
    ) -> UniverseSeedsResult:
        """获取概念板块成分作为候选池种子。"""

        collected_at = datetime.now(tz=UTC)
        fallback_trace: list[dict[str, str]] = []
        primary_source = "akshare:stock_board_concept_cons_em"
        try:
            df = ak.stock_board_concept_cons_em(symbol=concept_name)
            actual_source = primary_source
        except Exception as exc:
            fallback_trace.append({"source": primary_source, "error_message": str(exc)})
            try:
                df = eastmoney_curl.fetch_concept_members(concept_name)
                actual_source = str(
                    df.attrs.get(
                        "actual_source",
                        "eastmoney:curl_cffi:stock_board_concept_cons_em",
                    )
                )
            except Exception as fallback_exc:
                fallback_trace.append(
                    {
                        "source": "eastmoney:curl_cffi:stock_board_concept_cons_em",
                        "error_message": str(fallback_exc),
                    }
                )
                try:
                    df = ths_curl.fetch_concept_members(concept_name, limit=limit)
                    actual_source = str(
                        df.attrs.get(
                            "actual_source",
                            "ths:curl_cffi:stock_board_concept_detail_first_page",
                        )
                    )
                except Exception as ths_exc:
                    fallback_trace.append(
                        {
                            "source": "ths:curl_cffi:stock_board_concept_detail_first_page",
                            "error_message": str(ths_exc),
                        }
                    )
                    return UniverseSeedsResult(
                        provider_name=self.provider_name,
                        status="error",
                        collected_at=collected_at,
                        error_message=str(ths_exc),
                        payload={
                            "endpoint": "stock_board_concept_cons_em",
                            "symbol": concept_name,
                            "primary_source": primary_source,
                            "fallback_trace": fallback_trace,
                        },
                    )
        seeds = normalize_ashare_board_members(
            df,
            source_name=concept_name,
            source_type="concept",
            as_of=collected_at,
            limit=limit,
        )
        return UniverseSeedsResult(
            provider_name=self.provider_name,
            status="available" if seeds else "unavailable",
            collected_at=collected_at,
            seeds=seeds,
            payload={
                "endpoint": "stock_board_concept_cons_em",
                "symbol": concept_name,
                "row_count": len(seeds),
                "primary_source": primary_source,
                "actual_source": actual_source,
                "fallback_used": actual_source != primary_source,
                "fallback_trace": fallback_trace,
                "source_coverage": df.attrs.get("source_coverage"),
                "source_board_code": df.attrs.get("board_code"),
            },
        )


class AshareCapitalFlowProvider:
    """A 股资金流 Provider。"""

    provider_name = "akshare"

    def fetch_flow_rank(
        self,
        *,
        indicator: str = "今日",
        limit: int | None = None,
    ) -> CapitalFlowSnapshotsResult:
        """获取个股资金流排名快照。"""

        collected_at = datetime.now(tz=UTC)
        fallback_trace: list[dict[str, str]] = []
        primary_source = "akshare:stock_individual_fund_flow_rank"
        try:
            df = ak.stock_individual_fund_flow_rank(indicator=indicator)
            actual_source = primary_source
        except Exception as exc:
            fallback_trace.append({"source": primary_source, "error_message": str(exc)})
            try:
                df = eastmoney_curl.fetch_fund_flow_rank(indicator, limit=limit)
                actual_source = str(
                    df.attrs.get(
                        "actual_source",
                        "eastmoney:curl_cffi:stock_individual_fund_flow_rank",
                    )
                )
            except Exception as fallback_exc:
                fallback_trace.append(
                    {
                        "source": "eastmoney:curl_cffi:stock_individual_fund_flow_rank",
                        "error_message": str(fallback_exc),
                    }
                )
                return CapitalFlowSnapshotsResult(
                    provider_name=self.provider_name,
                    status="error",
                    collected_at=collected_at,
                    error_message=str(fallback_exc),
                    payload={
                        "endpoint": "stock_individual_fund_flow_rank",
                        "indicator": indicator,
                        "primary_source": primary_source,
                        "fallback_trace": fallback_trace,
                    },
                )
        snapshots = normalize_ashare_fund_flow_rank(
            df,
            source=actual_source,
            window=indicator,
            as_of=collected_at,
            limit=limit,
        )
        return CapitalFlowSnapshotsResult(
            provider_name=self.provider_name,
            status="available" if snapshots else "unavailable",
            collected_at=collected_at,
            snapshots=snapshots,
            payload={
                "endpoint": "stock_individual_fund_flow_rank",
                "indicator": indicator,
                "row_count": len(snapshots),
                "primary_source": primary_source,
                "actual_source": actual_source,
                "fallback_used": actual_source != primary_source,
                "fallback_trace": fallback_trace,
            },
        )


class AshareEventProvider:
    """A 股新闻公告 Provider。"""

    provider_name = "akshare"

    def fetch_stock_news(
        self,
        *,
        symbol: str,
        limit: int | None = None,
    ) -> EventRecordsResult:
        """获取个股新闻事件。"""

        collected_at = datetime.now(tz=UTC)
        try:
            df = ak.stock_news_em(symbol=symbol)
            events, evidence = normalize_ashare_stock_news(
                df,
                symbol=symbol,
                source="akshare:stock_news_em",
                collected_at=collected_at,
                limit=limit,
            )
        except Exception as exc:
            return EventRecordsResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
                payload={"endpoint": "stock_news_em", "symbol": symbol},
            )
        return EventRecordsResult(
            provider_name=self.provider_name,
            status="available" if events else "unavailable",
            collected_at=collected_at,
            events=events,
            evidence=evidence,
            payload={"endpoint": "stock_news_em", "symbol": symbol, "row_count": len(events)},
        )

    def fetch_notice_reports(
        self,
        *,
        symbol: str = "全部",
        date: str,
        limit: int | None = None,
    ) -> EventRecordsResult:
        """获取公告披露事件。"""

        collected_at = datetime.now(tz=UTC)
        try:
            df = ak.stock_notice_report(symbol=symbol, date=date)
            events, evidence = normalize_ashare_notice_reports(
                df,
                source="akshare:stock_notice_report",
                collected_at=collected_at,
                limit=limit,
            )
        except Exception as exc:
            return EventRecordsResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
                payload={"endpoint": "stock_notice_report", "symbol": symbol, "date": date},
            )
        return EventRecordsResult(
            provider_name=self.provider_name,
            status="available" if events else "unavailable",
            collected_at=collected_at,
            events=events,
            evidence=evidence,
            payload={
                "endpoint": "stock_notice_report",
                "symbol": symbol,
                "date": date,
                "row_count": len(events),
            },
        )
