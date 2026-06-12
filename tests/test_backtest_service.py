from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from finance_agent.backtesting.service import BacktestService, BacktestServiceRequest


@dataclass(frozen=True)
class _ScoreRow:
    asset_id: str
    symbol: str
    market: str
    total_score: Decimal
    as_of: datetime


@dataclass(frozen=True)
class _BarRow:
    asset_id: str
    timestamp: datetime
    close: Decimal


class _ScoreSource:
    def __init__(self, rows_by_date: dict[str, list[_ScoreRow]]) -> None:
        self.rows_by_date = rows_by_date
        self.replayed_calls: list[str] = []
        self.historical_calls: list[str] = []

    def list_replayed_scores(self, *, universe_id: str, strategy_id: str, as_of: datetime):
        self.replayed_calls.append(strategy_id)
        return self.rows_by_date.get(as_of.date().isoformat(), [])

    def list_historical_scores(self, *, universe_id: str, strategy_id: str, as_of: datetime):
        self.historical_calls.append(strategy_id)
        return self.rows_by_date.get(as_of.date().isoformat(), [])


class _PriceSource:
    def __init__(self, bars_by_asset: dict[str, list[_BarRow]]) -> None:
        self.bars_by_asset = bars_by_asset

    def list_bars(self, *, asset_id: str, start_at: datetime, end_at: datetime):
        return [
            bar
            for bar in self.bars_by_asset.get(asset_id, [])
            if start_at <= bar.timestamp <= end_at
        ]


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def test_backtest_service_runs_replayed_topn_strategy() -> None:
    """replayed 模式应按评分截面 TopN 选股并产出完整回测结果。"""

    score_source = _ScoreSource(
        {
            "2024-01-01": [
                _ScoreRow("asset:aaa", "AAA", "ashare", Decimal("95"), _dt("2024-01-01")),
                _ScoreRow("asset:bbb", "BBB", "ashare", Decimal("80"), _dt("2024-01-01")),
                _ScoreRow("asset:ccc", "CCC", "ashare", Decimal("60"), _dt("2024-01-01")),
            ],
        }
    )
    price_source = _PriceSource(
        {
            "asset:aaa": [
                _BarRow("asset:aaa", _dt("2024-01-01"), Decimal("10")),
                _BarRow("asset:aaa", _dt("2024-01-02"), Decimal("11")),
                _BarRow("asset:aaa", _dt("2024-01-03"), Decimal("12")),
            ],
            "asset:bbb": [
                _BarRow("asset:bbb", _dt("2024-01-01"), Decimal("20")),
                _BarRow("asset:bbb", _dt("2024-01-02"), Decimal("20")),
                _BarRow("asset:bbb", _dt("2024-01-03"), Decimal("21")),
            ],
        }
    )

    result = BacktestService(score_source=score_source, price_source=price_source).run_topn(
        BacktestServiceRequest(
            market="ashare",
            universe_id="universe:test",
            strategy_id="short_swing",
            score_mode="replayed",
            start_at=_dt("2024-01-01"),
            end_at=_dt("2024-01-03"),
            topn=2,
        )
    )

    payload = result.to_dict()

    assert payload["status"] == "completed"
    assert payload["strategy_params"]["topn"] == 2
    assert payload["data_versions"]["score_mode"] == "replayed"
    assert payload["data_versions"]["strategy_id"] == "short_swing"
    assert payload["metrics"]["selected_asset_count"] == 2
    assert payload["metrics"]["total_return"] > 0
    assert score_source.replayed_calls == ["short_swing"]
    assert score_source.historical_calls == []


def test_backtest_service_uses_historical_scores() -> None:
    """historical 模式应读取历史评分截面，而不是重放评分。"""

    score_source = _ScoreSource(
        {
            "2024-01-01": [
                _ScoreRow("asset:aaa", "AAA", "ashare", Decimal("91"), _dt("2024-01-01")),
            ],
        }
    )
    price_source = _PriceSource(
        {
            "asset:aaa": [
                _BarRow("asset:aaa", _dt("2024-01-01"), Decimal("10")),
                _BarRow("asset:aaa", _dt("2024-01-02"), Decimal("10.5")),
            ],
        }
    )

    result = BacktestService(score_source=score_source, price_source=price_source).run_topn(
        BacktestServiceRequest(
            market="ashare",
            universe_id="universe:test",
            strategy_id="short_swing",
            score_mode="historical",
            start_at=_dt("2024-01-01"),
            end_at=_dt("2024-01-02"),
            topn=1,
        )
    )

    assert result.status == "completed"
    assert result.data_versions["score_mode"] == "historical"
    assert score_source.historical_calls == ["short_swing"]
    assert score_source.replayed_calls == []


def test_backtest_service_marks_partial_when_data_is_insufficient() -> None:
    """评分或行情不足时应返回 partial，不编造完整回测结论。"""

    result = BacktestService(score_source=_ScoreSource({}), price_source=_PriceSource({})).run_topn(
        BacktestServiceRequest(
            market="ashare",
            universe_id="universe:test",
            strategy_id="short_swing",
            score_mode="replayed",
            start_at=_dt("2024-01-01"),
            end_at=_dt("2024-01-03"),
            topn=3,
        )
    )

    assert result.status == "partial"
    assert result.metrics["selected_asset_count"] == 0
    assert "评分截面为空" in result.warnings


def test_backtest_service_rejects_unknown_score_mode() -> None:
    """未知评分模式应明确报错，避免混淆历史实盘和模拟回放。"""

    with pytest.raises(ValueError, match="score_mode"):
        BacktestService(score_source=_ScoreSource({}), price_source=_PriceSource({})).run_topn(
            BacktestServiceRequest(
                market="ashare",
                universe_id="universe:test",
                strategy_id="short_swing",
                score_mode="future",
                start_at=_dt("2024-01-01"),
                end_at=_dt("2024-01-03"),
            )
        )
