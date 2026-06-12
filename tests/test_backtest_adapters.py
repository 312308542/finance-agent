from __future__ import annotations

import math

import pandas as pd

from finance_agent.backtesting.adapters import (
    BtBacktestAdapter,
    QuantstatsPerformanceAdapter,
)


def test_bt_backtest_adapter_returns_domain_result() -> None:
    """bt 适配器应只返回领域回测结果，不泄漏 bt 内部对象。"""

    dates = pd.date_range("2024-01-01", periods=6, freq="D")
    prices = pd.DataFrame(
        {
            "AAA": [100, 102, 104, 106, 108, 110],
            "BBB": [50, 50, 52, 52, 54, 54],
        },
        index=dates,
    )

    result = BtBacktestAdapter().run_equal_weight(
        prices,
        strategy_name="fixture_equal_weight",
        data_versions={"bars_watermark": "2024-01-06", "score_mode": "fixture"},
        signal_version="fixture-signal-v1",
    )

    payload = result.to_dict()

    assert payload["strategy_name"] == "fixture_equal_weight"
    assert payload["status"] == "completed"
    assert payload["data_versions"]["score_mode"] == "fixture"
    assert payload["signal_version"] == "fixture-signal-v1"
    assert payload["metrics"]["total_return"] > 0
    assert payload["metrics"]["max_drawdown"] <= 0
    assert payload["metrics"]["cagr"] > 0
    assert len(payload["equity_curve"]) >= len(prices)
    assert len(payload["drawdown_curve"]) == len(payload["equity_curve"])
    assert "bt" not in str(type(result)).lower()


def test_quantstats_adapter_returns_performance_report() -> None:
    """quantstats 适配器应输出统一绩效协议，并标明胜率口径。"""

    returns = pd.Series(
        [0.01, -0.005, 0.02, -0.01, 0.015, 0.0],
        index=pd.date_range("2024-01-01", periods=6, freq="D"),
        name="fixture_strategy",
    )

    report = QuantstatsPerformanceAdapter().analyze(
        returns,
        strategy_name="fixture_strategy",
        benchmark_name="cash",
    )
    payload = report.to_dict()

    assert payload["strategy_name"] == "fixture_strategy"
    assert payload["benchmark_name"] == "cash"
    assert payload["status"] == "completed"
    assert payload["metrics"]["period_count"] == 6
    assert math.isclose(payload["metrics"]["period_win_rate"], 0.5)
    assert payload["metrics"]["sharpe"] is not None
    assert payload["metrics"]["max_drawdown"] <= 0
    assert payload["win_rate_basis"] == "period_return"
    assert "quantstats" not in str(type(report)).lower()
