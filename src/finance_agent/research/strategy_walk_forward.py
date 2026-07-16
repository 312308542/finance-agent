"""策略历史 walk-forward 研究的无前视纯函数。"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any

import numpy as np
import pandas as pd

from finance_agent.factors.service import normalized_illiquidity, technical_score
from finance_agent.indicators.service import compute_indicator_values

JsonDict = dict[str, Any]

SUPPORTED_HORIZONS = (5, 10, 20)
MINIMUM_WARMUP_BARS = 120
MINIMUM_CROSS_SECTIONS = 120
MINIMUM_PHASE_CROSS_SECTIONS = 30
MINIMUM_ASSET_AVAILABLE_WEIGHT = 0.80
MINIMUM_COVERED_ASSET_RATIO = 0.90
BOOTSTRAP_ITERATIONS = 10_000
BOOTSTRAP_BLOCK_SIZE = 20
BOOTSTRAP_SEED = 20_260_716
MAX_DRAWDOWN_GAP = 0.05


@dataclass(frozen=True)
class PointInTimeFactorSnapshot:
    """只包含截面时点可见数据的因子快照。"""

    asset_id: str
    symbol: str
    as_of: datetime
    groups: JsonDict
    available_weight: float
    source_ids: tuple[str, ...]


@dataclass(frozen=True)
class WalkForwardOutcome:
    """单个信号截面在指定交易日周期的前向结果。"""

    signal_date: date
    entry_date: date
    exit_date: date
    horizon_days: int
    gross_return: float
    net_return: float
    benchmark_return: float
    excess_return: float
    entry_price: float | None = None
    exit_price: float | None = None


@dataclass(frozen=True)
class HistoricalGateResult:
    """历史门槛判断结果，区分数据不足与策略失败。"""

    status: str
    passed: bool
    metrics: JsonDict
    reasons: tuple[str, ...]


def build_price_feature_snapshot(
    bars: pd.DataFrame,
    *,
    as_of: date | datetime,
    asset_id: str = "unknown",
    symbol: str = "unknown",
    source_ids: Sequence[str] = (),
) -> PointInTimeFactorSnapshot:
    """使用不晚于 ``as_of`` 的 K 线构建技术面和流动性特征。"""

    normalized_as_of = _normalize_as_of(as_of)
    visible = _prepare_bars(bars)
    visible = visible.loc[visible["timestamp"] <= normalized_as_of].copy()
    if len(visible) < MINIMUM_WARMUP_BARS:
        groups = {
            "technical": _unavailable_price_group(
                "technical",
                observed_bars=len(visible),
            ),
            "liquidity": _unavailable_price_group(
                "liquidity",
                observed_bars=len(visible),
            ),
        }
        return PointInTimeFactorSnapshot(
            asset_id=asset_id,
            symbol=symbol,
            as_of=normalized_as_of.to_pydatetime(),
            groups=groups,
            available_weight=0.0,
            source_ids=tuple(source_ids),
        )

    values, _missing = compute_indicator_values(visible)
    normalized_values = {key: _finite_or_none(value) for key, value in values.items()}
    technical_keys = (
        "return_1d",
        "return_5d",
        "return_20d",
        "momentum_20d",
        "ma_20",
        "ma_60",
        "ma_slope",
        "rsi_14",
        "macd",
        "macd_hist",
        "atr_14",
        "bb_percent_b",
        "volatility_20d",
        "max_drawdown_20d",
    )
    technical_factors = {key: normalized_values.get(key) for key in technical_keys}
    technical_missing = [key for key, value in technical_factors.items() if value is None]
    technical_group = {
        "group": "technical",
        "status": "available" if not technical_missing else "partial",
        "score": technical_score(technical_factors),
        "factors": technical_factors,
        "missing_factors": technical_missing,
        "source_ids": list(source_ids),
    }

    liquidity_factors = {
        key: normalized_values.get(key)
        for key in (
            "amount_avg_20d",
            "amount_zscore_20d",
            "volatility_20d",
            "max_drawdown_20d",
        )
    }
    liquidity_factors["illiquidity_score"] = normalized_illiquidity(
        amount_avg_20d=liquidity_factors["amount_avg_20d"],
        amount_zscore_20d=liquidity_factors["amount_zscore_20d"],
        volatility_20d=liquidity_factors["volatility_20d"],
        turnover_rate=None,
    )
    liquidity_missing = [key for key, value in liquidity_factors.items() if value is None]
    liquidity_group = {
        "group": "liquidity",
        "status": "available" if not liquidity_missing else "partial",
        "score": _liquidity_score(liquidity_factors),
        "factors": liquidity_factors,
        "missing_factors": liquidity_missing,
        "source_ids": list(source_ids),
    }
    groups = {"technical": technical_group, "liquidity": liquidity_group}
    available_weight = sum(
        0.5 for group in groups.values() if group["status"] == "available"
    )
    return PointInTimeFactorSnapshot(
        asset_id=asset_id,
        symbol=symbol,
        as_of=normalized_as_of.to_pydatetime(),
        groups=groups,
        available_weight=available_weight,
        source_ids=tuple(source_ids),
    )


def label_forward_position(
    bars: pd.DataFrame,
    *,
    signal_date: date,
    horizon_days: int,
    round_trip_cost: float,
    benchmark_bars: pd.DataFrame | None = None,
) -> WalkForwardOutcome | None:
    """按 T+1 开盘入场和第 N 个后续交易日收盘生成标签。"""

    if horizon_days not in SUPPORTED_HORIZONS:
        raise ValueError(f"不支持的前向周期：{horizon_days}")
    if not 0 <= round_trip_cost < 1:
        raise ValueError("交易成本必须位于 [0, 1) 区间")

    prepared = _prepare_bars(bars)
    indexes = prepared.index[prepared["timestamp"].dt.date == signal_date].tolist()
    if not indexes:
        return None
    signal_index = int(indexes[-1])
    entry_index = signal_index + 1
    exit_index = signal_index + horizon_days
    if exit_index >= len(prepared):
        return None

    entry_price = _finite_or_none(prepared.iloc[entry_index]["open"])
    exit_price = _finite_or_none(prepared.iloc[exit_index]["close"])
    if entry_price is None or exit_price is None or entry_price <= 0:
        return None

    benchmark_return = 0.0
    if benchmark_bars is not None:
        benchmark_return = _forward_gross_return(
            benchmark_bars,
            signal_date=signal_date,
            horizon_days=horizon_days,
        )
        if benchmark_return is None:
            return None

    gross_return = exit_price / entry_price - 1.0
    net_return = gross_return - float(round_trip_cost)
    return WalkForwardOutcome(
        signal_date=signal_date,
        entry_date=prepared.iloc[entry_index]["timestamp"].date(),
        exit_date=prepared.iloc[exit_index]["timestamp"].date(),
        horizon_days=horizon_days,
        gross_return=gross_return,
        net_return=net_return,
        benchmark_return=benchmark_return,
        excess_return=net_return - benchmark_return,
        entry_price=entry_price,
        exit_price=exit_price,
    )


def evaluate_historical_gate(
    outcomes: Sequence[WalkForwardOutcome],
    *,
    coverage_by_date: Mapping[date, Sequence[float]],
) -> HistoricalGateResult:
    """按固定样本、收益、分阶段和回撤门槛评估历史结果。"""

    daily = _aggregate_daily_outcomes(outcomes)
    all_dates = sorted({signal_date for signal_date, _horizon in daily})
    valid_coverage_dates = {
        signal_date
        for signal_date, weights in coverage_by_date.items()
        if _coverage_is_valid(weights)
    }
    valid_dates = [
        signal_date
        for signal_date in all_dates
        if signal_date in valid_coverage_dates
        and all((signal_date, horizon) in daily for horizon in SUPPORTED_HORIZONS)
    ]
    phases = _split_three_phases(valid_dates)
    phase_metrics = [
        {
            "phase": index,
            "count": len(phase_dates),
            "start_date": phase_dates[0] if phase_dates else None,
            "end_date": phase_dates[-1] if phase_dates else None,
            "t10_mean_excess_return": _mean_daily_metric(
                daily,
                phase_dates,
                horizon_days=10,
                metric="excess_return",
            ),
        }
        for index, phase_dates in enumerate(phases, start=1)
    ]
    metrics: JsonDict = {
        "total_cross_sections": len(all_dates),
        "valid_cross_sections": len(valid_dates),
        "invalid_or_incomplete_cross_sections": len(all_dates) - len(valid_dates),
        "coverage": {
            "minimum_asset_available_weight": MINIMUM_ASSET_AVAILABLE_WEIGHT,
            "minimum_covered_asset_ratio": MINIMUM_COVERED_ASSET_RATIO,
        },
        "phases": phase_metrics,
    }

    insufficient_reasons: list[str] = []
    if len(valid_dates) < MINIMUM_CROSS_SECTIONS:
        insufficient_reasons.append("valid_cross_sections_below_minimum")
    if any(len(phase_dates) < MINIMUM_PHASE_CROSS_SECTIONS for phase_dates in phases):
        insufficient_reasons.append("phase_cross_sections_below_minimum")
    if insufficient_reasons:
        metrics["gate_passed"] = False
        return HistoricalGateResult(
            status="insufficient_data",
            passed=False,
            metrics=metrics,
            reasons=tuple(insufficient_reasons),
        )

    horizon_means = {
        horizon: _mean_daily_metric(
            daily,
            valid_dates,
            horizon_days=horizon,
            metric="excess_return",
        )
        for horizon in SUPPORTED_HORIZONS
    }
    t10_excess = [daily[(signal_date, 10)]["excess_return"] for signal_date in valid_dates]
    ci_lower, ci_upper = block_bootstrap_mean_ci(t10_excess)
    portfolio_max_drawdown = _maximum_drawdown(
        [daily[(signal_date, 10)]["net_return"] for signal_date in valid_dates]
    )
    benchmark_max_drawdown = _maximum_drawdown(
        [daily[(signal_date, 10)]["benchmark_return"] for signal_date in valid_dates]
    )
    drawdown_gap = benchmark_max_drawdown - portfolio_max_drawdown
    positive_phase_count = sum(
        (phase["t10_mean_excess_return"] or 0.0) > 0 for phase in phase_metrics
    )
    metrics.update(
        {
            "horizon_mean_excess_returns": {
                str(horizon): horizon_means[horizon] for horizon in SUPPORTED_HORIZONS
            },
            "t10_block_bootstrap": {
                "iterations": BOOTSTRAP_ITERATIONS,
                "block_size": BOOTSTRAP_BLOCK_SIZE,
                "seed": BOOTSTRAP_SEED,
                "ci_95": (ci_lower, ci_upper),
            },
            "positive_t10_phase_count": positive_phase_count,
            "portfolio_max_drawdown": portfolio_max_drawdown,
            "benchmark_max_drawdown": benchmark_max_drawdown,
            "drawdown_gap": drawdown_gap,
        }
    )

    failure_reasons: list[str] = []
    if ci_lower <= 0:
        failure_reasons.append("t10_bootstrap_lower_bound_not_positive")
    if positive_phase_count < 2:
        failure_reasons.append("fewer_than_two_positive_t10_phases")
    if horizon_means[5] <= 0 and horizon_means[20] <= 0:
        failure_reasons.append("t5_and_t20_non_positive")
    if drawdown_gap > MAX_DRAWDOWN_GAP:
        failure_reasons.append("drawdown_gap_above_limit")

    passed = not failure_reasons
    metrics["gate_passed"] = passed
    return HistoricalGateResult(
        status="available" if passed else "failed",
        passed=passed,
        metrics=metrics,
        reasons=tuple(failure_reasons),
    )


def block_bootstrap_mean_ci(
    values: Sequence[float],
    *,
    iterations: int = BOOTSTRAP_ITERATIONS,
    block_size: int = BOOTSTRAP_BLOCK_SIZE,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float]:
    """使用循环移动区块 bootstrap 计算均值的 95% 置信区间。"""

    sample = np.asarray(values, dtype=float)
    if sample.size == 0 or not np.isfinite(sample).all():
        raise ValueError("bootstrap 样本必须为非空有限数值")
    if iterations <= 0 or block_size <= 0:
        raise ValueError("bootstrap 次数和区块长度必须为正数")

    rng = np.random.default_rng(seed)
    block_count = math.ceil(sample.size / block_size)
    offsets = np.arange(block_size)
    bootstrap_means = np.empty(iterations, dtype=float)
    for index in range(iterations):
        starts = rng.integers(0, sample.size, size=block_count)
        selected = (starts[:, None] + offsets[None, :]) % sample.size
        bootstrap_means[index] = float(sample[selected].reshape(-1)[: sample.size].mean())
    lower, upper = np.quantile(bootstrap_means, [0.025, 0.975])
    return float(lower), float(upper)


def _prepare_bars(bars: pd.DataFrame) -> pd.DataFrame:
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = sorted(required.difference(bars.columns))
    if missing:
        raise ValueError(f"K 线缺少必要列：{', '.join(missing)}")
    prepared = bars.copy(deep=True)
    prepared["timestamp"] = pd.to_datetime(prepared["timestamp"], utc=True, errors="coerce")
    prepared = prepared.dropna(subset=["timestamp"])
    prepared = prepared.sort_values("timestamp", kind="stable").drop_duplicates(
        subset=["timestamp"],
        keep="last",
    )
    if "amount" not in prepared:
        prepared["amount"] = np.nan
    return prepared.reset_index(drop=True)


def _normalize_as_of(value: date | datetime) -> pd.Timestamp:
    if isinstance(value, datetime):
        normalized = pd.Timestamp(value)
        if normalized.tzinfo is None:
            return normalized.tz_localize(UTC)
        return normalized.tz_convert(UTC)
    return pd.Timestamp(datetime.combine(value, time.max, tzinfo=UTC))


def _unavailable_price_group(group: str, *, observed_bars: int) -> JsonDict:
    return {
        "group": group,
        "status": "unavailable",
        "score": None,
        "factors": {},
        "missing_factors": ["minimum_warmup_bars"],
        "source_ids": [],
        "observed_bars": observed_bars,
        "required_bars": MINIMUM_WARMUP_BARS,
    }


def _liquidity_score(factors: Mapping[str, float | None]) -> float | None:
    illiquidity = factors.get("illiquidity_score")
    if illiquidity is None:
        return None
    return (1.0 - illiquidity) * 100.0


def _forward_gross_return(
    bars: pd.DataFrame,
    *,
    signal_date: date,
    horizon_days: int,
) -> float | None:
    prepared = _prepare_bars(bars)
    indexes = prepared.index[prepared["timestamp"].dt.date == signal_date].tolist()
    if not indexes:
        return None
    signal_index = int(indexes[-1])
    entry_index = signal_index + 1
    exit_index = signal_index + horizon_days
    if exit_index >= len(prepared):
        return None
    entry = _finite_or_none(prepared.iloc[entry_index]["open"])
    exit_price = _finite_or_none(prepared.iloc[exit_index]["close"])
    if entry is None or exit_price is None or entry <= 0:
        return None
    return exit_price / entry - 1.0


def _aggregate_daily_outcomes(
    outcomes: Sequence[WalkForwardOutcome],
) -> dict[tuple[date, int], dict[str, float]]:
    grouped: dict[tuple[date, int], dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for outcome in outcomes:
        if outcome.horizon_days not in SUPPORTED_HORIZONS:
            continue
        values = {
            "net_return": outcome.net_return,
            "benchmark_return": outcome.benchmark_return,
            "excess_return": outcome.excess_return,
        }
        if not all(math.isfinite(float(value)) for value in values.values()):
            continue
        target = grouped[(outcome.signal_date, outcome.horizon_days)]
        for key, value in values.items():
            target[key].append(float(value))
    return {
        key: {metric: float(np.mean(values)) for metric, values in metrics.items()}
        for key, metrics in grouped.items()
    }


def _coverage_is_valid(weights: Sequence[float]) -> bool:
    if not weights:
        return False
    covered = sum(
        math.isfinite(float(weight)) and float(weight) >= MINIMUM_ASSET_AVAILABLE_WEIGHT
        for weight in weights
    )
    return covered / len(weights) >= MINIMUM_COVERED_ASSET_RATIO


def _split_three_phases(days: Sequence[date]) -> tuple[list[date], list[date], list[date]]:
    quotient, remainder = divmod(len(days), 3)
    sizes = [quotient + (1 if index < remainder else 0) for index in range(3)]
    output: list[list[date]] = []
    start = 0
    for size in sizes:
        output.append(list(days[start : start + size]))
        start += size
    return output[0], output[1], output[2]


def _mean_daily_metric(
    daily: Mapping[tuple[date, int], Mapping[str, float]],
    days: Sequence[date],
    *,
    horizon_days: int,
    metric: str,
) -> float | None:
    values = [daily[(day, horizon_days)][metric] for day in days]
    return float(np.mean(values)) if values else None


def _maximum_drawdown(returns: Sequence[float]) -> float:
    if not returns:
        return 0.0
    normalized = np.maximum(np.asarray(returns, dtype=float), -1.0)
    wealth = np.concatenate(([1.0], np.cumprod(1.0 + normalized)))
    peaks = np.maximum.accumulate(wealth)
    drawdown = np.divide(wealth, peaks, out=np.ones_like(wealth), where=peaks != 0) - 1.0
    return float(drawdown.min())


def _finite_or_none(value: Any) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None
