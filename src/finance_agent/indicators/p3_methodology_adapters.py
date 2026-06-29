"""P3 方法论确定性适配器与依赖占位。

P3 中一部分方法论依赖更成熟或更主观的结构引擎。当前模块只上线可用 pandas
稳定计算的季节性画像；谐波等外部引擎在未批准依赖前明确拒绝运行。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import pandas as pd

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class PricePoint:
    """单个收盘价点。"""

    timestamp: datetime
    close: float


@dataclass(frozen=True)
class SeasonalResult:
    """季节性收益画像。"""

    status: str
    asset_id: str
    symbol: str
    market: str
    timeframe: str
    input_start_at: datetime
    input_end_at: datetime
    observation_count: int
    monthly_profile: JsonDict
    best_month: int | None
    worst_month: int | None
    evidence_id: str

    def to_indicator_payload(self) -> JsonDict:
        """转换为可持久化 payload。"""

        return {
            "schema_version": "seasonal_v1",
            "status": self.status,
            "asset_id": self.asset_id,
            "symbol": self.symbol,
            "market": self.market,
            "timeframe": self.timeframe,
            "input_start_at": self.input_start_at.isoformat(),
            "input_end_at": self.input_end_at.isoformat(),
            "observation_count": self.observation_count,
            "monthly_profile": self.monthly_profile,
            "best_month": self.best_month,
            "worst_month": self.worst_month,
            "evidence_id": self.evidence_id,
            "red_lines": [
                "季节性画像由确定性适配器计算，LLM 只能解读。",
                "季节性只能作为弱辅助，不能覆盖系统评分或风险标记。",
            ],
        }


class HarmonicEngineUnavailable(RuntimeError):
    """谐波形态确定性引擎不可用。"""


class HarmonicEngine(Protocol):
    """谐波形态外部引擎协议。"""

    engine_name: str
    engine_version: str

    def compute(self, prices: list[PricePoint]) -> JsonDict:
        """计算谐波形态。"""


class SeasonalAdapter:
    """季节性收益画像适配器。"""

    def compute(
        self,
        *,
        asset_id: str,
        symbol: str,
        market: str,
        timeframe: str,
        prices: list[PricePoint],
        min_observations: int = 24,
    ) -> SeasonalResult:
        """按月份聚合收益率，输出季节性画像。"""

        if len(prices) < min_observations:
            raise ValueError(f"季节性画像至少需要 {min_observations} 个价格点。")
        frame = prices_to_frame(prices)
        returns = frame["close"].pct_change().dropna()
        if returns.empty:
            raise ValueError("季节性画像缺少有效收益率。")

        profile: JsonDict = {}
        grouped = returns.groupby(returns.index.month)
        for month, values in grouped:
            clean = values.dropna()
            if clean.empty:
                continue
            positive_count = int((clean > 0).sum())
            profile[int(month)] = {
                "average_return": round(float(clean.mean()), 6),
                "sample_count": int(len(clean)),
                "positive_rate": round(positive_count / len(clean), 6),
            }
        best_month = pick_month(profile, highest=True)
        worst_month = pick_month(profile, highest=False)
        input_start_at = normalize_datetime(prices[0].timestamp)
        input_end_at = normalize_datetime(prices[-1].timestamp)
        return SeasonalResult(
            status="available",
            asset_id=asset_id,
            symbol=symbol,
            market=market,
            timeframe=timeframe,
            input_start_at=input_start_at,
            input_end_at=input_end_at,
            observation_count=len(returns),
            monthly_profile=profile,
            best_month=best_month,
            worst_month=worst_month,
            evidence_id=build_evidence_id("seasonal", asset_id, timeframe, input_end_at),
        )


class HarmonicAdapter:
    """谐波形态适配器占位。"""

    def __init__(self, engine: HarmonicEngine | None = None) -> None:
        self.engine = engine

    def compute(
        self,
        *,
        asset_id: str,
        symbol: str,
        market: str,
        timeframe: str,
        prices: list[PricePoint],
    ) -> JsonDict:
        """调用外部谐波引擎；未配置时明确拒绝。"""

        if self.engine is None:
            raise HarmonicEngineUnavailable("pyharmonics 谐波形态确定性引擎尚未配置，不能上线谐波计算型方法论。")
        if len(prices) < 5:
            raise ValueError("谐波形态至少需要 5 个价格点。")
        input_end_at = normalize_datetime(prices[-1].timestamp)
        raw = self.engine.compute(prices)
        return {
            "schema_version": "harmonic_v1",
            "status": "available",
            "asset_id": asset_id,
            "symbol": symbol,
            "market": market,
            "timeframe": timeframe,
            "engine": getattr(self.engine, "engine_name", self.engine.__class__.__name__),
            "engine_version": getattr(self.engine, "engine_version", "unknown"),
            "input_end_at": input_end_at.isoformat(),
            "patterns": list(raw.get("patterns") or []),
            "evidence_id": build_evidence_id("harmonic", asset_id, timeframe, input_end_at),
            "red_lines": [
                "谐波形态由确定性引擎计算，LLM 只能解读。",
                "无引擎输出时不得声称存在 XABCD 形态。",
            ],
        }


def prices_to_frame(prices: list[PricePoint]) -> pd.DataFrame:
    """把价格点转换为按时间排序的 DataFrame。"""

    frame = pd.DataFrame(
        {
            "timestamp": [normalize_datetime(point.timestamp) for point in prices],
            "close": [float(point.close) for point in prices],
        }
    )
    frame = frame.sort_values("timestamp").set_index("timestamp")
    if frame["close"].isna().any():
        raise ValueError("价格序列包含空值。")
    return frame


def pick_month(profile: JsonDict, *, highest: bool) -> int | None:
    """从月份画像中选择均值最高或最低的月份。"""

    if not profile:
        return None
    items = [
        (int(month), float(values["average_return"]))
        for month, values in profile.items()
        if math.isfinite(float(values["average_return"]))
    ]
    if not items:
        return None
    return max(items, key=lambda item: item[1])[0] if highest else min(items, key=lambda item: item[1])[0]


def build_evidence_id(prefix: str, asset_id: str, timeframe: str, input_end_at: datetime) -> str:
    """生成方法论证据 ID。"""

    normalized = input_end_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}:{asset_id}:{timeframe}:{normalized}"


def normalize_datetime(value: datetime) -> datetime:
    """统一时间为 UTC aware datetime。"""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
