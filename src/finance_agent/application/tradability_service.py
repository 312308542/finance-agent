"""选股可买入性校验服务。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

BlockingLevel = Literal["ok", "warning", "blocked"]


@dataclass(frozen=True)
class TradabilityInput:
    """可买入性规则输入。"""

    asset_id: str
    symbol: str
    market: str
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    previous_close: float
    volume: float
    status_flags: tuple[str, ...] = ()
    leadership_flags: tuple[str, ...] = ()
    min_volume: float = 1000.0


@dataclass(frozen=True)
class TradabilityResult:
    """可买入性判断结果。"""

    asset_id: str
    symbol: str
    tradable: bool
    blocking_level: BlockingLevel
    reasons: tuple[str, ...]
    action_override: str | None = None
    leadership_flags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """转换为推荐 payload 可保存的结构。"""

        return {
            "asset_id": self.asset_id,
            "symbol": self.symbol,
            "tradable": self.tradable,
            "blocking_level": self.blocking_level,
            "reasons": list(self.reasons),
            "action_override": self.action_override,
            "leadership_flags": list(self.leadership_flags),
        }


class TradabilityService:
    """判断标的当前是否适合进入买入候选。"""

    def evaluate(self, data: TradabilityInput) -> TradabilityResult:
        """评估可买入性。"""

        flags = {flag.lower() for flag in data.status_flags}
        reasons: list[str] = []
        if "suspended" in flags:
            reasons.append("suspended")
        if "st" in flags or "special_treatment" in flags:
            reasons.append("st")
        if data.volume <= 0:
            reasons.append("no_volume")
        if is_one_word_limit_up(data):
            reasons.append("one_word_limit_up")
        if reasons:
            return TradabilityResult(
                asset_id=data.asset_id,
                symbol=data.symbol,
                tradable=False,
                blocking_level="blocked",
                reasons=tuple(sorted(set(reasons))),
                action_override="watch",
                leadership_flags=data.leadership_flags,
            )

        warnings: list[str] = []
        if data.volume < data.min_volume:
            warnings.append("low_liquidity")
        return TradabilityResult(
            asset_id=data.asset_id,
            symbol=data.symbol,
            tradable=True,
            blocking_level="warning" if warnings else "ok",
            reasons=tuple(warnings),
            leadership_flags=data.leadership_flags,
        )


def is_one_word_limit_up(data: TradabilityInput) -> bool:
    """识别一字涨停：开高低收都封在涨停价附近。"""

    if data.previous_close <= 0:
        return False
    limit_price = data.previous_close * limit_up_ratio(data.market)
    tolerance = max(data.previous_close * 0.002, 0.01)
    prices = (data.open_price, data.high_price, data.low_price, data.close_price)
    return all(abs(price - limit_price) <= tolerance for price in prices)


def limit_up_ratio(market: str) -> float:
    """第一版 A 股可买入性使用 10% 涨停近似。"""

    return 1.10 if market == "ashare" else 1.0
