"""技术指标计算服务。

本服务只负责从标准 K 线计算指标并写入 `indicator_frames`。它不做因子评分、
初筛、推荐或 Agent 分析。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
import talib
from sqlalchemy.orm import Session

from finance_agent.storage.orm import IndicatorFrameORM, MarketBarORM
from finance_agent.storage.repositories import IndicatorFrameRepository, MarketDataRepository

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class IndicatorComputationResult:
    """单次指标计算结果摘要。"""

    status: str
    indicator_frame_id: str | None
    asset_id: str
    symbol: str | None
    market: str | None
    timeframe: str
    horizon: str
    bar_count: int
    missing_indicators: tuple[str, ...]
    error_message: str | None = None


class IndicatorService:
    """从 `market_bars` 计算推荐链路需要的技术指标。"""

    library = "talib"

    def __init__(self, session: Session) -> None:
        self.market_data = MarketDataRepository(session)
        self.indicators = IndicatorFrameRepository(session)

    def compute_for_asset(
        self,
        *,
        asset_id: str,
        timeframe: str = "1d",
        horizon: str = "swing",
        source: str | None = None,
        window: int = 120,
        min_bars: int = 2,
    ) -> IndicatorComputationResult:
        """读取单标的最近 K 线，计算指标并落库。

        K 线不足时返回 `unavailable` 摘要，避免写入没有稳定输入窗口的指标快照。
        """

        bars = self.market_data.list_recent_bars(
            asset_id=asset_id,
            timeframe=timeframe,
            source=source,
            limit=window,
        )
        if len(bars) < min_bars:
            return IndicatorComputationResult(
                status="unavailable",
                indicator_frame_id=None,
                asset_id=asset_id,
                symbol=bars[-1].symbol if bars else None,
                market=bars[-1].market if bars else None,
                timeframe=timeframe,
                horizon=horizon,
                bar_count=len(bars),
                missing_indicators=("market_bars",),
                error_message=f"K 线数量不足，至少需要 {min_bars} 根",
            )

        frame = self._bars_to_frame(bars)
        values, missing = compute_indicator_values(frame)
        status = "available" if not missing else "partial"
        first_bar = bars[0]
        last_bar = bars[-1]
        indicator_frame_id = build_indicator_frame_id(
            asset_id=asset_id,
            timeframe=timeframe,
            horizon=horizon,
            input_end_at=last_bar.timestamp,
        )
        saved = self.indicators.upsert_indicator_frame(
            indicator_frame_id=indicator_frame_id,
            asset_id=asset_id,
            symbol=last_bar.symbol,
            market=last_bar.market,
            timeframe=timeframe,
            horizon=horizon,
            library=self.library,
            library_version=talib.__version__,
            input_start_at=first_bar.timestamp,
            input_end_at=last_bar.timestamp,
            bar_count=len(bars),
            rsi_14=to_decimal(values.get("rsi_14")),
            macd=to_decimal(values.get("macd")),
            macd_signal=to_decimal(values.get("macd_signal")),
            macd_hist=to_decimal(values.get("macd_hist")),
            atr_14=to_decimal(values.get("atr_14")),
            bb_percent_b=to_decimal(values.get("bb_percent_b")),
            ma_20=to_decimal(values.get("ma_20")),
            ma_60=to_decimal(values.get("ma_60")),
            status=status,
            as_of=datetime.now(tz=UTC),
            payload={
                "schema_version": "1.0",
                "source": source,
                "bar_window": window,
                "computed_values": {key: to_json_number(value) for key, value in values.items()},
                "missing_indicators": list(missing),
                "input": {
                    "sources": sorted({bar.source for bar in bars}),
                    "adjustments": sorted({bar.adjustment for bar in bars}),
                    "timeframe": timeframe,
                    "horizon": horizon,
                },
                "notes": [
                    "TA-Lib 负责 RSI、MACD、ATR、布林带等技术指标。",
                    "pandas/numpy 负责收益率、均线、波动率、回撤等序列派生指标。",
                ],
            },
        )
        return self._result_from_frame(saved, missing=missing)

    @staticmethod
    def _bars_to_frame(bars: list[MarketBarORM]) -> pd.DataFrame:
        """把 ORM K 线转换成指标计算 DataFrame。"""

        return pd.DataFrame(
            {
                "timestamp": [bar.timestamp for bar in bars],
                "open": [float(bar.open) for bar in bars],
                "high": [float(bar.high) for bar in bars],
                "low": [float(bar.low) for bar in bars],
                "close": [float(bar.close) for bar in bars],
                "volume": [float(bar.volume) for bar in bars],
                "amount": [float(bar.amount) if bar.amount is not None else np.nan for bar in bars],
            }
        )

    @staticmethod
    def _result_from_frame(
        frame: IndicatorFrameORM,
        *,
        missing: tuple[str, ...],
    ) -> IndicatorComputationResult:
        return IndicatorComputationResult(
            status=frame.status,
            indicator_frame_id=frame.indicator_frame_id,
            asset_id=frame.asset_id,
            symbol=frame.symbol,
            market=frame.market,
            timeframe=frame.timeframe,
            horizon=frame.horizon,
            bar_count=frame.bar_count,
            missing_indicators=missing,
        )


def compute_indicator_values(
    frame: pd.DataFrame,
) -> tuple[dict[str, float | None], tuple[str, ...]]:
    """计算第一版技术指标和序列派生值。"""

    close = frame["close"].to_numpy(dtype=float)
    high = frame["high"].to_numpy(dtype=float)
    low = frame["low"].to_numpy(dtype=float)
    amount = frame["amount"].astype(float)
    close_series = frame["close"].astype(float)
    returns = close_series.pct_change()

    macd, macd_signal, macd_hist = talib.MACD(
        close,
        fastperiod=12,
        slowperiod=26,
        signalperiod=9,
    )
    bb_upper, bb_middle, bb_lower = talib.BBANDS(close, timeperiod=20)
    latest_close = last_valid(close)
    latest_bb_upper = last_valid(bb_upper)
    latest_bb_lower = last_valid(bb_lower)
    bb_numerator = (
        latest_close - latest_bb_lower
        if latest_close is not None and latest_bb_lower is not None
        else None
    )
    bb_denominator = (
        latest_bb_upper - latest_bb_lower
        if latest_bb_upper is not None and latest_bb_lower is not None
        else None
    )
    bb_percent_b = safe_ratio(bb_numerator, bb_denominator)

    ma_20_series = close_series.rolling(20).mean()
    ma_60_series = close_series.rolling(60).mean()
    values = {
        "return_1d": period_return(close_series, 1),
        "return_5d": period_return(close_series, 5),
        "return_20d": period_return(close_series, 20),
        "momentum_20d": period_return(close_series, 20),
        "ma_20": last_valid(ma_20_series.to_numpy(dtype=float)),
        "ma_60": last_valid(ma_60_series.to_numpy(dtype=float)),
        "ma_slope": ma_slope(ma_20_series, periods=5),
        "rsi_14": last_valid(talib.RSI(close, timeperiod=14)),
        "macd": last_valid(macd),
        "macd_signal": last_valid(macd_signal),
        "macd_hist": last_valid(macd_hist),
        "atr_14": last_valid(talib.ATR(high, low, close, timeperiod=14)),
        "bb_upper": latest_bb_upper,
        "bb_middle": last_valid(bb_middle),
        "bb_lower": latest_bb_lower,
        "bb_percent_b": bb_percent_b,
        "volatility_20d": rolling_volatility(returns, window=20),
        "max_drawdown_20d": max_drawdown(close_series, window=20),
        "amount_avg_20d": last_valid(amount.rolling(20).mean().to_numpy(dtype=float)),
        "amount_zscore_20d": zscore_latest(amount, window=20),
    }
    missing = tuple(key for key, value in values.items() if value is None)
    return values, missing


def build_indicator_frame_id(
    *,
    asset_id: str,
    timeframe: str,
    horizon: str,
    input_end_at: datetime,
) -> str:
    """生成稳定指标结果 ID。"""

    normalized_time = input_end_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"ind:{asset_id}:{timeframe}:{horizon}:{normalized_time}"


def period_return(series: pd.Series, periods: int) -> float | None:
    """计算固定窗口收益率。"""

    if len(series) <= periods:
        return None
    base = float(series.iloc[-periods - 1])
    latest = float(series.iloc[-1])
    if base == 0:
        return None
    return latest / base - 1


def rolling_volatility(returns: pd.Series, *, window: int) -> float | None:
    """计算滚动收益波动率。"""

    if len(returns.dropna()) < window:
        return None
    value = float(returns.tail(window).std(ddof=1) * math.sqrt(252))
    return value if math.isfinite(value) else None


def max_drawdown(series: pd.Series, *, window: int) -> float | None:
    """计算窗口最大回撤。"""

    if len(series) < window:
        return None
    tail = series.tail(window)
    drawdown = tail / tail.cummax() - 1
    value = float(drawdown.min())
    return value if math.isfinite(value) else None


def ma_slope(series: pd.Series, *, periods: int) -> float | None:
    """计算均线短窗口斜率。"""

    clean = series.dropna()
    if len(clean) <= periods:
        return None
    previous = float(clean.iloc[-periods - 1])
    latest = float(clean.iloc[-1])
    if previous == 0:
        return None
    return latest / previous - 1


def zscore_latest(series: pd.Series, *, window: int) -> float | None:
    """计算最新值在窗口内的 z-score。"""

    clean = series.dropna()
    if len(clean) < window:
        return None
    tail = clean.tail(window)
    std = float(tail.std(ddof=1))
    if std == 0 or not math.isfinite(std):
        return None
    value = (float(tail.iloc[-1]) - float(tail.mean())) / std
    return value if math.isfinite(value) else None


def safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    """安全除法。"""

    if numerator is None or denominator in {None, 0}:
        return None
    value = numerator / denominator
    return value if math.isfinite(value) else None


def last_valid(values: np.ndarray) -> float | None:
    """返回数组最后一个有效数值。"""

    for value in reversed(values.tolist()):
        if value is None:
            continue
        parsed = float(value)
        if math.isfinite(parsed):
            return parsed
    return None


def to_decimal(value: float | None) -> Decimal | None:
    """把浮点指标转换为数据库 Decimal。"""

    if value is None or not math.isfinite(value):
        return None
    return Decimal(str(value))


def to_json_number(value: float | None) -> float | None:
    """把指标值转换为 JSON 数字。"""

    if value is None or not math.isfinite(value):
        return None
    return float(value)
