"""AKShare 能力注册表健康检查。

默认只检查 MVP/P0-P1 接口，避免一次性打爆免费数据源。脚本用于回答：

- 当前 AKShare 安装版本里接口是否存在。
- 核心接口在当前网络环境下是否可调用。
- 每个接口属于哪个推荐数据域、落哪些表。
"""

from __future__ import annotations

import argparse
import inspect
import json
from collections.abc import Callable
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

import akshare as ak

from finance_agent.data.akshare_capabilities import AkshareCapability, iter_capabilities
from finance_agent.data.providers import AkshareProvider

JsonDict = dict[str, Any]


def main() -> None:
    """执行 AKShare 能力健康检查。"""

    parser = argparse.ArgumentParser(description="检查 AKShare 能力注册表和核心接口可用性")
    parser.add_argument("--all", action="store_true", help="检查注册表里的所有接口")
    parser.add_argument(
        "--priority",
        choices=["P0", "P1", "P2", "P3"],
        help="只检查指定优先级接口",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="实际调用接口；默认只检查接口是否存在",
    )
    parser.add_argument(
        "--include-risky",
        action="store_true",
        help="允许调用可能较慢或参数不稳定的接口",
    )
    args = parser.parse_args()

    if args.all:
        capabilities = iter_capabilities(priority=args.priority)
    else:
        capabilities = tuple(
            item
            for item in iter_capabilities(priority=args.priority)
            if item.enabled_in_mvp or item.priority in {"P0", "P1"}
        )

    results = [
        check_capability(item, execute=args.execute, include_risky=args.include_risky)
        for item in capabilities
    ]
    summary = {
        "checked_at": datetime.now(tz=UTC).isoformat(),
        "akshare_version": getattr(ak, "__version__", "unknown"),
        "execute": args.execute,
        "total": len(results),
        "available": sum(1 for item in results if item["status"] == "available"),
        "missing": sum(1 for item in results if item["status"] == "missing"),
        "error": sum(1 for item in results if item["status"] == "error"),
        "skipped": sum(1 for item in results if item["status"] == "skipped"),
        "results": results,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


def check_capability(
    capability: AkshareCapability,
    *,
    execute: bool,
    include_risky: bool,
) -> JsonDict:
    """检查单个 AKShare 能力。"""

    started = perf_counter()
    function = resolve_akshare_callable(capability.name)
    result: JsonDict = {
        "name": capability.name,
        "provider_class": capability.provider_class,
        "data_domain": capability.data_domain,
        "priority": capability.priority,
        "enabled_in_mvp": capability.enabled_in_mvp,
        "storage_targets": list(capability.storage_targets),
        "factor_groups": list(capability.factor_groups),
        "fallback_names": list(capability.fallback_names),
        "status": "available" if function else "missing",
        "latency_ms": None,
        "row_count": None,
        "columns": [],
        "error_message": None,
    }
    if function is None:
        result["latency_ms"] = elapsed_ms(started)
        return result
    if not execute:
        result["latency_ms"] = elapsed_ms(started)
        return result
    if not should_execute(capability, include_risky=include_risky):
        result["status"] = "skipped"
        result["error_message"] = "该接口参数或耗时不适合默认健康检查"
        result["latency_ms"] = elapsed_ms(started)
        return result

    try:
        payload = call_sample(capability, function)
        result["row_count"] = len(payload) if hasattr(payload, "__len__") else None
        result["columns"] = list(getattr(payload, "columns", []))[:20]
        result["status"] = "available"
    except Exception as exc:
        result["status"] = "error"
        result["error_message"] = str(exc)
    result["latency_ms"] = elapsed_ms(started)
    return result


def resolve_akshare_callable(name: str) -> Callable[..., Any] | None:
    """解析 AKShare 函数，兼容腾讯实时行情的子模块导出。"""

    if hasattr(ak, name):
        return getattr(ak, name)
    if name == "stock_zh_a_spot_tx":
        from akshare.stock.stock_zh_a_tx import stock_zh_a_spot_tx

        return stock_zh_a_spot_tx
    if name == "eastmoney_kline_curl_cffi":
        provider = AkshareProvider()
        return provider._fetch_ohlcv_eastmoney_curl_cffi
    return None


def should_execute(capability: AkshareCapability, *, include_risky: bool) -> bool:
    """判断是否适合默认执行。"""

    if include_risky:
        return True
    # 当前网络下，部分东方财富接口可能长时间等待后才被上游断开。
    # 默认健康检查只执行较稳定的轻量接口；需要压测主源时显式加
    # `--include-risky`。
    return capability.name in {
        "stock_zh_a_spot_tx",
        "stock_zh_a_hist_tx",
        "stock_news_em",
    }


def call_sample(capability: AkshareCapability, function: Callable[..., Any]) -> Any:
    """用注册表样例参数调用接口。"""

    params = dict(capability.sample_params)
    if capability.name == "eastmoney_kline_curl_cffi":
        return function(
            symbol=params.get("symbol", "000001"),
            timeframe="1d",
            start=params.get("start_date", "20260501"),
            end=params.get("end_date", "20260514"),
            adjust=params.get("adjust", "qfq"),
        )
    if not params:
        signature = inspect.signature(function)
        required = [
            name
            for name, parameter in signature.parameters.items()
            if parameter.default is inspect._empty
            and parameter.kind in {parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY}
        ]
        if required:
            raise ValueError(f"缺少样例参数: {required}")
    return function(**params)


def elapsed_ms(started: float) -> int:
    """计算耗时毫秒。"""

    return int((perf_counter() - started) * 1000)


if __name__ == "__main__":
    main()
