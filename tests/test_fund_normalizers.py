from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pandas as pd

from finance_agent.data.normalizers import (
    normalize_fund_etf_hist_em,
    normalize_fund_etf_spot_em,
    normalize_fund_lof_hist_em,
    normalize_fund_lof_spot_em,
    normalize_fund_open_fund_daily_em,
    normalize_fund_open_nav_em,
)


def test_normalize_fund_asset_lists_cover_etf_lof_and_open_fund() -> None:
    """基金资产池应把 ETF、LOF、开放式基金映射到独立资产类型。"""

    etf_assets = normalize_fund_etf_spot_em(
        pd.DataFrame(
            [
                {
                    "代码": "510300",
                    "名称": "沪深300ETF",
                    "最新价": 3.912,
                    "成交额": 123456789,
                }
            ]
        )
    )
    lof_assets = normalize_fund_lof_spot_em(
        pd.DataFrame(
            [
                {
                    "代码": "160716",
                    "名称": "嘉实基本面50LOF",
                    "最新价": 1.234,
                }
            ]
        )
    )
    open_assets = normalize_fund_open_fund_daily_em(
        pd.DataFrame(
            [
                {
                    "基金代码": "000001",
                    "基金简称": "华夏成长混合",
                    "日期": "2026-06-05",
                    "单位净值": "1.2345",
                    "累计净值": "3.2100",
                    "日增长率": "1.23",
                    "申购状态": "开放申购",
                    "赎回状态": "开放赎回",
                }
            ]
        )
    )

    assert etf_assets[0].asset_id == "fund:etf:510300"
    assert etf_assets[0].market == "fund"
    assert etf_assets[0].asset_type == "etf"
    assert etf_assets[0].exchange == "SSE"
    assert lof_assets[0].asset_id == "fund:lof:160716"
    assert lof_assets[0].asset_type == "lof"
    assert open_assets[0].asset_id == "fund:open:000001"
    assert open_assets[0].asset_type == "open_fund"
    assert open_assets[0].exchange is None


def test_normalize_open_fund_assets_skips_exchange_traded_rows() -> None:
    """开放式基金列表混入场内 ETF/LOF 时应直接过滤，避免资产主键冲突。"""

    open_assets = normalize_fund_open_fund_daily_em(
        pd.DataFrame(
            [
                {
                    "基金代码": "511880",
                    "基金简称": "银华日利ETF",
                    "申购状态": "场内交易",
                    "赎回状态": "场内交易",
                },
                {
                    "基金代码": "000001",
                    "基金简称": "华夏成长混合",
                    "申购状态": "开放申购",
                    "赎回状态": "开放赎回",
                },
            ]
        )
    )

    assert [asset.symbol for asset in open_assets] == ["000001"]
    assert open_assets[0].asset_id == "fund:open:000001"


def test_normalize_fund_bars_keep_fund_market_and_symbol() -> None:
    """ETF/LOF 日 K 归一化后应写入 fund 市场而不是 ashare。"""

    bars = normalize_fund_etf_hist_em(
        pd.DataFrame(
            [
                {
                    "日期": "2026-06-05",
                    "开盘": "3.80",
                    "最高": "3.95",
                    "最低": "3.78",
                    "收盘": "3.91",
                    "成交量": "123456",
                    "成交额": "4567890",
                }
            ]
        ),
        symbol="510300",
        asset_type="etf",
        timeframe="1d",
        source="akshare:fund_etf_hist_em",
    )
    lof_bars = normalize_fund_lof_hist_em(
        pd.DataFrame(
            [
                {
                    "日期": "2026-06-05",
                    "开盘": "1.20",
                    "最高": "1.24",
                    "最低": "1.19",
                    "收盘": "1.23",
                    "成交量": "654321",
                    "成交额": "7654321",
                }
            ]
        ),
        symbol="160716",
        timeframe="1d",
        source="akshare:fund_lof_hist_em",
    )

    assert bars[0].asset_id == "fund:etf:510300"
    assert bars[0].market == "fund"
    assert bars[0].symbol == "510300"
    assert bars[0].close == Decimal("3.91")
    assert lof_bars[0].asset_id == "fund:lof:160716"
    assert lof_bars[0].market == "fund"
    assert lof_bars[0].volume == Decimal("654321")


def test_normalize_fund_bars_accepts_sina_english_columns() -> None:
    """ETF 新浪备源返回英文列名时也应能归一化为统一日 K。"""

    bars = normalize_fund_etf_hist_em(
        pd.DataFrame(
            [
                {
                    "date": "2026-06-05",
                    "open": 3.80,
                    "high": 3.95,
                    "low": 3.78,
                    "close": 3.91,
                    "volume": 123456,
                    "amount": 4567890,
                }
            ]
        ),
        symbol="510300",
        asset_type="etf",
        timeframe="1d",
        source="akshare:fund_etf_hist_sina",
    )

    assert bars[0].asset_id == "fund:etf:510300"
    assert bars[0].source == "akshare:fund_etf_hist_sina"
    assert bars[0].close == Decimal("3.91")


def test_normalize_open_fund_nav_maps_nav_fields() -> None:
    """开放式基金净值应保留单位净值、累计净值和申赎状态。"""

    snapshots = normalize_fund_open_nav_em(
        pd.DataFrame(
            [
                {
                    "净值日期": "2026-06-05",
                    "单位净值": "1.2345",
                    "累计净值": "3.2100",
                    "日增长率": "1.25",
                    "申购状态": "开放申购",
                    "赎回状态": "开放赎回",
                }
            ]
        ),
        symbol="000001",
        source="akshare:fund_open_fund_info_em",
    )

    snapshot = snapshots[0]
    assert snapshot.snapshot_id.startswith("fund_nav:")
    assert snapshot.asset_id == "fund:open:000001"
    assert snapshot.market == "fund"
    assert snapshot.nav_date == date(2026, 6, 5)
    assert snapshot.unit_nav == Decimal("1.2345")
    assert snapshot.accumulated_nav == Decimal("3.2100")
    assert snapshot.daily_return == Decimal("0.0125")
    assert snapshot.purchase_status == "开放申购"
    assert snapshot.redeem_status == "开放赎回"
