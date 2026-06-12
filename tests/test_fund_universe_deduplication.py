from __future__ import annotations

from finance_agent.data.collectors import _deduplicate_fund_assets_by_symbol
from finance_agent.data.models import AssetData


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
