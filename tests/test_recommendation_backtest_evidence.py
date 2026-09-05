from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from finance_agent.recommendations.service import RecommendationService, build_backtest_evidence


class _Screenings:
    def get_screening_result(self, screening_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            screening_id=screening_id,
            universe_id="universe:merged:ashare:recommendation",
            market="ashare",
            passed_count=1,
        )


class _Scores:
    def list_scores_for_screening(self, screening_id: str) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                score_id="score:ashare:000001",
                asset_id="ashare:000001",
                symbol="000001",
                market="ashare",
                horizon="swing",
                total_score=Decimal("82.5"),
                confidence=Decimal("0.72"),
                missing_penalty=Decimal("0"),
                rank=1,
                factor_frame_id="factor:ashare:000001:swing",
                payload={
                    "strategy_id": "strategy:ashare:short_swing",
                    "weight_snapshot": {"group_weights": {"technical": 0.8}},
                    "missing_groups": [],
                },
            )
        ]


class _Signals:
    def get_latest_signal(self, *, asset_id: str, horizon: str) -> SimpleNamespace:
        return SimpleNamespace(
            signal_id=f"signal:{asset_id}",
            direction="bullish",
            score=Decimal("76"),
            status="available",
        )


class _Risks:
    def list_recent_risks(self, *, asset_id: str, limit: int) -> list[SimpleNamespace]:
        return []


class _Assets:
    def get_asset_or_none(self, asset_id: str) -> SimpleNamespace:
        return SimpleNamespace(name="平安银行")


class _Recommendations:
    def __init__(self) -> None:
        self.asset_payloads: list[dict[str, Any]] = []
        self.run_payload: dict[str, Any] | None = None

    def upsert_run_universe(self, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(**kwargs)

    def upsert_asset_recommendation(self, **kwargs: Any) -> SimpleNamespace:
        self.asset_payloads.append(kwargs)
        return SimpleNamespace(recommendation_id=kwargs["recommendation_id"])

    def upsert_run(self, **kwargs: Any) -> SimpleNamespace:
        self.run_payload = kwargs["payload"]
        return SimpleNamespace(**kwargs)


class _Backtests:
    def __init__(self, row: SimpleNamespace | None) -> None:
        self.row = row
        self.calls: list[dict[str, str]] = []

    def get_latest_result(
        self,
        *,
        market: str,
        strategy_id: str,
        universe_id: str,
        status: str = "available",
    ) -> SimpleNamespace | None:
        self.calls.append(
            {
                "market": market,
                "strategy_id": strategy_id,
                "universe_id": universe_id,
                "status": status,
            }
        )
        return self.row


def test_recommendation_payload_attaches_latest_backtest_reference() -> None:
    backtest = SimpleNamespace(
        backtest_id="bt:factor_score_topn:ashare:1",
        market="ashare",
        strategy_id="strategy:ashare:short_swing",
        universe_id="universe:merged:ashare:recommendation",
        start_at=datetime(2021, 1, 1, tzinfo=UTC),
        end_at=datetime(2026, 1, 1, tzinfo=UTC),
        rebalance_frequency="once",
        metrics={
            "cagr": 0.1234,
            "max_drawdown": -0.182,
            "sharpe": 1.21,
            "period_win_rate": 0.57,
        },
        data_versions={"score_mode": "replayed", "bars": {"timeframe": "1d"}},
        status="available",
        created_at=datetime(2026, 6, 13, 9, 0, tzinfo=UTC),
        payload={"warnings": []},
    )
    recommendations = _Recommendations()
    backtests = _Backtests(backtest)
    service = build_service(recommendations=recommendations, backtests=backtests)

    service.rank_from_screening(screening_id="screen:1", strategy="balanced_swing_v1")

    assert backtests.calls == [
        {
            "market": "ashare",
            "strategy_id": "strategy:ashare:short_swing",
            "universe_id": "universe:merged:ashare:recommendation",
            "status": "available",
        }
    ]
    payload = recommendations.asset_payloads[0]["payload"]
    assert payload["backtest_evidence"]["status"] == "available"
    assert payload["backtest_evidence"]["backtest_id"] == "bt:factor_score_topn:ashare:1"
    assert payload["backtest_evidence"]["metrics"]["cagr"] == 0.1234
    assert "年化收益" in payload["backtest_evidence"]["summary"]
    assert recommendations.run_payload is not None
    assert recommendations.run_payload["backtest_evidence"]["backtest_id"] == "bt:factor_score_topn:ashare:1"


def test_recommendation_payload_marks_missing_backtest_evidence() -> None:
    recommendations = _Recommendations()
    service = build_service(
        recommendations=recommendations,
        backtests=_Backtests(None),
    )

    service.rank_from_screening(screening_id="screen:1", strategy="balanced_swing_v1")

    payload = recommendations.asset_payloads[0]["payload"]
    assert payload["backtest_evidence"] == {
        "status": "missing",
        "market": "ashare",
        "strategy_id": "strategy:ashare:short_swing",
        "universe_id": "universe:merged:ashare:recommendation",
        "reason": "暂无同策略回测证据",
        "certainty_adjustment": "lower",
    }


def build_service(*, recommendations: _Recommendations, backtests: _Backtests) -> RecommendationService:
    service = RecommendationService.__new__(RecommendationService)
    service.assets = _Assets()
    service.screenings = _Screenings()
    service.scores = _Scores()
    service.signals = _Signals()
    service.risks = _Risks()
    service.recommendations = recommendations
    service.backtests = backtests
    return service


def test_incomplete_v2_backtest_is_not_advertised_as_gating_eligible() -> None:
    row = SimpleNamespace(
        backtest_id="bt:incomplete", market="ashare", strategy_id="strategy:test",
        universe_id="universe:test", start_at=datetime(2025, 1, 1, tzinfo=UTC),
        end_at=datetime(2026, 1, 1, tzinfo=UTC), created_at=None,
        rebalance_frequency="daily_close", status="available", data_versions={},
        metrics={"gate_passed": True}, payload={"schema_version": "strategy_walk_forward_v2"},
    )
    evidence = build_backtest_evidence(
        backtests=_Backtests(row), market="ashare", strategy_id=row.strategy_id,
        universe_id=row.universe_id,
    )
    assert evidence["gating_eligible"] is False
    assert evidence["research_only"] is True
