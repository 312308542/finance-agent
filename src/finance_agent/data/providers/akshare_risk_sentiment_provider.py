"""AKShare A 股风险和情绪 Provider。

这一层只负责把停复牌、龙虎榜、大宗交易、两融、热度榜和涨停池
采集为结构化结果，不做推荐打分，也不直接生成买卖建议。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import akshare as ak

from finance_agent.data.models import RiskFindingsResult, SentimentSignalsResult
from finance_agent.data.normalizers import (
    normalize_ashare_block_trades,
    normalize_ashare_delist_list,
    normalize_ashare_hot_rank,
    normalize_ashare_lhb_detail,
    normalize_ashare_margin_summary,
    normalize_ashare_pledge_ratio,
    normalize_ashare_restricted_release_detail,
    normalize_ashare_st_list,
    normalize_ashare_stop_list,
    normalize_ashare_zt_pool,
)
from finance_agent.data.providers import eastmoney_curl


class AshareRiskProvider:
    """A 股风险数据 Provider。"""

    provider_name = "akshare"

    def fetch_stop_list(self, *, limit: int | None = None) -> RiskFindingsResult:
        """获取停复牌、ST 和退市相关风险，用于不可交易风险过滤。"""

        collected_at = datetime.now(tz=UTC)
        endpoint = "stock_zh_a_stop_em"
        primary_source = f"akshare:{endpoint}"
        fallback_trace: list[dict[str, str]] = []
        actual_source = primary_source
        source_coverage = None
        risks: list[Any] = []
        events: list[Any] = []
        try:
            df = ak.stock_zh_a_stop_em()
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
                df = None
        if df is not None:
            try:
                risks, events = normalize_ashare_stop_list(
                    df,
                    source=actual_source,
                    collected_at=collected_at,
                    limit=limit,
                )
                source_coverage = df.attrs.get("source_coverage")
            except Exception as exc:
                fallback_trace.append(
                    {
                        "source": actual_source,
                        "status": "normalize_error",
                        "error_message": str(exc),
                    }
                )
        supplemental_risks, supplemental_events, supplemental_trace = (
            self._fetch_supplemental_trading_status_risks(
                collected_at=collected_at,
                limit=limit,
            )
        )
        risks.extend(supplemental_risks)
        events.extend(supplemental_events)
        fallback_trace.extend(supplemental_trace)
        if not risks and not events and df is None:
            return RiskFindingsResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message="停复牌主源、curl_cffi fallback 和补充替代源均未返回可用风险数据",
                payload={
                    "endpoint": endpoint,
                    "primary_source": primary_source,
                    "fallback_trace": fallback_trace,
                },
            )
        status = "available" if risks or events else "unavailable"
        if df is None and (risks or events):
            status = "partial"
        return RiskFindingsResult(
            provider_name=self.provider_name,
            status=status,
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
                "source_coverage": source_coverage,
                "risk_types": sorted({risk.risk_type for risk in risks}),
            },
        )

    def _fetch_supplemental_trading_status_risks(
        self,
        *,
        collected_at: datetime,
        limit: int | None,
    ) -> tuple[list[Any], list[Any], list[dict[str, str]]]:
        """采集 ST 和退市替代源，补充单一停牌接口覆盖不足的问题。"""

        risks: list[Any] = []
        events: list[Any] = []
        trace: list[dict[str, str]] = []
        for endpoint, loader, normalizer in (
            ("stock_zh_a_st_em", ak.stock_zh_a_st_em, normalize_ashare_st_list),
            ("stock_info_sh_delist", ak.stock_info_sh_delist, normalize_ashare_delist_list),
            ("stock_info_sz_delist", ak.stock_info_sz_delist, normalize_ashare_delist_list),
            ("stock_staq_net_stop", ak.stock_staq_net_stop, normalize_ashare_delist_list),
        ):
            source = f"akshare:{endpoint}"
            try:
                df = loader()
                normalized_risks, normalized_events = normalizer(
                    df,
                    source=source,
                    collected_at=collected_at,
                    limit=limit,
                )
                risks.extend(normalized_risks)
                events.extend(normalized_events)
                trace.append(
                    {
                        "source": source,
                        "status": "available" if normalized_risks or normalized_events else "empty",
                    }
                )
            except AttributeError as exc:
                trace.append({"source": source, "status": "missing", "error_message": str(exc)})
            except Exception as exc:
                trace.append({"source": source, "status": "error", "error_message": str(exc)})
        return risks, events, trace

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

    def fetch_restricted_release(
        self,
        *,
        start_date: str,
        end_date: str,
        limit: int | None = None,
        risk_window_days: int = 30,
        risk_ratio_threshold: Any = "0.05",
    ) -> RiskFindingsResult:
        """获取限售解禁详情，用于事件因子和临近解禁风险。"""

        collected_at = datetime.now(tz=UTC)
        endpoint = "stock_restricted_release_detail_em"
        source = f"akshare:{endpoint}"
        try:
            df = ak.stock_restricted_release_detail_em(
                start_date=start_date,
                end_date=end_date,
            )
            risks, events = normalize_ashare_restricted_release_detail(
                df,
                source=source,
                collected_at=collected_at,
                limit=limit,
                risk_window_days=risk_window_days,
                risk_ratio_threshold=risk_ratio_threshold,
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
            status="available" if risks or events else "unavailable",
            collected_at=collected_at,
            risks=risks,
            events=events,
            payload={
                "endpoint": endpoint,
                "start_date": start_date,
                "end_date": end_date,
                "row_count": len(events),
                "risk_count": len(risks),
                "actual_source": source,
            },
        )

    def fetch_pledge_ratio(
        self,
        *,
        date: str | None = None,
        limit: int | None = None,
        risk_ratio_threshold: Any = "0.30",
    ) -> RiskFindingsResult:
        """获取上市公司股权质押比例，用于风险反驳和回避池。"""

        collected_at = datetime.now(tz=UTC)
        endpoint = "stock_gpzy_pledge_ratio_em"
        source = f"akshare:{endpoint}"
        try:
            requested_date = compact_akshare_date(date or collected_at.strftime("%Y%m%d"))
            query_date = requested_date
            fallback_trace: list[dict[str, str]] = []
            try:
                df = ak.stock_gpzy_pledge_ratio_em(date=query_date)
                fallback_used = False
            except Exception as primary_exc:
                fallback_trace.append(
                    {"date": query_date, "error_message": str(primary_exc)}
                )
                fallback_date = self._latest_pledge_ratio_date(requested_date)
                if not fallback_date or fallback_date == requested_date:
                    raise
                query_date = fallback_date
                df = ak.stock_gpzy_pledge_ratio_em(date=query_date)
                fallback_used = True
            risks = normalize_ashare_pledge_ratio(
                df,
                source=source,
                collected_at=collected_at,
                limit=limit,
                risk_ratio_threshold=Decimal(str(risk_ratio_threshold)),
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
                "requested_date": requested_date,
                "date": query_date,
                "row_count": len(risks),
                "actual_source": source,
                "risk_ratio_threshold": str(risk_ratio_threshold),
                "fallback_used": fallback_used,
                "fallback_trace": fallback_trace,
            },
        )

    def _latest_pledge_ratio_date(self, requested_date: str) -> str | None:
        """从股权质押概览中选择不晚于请求日的最近可用交易日。"""

        profile_df = ak.stock_gpzy_profile_em()
        requested = parse_compact_akshare_date(requested_date)
        candidates: list[date] = []
        for row in profile_df.to_dict("records"):
            trade_date = parse_compact_akshare_date(row.get("交易日期"))
            if trade_date is None:
                continue
            if requested is None or trade_date <= requested:
                candidates.append(trade_date)
        if not candidates:
            return None
        return max(candidates).strftime("%Y%m%d")


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


def compact_akshare_date(value: Any) -> str:
    """把日期值压缩为 AKShare 常用的 YYYYMMDD 字符串。"""

    parsed = parse_compact_akshare_date(value)
    if parsed is not None:
        return parsed.strftime("%Y%m%d")
    digits = "".join(char for char in str(value or "") if char.isdigit())
    return digits[:8] if len(digits) >= 8 else str(value or "").strip()


def parse_compact_akshare_date(value: Any) -> date | None:
    """解析 AKShare/东方财富返回的日期字段。"""

    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    digits = "".join(char for char in text if char.isdigit())
    for candidate, fmt in ((digits[:8], "%Y%m%d"), (text[:10], "%Y-%m-%d")):
        if not candidate:
            continue
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    return None
