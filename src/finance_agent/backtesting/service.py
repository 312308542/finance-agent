"""轻量回测服务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Protocol

import pandas as pd

from finance_agent.backtesting.adapters import BtBacktestAdapter
from finance_agent.backtesting.models import BacktestResult, JsonDict

ScoreMode = Literal["replayed", "historical"]


class BacktestScoreSource(Protocol):
    """回测服务读取评分截面的端口。"""

    def list_replayed_scores(self, *, universe_id: str, strategy_id: str, as_of: datetime) -> list[Any]:
        """按当前策略权重重放历史因子后的评分截面。"""

    def list_historical_scores(
        self, *, universe_id: str, strategy_id: str, as_of: datetime
    ) -> list[Any]:
        """历史真实落库的评分截面。"""


class BacktestPriceSource(Protocol):
    """回测服务读取 K 线价格的端口。"""

    def list_bars(self, *, asset_id: str, start_at: datetime, end_at: datetime) -> list[Any]:
        """读取单资产在窗口内的日 K。"""


@dataclass(frozen=True)
class BacktestServiceRequest:
    """factor_score_topn 回测请求。"""

    market: str
    universe_id: str
    strategy_id: str
    start_at: datetime
    end_at: datetime
    score_mode: ScoreMode
    topn: int = 20
    rebalance: str = "once"
    timeframe: str = "1d"


class BacktestService:
    """组合评分 TopN 回测服务。"""

    def __init__(
        self,
        *,
        score_source: BacktestScoreSource,
        price_source: BacktestPriceSource,
        adapter: BtBacktestAdapter | None = None,
    ) -> None:
        self.score_source = score_source
        self.price_source = price_source
        self.adapter = adapter or BtBacktestAdapter()

    def run_topn(self, request: BacktestServiceRequest) -> BacktestResult:
        """按评分截面选 TopN 标的并运行等权回测。"""

        if request.score_mode not in {"replayed", "historical"}:
            raise ValueError(f"不支持的 score_mode：{request.score_mode}")
        if request.topn <= 0:
            raise ValueError("topn 必须为正整数")
        if request.start_at >= request.end_at:
            raise ValueError("start_at 必须早于 end_at")

        scores = self._load_scores(request)
        selected_scores = sorted(scores, key=_score_value, reverse=True)[: request.topn]
        if not selected_scores:
            return self._partial_result(request, warnings=["评分截面为空"], selected_asset_count=0)

        prices = self._build_price_frame(
            selected_scores,
            start_at=request.start_at,
            end_at=request.end_at,
        )
        if prices.empty or prices.shape[1] == 0 or len(prices.index) < 2:
            return self._partial_result(
                request,
                warnings=["行情数据不足"],
                selected_asset_count=len(selected_scores),
            )

        result = self.adapter.run_equal_weight(
            prices,
            strategy_name="factor_score_topn",
            rebalance=request.rebalance,
            data_versions={
                "bars_start_at": request.start_at.isoformat(),
                "bars_end_at": request.end_at.isoformat(),
                "score_mode": request.score_mode,
                "strategy_id": request.strategy_id,
                "universe_id": request.universe_id,
                "timeframe": request.timeframe,
                "selected_assets": [_asset_id(score) for score in selected_scores],
            },
            strategy_params={
                "market": request.market,
                "topn": request.topn,
                "rebalance": request.rebalance,
            },
            signal_version=request.strategy_id,
        )
        metrics = dict(result.metrics)
        metrics["selected_asset_count"] = len(prices.columns)
        return BacktestResult(
            strategy_name=result.strategy_name,
            status=result.status,
            start=result.start,
            end=result.end,
            metrics=metrics,
            equity_curve=result.equity_curve,
            drawdown_curve=result.drawdown_curve,
            data_versions=result.data_versions,
            strategy_params=result.strategy_params,
            signal_version=result.signal_version,
            warnings=result.warnings,
        )

    def _load_scores(self, request: BacktestServiceRequest) -> list[Any]:
        if request.score_mode == "replayed":
            return self.score_source.list_replayed_scores(
                universe_id=request.universe_id,
                strategy_id=request.strategy_id,
                as_of=request.start_at,
            )
        return self.score_source.list_historical_scores(
            universe_id=request.universe_id,
            strategy_id=request.strategy_id,
            as_of=request.start_at,
        )

    def _build_price_frame(
        self,
        scores: list[Any],
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> pd.DataFrame:
        series_by_symbol: dict[str, pd.Series] = {}
        for score in scores:
            bars = self.price_source.list_bars(
                asset_id=_asset_id(score),
                start_at=start_at,
                end_at=end_at,
            )
            if len(bars) < 2:
                continue
            series = pd.Series(
                [_decimal_float(getattr(bar, "close")) for bar in bars],
                index=pd.to_datetime([getattr(bar, "timestamp") for bar in bars]),
                name=_symbol(score),
                dtype="float64",
            )
            series_by_symbol[_symbol(score)] = series.sort_index()
        if not series_by_symbol:
            return pd.DataFrame()
        return pd.DataFrame(series_by_symbol).sort_index().ffill().dropna(how="all")

    def _partial_result(
        self,
        request: BacktestServiceRequest,
        *,
        warnings: list[str],
        selected_asset_count: int,
    ) -> BacktestResult:
        return BacktestResult(
            strategy_name="factor_score_topn",
            status="partial",
            start=request.start_at.date().isoformat(),
            end=request.end_at.date().isoformat(),
            metrics={"selected_asset_count": selected_asset_count},
            data_versions={
                "score_mode": request.score_mode,
                "strategy_id": request.strategy_id,
                "universe_id": request.universe_id,
                "timeframe": request.timeframe,
            },
            strategy_params={
                "market": request.market,
                "topn": request.topn,
                "rebalance": request.rebalance,
            },
            signal_version=request.strategy_id,
            warnings=warnings,
        )


def _asset_id(score: Any) -> str:
    return str(getattr(score, "asset_id"))


def _symbol(score: Any) -> str:
    return str(getattr(score, "symbol", _asset_id(score)))


def _score_value(score: Any) -> float:
    return _decimal_float(getattr(score, "total_score"))


def _decimal_float(value: Decimal | int | float | str) -> float:
    return float(value)
