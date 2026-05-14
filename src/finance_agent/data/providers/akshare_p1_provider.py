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
        try:
            df = ak.stock_board_industry_cons_em(symbol=industry_name)
            seeds = normalize_ashare_board_members(
                df,
                source_name=industry_name,
                source_type="industry",
                as_of=collected_at,
                limit=limit,
            )
        except Exception as exc:
            return UniverseSeedsResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
                payload={"endpoint": "stock_board_industry_cons_em", "symbol": industry_name},
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
        try:
            df = ak.stock_board_concept_cons_em(symbol=concept_name)
            seeds = normalize_ashare_board_members(
                df,
                source_name=concept_name,
                source_type="concept",
                as_of=collected_at,
                limit=limit,
            )
        except Exception as exc:
            return UniverseSeedsResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
                payload={"endpoint": "stock_board_concept_cons_em", "symbol": concept_name},
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
        try:
            df = ak.stock_individual_fund_flow_rank(indicator=indicator)
            snapshots = normalize_ashare_fund_flow_rank(
                df,
                source="akshare:stock_individual_fund_flow_rank",
                window=indicator,
                as_of=collected_at,
                limit=limit,
            )
        except Exception as exc:
            return CapitalFlowSnapshotsResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=str(exc),
                payload={
                    "endpoint": "stock_individual_fund_flow_rank",
                    "indicator": indicator,
                },
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
