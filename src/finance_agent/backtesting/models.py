"""回测与绩效领域协议。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class BacktestResult:
    """统一回测结果，不暴露 bt 内部对象。"""

    strategy_name: str
    status: str
    start: str | None
    end: str | None
    metrics: JsonDict
    equity_curve: list[JsonDict] = field(default_factory=list)
    drawdown_curve: list[JsonDict] = field(default_factory=list)
    data_versions: JsonDict = field(default_factory=dict)
    strategy_params: JsonDict = field(default_factory=dict)
    signal_version: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        """转换为可落库或 API 返回的 JSON 字典。"""

        return asdict(self)


@dataclass(frozen=True)
class PerformanceReport:
    """统一绩效报告，不暴露 quantstats 内部对象。"""

    strategy_name: str
    status: str
    metrics: JsonDict
    benchmark_name: str | None = None
    win_rate_basis: str = "period_return"
    html_report_path: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        """转换为可落库或 API 返回的 JSON 字典。"""

        return asdict(self)
