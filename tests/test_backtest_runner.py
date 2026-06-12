from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from finance_agent.backtesting.models import BacktestResult


def test_run_factor_score_topn_backtest_persists_result(monkeypatch: Any) -> None:
    """回测运行入口应组装服务请求，并把领域结果落库为可追溯记录。"""

    from finance_agent.backtesting import runner

    calls: list[dict[str, Any]] = []

    class FakeBacktestService:
        def __init__(self, *, score_source: Any, price_source: Any) -> None:
            calls.append(
                {
                    "kind": "service_init",
                    "score_source_type": type(score_source).__name__,
                    "price_source_type": type(price_source).__name__,
                }
            )

        def run_topn(self, request: Any) -> BacktestResult:
            calls.append(
                {
                    "kind": "run_topn",
                    "market": request.market,
                    "universe_id": request.universe_id,
                    "strategy_id": request.strategy_id,
                    "score_mode": request.score_mode,
                    "topn": request.topn,
                    "rebalance": request.rebalance,
                    "timeframe": request.timeframe,
                }
            )
            return BacktestResult(
                strategy_name="factor_score_topn",
                status="available",
                start=request.start_at.date().isoformat(),
                end=request.end_at.date().isoformat(),
                metrics={"cagr": 0.12, "selected_asset_count": 20},
                data_versions={"score_mode": request.score_mode},
                strategy_params={"topn": request.topn},
                signal_version=request.strategy_id,
            )

    class FakeBacktestRepository:
        def __init__(self, session: object) -> None:
            calls.append({"kind": "repository_init", "session": session})

        def upsert_result(self, **kwargs: Any) -> SimpleNamespace:
            calls.append({"kind": "upsert_result", **kwargs})
            return SimpleNamespace(backtest_id=kwargs["backtest_id"])

    monkeypatch.setattr(runner, "BacktestService", FakeBacktestService)
    monkeypatch.setattr(runner, "BacktestRepository", FakeBacktestRepository)

    session = object()
    payload = runner.run_factor_score_topn_backtest(
        session,
        strategy="factor_score_topn",
        market="ashare",
        universe_id="universe:merged:ashare:recommendation",
        strategy_id="strategy:ashare:short_swing",
        years=3,
        score_mode="replayed",
        topn=20,
        rebalance="once",
        timeframe="1d",
        end_at=datetime(2026, 6, 13, tzinfo=UTC),
    )

    assert payload["status"] == "available"
    assert payload["backtest_id"].startswith("bt:")
    assert payload["metrics"] == {"cagr": 0.12, "selected_asset_count": 20}
    assert calls[0]["kind"] == "service_init"
    assert calls[1]["kind"] == "run_topn"
    assert calls[1]["strategy_id"] == "strategy:ashare:short_swing"
    assert calls[2] == {"kind": "repository_init", "session": session}
    assert calls[3]["kind"] == "upsert_result"
    assert calls[3]["market"] == "ashare"
    assert calls[3]["strategy_id"] == "strategy:ashare:short_swing"
    assert calls[3]["universe_id"] == "universe:merged:ashare:recommendation"
    assert calls[3]["status"] == "available"
    assert calls[3]["metrics"] == {"cagr": 0.12, "selected_asset_count": 20}
    assert calls[3]["payload"]["strategy_name"] == "factor_score_topn"


def test_run_factor_score_topn_backtest_rejects_unknown_strategy() -> None:
    """回测入口暂时只允许方案内置的 factor_score_topn 策略。"""

    from finance_agent.backtesting import runner

    try:
        runner.run_factor_score_topn_backtest(
            object(),
            strategy="unknown",
            market="ashare",
            universe_id="universe:merged:ashare:recommendation",
            strategy_id="strategy:ashare:short_swing",
            years=5,
        )
    except ValueError as exc:
        assert "factor_score_topn" in str(exc)
    else:
        raise AssertionError("未知回测策略应被拒绝")
