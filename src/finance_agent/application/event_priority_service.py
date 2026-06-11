"""事件重点资产解析服务。"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Iterable

from sqlalchemy import select

from finance_agent.application.asset_eligibility_service import is_tradeable_ashare_symbol
from finance_agent.data.normalizers import normalize_ashare_symbol
from finance_agent.storage.orm import (
    AssetRecommendationORM,
    ScreeningResultItemORM,
    WatchlistItemORM,
    WatchlistORM,
)


USER_WATCHLIST_PURPOSES = frozenset(
    {"manual_watchlist", "portfolio_watchlist", "fund_watchlist"}
)
SYSTEM_RESEARCH_PURPOSE = "system_research_pool"
TECHNICAL_SCREENING_POOL_SOURCE = "technical_screening_pool"
RECENT_RECOMMENDATION_SOURCE = "recent_recommendation"
SKIPPED_RECOMMENDATION_ACTIONS = frozenset({"avoid", "reject"})


@dataclass(frozen=True)
class EventPriorityAsset:
    """一次事件任务中的重点资产条目。"""

    asset_id: str
    symbol: str
    market: str
    sources: tuple[str, ...]
    priority_score: int


class EventPriorityResolver:
    """按用户观察、系统研究和最近推荐动态解析事件重点资产。"""

    def __init__(self, session: Any | None) -> None:
        self.session = session

    def resolve_ashare_symbols(self, *, limit: int = 200) -> list[str]:
        """返回 A 股逐股新闻任务使用的重点代码列表。"""

        return [item.symbol for item in self.resolve_ashare_assets(limit=limit)]

    def resolve_ashare_assets(self, *, limit: int = 200) -> list[EventPriorityAsset]:
        """从数据库读取来源并生成 A 股事件重点资产。"""

        if self.session is None or limit <= 0:
            return []
        return self.resolve_from_sources(
            user_watchlist=self._fetch_watchlist_items(
                purposes=USER_WATCHLIST_PURPOSES,
                limit=limit,
            ),
            research_watchlist=self._fetch_watchlist_items(
                purposes={SYSTEM_RESEARCH_PURPOSE},
                limit=limit,
            ),
            technical_screening=self._fetch_technical_screening_items(limit=limit * 2),
            recommendations=self._fetch_recent_recommendations(limit=limit * 2),
            limit=limit,
        )

    def resolve_from_sources(
        self,
        *,
        user_watchlist: Iterable[Any],
        research_watchlist: Iterable[Any],
        recommendations: Iterable[Any],
        technical_screening: Iterable[Any] = (),
        limit: int,
    ) -> list[EventPriorityAsset]:
        """根据已加载来源计算事件重点资产，便于单元测试和离线复用。"""

        if limit <= 0:
            return []

        candidates: dict[str, dict[str, Any]] = {}
        ordered_symbols: list[str] = []

        for item in user_watchlist:
            self._append_candidate(
                candidates,
                ordered_symbols,
                item,
                source=str(getattr(item, "purpose", "") or "manual_watchlist"),
            )
            if len(ordered_symbols) >= limit:
                break

        if len(ordered_symbols) < limit:
            for item in research_watchlist:
                self._append_candidate(
                    candidates,
                    ordered_symbols,
                    item,
                    source=SYSTEM_RESEARCH_PURPOSE,
                )
                if len(ordered_symbols) >= limit:
                    break

        if len(ordered_symbols) < limit:
            for item in technical_screening:
                if getattr(item, "passed", True) is False:
                    continue
                self._append_candidate(
                    candidates,
                    ordered_symbols,
                    item,
                    source=TECHNICAL_SCREENING_POOL_SOURCE,
                )
                if len(ordered_symbols) >= limit:
                    break

        if len(ordered_symbols) < limit:
            for item in recommendations:
                action = str(getattr(item, "action", "") or "").strip().lower()
                if action in SKIPPED_RECOMMENDATION_ACTIONS:
                    continue
                self._append_candidate(
                    candidates,
                    ordered_symbols,
                    item,
                    source=RECENT_RECOMMENDATION_SOURCE,
                )
                if len(ordered_symbols) >= limit:
                    break

        return [
            EventPriorityAsset(
                asset_id=candidate["asset_id"],
                symbol=symbol,
                market="ashare",
                sources=tuple(candidate["sources"]),
                priority_score=int(candidate["priority_score"]),
            )
            for symbol in ordered_symbols[:limit]
            for candidate in [candidates[symbol]]
        ]

    def _append_candidate(
        self,
        candidates: dict[str, dict[str, Any]],
        ordered_symbols: list[str],
        item: Any,
        *,
        source: str,
    ) -> None:
        normalized = normalize_ashare_symbol(str(getattr(item, "symbol", "") or ""))
        if not is_tradeable_ashare_symbol(normalized):
            return

        if normalized not in candidates:
            candidates[normalized] = {
                "asset_id": getattr(item, "asset_id", None) or f"ashare:{normalized}",
                "sources": [],
                "priority_score": source_priority_score(source),
            }
            ordered_symbols.append(normalized)
        candidate = candidates[normalized]
        if source not in candidate["sources"]:
            candidate["sources"].append(source)
        candidate["priority_score"] = max(
            int(candidate["priority_score"]),
            source_priority_score(source),
        )

    def _fetch_watchlist_items(self, *, purposes: Iterable[str], limit: int) -> list[Any]:
        """读取指定 purpose 的 active 观察项。"""

        statement = (
            select(WatchlistItemORM, WatchlistORM.purpose)
            .join(WatchlistORM, WatchlistORM.watchlist_id == WatchlistItemORM.watchlist_id)
            .where(
                WatchlistItemORM.market == "ashare",
                WatchlistItemORM.status == "active",
                WatchlistORM.status == "active",
                WatchlistORM.purpose.in_(set(purposes)),
            )
            .order_by(
                WatchlistItemORM.next_review_at.asc().nullslast(),
                WatchlistItemORM.updated_at.desc(),
            )
            .limit(limit)
        )
        return [
            SimpleNamespace(
                asset_id=item.asset_id,
                symbol=item.symbol,
                purpose=purpose,
            )
            for item, purpose in self.session.execute(statement)
        ]

    def _fetch_technical_screening_items(self, *, limit: int) -> list[Any]:
        """读取最近技术初筛通过项。"""

        statement = (
            select(ScreeningResultItemORM)
            .where(
                ScreeningResultItemORM.market == "ashare",
                ScreeningResultItemORM.passed.is_(True),
                ScreeningResultItemORM.screening_id.like("screen:technical:ashare:%"),
            )
            .order_by(
                ScreeningResultItemORM.as_of.desc(),
                ScreeningResultItemORM.symbol.asc(),
            )
            .limit(limit)
        )
        return [
            SimpleNamespace(
                asset_id=item.asset_id,
                symbol=item.symbol,
                passed=item.passed,
                source_type=TECHNICAL_SCREENING_POOL_SOURCE,
            )
            for item in self.session.scalars(statement)
        ]

    def _fetch_recent_recommendations(self, *, limit: int) -> list[Any]:
        """读取最近非硬过滤推荐。"""

        statement = (
            select(AssetRecommendationORM)
            .where(
                AssetRecommendationORM.market == "ashare",
                AssetRecommendationORM.action.not_in(SKIPPED_RECOMMENDATION_ACTIONS),
            )
            .order_by(
                AssetRecommendationORM.created_at.desc(),
                AssetRecommendationORM.rank.asc(),
            )
            .limit(limit)
        )
        return list(self.session.scalars(statement))


def source_priority_score(source: str) -> int:
    """按来源给出粗粒度优先级分。"""

    return {
        "portfolio_watchlist": 110,
        "manual_watchlist": 100,
        "fund_watchlist": 90,
        SYSTEM_RESEARCH_PURPOSE: 70,
        TECHNICAL_SCREENING_POOL_SOURCE: 60,
        RECENT_RECOMMENDATION_SOURCE: 50,
    }.get(source, 40)
