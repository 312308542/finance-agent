"""因子增强链路仓储级冒烟验证。

本脚本覆盖第 5 项“推荐链路规模化与因子增强”的基础闭环：
- A 股链路要能生成基本面、估值、资金流、流动性、事件衰减等因子组。
- 数字货币链路要能生成衍生品、流动性、事件衰减等因子组。
- 市场不适用的因子组必须被过滤，避免因为缺基础数据而误罚。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from finance_agent.factors import FactorService
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.repositories import (
    CapitalFlowRepository,
    DerivativeDataRepository,
    EventRepository,
    FundamentalDataRepository,
    IndicatorFrameRepository,
    RiskRepository,
)

JsonDict = dict[str, Any]


def main() -> None:
    """执行因子增强冒烟验证。"""

    summary = run_smoke()
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


def run_smoke() -> JsonDict:
    """写入样例数据并校验增强因子组。"""

    session_factory = create_session_factory()
    as_of = datetime.now(tz=UTC).replace(microsecond=0)
    with session_scope(session_factory) as session:
        seed_ashare(session=session, as_of=as_of)
        seed_crypto(session=session, as_of=as_of)

        factor_service = FactorService(session)
        ashare_result = factor_service.compute_for_asset(
            asset_id="ashare:smoke_factor_600519",
            timeframe="1d",
            horizon="swing",
        )
        crypto_result = factor_service.compute_for_asset(
            asset_id="crypto_spot:SMOKEBTCUSDT",
            timeframe="1h",
            horizon="swing",
        )
        ashare_frame = factor_service.factors.get_latest_factor_frame(
            asset_id=ashare_result.asset_id,
            horizon=ashare_result.horizon,
        )
        crypto_frame = factor_service.factors.get_latest_factor_frame(
            asset_id=crypto_result.asset_id,
            horizon=crypto_result.horizon,
        )

    if ashare_frame is None or crypto_frame is None:
        raise AssertionError("因子快照未生成")

    ashare_groups = groups_by_name(ashare_frame.payload)
    crypto_groups = groups_by_name(crypto_frame.payload)
    assert_required_groups(
        market="ashare",
        groups=ashare_groups,
        required={
            "technical",
            "fundamental",
            "valuation",
            "capital_flow",
            "liquidity",
            "event_decay",
        },
        forbidden={"derivatives"},
    )
    assert_required_groups(
        market="crypto_spot",
        groups=crypto_groups,
        required={"technical", "derivatives", "liquidity", "event_decay"},
        forbidden={"fundamental", "valuation", "capital_flow"},
    )
    assert_liquidity_group(ashare_groups["liquidity"])
    assert_liquidity_group(crypto_groups["liquidity"])

    return {
        "ashare": summarize_groups(ashare_groups),
        "crypto_spot": summarize_groups(crypto_groups),
    }


def seed_ashare(*, session: Any, as_of: datetime) -> None:
    """写入 A 股样例指标、估值、资金流、事件和风险。"""

    asset_id = "ashare:smoke_factor_600519"
    symbol = "600519"
    seed_indicator(
        session=session,
        asset_id=asset_id,
        symbol=symbol,
        market="ashare",
        timeframe="1d",
        as_of=as_of,
        amount_avg_20d=1_800_000_000,
        amount_zscore_20d=0.45,
        volatility_20d=0.18,
    )
    fundamentals = FundamentalDataRepository(session)
    capital_flows = CapitalFlowRepository(session)
    for index in range(8):
        item_as_of = as_of - timedelta(days=7 - index)
        fundamentals.upsert_fundamental_snapshot(
            snapshot_id=f"fundamental:{asset_id}:factor_enhancement:{item_as_of:%Y%m%d}",
            asset_id=asset_id,
            symbol=symbol,
            source="factor_enhancement_smoke",
            status="available",
            as_of=item_as_of,
            report_period="2026Q1",
            pe_ttm=Decimal("18") + Decimal(index),
            pb=Decimal("2.1") + Decimal(index) * Decimal("0.08"),
            roe=Decimal("0.17"),
            revenue_growth_yoy=Decimal("0.12"),
            net_profit_growth_yoy=Decimal("0.13"),
            debt_to_asset=Decimal("0.38"),
            operating_cashflow=Decimal("1500000000"),
            payload={"dividend_yield": 0.028},
        )
        capital_flows.upsert_capital_flow_snapshot(
            snapshot_id=f"capital_flow:{asset_id}:factor_enhancement:{item_as_of:%Y%m%d}",
            asset_id=asset_id,
            symbol=symbol,
            market="ashare",
            window="5d",
            source="factor_enhancement_smoke",
            status="available",
            as_of=item_as_of,
            main_net_inflow=Decimal("22000000") + Decimal(index * 1000000),
            northbound_net_inflow=Decimal("8000000"),
            turnover_rate=Decimal("0.026"),
            amount=Decimal("1800000000"),
            payload={"flow_price_divergence": "0.01", "rank_hint": 2, "rank_total": 50},
        )
    seed_events_and_risks(
        session=session,
        asset_id=asset_id,
        symbol=symbol,
        market="ashare",
        as_of=as_of,
    )


def seed_crypto(*, session: Any, as_of: datetime) -> None:
    """写入数字货币样例指标、衍生品、事件和风险。"""

    asset_id = "crypto_spot:SMOKEBTCUSDT"
    symbol = "SMOKEBTCUSDT"
    seed_indicator(
        session=session,
        asset_id=asset_id,
        symbol=symbol,
        market="crypto_spot",
        timeframe="1h",
        as_of=as_of,
        amount_avg_20d=95_000_000,
        amount_zscore_20d=0.25,
        volatility_20d=0.32,
    )
    derivatives = DerivativeDataRepository(session)
    for index in range(12):
        item_as_of = as_of - timedelta(hours=11 - index)
        derivatives.upsert_crypto_derivative_snapshot(
            snapshot_id=f"crypto_derivative:{asset_id}:factor_enhancement:{item_as_of:%Y%m%dT%H%M%S}",
            asset_id=asset_id,
            symbol=symbol,
            market="crypto_spot",
            source="factor_enhancement_smoke",
            as_of=item_as_of,
            funding_rate=Decimal("0.00008"),
            open_interest=Decimal("250000") + Decimal(index * 1000),
            open_interest_value=Decimal("16000000000"),
            long_short_ratio=Decimal("1.08"),
            basis_rate=Decimal("0.0009"),
            status="available",
            payload={"source": "factor_enhancement_smoke"},
        )
    seed_events_and_risks(
        session=session,
        asset_id=asset_id,
        symbol=symbol,
        market="crypto_spot",
        as_of=as_of,
    )


def seed_indicator(
    *,
    session: Any,
    asset_id: str,
    symbol: str,
    market: str,
    timeframe: str,
    as_of: datetime,
    amount_avg_20d: float,
    amount_zscore_20d: float,
    volatility_20d: float,
) -> None:
    """写入包含流动性输入的技术指标快照。"""

    IndicatorFrameRepository(session).upsert_indicator_frame(
        indicator_frame_id=f"indicator:{asset_id}:factor_enhancement:{timeframe}:{as_of:%Y%m%dT%H%M%S}",
        asset_id=asset_id,
        symbol=symbol,
        market=market,
        timeframe=timeframe,
        horizon="swing",
        library="talib",
        input_start_at=as_of - timedelta(days=80),
        input_end_at=as_of,
        bar_count=80,
        status="available",
        as_of=as_of,
        rsi_14=Decimal("58"),
        macd=Decimal("2.4"),
        macd_signal=Decimal("2.0"),
        macd_hist=Decimal("0.4"),
        atr_14=Decimal("1.8"),
        bb_percent_b=Decimal("0.62"),
        ma_20=Decimal("100"),
        ma_60=Decimal("96"),
        payload={
            "computed_values": {
                "return_1d": 0.012,
                "return_5d": 0.036,
                "return_20d": 0.11,
                "momentum_20d": 0.09,
                "ma_slope": 0.018,
                "volatility_20d": volatility_20d,
                "max_drawdown_20d": -0.08,
                "amount_avg_20d": amount_avg_20d,
                "amount_zscore_20d": amount_zscore_20d,
            }
        },
    )


def seed_events_and_risks(
    *,
    session: Any,
    asset_id: str,
    symbol: str,
    market: str,
    as_of: datetime,
) -> None:
    """写入事件衰减和风险反驳使用的样例事实。"""

    events = EventRepository(session)
    events.upsert_event(
        event_id=f"event:{asset_id}:positive:factor_enhancement",
        asset_id=asset_id,
        symbol=symbol,
        market=market,
        event_type="news",
        title="样例正向事件",
        summary="用于验证事件衰减因子。",
        sentiment="positive",
        importance="medium",
        source="factor_enhancement_smoke",
        published_at=as_of - timedelta(hours=4),
        collected_at=as_of,
        payload={"source": "factor_enhancement_smoke"},
    )
    events.upsert_event(
        event_id=f"event:{asset_id}:negative:factor_enhancement",
        asset_id=asset_id,
        symbol=symbol,
        market=market,
        event_type="risk_notice",
        title="样例负向事件",
        summary="用于验证负向事件扣分。",
        sentiment="negative",
        importance="high",
        source="factor_enhancement_smoke",
        published_at=as_of - timedelta(hours=30),
        collected_at=as_of,
        payload={"source": "factor_enhancement_smoke"},
    )
    RiskRepository(session).upsert_risk_finding(
        risk_id=f"risk:{asset_id}:factor_enhancement",
        asset_id=asset_id,
        scope="asset",
        risk_type="volatility",
        severity="medium",
        title="样例波动风险",
        as_of=as_of,
        score=Decimal("35"),
        description="用于验证风险组和评分扣分。",
        evidence_ids=[],
        payload={"source": "factor_enhancement_smoke"},
    )


def groups_by_name(payload: JsonDict) -> dict[str, JsonDict]:
    """把因子快照中的分组列表转成字典。"""

    return {str(group["group"]): group for group in payload.get("factor_groups") or []}


def assert_required_groups(
    *,
    market: str,
    groups: dict[str, JsonDict],
    required: set[str],
    forbidden: set[str],
) -> None:
    """校验市场专属因子组集合。"""

    missing = sorted(required - set(groups))
    unexpected = sorted(forbidden & set(groups))
    if missing or unexpected:
        raise AssertionError(f"{market} 因子组不符合预期，缺失={missing}，不应出现={unexpected}")
    unavailable = sorted(name for name in required if groups[name].get("status") == "unavailable")
    if unavailable:
        raise AssertionError(f"{market} 存在不可用因子组：{unavailable}")


def assert_liquidity_group(group: JsonDict) -> None:
    """校验流动性因子尺度和得分。"""

    factors = group.get("factors") or {}
    illiquidity_score = factors.get("illiquidity_score")
    if not isinstance(illiquidity_score, int | float) or not 0 <= illiquidity_score <= 1:
        raise AssertionError(f"illiquidity_score 必须归一化到 0-1，当前值={illiquidity_score}")
    score = group.get("score")
    if not isinstance(score, int | float) or not 0 <= score <= 100:
        raise AssertionError(f"liquidity 组得分必须在 0-100，当前值={score}")


def summarize_groups(groups: dict[str, JsonDict]) -> JsonDict:
    """生成便于阅读的因子组摘要。"""

    return {
        name: {
            "status": group.get("status"),
            "score": group.get("score"),
            "missing_factors": group.get("missing_factors") or [],
        }
        for name, group in sorted(groups.items())
    }


if __name__ == "__main__":
    main()
