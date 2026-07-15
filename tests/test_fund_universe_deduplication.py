from __future__ import annotations

from datetime import UTC, datetime

from finance_agent.data import collectors as collectors_module
from finance_agent.data.collectors import (
    FundDataCollector,
    _deduplicate_fund_assets_by_symbol,
)
from finance_agent.data.models import AssetData, AssetListResult


def test_fund_universe_deduplication_prefers_exchange_traded_assets() -> None:
    """同代码基金同时出现在开放式和场内列表时，应优先保留 ETF/LOF 资产。"""

    open_fund = AssetData(
        asset_id="fund:open:501046",
        symbol="501046",
        name="财通多策略福鑫定开混合",
        market="fund",
        asset_type="open_fund",
    )
    lof = AssetData(
        asset_id="fund:lof:501046",
        symbol="501046",
        name="财通福鑫定开混合",
        market="fund",
        asset_type="lof",
    )
    regular_open_fund = AssetData(
        asset_id="fund:open:000001",
        symbol="000001",
        name="华夏成长混合",
        market="fund",
        asset_type="open_fund",
    )

    deduped = _deduplicate_fund_assets_by_symbol([open_fund, lof, regular_open_fund])

    assert [asset.asset_id for asset in deduped] == ["fund:lof:501046", "fund:open:000001"]


def test_fund_universe_reuses_existing_lof_identity_when_lof_source_fails(
    monkeypatch,
) -> None:
    """LOF 源失败时也应继承库内场内身份，避免开放式占位冲突。"""

    collected_at = datetime(2026, 7, 16, tzinfo=UTC)
    persisted_assets: list[list[AssetData]] = []
    universe_members: list[dict[str, object]] = []
    prune_calls: list[dict[str, object]] = []

    class _Assets:
        def find_by_market(self, market: str, *, only_tradable: bool = True):
            assert market == "fund"
            assert only_tradable is False
            return [
                AssetData(
                    asset_id="fund:lof:160632",
                    symbol="160632",
                    name="酒LOF",
                    market="fund",
                    asset_type="lof",
                )
            ]

        def delete_fund_open_placeholders_without_nav(self, _symbols):
            return 0

    class _Universes:
        def upsert_universe(self, **_kwargs):
            pass

        def replace_members(self, *, universe_id: str, members):
            assert universe_id == "universe:base:fund:all"
            universe_members.extend(members)

        def prune_missing_members(self, **kwargs):
            prune_calls.append(kwargs)
            return 1

    class _Provider:
        def fetch_etf_assets(self):
            return AssetListResult(
                provider_name="akshare",
                status="available",
                collected_at=collected_at,
                assets=[
                    AssetData(
                        asset_id="fund:etf:510300",
                        symbol="510300",
                        name="沪深300ETF",
                        market="fund",
                        asset_type="etf",
                    )
                ],
            )

        def fetch_lof_assets(self):
            return AssetListResult(
                provider_name="akshare",
                status="error",
                collected_at=collected_at,
                error_message="连接中断",
                assets=[],
            )

        def fetch_open_fund_assets(self):
            return AssetListResult(
                provider_name="akshare",
                status="available",
                collected_at=collected_at,
                assets=[
                    AssetData(
                        asset_id="fund:open:160632",
                        symbol="160632",
                        name="鹏华酒A",
                        market="fund",
                        asset_type="open_fund",
                    ),
                    AssetData(
                        asset_id="fund:open:000001",
                        symbol="000001",
                        name="华夏成长混合",
                        market="fund",
                        asset_type="open_fund",
                    ),
                ],
            )

    monkeypatch.setattr(
        collectors_module,
        "archive_provider_result",
        lambda _repository, _result, *, endpoint, **_kwargs: f"raw:{endpoint}",
    )
    monkeypatch.setattr(
        collectors_module,
        "_persist_asset_identity_rows",
        lambda _repository, assets, **_kwargs: persisted_assets.append(list(assets)),
    )

    collector = FundDataCollector.__new__(FundDataCollector)
    collector.assets = _Assets()
    collector.universes = _Universes()
    collector.raw_records = object()
    collector.provider = _Provider()

    collector.collect_universe(
        universe_id="universe:base:fund:all",
        universe_name="基础数据采集基金候选池",
        strategy_context="base_data_collect",
    )

    persisted_ids = [asset.asset_id for batch in persisted_assets for asset in batch]
    member_ids = [str(member["asset_id"]) for member in universe_members]
    assert persisted_ids == ["fund:etf:510300", "fund:open:000001"]
    assert member_ids == ["fund:etf:510300", "fund:lof:160632", "fund:open:000001"]
    assert prune_calls == [
        {
            "universe_id": "universe:base:fund:all",
            "current_asset_ids": [
                "fund:etf:510300",
                "fund:lof:160632",
                "fund:open:000001",
            ],
            "as_of": collected_at,
            "removed_reason": "not_in_latest_fund_universe",
        }
    ]
