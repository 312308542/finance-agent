"""缠论确定性引擎适配器。

本模块只定义 finance-agent 侧稳定接口。真实 czsc 依赖待总负责人批准后接入；
在此之前通过注入 engine 的方式做 fixture/fake 验证，禁止让 LLM 自己计算笔、
中枢或买卖点。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

JsonDict = dict[str, Any]


class ChanlunEngineUnavailable(RuntimeError):
    """缠论确定性引擎不可用。"""


@dataclass(frozen=True)
class ChanlunBar:
    """缠论引擎输入 K 线。"""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class ChanlunEngine(Protocol):
    """外部缠论引擎协议。"""

    engine_name: str
    engine_version: str

    def compute(self, bars: list[ChanlunBar]) -> JsonDict:
        """计算分型、笔、中枢和买卖点。"""


@dataclass(frozen=True)
class ChanlunComputationResult:
    """缠论结构化结果。"""

    status: str
    asset_id: str
    symbol: str
    market: str
    timeframe: str
    engine: str
    engine_version: str
    input_start_at: datetime
    input_end_at: datetime
    bar_count: int
    patterns: JsonDict
    signals: tuple[JsonDict, ...]
    evidence_id: str

    def to_indicator_payload(self) -> JsonDict:
        """转换为 indicator payload 可持久化结构。"""

        return {
            "schema_version": "chanlun_v1",
            "status": self.status,
            "asset_id": self.asset_id,
            "symbol": self.symbol,
            "market": self.market,
            "timeframe": self.timeframe,
            "engine": self.engine,
            "engine_version": self.engine_version,
            "input_start_at": self.input_start_at.isoformat(),
            "input_end_at": self.input_end_at.isoformat(),
            "bar_count": self.bar_count,
            "patterns": self.patterns,
            "signals": list(self.signals),
            "evidence_id": self.evidence_id,
            "red_lines": [
                "缠论结构由确定性引擎计算，LLM 只能解读。",
                "无确定性引擎输出时不得声称存在分型、笔、中枢或买卖点。",
            ],
        }


class ChanlunAdapter:
    """finance-agent 侧缠论适配器。"""

    def __init__(self, engine: ChanlunEngine | None = None) -> None:
        self.engine = engine

    def compute(
        self,
        *,
        asset_id: str,
        symbol: str,
        market: str,
        timeframe: str,
        bars: list[ChanlunBar],
    ) -> ChanlunComputationResult:
        """用确定性引擎计算缠论结构。"""

        if self.engine is None:
            raise ChanlunEngineUnavailable("czsc 缠论确定性引擎尚未配置，不能上线缠论计算型方法论。")
        if not bars:
            raise ValueError("缠论计算至少需要一根 K 线。")

        raw = self.engine.compute(bars)
        input_start_at = normalize_datetime(bars[0].timestamp)
        input_end_at = normalize_datetime(bars[-1].timestamp)
        return ChanlunComputationResult(
            status="available",
            asset_id=asset_id,
            symbol=symbol,
            market=market,
            timeframe=timeframe,
            engine=getattr(self.engine, "engine_name", self.engine.__class__.__name__),
            engine_version=getattr(self.engine, "engine_version", "unknown"),
            input_start_at=input_start_at,
            input_end_at=input_end_at,
            bar_count=len(bars),
            patterns={
                "fractals": list(raw.get("fractals") or []),
                "strokes": list(raw.get("strokes") or []),
                "centers": list(raw.get("centers") or []),
            },
            signals=tuple(raw.get("signals") or []),
            evidence_id=build_chanlun_evidence_id(
                asset_id=asset_id,
                timeframe=timeframe,
                input_end_at=input_end_at,
            ),
        )


def build_chanlun_evidence_id(*, asset_id: str, timeframe: str, input_end_at: datetime) -> str:
    """生成缠论引擎输出证据 ID。"""

    normalized = input_end_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"chanlun:{asset_id}:{timeframe}:{normalized}"


def normalize_datetime(value: datetime) -> datetime:
    """统一时间为 UTC aware datetime。"""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
