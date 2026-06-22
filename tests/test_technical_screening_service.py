from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from finance_agent.application.technical_screening_service import (
    TECHNICAL_SCREENING_SOURCE_TYPE,
    TECHNICAL_SCREENING_STRATEGY,
    TECHNICAL_SCREENING_UNIVERSE_ID,
    TechnicalScreeningService,
)


def test_technical_screening_accepts_tradeable_main_board_assets_only() -> None:
    """技术初筛只接收可交易主板股票，并跳过覆盖不足的标的。"""

    as_of = datetime(2026, 6, 9, tzinfo=UTC)
    service = TechnicalScreeningService(session=None)
    assets = [
        asset("ashare:600519", "600519"),
        asset("ashare:300750", "300750"),
        asset("ashare:000001", "000001"),
    ]

    result = service.screen_assets(
        assets=assets,
        bars_by_asset_id={
            "ashare:600519": rising_bars("600519", as_of=as_of, count=260),
            "ashare:000001": rising_bars("000001", as_of=as_of, count=120),
        },
        as_of=as_of,
        min_bars=250,
        persist=False,
    )

    assert [item.asset_id for item in result.candidates] == ["ashare:600519", "ashare:000001"]
    assert result.accepted_count == 1
    assert result.rejected_count == 1
    assert result.skipped_count == 1
    assert result.candidates[0].passed is True
    assert result.candidates[1].passed is False
    assert result.candidates[1].removed_reason == "历史日 K 覆盖不足"


def test_screen_ashare_filters_tradeable_assets_before_limit() -> None:
    """limit 应限制可交易资产数量，不能先截断后再过滤。"""

    as_of = datetime(2026, 6, 9, tzinfo=UTC)
    assets = [
        asset("ashare:300750", "300750"),
        asset("ashare:600519", "600519"),
    ]
    service = TechnicalScreeningService(
        session=None,
        assets=FakeAssetRepository(assets),
        market_data=FakeMarketDataRepository(
            {"ashare:600519": rising_bars("600519", as_of=as_of, count=260)}
        ),
    )

    result = service.screen_ashare(limit=1, as_of=as_of, min_bars=250, persist=False)

    assert [item.asset_id for item in result.candidates] == ["ashare:600519"]
    assert result.accepted_count == 1


def test_technical_screening_scores_trend_momentum_risk_and_liquidity() -> None:
    """技术初筛结果应包含可解释得分、规则命中、有效期和来源类型。"""

    as_of = datetime(2026, 6, 9, 15, 30, tzinfo=UTC)
    service = TechnicalScreeningService(session=None)

    result = service.screen_assets(
        assets=[asset("ashare:600519", "600519")],
        bars_by_asset_id={"ashare:600519": rising_bars("600519", as_of=as_of, count=260)},
        as_of=as_of,
        min_bars=250,
        persist=False,
    )

    candidate = result.candidates[0]
    assert candidate.source_type == TECHNICAL_SCREENING_SOURCE_TYPE
    assert candidate.passed is True
    assert candidate.technical_score >= Decimal("75")
    assert candidate.expires_at == as_of + timedelta(days=3)
    assert "trend_ma60" in candidate.passed_rules
    assert "momentum_60d" in candidate.passed_rules
    assert "liquidity_amount" in candidate.passed_rules
    assert candidate.payload["strategy"] == TECHNICAL_SCREENING_STRATEGY
    assert candidate.payload["recommendation_semantics"] == "not_buy_signal"
    assert candidate.payload["metrics"]["bar_count"] == 260


def test_technical_screening_persists_screening_result_items() -> None:
    """技术初筛应写入 screening_results / screening_result_items 作为池子快照。"""

    as_of = datetime(2026, 6, 9, 15, 30, tzinfo=UTC)
    fake_screenings = FakeScreeningRepository()
    service = TechnicalScreeningService(session=None, screenings=fake_screenings)

    result = service.screen_assets(
        assets=[asset("ashare:600519", "600519")],
        bars_by_asset_id={"ashare:600519": rising_bars("600519", as_of=as_of, count=260)},
        as_of=as_of,
        min_bars=250,
        persist=True,
    )

    assert result.screening_id.startswith("screen:technical:ashare:main_board:")
    assert fake_screenings.results[0]["strategy"] == TECHNICAL_SCREENING_STRATEGY
    assert fake_screenings.results[0]["passed_count"] == 1
    assert fake_screenings.items[0]["source_type"] == TECHNICAL_SCREENING_SOURCE_TYPE
    assert fake_screenings.items[0]["payload"]["expires_at"] == candidate_iso(result)


