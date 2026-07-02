from datetime import UTC, datetime

import pandas as pd

from finance_agent.data.normalizers import (
    is_main_board_ashare_stock_symbol,
    normalize_ashare_spot,
    normalize_ashare_fund_flow_rank,
)


def test_fund_flow_rank_normalizes_and_filters_invalid_ashare_symbols() -> None:
    """资金流排名只应产出规范 6 位 A 股代码，避免短代码污染资产池。"""

    rows = pd.DataFrame(
        [
            {"代码": "1400", "主力净流入": "100", "成交额": "2000", "换手率": "1.2"},
            {"代码": "000001", "主力净流入": "300", "成交额": "4000", "换手率": "2.3"},
            {"代码": "sz000002", "主力净流入": "500", "成交额": "6000", "换手率": "3.4"},
            {"代码": "600519.SH", "主力净流入": "700", "成交额": "8000", "换手率": "4.5"},
        ]
    )

    snapshots = normalize_ashare_fund_flow_rank(
        rows,
        source="ths:curl_cffi:stock_fund_flow_individual",
        window="5日",
        as_of=datetime(2026, 6, 3, tzinfo=UTC),
    )

    assert [item.symbol for item in snapshots] == ["000001", "000002", "600519"]
    assert [item.asset_id for item in snapshots] == [
        "ashare:000001",
        "ashare:000002",
        "ashare:600519",
    ]


def test_ashare_spot_marks_dash_price_assets_untradable() -> None:
    """A 股实时列表里最新价为 '-' 的历史标的应保留身份但标记不可交易。"""

    assets = normalize_ashare_spot(
        pd.DataFrame(
            [
                {"代码": "000024", "名称": "招商地产", "最新价": "-", "涨跌幅": "-"},
                {"代码": "600519", "名称": "贵州茅台", "最新价": 1410.0, "涨跌幅": 0.12},
            ]
        )
    )

    assert [(asset.symbol, asset.tradable, asset.status) for asset in assets] == [
        ("000024", False, "unavailable"),
        ("600519", True, "available"),
    ]


def test_main_board_symbol_filter_excludes_growth_star_and_bse() -> None:
    """主板股票过滤应排除创业板、科创板、北交所、B 股和可转债。"""

    assert is_main_board_ashare_stock_symbol("000001") is True
    assert is_main_board_ashare_stock_symbol("002594") is True
    assert is_main_board_ashare_stock_symbol("600519") is True
    assert is_main_board_ashare_stock_symbol("605499") is True
    assert is_main_board_ashare_stock_symbol("300750") is False
    assert is_main_board_ashare_stock_symbol("688363") is False
    assert is_main_board_ashare_stock_symbol("873124") is False
    assert is_main_board_ashare_stock_symbol("920001") is False
    assert is_main_board_ashare_stock_symbol("200001") is False
    assert is_main_board_ashare_stock_symbol("123456") is False


def test_fund_flow_rank_keeps_only_main_board_stock_symbols() -> None:
    """个股资金流属于用户可交易标的明细，只保留主板股票。"""

    rows = pd.DataFrame(
        [
            {"代码": "000001", "主力净流入": "300", "成交额": "4000", "换手率": "2.3"},
            {"代码": "300750", "主力净流入": "500", "成交额": "6000", "换手率": "3.4"},
            {"代码": "688363", "主力净流入": "700", "成交额": "8000", "换手率": "4.5"},
            {"代码": "873124", "主力净流入": "900", "成交额": "10000", "换手率": "5.6"},
            {"代码": "600519", "主力净流入": "1100", "成交额": "12000", "换手率": "6.7"},
        ]
    )

    snapshots = normalize_ashare_fund_flow_rank(
        rows,
        source="ths:curl_cffi:stock_fund_flow_individual",
        window="5日",
        as_of=datetime(2026, 6, 3, tzinfo=UTC),
    )

    assert [item.symbol for item in snapshots] == ["000001", "600519"]
