"""对比 A 股 K 线数据源的稳定性和响应时间。

默认对腾讯 Direct、东方财富 Direct、AKShare 东方财富三条链路做小样本比较。
输出用于调整 Provider 源优先级，不直接写库。
"""

from __future__ import annotations

import argparse
import time
from typing import Callable

import akshare as ak
import pandas as pd

from finance_agent.data.providers.akshare_provider import AkshareProvider
from finance_agent.data.providers.ashare_kline_sources import (
    fetch_eastmoney_kline_direct,
    fetch_tencent_kline_direct,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="对比 A 股 K 线源响应时间和稳定性")
    parser.add_argument("--symbols", nargs="+", default=["603507", "600519", "000001"])
    parser.add_argument("--start", default="20240101")
    parser.add_argument("--end", default="20251231")
    parser.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args()

    provider = AkshareProvider(request_timeout_seconds=args.timeout)
    probes = [
        (
            "tencent:direct:kline",
            lambda symbol: fetch_tencent_kline_direct(
                symbol=symbol,
                start=args.start,
                end=args.end,
                adjust="qfq",
                timeout=args.timeout,
            ),
        ),
        (
            "eastmoney:direct:kline",
            lambda symbol: fetch_eastmoney_kline_direct(
                symbol=symbol,
                timeframe="1d",
                start=args.start,
                end=args.end,
                adjust="qfq",
                timeout=args.timeout,
            ),
        ),
        (
            "akshare:stock_zh_a_hist",
            lambda symbol: ak.stock_zh_a_hist(
                symbol=symbol,
                period=provider._to_ak_period("1d"),
                start_date=args.start,
                end_date=args.end,
                adjust="qfq",
                timeout=args.timeout,
            ),
        ),
    ]

    print("symbol | source | status | rows | elapsed_s | range | error")
    print("-" * 110)
    for symbol in args.symbols:
        for source, loader in probes:
            result = run_probe(symbol=symbol, source=source, loader=loader)
            print(
                f"{result['symbol']} | {result['source']} | {result['status']} | "
                f"{result['rows']} | {result['elapsed_s']} | {result['range']} | {result['error']}"
            )


def run_probe(
    *,
    symbol: str,
    source: str,
    loader: Callable[[str], pd.DataFrame],
) -> dict[str, object]:
    started = time.perf_counter()
    try:
        frame = loader(symbol)
        return {
            "symbol": symbol,
            "source": source,
            "status": "ok",
            "rows": len(frame.index),
            "elapsed_s": round(time.perf_counter() - started, 3),
            "range": infer_range(frame),
            "error": "",
        }
    except Exception as exc:  # noqa: BLE001 - 诊断脚本需要保留所有异常
        return {
            "symbol": symbol,
            "source": source,
            "status": "error",
            "rows": 0,
            "elapsed_s": round(time.perf_counter() - started, 3),
            "range": "",
            "error": f"{type(exc).__name__}: {str(exc)[:140]}",
        }


def infer_range(frame: pd.DataFrame) -> str:
    if frame.empty:
        return ""
    for column in ["date", "日期"]:
        if column in frame.columns:
            values = pd.to_datetime(frame[column], errors="coerce").dropna()
            if values.empty:
                return ""
            return f"{values.min().date()}~{values.max().date()}"
    return ""


if __name__ == "__main__":
    main()
