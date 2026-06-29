"""P2 方法论轻量确定性适配器。

这些适配器只负责把已入库行情序列转换为可审计的结构化结果，供圆桌 skill
解读。LLM 不参与任何线、相关系数、配对价差或信号方向计算。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class PriceBar:
    """OHLCV K 线输入。"""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class PricePoint:
    """单个收盘价点。"""

    timestamp: datetime
    close: float


@dataclass(frozen=True)
class AssetCloseSeries:
    """单标的收盘价序列。"""

    asset_id: str
    symbol: str
    market: str
    prices: list[PricePoint]


@dataclass(frozen=True)
class IchimokuResult:
    """一目均衡表结构化结果。"""

    status: str
    asset_id: str
    symbol: str
    market: str
    timeframe: str
    input_start_at: datetime
    input_end_at: datetime
    bar_count: int
    lines: JsonDict
    signals: tuple[JsonDict, ...]
    evidence_id: str

    def to_indicator_payload(self) -> JsonDict:
        """转换为可持久化 payload。"""

        return {
            "schema_version": "ichimoku_v1",
            "status": self.status,
            "asset_id": self.asset_id,
            "symbol": self.symbol,
            "market": self.market,
            "timeframe": self.timeframe,
            "input_start_at": self.input_start_at.isoformat(),
            "input_end_at": self.input_end_at.isoformat(),
            "bar_count": self.bar_count,
            "lines": self.lines,
            "signals": list(self.signals),
            "evidence_id": self.evidence_id,
            "red_lines": [
                "一目均衡线由确定性适配器计算，LLM 只能解读。",
                "不得用模型自行补算缺失线值或修改信号方向。",
            ],
        }


@dataclass(frozen=True)
class CorrelationResult:
    """相关性矩阵结构化结果。"""

    status: str
    market: str
    timeframe: str
    input_end_at: datetime
    observation_count: int
    correlations: tuple[JsonDict, ...]
    evidence_id: str

    def to_indicator_payload(self) -> JsonDict:
        """转换为可持久化 payload。"""

        return {
            "schema_version": "correlation_v1",
            "status": self.status,
            "market": self.market,
            "timeframe": self.timeframe,
            "input_end_at": self.input_end_at.isoformat(),
            "observation_count": self.observation_count,
            "correlations": list(self.correlations),
            "evidence_id": self.evidence_id,
            "red_lines": [
                "相关系数由确定性适配器计算，LLM 只能解释分散化或共振含义。",
                "不得引用入库数据之外的相关性事实。",
            ],
        }


@dataclass(frozen=True)
class PairTradingResult:
    """配对交易轻量统计结果。"""

    status: str
    left_asset_id: str
    right_asset_id: str
    market: str
    timeframe: str
    input_end_at: datetime
    observation_count: int
    hedge_ratio: float
    intercept: float
    spread_latest: float
    spread_zscore: float
    signal: JsonDict
    evidence_id: str

    def to_indicator_payload(self) -> JsonDict:
        """转换为可持久化 payload。"""

        return {
            "schema_version": "pair_trading_v1",
            "status": self.status,
            "left_asset_id": self.left_asset_id,
            "right_asset_id": self.right_asset_id,
            "market": self.market,
            "timeframe": self.timeframe,
            "input_end_at": self.input_end_at.isoformat(),
            "observation_count": self.observation_count,
            "hedge_ratio": self.hedge_ratio,
            "intercept": self.intercept,
            "spread_latest": self.spread_latest,
            "spread_zscore": self.spread_zscore,
            "signal": self.signal,
            "evidence_id": self.evidence_id,
            "red_lines": [
                "配对价差和 z-score 由确定性适配器计算，LLM 只能解读。",
                "当前轻量版不替代 statsmodels 协整检验。",
            ],
        }


class IchimokuAdapter:
    """一目均衡五线确定性适配器。"""

    def compute(
        self,
        *,
        asset_id: str,
        symbol: str,
        market: str,
        timeframe: str,
        bars: list[PriceBar],
    ) -> IchimokuResult:
        """计算最新一目均衡线与基础状态信号。"""

        if len(bars) < 52:
            raise ValueError("一目均衡计算至少需要 52 根 K 线。")

        frame = price_bars_to_frame(bars)
        tenkan = midpoint(frame["high"], frame["low"], window=9)
        kijun = midpoint(frame["high"], frame["low"], window=26)
        senkou_b = midpoint(frame["high"], frame["low"], window=52)
        senkou_a = (tenkan + kijun) / 2
        close = float(frame["close"].iloc[-1])
        lines = {
            "tenkan_sen": to_finite_float(tenkan),
            "kijun_sen": to_finite_float(kijun),
            "senkou_span_a": to_finite_float(senkou_a),
            "senkou_span_b": to_finite_float(senkou_b),
            "chikou_span": close,
        }
        signals = build_ichimoku_signals(close=close, lines=lines)
        input_start_at = normalize_datetime(bars[0].timestamp)
        input_end_at = normalize_datetime(bars[-1].timestamp)
        return IchimokuResult(
            status="available",
            asset_id=asset_id,
            symbol=symbol,
            market=market,
            timeframe=timeframe,
            input_start_at=input_start_at,
            input_end_at=input_end_at,
            bar_count=len(bars),
            lines=lines,
            signals=signals,
            evidence_id=build_evidence_id("ichimoku", asset_id, timeframe, input_end_at),
        )


class CorrelationAdapter:
    """收益率相关性确定性适配器。"""

    def compute(
        self,
        *,
        market: str,
        timeframe: str,
        series: list[AssetCloseSeries],
        min_observations: int = 20,
    ) -> CorrelationResult:
        """按时间对齐收盘价，计算收益率相关系数。"""

        if len(series) < 2:
            raise ValueError("相关性计算至少需要两个标的。")

        returns = build_aligned_returns(series)
        if len(returns) < min_observations:
            raise ValueError(f"相关性计算至少需要 {min_observations} 个对齐收益率。")

        correlations: list[JsonDict] = []
        for left_index, left in enumerate(series):
            for right in series[left_index + 1 :]:
                value = returns[left.asset_id].corr(returns[right.asset_id])
                if value is None or not math.isfinite(float(value)):
                    continue
                correlations.append(
                    {
                        "left_asset_id": left.asset_id,
                        "right_asset_id": right.asset_id,
                        "left_symbol": left.symbol,
                        "right_symbol": right.symbol,
                        "correlation": round(float(value), 6),
                    }
                )

        input_end_at = normalize_datetime(max(point.timestamp for item in series for point in item.prices))
        return CorrelationResult(
            status="available",
            market=market,
            timeframe=timeframe,
            input_end_at=input_end_at,
            observation_count=len(returns),
            correlations=tuple(correlations),
            evidence_id=build_evidence_id("correlation", market, timeframe, input_end_at),
        )


class PairTradingAdapter:
    """配对交易轻量统计适配器。"""

    def compute(
        self,
        *,
        left: AssetCloseSeries,
        right: AssetCloseSeries,
        timeframe: str,
        entry_zscore: float = 2.0,
        min_observations: int = 60,
    ) -> PairTradingResult:
        """计算 OLS hedge ratio、残差价差和最新 z-score。"""

        aligned = build_aligned_closes([left, right])
        if len(aligned) < min_observations:
            raise ValueError(f"配对交易计算至少需要 {min_observations} 个对齐收盘价。")

        left_values = aligned[left.asset_id].to_numpy(dtype=float)
        right_values = aligned[right.asset_id].to_numpy(dtype=float)
        hedge_ratio, intercept = ordinary_least_squares(x=right_values, y=left_values)
        spread = left_values - (hedge_ratio * right_values + intercept)
        spread_std = float(np.std(spread, ddof=1))
        if spread_std == 0 or not math.isfinite(spread_std):
            spread_zscore = 0.0
        else:
            spread_zscore = float((spread[-1] - float(np.mean(spread))) / spread_std)
        input_end_at = normalize_datetime(max(point.timestamp for point in left.prices + right.prices))
        return PairTradingResult(
            status="available",
            left_asset_id=left.asset_id,
            right_asset_id=right.asset_id,
            market=left.market,
            timeframe=timeframe,
            input_end_at=input_end_at,
            observation_count=len(aligned),
            hedge_ratio=round(hedge_ratio, 6),
            intercept=round(intercept, 6),
            spread_latest=round(float(spread[-1]), 6),
            spread_zscore=round(spread_zscore, 6),
            signal=build_pair_signal(spread_zscore, entry_zscore=entry_zscore),
            evidence_id=build_evidence_id(
                "pair_trading",
                f"{left.asset_id}:{right.asset_id}",
                timeframe,
                input_end_at,
            ),
        )


def price_bars_to_frame(bars: list[PriceBar]) -> pd.DataFrame:
    """把 K 线输入转换为 DataFrame。"""

    return pd.DataFrame(
        {
            "timestamp": [normalize_datetime(bar.timestamp) for bar in bars],
            "open": [float(bar.open) for bar in bars],
            "high": [float(bar.high) for bar in bars],
            "low": [float(bar.low) for bar in bars],
            "close": [float(bar.close) for bar in bars],
            "volume": [float(bar.volume) for bar in bars],
        }
    )


def midpoint(high: pd.Series, low: pd.Series, *, window: int) -> float:
    """计算指定窗口最高价和最低价的中点。"""

    if len(high) < window or len(low) < window:
        raise ValueError(f"窗口 {window} 需要足够 K 线。")
    return (float(high.tail(window).max()) + float(low.tail(window).min())) / 2


def build_ichimoku_signals(*, close: float, lines: JsonDict) -> tuple[JsonDict, ...]:
    """生成一目均衡基础信号。"""

    span_a = float(lines["senkou_span_a"])
    span_b = float(lines["senkou_span_b"])
    cloud_top = max(span_a, span_b)
    cloud_bottom = min(span_a, span_b)
    if close > cloud_top:
        return ({"name": "price_above_cloud", "direction": "bullish", "confidence": 0.7},)
    if close < cloud_bottom:
        return ({"name": "price_below_cloud", "direction": "bearish", "confidence": 0.7},)
    return ({"name": "price_inside_cloud", "direction": "neutral", "confidence": 0.5},)


def build_aligned_closes(series: list[AssetCloseSeries]) -> pd.DataFrame:
    """按时间对齐多个标的的收盘价。"""

    columns: dict[str, pd.Series] = {}
    for item in series:
        if not item.prices:
            raise ValueError(f"{item.asset_id} 缺少价格序列。")
        values = {
            normalize_datetime(point.timestamp): float(point.close)
            for point in item.prices
        }
        columns[item.asset_id] = pd.Series(values, name=item.asset_id).sort_index()
    frame = pd.concat(columns.values(), axis=1, join="inner").dropna()
    if frame.empty:
        raise ValueError("价格序列没有可对齐的时间点。")
    return frame


def build_aligned_returns(series: list[AssetCloseSeries]) -> pd.DataFrame:
    """按时间对齐多个标的的收益率。"""

    return build_aligned_closes(series).pct_change().dropna()


def ordinary_least_squares(*, x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """用 numpy 计算一元 OLS 斜率和截距。"""

    if len(x) != len(y) or len(x) < 2:
        raise ValueError("OLS 至少需要两个对齐观测。")
    slope, intercept = np.polyfit(x, y, deg=1)
    return float(slope), float(intercept)


def build_pair_signal(spread_zscore: float, *, entry_zscore: float) -> JsonDict:
    """根据价差 z-score 生成配对信号。"""

    if spread_zscore >= entry_zscore:
        return {
            "name": "short_left_long_right",
            "direction": "mean_reversion",
            "confidence": min(0.95, abs(spread_zscore) / (entry_zscore * 2)),
        }
    if spread_zscore <= -entry_zscore:
        return {
            "name": "long_left_short_right",
            "direction": "mean_reversion",
            "confidence": min(0.95, abs(spread_zscore) / (entry_zscore * 2)),
        }
    return {
        "name": "wait_for_spread",
        "direction": "neutral",
        "confidence": 0.5,
    }


def build_evidence_id(prefix: str, asset_or_market: str, timeframe: str, input_end_at: datetime) -> str:
    """生成方法论适配器证据 ID。"""

    normalized = input_end_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}:{asset_or_market}:{timeframe}:{normalized}"


def normalize_datetime(value: datetime) -> datetime:
    """统一时间为 UTC aware datetime。"""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_finite_float(value: float) -> float:
    """校验并返回有限浮点数。"""

    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("方法论计算结果出现非有限数值。")
    return parsed