def test_technical_screening_persists_universe_members_for_merge_source() -> None:
    """技术初筛结果应同步成候选池，供后续推荐候选池合并任务读取。"""

    as_of = datetime(2026, 6, 9, 15, 30, tzinfo=UTC)
    fake_screenings = FakeScreeningRepository()
    fake_universes = FakeUniverseRepository()
    service = TechnicalScreeningService(
        session=None,
        screenings=fake_screenings,
        universes=fake_universes,
    )

    result = service.screen_assets(
        assets=[asset("ashare:600519", "600519"), asset("ashare:000001", "000001")],
        bars_by_asset_id={
            "ashare:600519": rising_bars("600519", as_of=as_of, count=260),
            "ashare:000001": rising_bars("000001", as_of=as_of, count=120),
        },
        as_of=as_of,
        min_bars=250,
        persist=True,
    )

    assert fake_universes.universes[0]["universe_id"] == TECHNICAL_SCREENING_UNIVERSE_ID
    assert fake_universes.universes[0]["total_before_filter"] == 2
    assert fake_universes.universes[0]["total_after_filter"] == 1
    assert fake_universes.universes[0]["payload"]["screening_id"] == result.screening_id
    members = fake_universes.members[TECHNICAL_SCREENING_UNIVERSE_ID]
    assert [item["asset_id"] for item in members if item["included"]] == ["ashare:600519"]
    assert [item["asset_id"] for item in members if not item["included"]] == ["ashare:000001"]
    assert members[0]["payload"]["screening_id"] == result.screening_id
    assert members[0]["payload"]["source_type"] == TECHNICAL_SCREENING_SOURCE_TYPE


class FakeScreeningRepository:
    def __init__(self) -> None:
        self.results: list[dict[str, Any]] = []
        self.items: list[dict[str, Any]] = []

    def upsert_screening_result(self, **kwargs: Any) -> SimpleNamespace:
        self.results.append(kwargs)
        return SimpleNamespace(**kwargs)

    def upsert_screening_item(self, **kwargs: Any) -> SimpleNamespace:
        self.items.append(kwargs)
        payload = dict(kwargs.get("payload") or {})
        return SimpleNamespace(source_type=payload.get("source_type"), **kwargs)


class FakeUniverseRepository:
    def __init__(self) -> None:
        self.universes: list[dict[str, Any]] = []
        self.members: dict[str, list[dict[str, Any]]] = {}

    def upsert_universe(self, **kwargs: Any) -> SimpleNamespace:
        self.universes.append(kwargs)
        return SimpleNamespace(**kwargs)

    def replace_members(self, *, universe_id: str, members: list[dict[str, Any]]) -> list[Any]:
        self.members[universe_id] = list(members)
        return [SimpleNamespace(**member) for member in members]


class FakeAssetRepository:
    def __init__(self, assets: list[SimpleNamespace]) -> None:
        self.assets = assets

    def find_by_market(self, market: str, *, only_tradable: bool = False) -> list[SimpleNamespace]:
        return [
            item
            for item in self.assets
            if item.market == market and (not only_tradable or item.tradable)
        ]


class FakeMarketDataRepository:
    def __init__(self, bars_by_asset_id: dict[str, list[SimpleNamespace]]) -> None:
        self.bars_by_asset_id = bars_by_asset_id

    def list_recent_bars(
        self,
        *,
        asset_id: str,
        timeframe: str,
        limit: int,
        source: str | None = None,
    ) -> list[SimpleNamespace]:
        return self.bars_by_asset_id.get(asset_id, [])[-limit:]


def candidate_iso(result: Any) -> str:
    return result.candidates[0].expires_at.isoformat()


def asset(asset_id: str, symbol: str) -> SimpleNamespace:
    return SimpleNamespace(
        asset_id=asset_id,
        symbol=symbol,
        market="ashare",
        asset_type="stock",
        tradable=True,
    )


def rising_bars(symbol: str, *, as_of: datetime, count: int) -> list[SimpleNamespace]:
    start = as_of - timedelta(days=count)
    rows: list[SimpleNamespace] = []
    price = Decimal("20")
    for index in range(count):
        price += Decimal("0.05")
        rows.append(
            SimpleNamespace(
                asset_id=f"ashare:{symbol}",
                symbol=symbol,
                market="ashare",
                timestamp=start + timedelta(days=index),
                open=price - Decimal("0.03"),
                high=price + Decimal("0.10"),
                low=price - Decimal("0.10"),
                close=price,
                volume=Decimal("1000000"),
                amount=Decimal("120000000"),
            )
        )
    return rows
