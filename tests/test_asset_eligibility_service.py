from __future__ import annotations

from types import SimpleNamespace

from finance_agent.application.asset_eligibility_service import (
    TradeableAssetEligibilityService,
    is_tradeable_ashare_symbol,
)


def test_tradeable_ashare_symbol_allows_main_board_only() -> None:
    """A 股可交易准入只允许用户可买的主板 6 位股票代码。"""

    assert is_tradeable_ashare_symbol("000001") is True
    assert is_tradeable_ashare_symbol("002594") is True
    assert is_tradeable_ashare_symbol("600519") is True
    assert is_tradeable_ashare_symbol("605499") is True
    assert is_tradeable_ashare_symbol("300750") is False
    assert is_tradeable_ashare_symbol("301611") is False
    assert is_tradeable_ashare_symbol("688363") is False
    assert is_tradeable_ashare_symbol("873124") is False
    assert is_tradeable_ashare_symbol("920001") is False
    assert is_tradeable_ashare_symbol("123456") is False


def test_filter_tradeable_assets_keeps_ashare_main_board_and_funds() -> None:
    """可交易资产池保留主板股票和基金，排除指数、概念和不可交易标的。"""

    assets = [
        SimpleNamespace(
            asset_id="ashare:000001",
            market="ashare",
            symbol="000001",
            asset_type="stock",
            tradable=True,
        ),
        SimpleNamespace(
            asset_id="ashare:300750",
            market="ashare",
            symbol="300750",
            asset_type="stock",
            tradable=True,
        ),
        SimpleNamespace(
            asset_id="fund:510300",
            market="fund",
            symbol="510300",
            asset_type="etf",
            tradable=True,
        ),
        SimpleNamespace(
            asset_id="fund:161725",
            market="fund",
            symbol="161725",
            asset_type="lof",
            tradable=True,
        ),
        SimpleNamespace(
            asset_id="index:000300",
            market="index",
            symbol="000300",
            asset_type="index",
            tradable=True,
        ),
        SimpleNamespace(
            asset_id="ashare:600519",
            market="ashare",
            symbol="600519",
            asset_type="stock",
            tradable=False,
        ),
    ]

    selected = TradeableAssetEligibilityService().filter_tradeable_assets(assets)

    assert [asset.asset_id for asset in selected] == [
        "ashare:000001",
        "fund:510300",
        "fund:161725",
    ]


def test_filter_tradeable_assets_treats_missing_tradable_as_candidate() -> None:
    """轻量资产对象缺少 tradable 字段时，应继续按市场和代码准入判断。"""

    assets = [
        SimpleNamespace(
            asset_id="ashare:600519",
            market="ashare",
            symbol="600519",
            asset_type="stock",
        ),
        SimpleNamespace(
            asset_id="ashare:688363",
            market="ashare",
            symbol="688363",
            asset_type="stock",
        ),
    ]

    selected = TradeableAssetEligibilityService().filter_tradeable_assets(assets)

    assert [asset.asset_id for asset in selected] == ["ashare:600519"]


def test_filter_tradeable_ashare_symbols_normalizes_and_deduplicates() -> None:
    """标的列表过滤应归一化代码、去重并跳过非主板标的。"""

    symbols = TradeableAssetEligibilityService().filter_tradeable_ashare_symbols(
        ["sz000001", "000001", "sh600519", "300750", "688363", "", None]
    )

    assert symbols == ["000001", "600519"]
