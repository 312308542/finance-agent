"""可交易资产准入服务。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from finance_agent.data.normalizers import (
    is_main_board_ashare_stock_symbol,
    normalize_ashare_symbol,
)

TRADEABLE_FUND_TYPES = {
    "fund",
    "etf",
    "lof",
    "open_fund",
}
UNLISTED_ASHARE_NAME_MARKERS = ("PT", "退市", "终止上市", "摘牌")


def is_tradeable_ashare_symbol(symbol: Any) -> bool:
    """判断 A 股代码是否属于用户当前可交易的主板股票。"""

    return is_main_board_ashare_stock_symbol(normalize_ashare_symbol(str(symbol or "")))


class TradeableAssetEligibilityService:
    """统一判断资产是否允许进入推荐、研究跟踪和逐股高频任务。"""

    def is_tradeable_asset(self, asset: Any) -> bool:
        """判断单个资产是否属于当前产品定义的可交易资产池。"""

        if getattr(asset, "tradable", True) is False:
            return False
        market = asset_market(asset)
        asset_type = str(getattr(asset, "asset_type", "") or "").strip().lower()
        symbol = str(getattr(asset, "symbol", "") or "").strip()
        if market == "ashare":
            name = str(getattr(asset, "name", "") or "").strip()
            return (
                asset_type in {"", "stock"}
                and is_tradeable_ashare_symbol(symbol)
                and not is_obviously_unlisted_ashare_name(name)
            )
        if market == "fund":
            return asset_type in TRADEABLE_FUND_TYPES
        return False

    def filter_tradeable_assets(self, assets: Iterable[Any]) -> list[Any]:
        """按原顺序过滤可交易资产。"""

        return [asset for asset in assets if self.is_tradeable_asset(asset)]

    def filter_tradeable_ashare_symbols(self, symbols: Iterable[Any]) -> list[str]:
        """归一化、去重并过滤 A 股主板股票代码。"""

        selected: list[str] = []
        for symbol in symbols:
            normalized = normalize_ashare_symbol(str(symbol or ""))
            if not is_tradeable_ashare_symbol(normalized):
                continue
            if normalized not in selected:
                selected.append(normalized)
        return selected


def asset_market(asset: Any) -> str:
    """读取资产市场；轻量对象缺少 market 时从 asset_id 前缀推断。"""

    market = str(getattr(asset, "market", "") or "").strip().lower()
    if market:
        return market
    asset_id = str(getattr(asset, "asset_id", "") or "").strip().lower()
    if ":" in asset_id:
        return asset_id.split(":", 1)[0]
    return ""


def is_obviously_unlisted_ashare_name(name: str) -> bool:
    """判断 A 股名称是否明确指向不可交易/退市状态。"""

    normalized = name.strip().upper().replace(" ", "")
    if not normalized:
        return False
    return any(marker in normalized for marker in UNLISTED_ASHARE_NAME_MARKERS)
