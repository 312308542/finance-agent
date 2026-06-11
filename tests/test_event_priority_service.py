from types import SimpleNamespace

from finance_agent.application.event_priority_service import EventPriorityResolver


def test_event_priority_resolver_prefers_user_watch_then_research_then_recommendations() -> None:
    """事件重点池应按用户观察、系统研究、最近推荐的顺序生成名单。"""

    resolver = EventPriorityResolver(session=None)

    result = resolver.resolve_from_sources(
        user_watchlist=[
            SimpleNamespace(asset_id="ashare:600519", symbol="600519", purpose="manual_watchlist"),
        ],
        research_watchlist=[
            SimpleNamespace(
                asset_id="ashare:000001",
                symbol="000001",
                purpose="system_research_pool",
            ),
        ],
        recommendations=[
            SimpleNamespace(asset_id="ashare:600036", symbol="600036", action="watch"),
            SimpleNamespace(asset_id="ashare:000001", symbol="000001", action="buy_candidate"),
        ],
        technical_screening=[],
        limit=10,
    )

    assert [item.symbol for item in result] == ["600519", "000001", "600036"]
    assert result[0].sources == ("manual_watchlist",)
    assert result[1].sources == ("system_research_pool", "recent_recommendation")


def test_event_priority_resolver_filters_non_tradeable_ashare_symbols() -> None:
    """事件重点池只保留用户可交易的 A 股主板代码。"""

    resolver = EventPriorityResolver(session=None)

    result = resolver.resolve_from_sources(
        user_watchlist=[
            SimpleNamespace(asset_id="ashare:300750", symbol="300750", purpose="manual_watchlist"),
            SimpleNamespace(asset_id="ashare:688363", symbol="688363", purpose="manual_watchlist"),
            SimpleNamespace(asset_id="ashare:873124", symbol="873124", purpose="manual_watchlist"),
            SimpleNamespace(asset_id="ashare:002594", symbol="002594", purpose="manual_watchlist"),
        ],
        research_watchlist=[],
        recommendations=[],
        technical_screening=[],
        limit=10,
    )

    assert [item.symbol for item in result] == ["002594"]


def test_event_priority_resolver_uses_technical_screening_before_recent_recommendations() -> None:
    """技术初筛池应低于研究跟踪池、高于普通最近推荐。"""

    resolver = EventPriorityResolver(session=None)

    result = resolver.resolve_from_sources(
        user_watchlist=[],
        research_watchlist=[
            SimpleNamespace(asset_id="ashare:000001", symbol="000001", purpose="system_research_pool"),
        ],
        technical_screening=[
            SimpleNamespace(
                asset_id="ashare:600519",
                symbol="600519",
                passed=True,
                source_type="technical_screening",
            ),
            SimpleNamespace(
                asset_id="ashare:300750",
                symbol="300750",
                passed=True,
                source_type="technical_screening",
            ),
        ],
        recommendations=[
            SimpleNamespace(asset_id="ashare:600036", symbol="600036", action="watch"),
            SimpleNamespace(asset_id="ashare:600519", symbol="600519", action="buy_candidate"),
        ],
        limit=10,
    )

    assert [item.symbol for item in result] == ["000001", "600519", "600036"]
    assert result[1].sources == ("technical_screening_pool", "recent_recommendation")
