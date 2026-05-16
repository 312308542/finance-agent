"""因子计算规格配置。

这里先放确定性数据层会直接使用的阈值和窗口。后续如果要做策略版本化，可以把这些
dataclass 序列化到数据库或 YAML 配置，但第一版保持代码内显式常量，便于审计。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AshareFactorSpec:
    """A 股基础因子派生规格。"""

    valuation_history_limit: int = 120
    valuation_percentile_min_observations: int = 3
    dividend_full_score_yield: float = 0.05
    capital_flow_history_limit: int = 20
    capital_flow_continuity_window: int = 5
    main_flow_strength_scale: float = 0.10


@dataclass(frozen=True)
class CryptoFactorSpec:
    """数字货币衍生品因子派生规格。"""

    derivative_history_limit: int = 48
    funding_zscore_min_observations: int = 6
    open_interest_change_lag: int = 24
    open_interest_positive_scale: float = 0.15


@dataclass(frozen=True)
class FactorSpec:
    """推荐系统数据层因子规格入口。"""

    ashare: AshareFactorSpec = AshareFactorSpec()
    crypto: CryptoFactorSpec = CryptoFactorSpec()


DEFAULT_FACTOR_SPEC = FactorSpec()
