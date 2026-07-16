"""探测 A 股 K 线直连源在不同批内并发下的稳定性。

脚本只发起真实 HTTP 请求，不写数据库。默认使用 10 年窗口和一组常见 A 股样本，
从较高并发逐级降到较低并发，输出每档成功率、耗时和错误摘要。
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, timedelta

import pandas as pd

from finance_agent.data.providers.ashare_kline_sources import (
    fetch_eastmoney_kline_direct,
    fetch_tencent_kline_direct,
)


@dataclass(frozen=True)
class ProbeResult:
    """单只股票单次请求结果。"""

    symbol: str
    status: str
    rows: int
    elapsed_seconds: float
    date_range: str
    error: str = ""


@dataclass(frozen=True)
class WorkerSummary:
    """单个并发档位的汇总结果。"""

    source: str
    workers: int
    total: int
    ok: int
    failed: int
    success_rate: float
    elapsed_seconds: float
    avg_request_seconds: float
    max_request_seconds: float
    min_rows: int
    max_rows: int
    errors: list[str]


def main() -> None:
    """解析参数并执行并发探测。"""

    parser = argparse.ArgumentParser(description="探测 A 股 K 线源适合的批内并发数")
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["603507", "603551", "603612", "688025", "600519", "000001", "300750", "601330"],
        help="用于探测的股票代码列表",
    )
    parser.add_argument(
        "--workers",
        nargs="+",
        type=int,
        default=[6, 4, 3, 2, 1],
        help="按顺序测试的并发档位，建议从高到低",
    )
    parser.add_argument("--source", choices=["eastmoney", "tencent"], default="eastmoney")
    parser.add_argument("--start", default=None, help="开始日期 YYYYMMDD，默认约 10 年前")
    parser.add_argument("--end", default=None, help="结束日期 YYYYMMDD，默认今天")
    parser.add_argument("--adjust", default="qfq", choices=["qfq", "hfq", ""])
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--min-success-rate",
        type=float,
        default=1.0,
        help="推荐并发需要达到的最低成功率，默认要求全部成功",
    )
    args = parser.parse_args()

    end_date = _parse_date(args.end, default=date.today())
    start_date = _parse_date(args.start, default=end_date - timedelta(days=3653))
    loader = _build_loader(
        source=args.source,
        start=_format_compact(start_date),
        end=_format_compact(end_date),
        adjust=args.adjust,
        timeout=args.timeout,
    )

    summaries: list[WorkerSummary] = []
    for workers in args.workers:
        summary = run_worker_probe(
            source=args.source,
            symbols=args.symbols,
            workers=workers,
            loader=loader,
        )
        summaries.append(summary)
        print(json.dumps(asdict(summary), ensure_ascii=False))

    recommended = choose_recommended_workers(
        summaries,
        min_success_rate=max(0.0, min(args.min_success_rate, 1.0)),
    )
    print(json.dumps({"recommended_workers": recommended}, ensure_ascii=False))


def run_worker_probe(
    *,
    source: str,
    symbols: list[str],
    workers: int,
    loader: Callable[[str], pd.DataFrame],
) -> WorkerSummary:
    """按指定并发执行一轮真实请求探测。"""

    started = time.perf_counter()
    results: list[ProbeResult] = []
    worker_count = max(1, min(int(workers), len(symbols)))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="kline-probe") as executor:
        future_to_symbol = {executor.submit(run_symbol_probe, symbol, loader): symbol for symbol in symbols}
        for future in as_completed(future_to_symbol):
            results.append(future.result())

    elapsed = round(time.perf_counter() - started, 3)
    ok_results = [result for result in results if result.status == "ok"]
    failed_results = [result for result in results if result.status != "ok"]
    request_seconds = [result.elapsed_seconds for result in results]
    row_counts = [result.rows for result in ok_results]
    return WorkerSummary(
        source=source,
        workers=worker_count,
        total=len(symbols),
        ok=len(ok_results),
        failed=len(failed_results),
        success_rate=round(len(ok_results) / max(len(symbols), 1), 4),
        elapsed_seconds=elapsed,
        avg_request_seconds=round(statistics.mean(request_seconds), 3) if request_seconds else 0.0,
        max_request_seconds=round(max(request_seconds), 3) if request_seconds else 0.0,
        min_rows=min(row_counts) if row_counts else 0,
        max_rows=max(row_counts) if row_counts else 0,
        errors=[f"{result.symbol}: {result.error}" for result in failed_results[:5]],
    )


def run_symbol_probe(symbol: str, loader: Callable[[str], pd.DataFrame]) -> ProbeResult:
    """执行单只股票请求并归一化结果。"""

    started = time.perf_counter()
    try:
        frame = loader(symbol)
        return ProbeResult(
            symbol=symbol,
            status="ok",
            rows=len(frame.index),
            elapsed_seconds=round(time.perf_counter() - started, 3),
            date_range=infer_date_range(frame),
        )
    except Exception as exc:  # noqa: BLE001 - 诊断脚本需要保留完整失败类型
        return ProbeResult(
            symbol=symbol,
            status="error",
            rows=0,
            elapsed_seconds=round(time.perf_counter() - started, 3),
            date_range="",
            error=f"{type(exc).__name__}: {str(exc)[:220]}",
        )


def choose_recommended_workers(
    summaries: list[WorkerSummary],
    *,
    min_success_rate: float,
) -> int:
    """从高到低测试结果中选择第一个满足成功率要求的并发值。"""

    for summary in summaries:
        if summary.success_rate >= min_success_rate:
            return summary.workers
    return summaries[-1].workers if summaries else 1


def _build_loader(
    *,
    source: str,
    start: str,
    end: str,
    adjust: str,
    timeout: float,
) -> Callable[[str], pd.DataFrame]:
    if source == "tencent":
        return lambda symbol: fetch_tencent_kline_direct(
            symbol=symbol,
            start=start,
            end=end,
            adjust=adjust,
            timeout=timeout,
        )
    return lambda symbol: fetch_eastmoney_kline_direct(
        symbol=symbol,
        timeframe="1d",
        start=start,
        end=end,
        adjust=adjust,
        timeout=timeout,
    )


def infer_date_range(frame: pd.DataFrame) -> str:
    """从不同源返回的 DataFrame 中推断日期范围。"""

    for column in ("日期", "date"):
        if column in frame.columns:
            values = pd.to_datetime(frame[column], errors="coerce").dropna()
            if not values.empty:
                return f"{values.min().date()}~{values.max().date()}"
    return ""


def _parse_date(value: str | None, *, default: date) -> date:
    if not value:
        return default
    return date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:8]}")


def _format_compact(value: date) -> str:
    return value.strftime("%Y%m%d")


if __name__ == "__main__":
    main()
