"""AKShare A 股 P1 数据 Provider。

P1 侧重让选股推荐具备行业/主题、资金流和事件证据。
这些 Provider 只负责采集和归一化，不负责因子打分或推荐决策。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import akshare as ak

from finance_agent.data.models import (
    CapitalFlowSnapshotsResult,
    EventRecordsResult,
    ProviderResult,
    UniverseSeedsResult,
)
from finance_agent.data.normalizers import (
    normalize_ashare_board_members,
    normalize_ashare_fund_flow_rank,
    normalize_ashare_index_members,
    normalize_ashare_individual_fund_flow,
    normalize_ashare_northbound_individual_flow,
    normalize_ashare_northbound_market_flow,
    normalize_ashare_notice_reports,
    normalize_ashare_stock_news,
    strip_ashare_exchange_prefix,
)
from finance_agent.data.providers import eastmoney_curl, sina_curl, ths_curl


class AshareSectorProvider:
    """A 股行业/概念种子 Provider。"""

    provider_name = "akshare"

    def fetch_index_catalog(self, *, limit: int | None = None) -> ProviderResult:
        """获取 A 股指数目录，供 Universe 刷新自动展开指数成分。"""

        collected_at = datetime.now(tz=UTC)
        try:
            df = ak.index_stock_info()
            indexes = _extract_index_catalog(df, limit=limit)
        except Exception as exc:
            return ProviderResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
                payload={"endpoint": "index_stock_info"},
            )
        return ProviderResult(
            provider_name=self.provider_name,
            status="available" if indexes else "unavailable",
            collected_at=collected_at,
            payload={
                "endpoint": "index_stock_info",
                "row_count": len(indexes),
                "indexes": indexes,
            },
        )

    def fetch_industry_names(self, *, limit: int | None = None) -> ProviderResult:
        """获取东方财富行业板块目录。"""

        collected_at = datetime.now(tz=UTC)
        fallback_trace: list[dict[str, str]] = []
        actual_source = "akshare:stock_board_industry_name_em"
        try:
            df = ak.stock_board_industry_name_em()
        except Exception as exc:
            fallback_trace.append(
                {
                    "source": "akshare:stock_board_industry_name_em",
                    "error_message": str(exc),
                }
            )
            try:
                df = eastmoney_curl.fetch_industry_names(limit=limit)
                actual_source = str(
                    df.attrs.get(
                        "actual_source",
                        "eastmoney:curl_cffi:stock_board_industry_name_em",
                    )
                )
            except Exception as fallback_exc:
                fallback_trace.append(
                    {
                        "source": "eastmoney:curl_cffi:stock_board_industry_name_em",
                        "error_message": str(fallback_exc),
                    }
                )
                try:
                    df = ak.stock_board_industry_name_ths()
                    actual_source = "akshare:stock_board_industry_name_ths"
                except Exception as ak_ths_exc:
                    fallback_trace.append(
                        {
                            "source": "akshare:stock_board_industry_name_ths",
                            "error_message": str(ak_ths_exc),
                        }
                    )
                    try:
                        df = ths_curl.fetch_industry_names(limit=limit)
                        actual_source = str(
                            df.attrs.get(
                                "actual_source",
                                "ths:curl_cffi:stock_board_industry_name",
                            )
                        )
                    except Exception as ths_exc:
                        fallback_trace.append(
                            {
                                "source": "ths:curl_cffi:stock_board_industry_name",
                                "error_message": str(ths_exc),
                            }
                        )
                        return ProviderResult(
                            provider_name=self.provider_name,
                            status="error",
                            collected_at=collected_at,
                            error_message=str(ths_exc),
                            payload={
                                "endpoint": "stock_board_industry_name_em",
                                "fallback_trace": fallback_trace,
                            },
                        )
                else:
                    actual_source = str(
                        df.attrs.get(
                            "actual_source",
                            actual_source,
                        )
                    )
        names = _extract_name_catalog(df, ["板块名称", "名称", "行业名称", "name"], limit=limit)
        names, supplemental_sources, supplemental_trace = _supplement_name_catalog(
            names,
            fetch=sina_curl.fetch_industry_names,
            candidates=["板块名称", "名称", "行业名称", "name"],
            source="sina:curl_cffi:stock_sector_spot:industry",
            enabled=limit is None,
        )
        return ProviderResult(
            provider_name=self.provider_name,
            status="available" if names else "unavailable",
            collected_at=collected_at,
            payload={
                "endpoint": "stock_board_industry_name_em",
                "row_count": len(names),
                "names": names,
                "actual_source": actual_source,
                "fallback_used": bool(fallback_trace),
                "fallback_trace": fallback_trace,
                "supplemental_sources": supplemental_sources,
                "supplemental_trace": supplemental_trace,
            },
        )

    def fetch_concept_names(self, *, limit: int | None = None) -> ProviderResult:
        """获取东方财富概念板块目录。"""

        collected_at = datetime.now(tz=UTC)
        fallback_trace: list[dict[str, str]] = []
        actual_source = "akshare:stock_board_concept_name_em"
        try:
            df = ak.stock_board_concept_name_em()
        except Exception as exc:
            fallback_trace.append(
                {
                    "source": "akshare:stock_board_concept_name_em",
                    "error_message": str(exc),
                }
            )
            try:
                df = eastmoney_curl.fetch_concept_names(limit=limit)
                actual_source = str(
                    df.attrs.get(
                        "actual_source",
                        "eastmoney:curl_cffi:stock_board_concept_name_em",
                    )
                )
            except Exception as fallback_exc:
                fallback_trace.append(
                    {
                        "source": "eastmoney:curl_cffi:stock_board_concept_name_em",
                        "error_message": str(fallback_exc),
                    }
                )
                try:
                    df = ak.stock_board_concept_name_ths()
                    actual_source = "akshare:stock_board_concept_name_ths"
                except Exception as ak_ths_exc:
                    fallback_trace.append(
                        {
                            "source": "akshare:stock_board_concept_name_ths",
                            "error_message": str(ak_ths_exc),
                        }
                    )
                    try:
                        df = ths_curl.fetch_concept_names(limit=limit)
                        actual_source = str(
                            df.attrs.get(
                                "actual_source",
                                "ths:curl_cffi:stock_board_concept_name",
                            )
                        )
                    except Exception as ths_exc:
                        fallback_trace.append(
                            {
                                "source": "ths:curl_cffi:stock_board_concept_name",
                                "error_message": str(ths_exc),
                            }
                        )
                        return ProviderResult(
                            provider_name=self.provider_name,
                            status="error",
                            collected_at=collected_at,
                            error_message=str(ths_exc),
                            payload={
                                "endpoint": "stock_board_concept_name_em",
                                "fallback_trace": fallback_trace,
                            },
                        )
                else:
                    actual_source = str(
                        df.attrs.get(
                            "actual_source",
                            actual_source,
                        )
                    )
        names = _extract_name_catalog(df, ["板块名称", "名称", "概念名称", "name"], limit=limit)
        names, supplemental_sources, supplemental_trace = _supplement_name_catalog(
            names,
            fetch=sina_curl.fetch_concept_names,
            candidates=["板块名称", "名称", "概念名称", "name"],
            source="sina:curl_cffi:stock_sector_spot:concept",
            enabled=limit is None,
        )
        return ProviderResult(
            provider_name=self.provider_name,
            status="available" if names else "unavailable",
            collected_at=collected_at,
            payload={
                "endpoint": "stock_board_concept_name_em",
                "row_count": len(names),
                "names": names,
                "actual_source": actual_source,
                "fallback_used": bool(fallback_trace),
                "fallback_trace": fallback_trace,
                "supplemental_sources": supplemental_sources,
                "supplemental_trace": supplemental_trace,
            },
        )

    def fetch_index_members(
        self,
        *,
        index_code: str,
        index_name: str | None = None,
        limit: int | None = None,
    ) -> UniverseSeedsResult:
        """获取指数成分股作为候选池种子。"""

        collected_at = datetime.now(tz=UTC)
        fallback_trace: list[dict[str, str]] = []
        primary_source = "akshare:index_stock_cons_csindex"
        try:
            df = ak.index_stock_cons_csindex(symbol=index_code)
            actual_source = primary_source
        except Exception as exc:
            fallback_trace.append({"source": primary_source, "error_message": str(exc)})
            try:
                df = ak.index_stock_cons_sina(symbol=index_code)
                actual_source = "akshare:index_stock_cons_sina"
            except Exception as sina_exc:
                fallback_trace.append(
                    {
                        "source": "akshare:index_stock_cons_sina",
                        "error_message": str(sina_exc),
                    }
                )
                try:
                    df = ak.index_stock_cons(symbol=index_code)
                    actual_source = "akshare:index_stock_cons"
                except Exception as fallback_exc:
                    fallback_trace.append(
                        {
                            "source": "akshare:index_stock_cons",
                            "error_message": str(fallback_exc),
                        }
                    )
                    return UniverseSeedsResult(
                        provider_name=self.provider_name,
                        status="error",
                        collected_at=collected_at,
                        error_message=str(fallback_exc),
                        payload={
                            "endpoint": "index_stock_cons_csindex",
                            "symbol": index_code,
                            "primary_source": primary_source,
                            "fallback_trace": fallback_trace,
                        },
                    )
        seeds = normalize_ashare_index_members(
            df,
            index_code=index_code,
            index_name=index_name or index_code,
            source=actual_source,
            as_of=collected_at,
            limit=limit,
        )
        return UniverseSeedsResult(
            provider_name=self.provider_name,
            status="available" if seeds else "unavailable",
            collected_at=collected_at,
            seeds=seeds,
            payload={
                "endpoint": "index_stock_cons_csindex",
                "symbol": index_code,
                "index_name": index_name,
                "row_count": len(seeds),
                "primary_source": primary_source,
                "actual_source": actual_source,
                "fallback_used": actual_source != primary_source,
                "fallback_trace": fallback_trace,
            },
        )

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
        source_routed = False
        try:
            if sina_curl.has_industry(industry_name):
                df = sina_curl.fetch_industry_members(industry_name, limit=limit)
                actual_source = str(
                    df.attrs.get(
                        "actual_source",
                        "sina:curl_cffi:stock_sector_detail:industry",
                    )
                )
                source_routed = True
        except Exception as exc:
            fallback_trace.append(
                {
                    "source": "sina:curl_cffi:stock_sector_detail:industry",
                    "error_message": str(exc),
                }
            )
        if not source_routed:
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
                        df = sina_curl.fetch_industry_members(industry_name, limit=limit)
                        actual_source = str(
                            df.attrs.get(
                                "actual_source",
                                "sina:curl_cffi:stock_sector_detail:industry",
                            )
                        )
                    except Exception as sina_exc:
                        fallback_trace.append(
                            {
                                "source": "sina:curl_cffi:stock_sector_detail:industry",
                                "error_message": str(sina_exc),
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
                                    "source": (
                                        "ths:curl_cffi:"
                                        "stock_board_industry_detail_first_page"
                                    ),
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
                "source_routed": source_routed,
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
        source_routed = False
        try:
            if sina_curl.has_concept(concept_name):
                df = sina_curl.fetch_concept_members(concept_name, limit=limit)
                actual_source = str(
                    df.attrs.get(
                        "actual_source",
                        "sina:curl_cffi:stock_sector_detail:concept",
                    )
                )
                source_routed = True
        except Exception as exc:
            fallback_trace.append(
                {
                    "source": "sina:curl_cffi:stock_sector_detail:concept",
                    "error_message": str(exc),
                }
            )
        if not source_routed:
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
                        df = sina_curl.fetch_concept_members(concept_name, limit=limit)
                        actual_source = str(
                            df.attrs.get(
                                "actual_source",
                                "sina:curl_cffi:stock_sector_detail:concept",
                            )
                        )
                    except Exception as sina_exc:
                        fallback_trace.append(
                            {
                                "source": "sina:curl_cffi:stock_sector_detail:concept",
                                "error_message": str(sina_exc),
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
                "source_routed": source_routed,
                "source_coverage": df.attrs.get("source_coverage"),
                "source_board_code": df.attrs.get("board_code"),
            },
        )


class AshareCapitalFlowProvider:
    """A 股资金流 Provider。"""

    provider_name = "akshare"

    def fetch_individual_flow(
        self,
        *,
        symbol: str,
        market: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int | None = None,
    ) -> CapitalFlowSnapshotsResult:
        """获取单只 A 股的历史资金流明细。"""

        collected_at = datetime.now(tz=UTC)
        clean_symbol = strip_ashare_exchange_prefix(symbol)
        market_code = str(market or "").strip().lower()
        if market_code not in {"sh", "sz"}:
            market_code = "sh" if clean_symbol.startswith(("5", "6", "9")) else "sz"
        source = "akshare:stock_individual_fund_flow"
        fallback_trace: list[dict[str, str]] = []
        try:
            df = ak.stock_individual_fund_flow(
                stock=clean_symbol,
                market=market_code,
            )
        except Exception as exc:
            fallback_trace.append({"source": source, "error_message": str(exc)})
            try:
                df = eastmoney_curl.fetch_individual_fund_flow(
                    clean_symbol,
                    market_code,
                )
                source = str(
                    df.attrs.get(
                        "actual_source",
                        "eastmoney:curl_cffi:stock_individual_fund_flow",
                    )
                )
            except Exception as fallback_exc:
                fallback_trace.append(
                    {
                        "source": "eastmoney:curl_cffi:stock_individual_fund_flow",
                        "error_message": str(fallback_exc),
                    }
                )
                return CapitalFlowSnapshotsResult(
                    provider_name=self.provider_name,
                    status="error",
                    collected_at=collected_at,
                    error_message=str(fallback_exc),
                    payload={
                        "endpoint": "stock_individual_fund_flow",
                        "symbol": clean_symbol,
                        "market": market_code,
                        "fallback_trace": fallback_trace,
                    },
                )
        snapshots = normalize_ashare_individual_fund_flow(
            df,
            source=source,
            symbol=clean_symbol,
            as_of=collected_at,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )
        return CapitalFlowSnapshotsResult(
            provider_name=self.provider_name,
            status="available" if snapshots else "unavailable",
            collected_at=collected_at,
            snapshots=snapshots,
            payload={
                "endpoint": "stock_individual_fund_flow",
                "symbol": clean_symbol,
                "market": market_code,
                "row_count": len(snapshots),
                "actual_source": source,
                "fallback_trace": fallback_trace,
            },
        )

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

    def fetch_northbound_flow(
        self,
        *,
        symbol: str = "北向资金",
        limit: int | None = None,
    ) -> CapitalFlowSnapshotsResult:
        """按 symbol 路由北向市场级或个股资金数据。"""

        normalized_symbol = str(symbol or "北向资金").strip()
        if normalized_symbol in {"北向资金", "沪股通", "深股通"}:
            return self.fetch_northbound_market_flow(symbol=normalized_symbol, limit=limit)
        return self.fetch_northbound_individual_flow(symbol=normalized_symbol, limit=limit)

    def fetch_northbound_market_flow(
        self,
        *,
        symbol: str = "北向资金",
        limit: int | None = None,
    ) -> CapitalFlowSnapshotsResult:
        """获取沪深港通市场级北向资金历史。"""

        collected_at = datetime.now(tz=UTC)
        try:
            df = ak.stock_hsgt_hist_em(symbol=symbol)
            snapshots = normalize_ashare_northbound_market_flow(
                df,
                source="akshare:stock_hsgt_hist_em",
                symbol=symbol,
                as_of=collected_at,
                limit=limit,
            )
        except Exception as exc:
            return CapitalFlowSnapshotsResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
                payload={"endpoint": "stock_hsgt_hist_em", "symbol": symbol},
            )
        return CapitalFlowSnapshotsResult(
            provider_name=self.provider_name,
            status="available" if snapshots else "unavailable",
            collected_at=collected_at,
            snapshots=snapshots,
            payload={
                "endpoint": "stock_hsgt_hist_em",
                "symbol": symbol,
                "row_count": len(snapshots),
                "actual_source": "akshare:stock_hsgt_hist_em",
            },
        )

    def fetch_northbound_individual_flow(
        self,
        *,
        symbol: str,
        limit: int | None = None,
    ) -> CapitalFlowSnapshotsResult:
        """获取沪深港通个股持仓/资金历史。"""

        collected_at = datetime.now(tz=UTC)
        try:
            df = ak.stock_hsgt_individual_em(symbol=symbol)
            snapshots = normalize_ashare_northbound_individual_flow(
                df,
                source="akshare:stock_hsgt_individual_em",
                symbol=symbol,
                as_of=collected_at,
                limit=limit,
            )
        except TypeError as exc:
            if str(exc) != "'NoneType' object is not subscriptable":
                return CapitalFlowSnapshotsResult(
                    provider_name=self.provider_name,
                    status="error",
                    collected_at=collected_at,
                    error_message=str(exc),
                    payload={"endpoint": "stock_hsgt_individual_em", "symbol": symbol},
                )
            return CapitalFlowSnapshotsResult(
                provider_name=self.provider_name,
                status="unavailable",
                collected_at=collected_at,
                payload={
                    "endpoint": "stock_hsgt_individual_em",
                    "symbol": symbol,
                    "unavailable_reason": "source_returned_no_data",
                },
            )
        except Exception as exc:
            return CapitalFlowSnapshotsResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
                payload={"endpoint": "stock_hsgt_individual_em", "symbol": symbol},
            )
        return CapitalFlowSnapshotsResult(
            provider_name=self.provider_name,
            status="available" if snapshots else "unavailable",
            collected_at=collected_at,
            snapshots=snapshots,
            payload={
                "endpoint": "stock_hsgt_individual_em",
                "symbol": symbol,
                "row_count": len(snapshots),
                "actual_source": "akshare:stock_hsgt_individual_em",
            },
        )


class AshareEventProvider:
    """A 股新闻公告 Provider。"""

    provider_name = "akshare"

    def fetch_stock_news(
        self,
        *,
        symbol: str,
        asset_name: str,
        limit: int | None = None,
    ) -> EventRecordsResult:
        """获取个股新闻事件。"""

        collected_at = datetime.now(tz=UTC)
        clean_symbol = strip_ashare_exchange_prefix(symbol)
        clean_asset_name = str(asset_name or "").strip()
        if not clean_asset_name or clean_asset_name == clean_symbol:
            return EventRecordsResult(
                provider_name=self.provider_name,
                status="unavailable",
                collected_at=collected_at,
                payload={
                    "endpoint": "stock_news_em",
                    "symbol": clean_symbol,
                    "asset_name": clean_asset_name,
                    "reason": "missing_asset_name",
                },
            )
        query = f"{clean_symbol} {clean_asset_name}"
        try:
            df = ak.stock_news_em(symbol=query)
            normalized = normalize_ashare_stock_news(
                df,
                symbol=clean_symbol,
                asset_name=clean_asset_name,
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
                payload={
                    "endpoint": "stock_news_em",
                    "symbol": clean_symbol,
                    "asset_name": clean_asset_name,
                    "query": query,
                },
            )
        return EventRecordsResult(
            provider_name=self.provider_name,
            status="available" if normalized.events else "unavailable",
            collected_at=collected_at,
            events=normalized.events,
            evidence=normalized.evidence,
            payload={
                "endpoint": "stock_news_em",
                "symbol": clean_symbol,
                "asset_name": clean_asset_name,
                "query": query,
                "row_count": len(normalized.events),
                "entity_validation": normalized.entity_validation,
            },
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


def _extract_name_catalog(
    df: object,
    candidates: list[str],
    *,
    limit: int | None = None,
) -> list[str]:
    """从 AKShare 目录 DataFrame 中提取板块名称。"""

    rows = df.head(limit).to_dict("records") if limit else df.to_dict("records")
    names: list[str] = []
    for row in rows:
        value = _first_present(row, candidates)
        name = str(value or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _supplement_name_catalog(
    names: list[str],
    *,
    fetch: Any,
    candidates: list[str],
    source: str,
    enabled: bool,
) -> tuple[list[str], list[str], list[dict[str, str]]]:
    """在全量刷新时合并独立免费目录，失败不影响已有主目录。"""

    if not enabled:
        return names, [], []
    try:
        frame = fetch(limit=None)
    except Exception as exc:
        return names, [], [{"source": source, "error_message": str(exc)}]
    supplemental = _extract_name_catalog(frame, candidates)
    merged = list(dict.fromkeys([*names, *supplemental]))
    actual_source = str(frame.attrs.get("actual_source", source))
    return merged, [actual_source], []


def _extract_index_catalog(df: object, *, limit: int | None = None) -> list[dict[str, str]]:
    """从 AKShare 指数目录 DataFrame 中提取指数代码和名称。"""

    rows = df.head(limit).to_dict("records") if limit else df.to_dict("records")
    indexes: list[dict[str, str]] = []
    for row in rows:
        code = str(
            _first_present(row, ["index_code", "指数代码", "代码", "symbol"]) or ""
        ).strip()
        name = str(
            _first_present(row, ["display_name", "指数名称", "名称", "name"]) or code
        ).strip()
        if code:
            indexes.append({"code": code, "name": name or code})
    return indexes


def _first_present(row: dict[str, object], candidates: list[str]) -> object | None:
    """返回第一列存在且非空的值。"""

    for key in candidates:
        value = row.get(key)
        if value is not None and str(value).strip():
            return value
    return None
