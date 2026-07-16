from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from finance_agent.research.strategy_walk_forward import (
    WalkForwardOutcome,
    build_price_feature_snapshot,
    evaluate_historical_gate,
    label_forward_position,
)


def _price_bars(*, count: int = 130) -> pd.DataFrame:
    timestamps = pd.date_range("2025-01-02", periods=count, freq="B", tz="UTC")
    trend = np.arange(count, dtype=float)
    close = 100.0 + trend * 0.25 + np.sin(trend / 4.0)
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": close - 0.20,
            "high": close + 0.80,
            "low": close - 0.90,
            "close": close,
            "volume": 1_000_000.0 + trend * 5_000.0,
            "amount": (1_000_000.0 + trend * 5_000.0) * close,
        }
    )


def _coverage(days: list[date], *, weight: float = 0.90) -> dict[date, list[float]]:
    return {day: [weight] * 20 for day in days}


def _historical_outcomes(
    days: list[date],
    *,
    t5_excess: float = 0.005,
    t10_excess: float = 0.010,
    t20_excess: float = 0.015,
    benchmark_return: float = 0.0,
) -> list[WalkForwardOutcome]:
    outcomes: list[WalkForwardOutcome] = []
    for signal_date in days:
        for horizon_days, excess_return in (
            (5, t5_excess),
            (10, t10_excess),
            (20, t20_excess),
        ):
            net_return = benchmark_return + excess_return
            outcomes.append(
                WalkForwardOutcome(
                    signal_date=signal_date,
                    entry_date=signal_date + timedelta(days=1),
                    exit_date=signal_date + timedelta(days=horizon_days),
                    horizon_days=horizon_days,
                    gross_return=net_return + 0.003,
                    net_return=net_return,
                    benchmark_return=benchmark_return,
                    excess_return=excess_return,
                )
            )
    return outcomes


def test_snapshot_features_ignore_future_price_spike() -> None:
    """截面后的极端价格不得改变截面特征。"""

    normal = _price_bars()
    spiked = normal.copy(deep=True)
    as_of = normal.iloc[119]["timestamp"].to_pydatetime()
    spiked.loc[120, ["open", "high", "low", "close"]] = [9_999, 10_001, 9_998, 10_000]

    normal_snapshot = build_price_feature_snapshot(normal, as_of=as_of)
    spiked_snapshot = build_price_feature_snapshot(spiked, as_of=as_of)

    assert normal_snapshot == spiked_snapshot
    assert normal_snapshot.groups["technical"]["status"] == "available"
    assert normal_snapshot.available_weight == pytest.approx(1.0)


def test_forward_position_enters_next_open_and_exits_by_trading_day() -> None:
    """T 日信号应使用 T+1 开盘入场，并按后续交易日收盘退出。"""

    bars = _price_bars()
    benchmark = _price_bars()
    benchmark[["open", "high", "low", "close"]] *= 2
    signal_index = 119
    signal_date = bars.iloc[signal_index]["timestamp"].date()

    outcome = label_forward_position(
        bars,
        signal_date=signal_date,
        horizon_days=5,
        round_trip_cost=0.003,
        benchmark_bars=benchmark,
    )

    assert outcome is not None
    assert outcome.entry_date == bars.iloc[signal_index + 1]["timestamp"].date()
    assert outcome.entry_price == pytest.approx(bars.iloc[signal_index + 1]["open"])
    assert outcome.exit_date == bars.iloc[signal_index + 5]["timestamp"].date()
    assert outcome.exit_price == pytest.approx(bars.iloc[signal_index + 5]["close"])
    assert outcome.net_return == pytest.approx(outcome.gross_return - 0.003)
    assert outcome.excess_return == pytest.approx(
        outcome.net_return - outcome.benchmark_return
    )


def test_forward_position_returns_none_when_future_trading_days_are_missing() -> None:
    """标签尚未成熟时必须保持缺失，不能借用最后一根 K 线。"""

    bars = _price_bars(count=123)
    signal_date = bars.iloc[119]["timestamp"].date()

    assert (
        label_forward_position(
            bars,
            signal_date=signal_date,
            horizon_days=5,
            round_trip_cost=0.003,
        )
        is None
    )


def test_historical_gate_passes_fixed_thresholds_and_three_disjoint_phases() -> None:
    """完整正向样本应通过固定门槛，并形成三个连续互斥阶段。"""

    days = [date(2025, 1, 1) + timedelta(days=index) for index in range(120)]

    result = evaluate_historical_gate(
        _historical_outcomes(days),
        coverage_by_date=_coverage(days),
    )

    assert result.status == "available"
    assert result.passed is True
    assert result.reasons == ()
    assert result.metrics["valid_cross_sections"] == 120
    assert [item["count"] for item in result.metrics["phases"]] == [40, 40, 40]
    assert result.metrics["phases"][0]["end_date"] < result.metrics["phases"][1]["start_date"]
    assert result.metrics["phases"][1]["end_date"] < result.metrics["phases"][2]["start_date"]
    assert result.metrics["t10_block_bootstrap"]["iterations"] == 10_000
    assert result.metrics["t10_block_bootstrap"]["block_size"] == 20
    assert result.metrics["t10_block_bootstrap"]["seed"] == 20_260_716
    assert result.metrics["t10_block_bootstrap"]["ci_95"][0] > 0


def test_historical_gate_reports_insufficient_data_when_coverage_is_below_threshold() -> None:
    """覆盖率不达标会减少有效截面，并返回 insufficient_data。"""

    days = [date(2025, 1, 1) + timedelta(days=index) for index in range(120)]
    coverage = _coverage(days)
    coverage[days[-1]] = [0.80] * 17 + [0.79] * 3

    result = evaluate_historical_gate(
        _historical_outcomes(days),
        coverage_by_date=coverage,
    )

    assert result.status == "insufficient_data"
    assert result.passed is False
    assert result.metrics["valid_cross_sections"] == 119
    assert "valid_cross_sections_below_minimum" in result.reasons


def test_historical_gate_fails_when_t5_and_t20_are_both_non_positive() -> None:
    """数据充足但收益门槛不达标时应为 failed，而不是数据不足。"""

    days = [date(2025, 1, 1) + timedelta(days=index) for index in range(120)]

    result = evaluate_historical_gate(
        _historical_outcomes(days, t5_excess=0.0, t20_excess=-0.001),
        coverage_by_date=_coverage(days),
    )

    assert result.status == "failed"
    assert result.passed is False
    assert "t5_and_t20_non_positive" in result.reasons


def test_historical_gate_fails_when_drawdown_exceeds_benchmark_by_five_points() -> None:
    """组合回撤比基准多五个百分点以上时不得通过。"""

    days = [date(2025, 1, 1) + timedelta(days=index) for index in range(120)]
    outcomes = _historical_outcomes(days)
    stressed_day = days[60]
    outcomes = [
        WalkForwardOutcome(
            signal_date=item.signal_date,
            entry_date=item.entry_date,
            exit_date=item.exit_date,
            horizon_days=item.horizon_days,
            gross_return=(-0.097 if item.signal_date == stressed_day else item.gross_return),
            net_return=(-0.10 if item.signal_date == stressed_day else item.net_return),
            benchmark_return=item.benchmark_return,
            excess_return=(-0.10 if item.signal_date == stressed_day else item.excess_return),
        )
        for item in outcomes
    ]

    result = evaluate_historical_gate(outcomes, coverage_by_date=_coverage(days))

    assert result.status == "failed"
    assert result.metrics["drawdown_gap"] > 0.05
    assert "drawdown_gap_above_limit" in result.reasons
