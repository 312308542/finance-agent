"""A 股和数字货币推荐链路市场边界冒烟验证。

这个脚本不依赖数据库，只验证基础服务层的硬边界：
- 候选池不能使用 mixed 市场。
- 候选池成员必须和候选池市场一致。
- 推荐运行不能使用 mixed 市场。
- 同一次推荐运行的评分必须属于同一市场。
"""

from __future__ import annotations

from dataclasses import dataclass

from finance_agent.recommendations.service import (
    ensure_recommendation_market,
    ensure_scores_match_market,
)
from finance_agent.screening.service import ensure_single_market_universe


@dataclass(frozen=True)
class BoundaryMember:
    """用于验证候选池市场边界的最小成员对象。"""

    asset_id: str
    market: str


@dataclass(frozen=True)
class BoundaryScore:
    """用于验证推荐评分市场边界的最小评分对象。"""

    asset_id: str
    market: str


def main() -> None:
    """执行市场边界冒烟验证。"""

    assert_raises(
        "mixed 候选池被拒绝",
        lambda: ensure_single_market_universe(
            "mixed",
            [BoundaryMember(asset_id="ashare:600519", market="ashare")],
        ),
    )
    assert_raises(
        "跨市场候选池成员被拒绝",
        lambda: ensure_single_market_universe(
            "ashare",
            [
                BoundaryMember(asset_id="ashare:600519", market="ashare"),
                BoundaryMember(asset_id="crypto_spot:BTCUSDT", market="crypto_spot"),
            ],
        ),
    )
    assert_raises(
        "mixed 推荐运行被拒绝",
        lambda: ensure_recommendation_market("mixed"),
    )
    assert_raises(
        "跨市场评分被拒绝",
        lambda: ensure_scores_match_market(
            scores=[
                BoundaryScore(asset_id="ashare:600519", market="ashare"),
                BoundaryScore(asset_id="crypto_spot:BTCUSDT", market="crypto_spot"),
            ],
            market="ashare",
        ),
    )

    ensure_single_market_universe(
        "crypto_spot",
        [BoundaryMember(asset_id="crypto_spot:BTCUSDT", market="crypto_spot")],
    )
    ensure_recommendation_market("ashare")
    ensure_scores_match_market(
        scores=[BoundaryScore(asset_id="ashare:600519", market="ashare")],
        market="ashare",
    )
    print({"status": "ok", "checked": 7})


def assert_raises(case: str, action) -> None:
    """断言指定动作会触发 ValueError。"""

    try:
        action()
    except ValueError:
        return
    raise AssertionError(f"{case}：预期抛出 ValueError，但实际通过")


if __name__ == "__main__":
    main()
