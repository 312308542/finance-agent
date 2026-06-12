"""第三方回测/绩效库适配层。

本模块是 bt 与 quantstats 的唯一直接调用边界，向上只返回本项目的
`BacktestResult` 与 `PerformanceReport`。
"""

from __future__ import annotations

from typing import Any

import bt
import pandas as pd
import quantstats as qs

from finance_agent.backtesting.models import BacktestResult, JsonDict, PerformanceReport


class BtBacktestAdapter:
    """bt 组合回测适配器。"""

    def run_equal_weight(
        self,
        prices: pd.DataFrame,
        *,
        strategy_name: str = "equal_weight",
        rebalance: str = "once",
        data_versions: JsonDict | None = None,
        strategy_params: JsonDict | None = None,
        signal_version: str | None = None,
    ) -> BacktestResult:
        """运行等权组合回测，并返回统一领域结果。"""

        normalized_prices = _validate_prices(prices)
        strategy = bt.Strategy(
            strategy_name,
            [
                _rebalance_algo(rebalance),
                bt.algos.SelectAll(),
                bt.algos.WeighEqually(),
                bt.algos.Rebalance(),
            ],
        )
        result = bt.run(bt.Backtest(strategy, normalized_prices))
        equity = result.prices[strategy_name].dropna()
        returns = equity.pct_change().fillna(0.0)
        drawdown = (equity / equity.cummax() - 1.0).fillna(0.0)
        stats = result.stats[strategy_name] if strategy_name in result.stats.columns else pd.Series()

        metrics = {
            "total_return": _finite_float(stats.get("total_return"), default=_total_return(equity)),
            "cagr": _finite_float(stats.get("cagr")),
            "max_drawdown": _finite_float(stats.get("max_drawdown"), default=float(drawdown.min())),
            "sharpe": _finite_float(stats.get("daily_sharpe")),
            "volatility": _finite_float(stats.get("daily_vol")),
            "period_count": int(len(returns)),
        }
        params = {"rebalance": rebalance} | dict(strategy_params or {})

        return BacktestResult(
            strategy_name=strategy_name,
            status="completed",
            start=_date_string(equity.index.min()),
            end=_date_string(equity.index.max()),
            metrics=metrics,
            equity_curve=_series_curve(equity, "nav"),
            drawdown_curve=_series_curve(drawdown, "drawdown"),
            data_versions=dict(data_versions or {}),
            strategy_params=params,
            signal_version=signal_version,
        )


class QuantstatsPerformanceAdapter:
    """quantstats 收益绩效适配器。"""

    def analyze(
        self,
        returns: pd.Series,
        *,
        strategy_name: str,
        benchmark_name: str | None = None,
        html_report_path: str | None = None,
    ) -> PerformanceReport:
        """分析收益率序列，输出统一绩效协议。"""

        normalized_returns = _validate_returns(returns)
        metrics = {
            "cagr": _finite_float(qs.stats.cagr(normalized_returns)),
            "sharpe": _finite_float(qs.stats.sharpe(normalized_returns)),
            "sortino": _finite_float(qs.stats.sortino(normalized_returns)),
            "max_drawdown": _finite_float(qs.stats.max_drawdown(normalized_returns)),
            "volatility": _finite_float(qs.stats.volatility(normalized_returns)),
            "period_win_rate": _period_win_rate(normalized_returns),
            "period_count": int(len(normalized_returns)),
        }
        return PerformanceReport(
            strategy_name=strategy_name,
            status="completed",
            metrics=metrics,
            benchmark_name=benchmark_name,
            win_rate_basis="period_return",
            html_report_path=html_report_path,
        )


def _validate_prices(prices: pd.DataFrame) -> pd.DataFrame:
    if prices.empty:
        raise ValueError("prices 不能为空")
    numeric_prices = prices.copy()
    numeric_prices.index = pd.to_datetime(numeric_prices.index)
    numeric_prices = numeric_prices.sort_index().astype(float)
    if numeric_prices.isna().all(axis=None):
        raise ValueError("prices 不能全部为空")
    return numeric_prices.ffill().dropna(how="all")


def _validate_returns(returns: pd.Series) -> pd.Series:
    if returns.empty:
        raise ValueError("returns 不能为空")
    normalized = returns.copy()
    normalized.index = pd.to_datetime(normalized.index)
    normalized = normalized.sort_index().astype(float).fillna(0.0)
    return normalized


def _rebalance_algo(rebalance: str) -> bt.Algo:
    if rebalance == "once":
        return bt.algos.RunOnce()
    if rebalance == "monthly":
        return bt.algos.RunMonthly()
    if rebalance == "weekly":
        return bt.algos.RunWeekly()
    raise ValueError(f"不支持的再平衡频率：{rebalance}")


def _date_string(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).date().isoformat()


def _series_curve(series: pd.Series, value_key: str) -> list[JsonDict]:
    return [
        {"date": _date_string(index), value_key: _finite_float(value)}
        for index, value in series.items()
        if _finite_float(value) is not None
    ]


def _total_return(equity: pd.Series) -> float | None:
    if len(equity) < 2 or float(equity.iloc[0]) == 0:
        return None
    return float(equity.iloc[-1] / equity.iloc[0] - 1.0)


def _period_win_rate(returns: pd.Series) -> float | None:
    """按周期收益计算胜率，0 收益周期计入分母。"""

    if returns.empty:
        return None
    return float((returns > 0).sum() / len(returns))


def _finite_float(value: Any, *, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if pd.isna(number) or number in {float("inf"), float("-inf")}:
        return default
    return number
