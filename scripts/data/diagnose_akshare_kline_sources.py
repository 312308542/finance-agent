"""诊断 AKShare A 股 K 线各数据源的可用性。

用途：
- 对同一批股票分别调用 AKShare 腾讯 K 线、AKShare 东方财富 K 线。
- 可选调用项目内的东方财富 curl_cffi fallback，便于对照当前采集链路。
- 输出每个源的耗时、行数、日期范围、异常类型和异常链路。

示例：
    .venv\\Scripts\\python.exe scripts\\data\\diagnose_akshare_kline_sources.py
    .venv\\Scripts\\python.exe scripts\\data\\diagnose_akshare_kline_sources.py --symbols 603507 600519 000001
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import akshare as ak
import pandas as pd
import requests
from curl_cffi import requests as curl_requests

from finance_agent.data.normalizers import with_ashare_exchange_prefix
from finance_agent.data.providers.akshare_provider import AkshareProvider


JsonDict = dict[str, Any]


@dataclass(frozen=True)
class SourceProbe:
    """单个数据源探针。"""

    key: str
    description: str
    sample_url: str
    loader: Callable[[], pd.DataFrame]


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""

    parser = argparse.ArgumentParser(description="诊断 AKShare A 股 K 线数据源")
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=["603507", "600519", "000001", "688025"],
        help="要诊断的 6 位 A 股代码，默认覆盖沪市、深市和科创板样例。",
    )
    parser.add_argument("--start", default="20240101", help="开始日期，格式 YYYYMMDD。")
    parser.add_argument("--end", default="20251231", help="结束日期，格式 YYYYMMDD。")
    parser.add_argument("--adjust", default="qfq", choices=["", "qfq", "hfq"], help="复权方式。")
    parser.add_argument("--timeout", type=float, default=20.0, help="单次请求超时时间，单位秒。")
    parser.add_argument(
        "--skip-repo-fallback",
        action="store_true",
        help="只诊断 AKShare 两个源，不诊断项目内 curl_cffi fallback。",
    )
    parser.add_argument(
        "--include-transport-probes",
        action="store_true",
        help="额外诊断同 URL 在 requests/curl_cffi/浏览器请求头下的差异。",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=None,
        help="可选：把完整诊断结果写入 JSON 文件。",
    )
    return parser.parse_args()


def main() -> None:
    """执行诊断并输出摘要。"""

    args = parse_args()
    results: list[JsonDict] = []
    started_at = datetime.now().isoformat(timespec="seconds")
    print(f"AKShare K 线源诊断开始 started_at={started_at}")
    print(
        "symbol | source | status | rows | elapsed_s | date_range | error_type | error_message"
    )
    print("-" * 120)
    for symbol in args.symbols:
        clean_symbol = normalize_symbol(symbol)
        for probe in build_source_probes(clean_symbol, args):
            result = run_probe(clean_symbol, probe)
            results.append(result)
            print(format_summary_row(result))
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(
                {
                    "started_at": started_at,
            "parameters": {
                        "symbols": [normalize_symbol(item) for item in args.symbols],
                        "start": args.start,
                        "end": args.end,
                        "adjust": args.adjust,
                        "timeout": args.timeout,
                        "skip_repo_fallback": args.skip_repo_fallback,
                        "include_transport_probes": args.include_transport_probes,
                    },
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n完整诊断结果已写入：{args.json_output}")


def build_source_probes(symbol: str, args: argparse.Namespace) -> list[SourceProbe]:
    """为单只股票构造各数据源探针。"""

    prefixed_symbol = with_ashare_exchange_prefix(symbol)
    period = "daily"
    probes = [
        SourceProbe(
            key="akshare:stock_zh_a_hist_tx",
            description="AKShare 腾讯证券历史 K 线",
            sample_url=build_tencent_sample_url(prefixed_symbol, args),
            loader=lambda: ak.stock_zh_a_hist_tx(
                symbol=prefixed_symbol,
                start_date=args.start,
                end_date=args.end,
                adjust=args.adjust,
                timeout=args.timeout,
            ),
        ),
        SourceProbe(
            key="akshare:stock_zh_a_hist",
            description="AKShare 东方财富历史 K 线",
            sample_url=build_eastmoney_sample_url(symbol, args),
            loader=lambda: ak.stock_zh_a_hist(
                symbol=symbol,
                period=period,
                start_date=args.start,
                end_date=args.end,
                adjust=args.adjust,
                timeout=args.timeout,
            ),
        ),
    ]
    if not args.skip_repo_fallback:
        provider = AkshareProvider(request_timeout_seconds=args.timeout)
        probes.append(
            SourceProbe(
                key="repo:eastmoney_curl_cffi",
                description="项目内东方财富 curl_cffi fallback",
                sample_url=build_eastmoney_sample_url(symbol, args),
                loader=lambda: provider._fetch_ohlcv_eastmoney_curl_cffi(
                    symbol=symbol,
                    timeframe="1d",
                    start=args.start,
                    end=args.end,
                    adjust=args.adjust,
                ),
            )
        )
    if args.include_transport_probes:
        probes.extend(build_transport_probes(symbol, args))
    return probes


def build_transport_probes(symbol: str, args: argparse.Namespace) -> list[SourceProbe]:
    """构造同 URL 不同传输层的对照探针。"""

    prefixed_symbol = with_ashare_exchange_prefix(symbol)
    year = int(args.start[:4])
    url = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
    params = {
        "_var": f"kline_day{args.adjust}{year}",
        "param": f"{prefixed_symbol},day,{year}-01-01,{year + 1}-12-31,640,{args.adjust}",
        "r": "0.8205512681390605",
    }
    sample_url = build_tencent_sample_url(prefixed_symbol, args)
    return [
        SourceProbe(
            key="transport:tencent_requests_plain",
            description="腾讯同 URL：Python requests 默认请求头",
            sample_url=sample_url,
            loader=lambda: response_to_probe_frame(requests.get(url, params=params, timeout=args.timeout)),
        ),
        SourceProbe(
            key="transport:tencent_requests_browser_headers",
            description="腾讯同 URL：Python requests + 浏览器请求头",
            sample_url=sample_url,
            loader=lambda: response_to_probe_frame(
                requests.get(
                    url,
                    params=params,
                    timeout=args.timeout,
                    headers=tencent_browser_headers(),
                )
            ),
        ),
        SourceProbe(
            key="transport:tencent_curl_cffi_browser_headers",
            description="腾讯同 URL：curl_cffi + chrome impersonate + 浏览器请求头",
            sample_url=sample_url,
            loader=lambda: response_to_probe_frame(
                curl_requests.get(
                    url,
                    params=params,
                    timeout=args.timeout,
                    impersonate="chrome120",
                    headers=tencent_browser_headers(),
                )
            ),
        ),
    ]


def response_to_probe_frame(response: Any) -> pd.DataFrame:
    """把 HTTP 响应包装成 DataFrame，复用统一摘要输出。"""

    response.raise_for_status()
    return pd.DataFrame(
        [
            {
                "date": None,
                "status_code": getattr(response, "status_code", None),
                "body_size": len(getattr(response, "text", "") or ""),
            }
        ]
    )


def tencent_browser_headers() -> dict[str, str]:
    """生成腾讯 K 线接口的浏览器态请求头，不包含 Cookie。"""

    return {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
            "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
        "Cache-Control": "max-age=0",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36 Edg/148.0.0.0"
        ),
        "sec-ch-ua": '"Chromium";v="148", "Microsoft Edge";v="148", "Not/A)Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }


def run_probe(symbol: str, probe: SourceProbe) -> JsonDict:
    """执行单个数据源探针。"""

    started = time.perf_counter()
    try:
        frame = probe.loader()
        elapsed = time.perf_counter() - started
        return {
            "symbol": symbol,
            "source": probe.key,
            "description": probe.description,
            "status": "ok",
            "elapsed_seconds": round(elapsed, 3),
            "row_count": int(len(frame.index)),
            "date_range": infer_date_range(frame),
            "columns": [str(column) for column in frame.columns],
            "sample_url": probe.sample_url,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - 诊断脚本需要保留所有异常类型
        elapsed = time.perf_counter() - started
        return {
            "symbol": symbol,
            "source": probe.key,
            "description": probe.description,
            "status": "error",
            "elapsed_seconds": round(elapsed, 3),
            "row_count": 0,
            "date_range": None,
            "columns": [],
            "sample_url": probe.sample_url,
            "error": summarize_exception(exc),
        }


def infer_date_range(frame: pd.DataFrame) -> JsonDict | None:
    """从不同 AKShare 字段命名中推断日期范围。"""

    if frame.empty:
        return None
    for column in ["date", "日期"]:
        if column in frame.columns:
            values = pd.to_datetime(frame[column], errors="coerce").dropna()
            if values.empty:
                return None
            return {
                "start": values.min().date().isoformat(),
                "end": values.max().date().isoformat(),
            }
    return None


def summarize_exception(exc: Exception) -> JsonDict:
    """提取异常类型、消息和 cause/context 链路。"""

    chain: list[JsonDict] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(
            {
                "type": f"{type(current).__module__}.{type(current).__name__}",
                "message": str(current),
            }
        )
        current = current.__cause__ or current.__context__
    return {
        "type": f"{type(exc).__module__}.{type(exc).__name__}",
        "message": str(exc),
        "chain": chain,
    }


def format_summary_row(result: JsonDict) -> str:
    """格式化摘要表的一行。"""

    error = result.get("error") or {}
    date_range = result.get("date_range") or {}
    date_text = (
        f"{date_range.get('start')}~{date_range.get('end')}"
        if date_range
        else "-"
    )
    message = str(error.get("message") or "").replace("\n", " ")[:140]
    return (
        f"{result['symbol']} | {result['source']} | {result['status']} | "
        f"{result['row_count']} | {result['elapsed_seconds']} | {date_text} | "
        f"{error.get('type', '-')} | {message}"
    )


def normalize_symbol(symbol: str) -> str:
    """规范化为 6 位 A 股代码。"""

    clean = "".join(ch for ch in str(symbol).strip() if ch.isdigit())
    return clean.zfill(6)[-6:]


def build_tencent_sample_url(prefixed_symbol: str, args: argparse.Namespace) -> str:
    """生成腾讯样例 URL，便于在浏览器中复查。"""

    year = int(args.start[:4])
    return (
        "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
        f"?_var=kline_day{args.adjust}{year}"
        f"&param={prefixed_symbol},day,{year}-01-01,{year + 1}-12-31,640,{args.adjust}"
    )


def build_eastmoney_sample_url(symbol: str, args: argparse.Namespace) -> str:
    """生成东方财富样例 URL，便于在浏览器中复查。"""

    market_code = 1 if symbol.startswith(("6", "9")) else 0
    adjust_dict = {"qfq": "1", "hfq": "2", "": "0"}
    return (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={market_code}.{symbol}"
        "&fields1=f1,f2,f3,f4,f5,f6"
        "&fields2=f51,f52,f53,f54,f55,f56,f57"
        f"&klt=101&fqt={adjust_dict[args.adjust]}"
        f"&beg={args.start}&end={args.end}"
    )


if __name__ == "__main__":
    main()
