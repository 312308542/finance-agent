"""AKShare A 股数据 Provider。"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import akshare as ak
import pandas as pd
from curl_cffi import requests as curl_requests
from pandas import DataFrame

from finance_agent.data.models import AssetListResult, MarketBarsResult
from finance_agent.data.normalizers import (
    normalize_ashare_code_name,
    normalize_ashare_hist,
    normalize_ashare_hist_tx,
    normalize_ashare_spot,
    normalize_ashare_spot_tx,
)
from finance_agent.data.providers.ashare_kline_sources import (
    fetch_eastmoney_kline_direct,
    fetch_tencent_kline_direct,
)
from finance_agent.data.providers.eastmoney_curl import eastmoney_headers

KlineSourceGate = Callable[[str, Callable[[], DataFrame]], DataFrame]


class AkshareProvider:
    """A 股数据 Provider。"""

    provider_name = "akshare"

    def __init__(self, *, request_timeout_seconds: float = 15.0) -> None:
        """设置 AKShare 单次 HTTP 请求超时。"""

        self.request_timeout_seconds = request_timeout_seconds

    def fetch_assets(self, *, limit: int | None = None) -> AssetListResult:
        """获取 A 股可交易资产列表。"""

        collected_at = datetime.now(tz=UTC)
        fallback_trace: list[dict[str, str]] = []
        try:
            df = self._fetch_assets_eastmoney()
            assets = normalize_ashare_spot(df, limit=limit)
            actual_source = df.attrs.get("actual_source", "akshare:stock_zh_a_spot_em")
        except Exception as exc:
            fallback_trace.append(
                {"source": "akshare:stock_zh_a_spot_em", "error_message": str(exc)}
            )
            try:
                df = self._fetch_assets_code_name()
                assets = normalize_ashare_code_name(df, limit=limit)
                fallback_trace.extend(df.attrs.get("source_errors", []))
                actual_source = "akshare:stock_info_a_code_name"
            except Exception as code_name_exc:
                fallback_trace.append(
                    {
                        "source": "akshare:stock_info_a_code_name",
                        "error_message": str(code_name_exc),
                    }
                )
                try:
                    df = self._fetch_assets_tencent()
                    assets = normalize_ashare_spot_tx(df, limit=limit)
                    actual_source = "akshare:stock_zh_a_spot_tx"
                except Exception as fallback_exc:
                    fallback_trace.append(
                        {
                            "source": "akshare:stock_zh_a_spot_tx",
                            "error_message": str(fallback_exc),
                        }
                    )
                    return AssetListResult(
                        provider_name=self.provider_name,
                        status="error",
                        collected_at=collected_at,
                        error_message=str(fallback_exc),
                        payload={
                            "primary_source": "akshare:stock_zh_a_spot_em",
                            "fallback_trace": fallback_trace,
                        },
                    )
        return AssetListResult(
            provider_name=self.provider_name,
            status="available" if assets else "unavailable",
            collected_at=collected_at,
            assets=assets,
            payload={
                "row_count": len(assets),
                "primary_source": "akshare:stock_zh_a_spot_em",
                "actual_source": actual_source,
                "fallback_used": actual_source != "akshare:stock_zh_a_spot_em",
                "fallback_trace": fallback_trace,
            },
        )

    def fetch_ohlcv(
        self,
        *,
        symbol: str,
        timeframe: str,
        start: str | None = None,
        end: str | None = None,
        limit: int | None = None,
        adjust: str = "qfq",
        is_closed: bool = True,
        status: str = "available",
        source_gate: KlineSourceGate | None = None,
    ) -> MarketBarsResult:
        """获取 A 股历史 K 线。"""

        collected_at = datetime.now(tz=UTC)
        primary_source = "eastmoney:direct:kline"
        fallback_trace: list[dict[str, str]] = []
        source_attempts: list[dict[str, Any]] = []
        bars = []
        actual_source = primary_source
        last_error_message: str | None = None
        source_specs = [
            {
                "source": primary_source,
                "rate_key": "eastmoney_kline",
                "complexity": "direct-cookie",
                "loader": lambda: self._fetch_ohlcv_eastmoney_curl_cffi(
                    symbol=symbol,
                    timeframe=timeframe,
                    start=start,
                    end=end,
                    adjust=adjust,
                ),
            },
            {
                "source": "tencent:direct:kline",
                "rate_key": "tencent_kline",
                "complexity": "direct-windowed",
                "loader": lambda: self._fetch_ohlcv_tencent(symbol=symbol, start=start, end=end, adjust=adjust),
            },
            {
                "source": "akshare:stock_zh_a_hist",
                "rate_key": "stock_zh_a_hist",
                "complexity": "akshare-wrapper",
                "loader": lambda: self._fetch_ohlcv_eastmoney(
                    symbol=symbol,
                    timeframe=timeframe,
                    start=start,
                    end=end,
                    adjust=adjust,
                ),
            },
        ]

        for spec in source_specs:
            source = str(spec["source"])
            rate_key = str(spec["rate_key"])
            complexity = str(spec["complexity"])
            loader = spec["loader"]
            started = time.perf_counter()
            try:
                df = source_gate(rate_key, loader) if source_gate else loader()
                row_count = len(df.index)
                source_attempts.append(
                    self._source_attempt(
                        source=source,
                        rate_key=rate_key,
                        status="ok" if row_count else "empty",
                        started=started,
                        row_count=row_count,
                        complexity=complexity,
                    )
                )
                if row_count == 0:
                    fallback_trace.append(
                        {"source": source, "error_message": f"{source} returned 0 rows"}
                    )
                    actual_source = source
                    continue
                if limit:
                    df = df.tail(limit)
                if source == "tencent:direct:kline":
                    bars = normalize_ashare_hist_tx(
                        df,
                        symbol=symbol,
                        timeframe=timeframe,
                        source=source,
                        adjustment=adjust,
                        is_closed=is_closed,
                        status=status,
                    )
                else:
                    bars = normalize_ashare_hist(
                        df,
                        symbol=symbol,
                        timeframe=timeframe,
                        source=source,
                        adjustment=adjust,
                        is_closed=is_closed,
                        status=status,
                    )
                actual_source = source
                break
            except Exception as exc:
                last_error_message = str(exc)
                source_attempts.append(
                    self._source_attempt(
                        source=source,
                        rate_key=rate_key,
                        status="error",
                        started=started,
                        error_message=str(exc),
                        complexity=complexity,
                    )
                )
                fallback_trace.append({"source": source, "error_message": str(exc)})

        has_empty_attempt = any(item.get("status") == "empty" for item in source_attempts)
        if (
            not bars
            and source_attempts
            and source_attempts[-1]["status"] == "error"
            and not has_empty_attempt
        ):
            return MarketBarsResult(
                provider_name=self.provider_name,
                status="error",
                collected_at=collected_at,
                error_message=last_error_message,
                payload={
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "adjust": adjust,
                    "primary_source": primary_source,
                    "fallback_trace": fallback_trace,
                    "source_attempts": source_attempts,
                },
            )
        return MarketBarsResult(
            provider_name=self.provider_name,
            status="available" if bars else "unavailable",
            collected_at=collected_at,
            bars=bars,
            payload={
                "symbol": symbol,
                "timeframe": timeframe,
                "adjust": adjust,
                "primary_source": primary_source,
                "actual_source": actual_source,
                "fallback_used": actual_source != primary_source,
                "fallback_trace": fallback_trace,
                "source_attempts": source_attempts,
            },
        )

    @staticmethod
    def _source_attempt(
        *,
        source: str,
        rate_key: str,
        status: str,
        started: float,
        row_count: int = 0,
        error_message: str | None = None,
        complexity: str,
    ) -> dict[str, Any]:
        """记录单个 K 线源的尝试结果，用于后续稳定性和响应时间对比。"""

        attempt: dict[str, Any] = {
            "source": source,
            "rate_key": rate_key,
            "status": status,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "row_count": row_count,
            "complexity": complexity,
        }
        if error_message:
            attempt["error_message"] = error_message
        return attempt

    @staticmethod
    def _to_ak_period(timeframe: str) -> str:
        """转换为 AKShare 支持的 period。"""

        mapping: dict[str, str] = {
            "1d": "daily",
            "1w": "weekly",
            "1M": "monthly",
        }
        if timeframe not in mapping:
            raise ValueError(f"AKShare A 股历史行情暂不支持周期: {timeframe}")
        return mapping[timeframe]

    def _fetch_assets_eastmoney(self) -> DataFrame:
        """从东方财富接口获取 A 股实时行情。"""

        try:
            return ak.stock_zh_a_spot_em()
        except Exception:
            return self._fetch_assets_eastmoney_curl_cffi()

    def _fetch_assets_eastmoney_curl_cffi(self) -> DataFrame:
        """使用 curl_cffi 直连东方财富全 A 列表接口。"""

        urls = [
            "https://push2.eastmoney.com/api/qt/clist/get",
            "https://82.push2.eastmoney.com/api/qt/clist/get",
            "https://20.push2.eastmoney.com/api/qt/clist/get",
            "https://29.push2.eastmoney.com/api/qt/clist/get",
            "https://push2his.eastmoney.com/api/qt/clist/get",
            "http://push2delay.eastmoney.com/api/qt/clist/get",
        ]
        page_size = 200
        max_pages = 80
        rows: list[dict[str, Any]] = []
        total: int | None = None
        preferred_url: str | None = None
        base_params = {
            "po": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fid": "f3",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f12,f14,f2,f3,f9,f23",
            "pz": str(page_size),
        }
        for page in range(1, max_pages + 1):
            last_error: Exception | None = None
            data: dict[str, Any] = {}
            page_rows: list[dict[str, Any]] = []
            page_urls = (
                [preferred_url, *(url for url in urls if url != preferred_url)]
                if preferred_url is not None
                else urls
            )
            host_succeeded = False
            for url in page_urls:
                max_attempts = 3 if url == preferred_url or "push2delay" in url else 1
                for _attempt in range(max_attempts):
                    try:
                        response = curl_requests.get(
                            url,
                            params=base_params | {"pn": str(page)},
                            timeout=self.request_timeout_seconds,
                            impersonate="chrome120",
                            headers=eastmoney_headers(),
                        )
                        response.raise_for_status()
                        candidate_data = response.json().get("data") or {}
                        candidate_rows = candidate_data.get("diff") or []
                    except Exception as exc:
                        last_error = exc
                        continue
                    if candidate_rows and not all(
                        "f9" in row and "f23" in row for row in candidate_rows
                    ):
                        last_error = RuntimeError(
                            f"东方财富 A 股列表响应缺少估值字段 url={url}"
                        )
                        break
                    data = candidate_data
                    page_rows = candidate_rows
                    preferred_url = url
                    host_succeeded = True
                    break
                if host_succeeded:
                    break
            if not host_succeeded:
                if last_error is not None:
                    raise last_error
                raise RuntimeError("未配置可用的东方财富 A 股列表接口")
            if total is None:
                total_value = data.get("total")
                total = int(total_value) if total_value is not None else None
            if not page_rows:
                break
            rows.extend(page_rows)
            if total is not None and len(rows) >= total:
                break
        result = pd.DataFrame(rows)
        if result.empty:
            return result
        result.rename(
            columns={
                "f12": "代码",
                "f14": "名称",
                "f2": "最新价",
                "f3": "涨跌幅",
                "f9": "市盈率-动态",
                "f23": "市净率",
            },
            inplace=True,
        )
        result.attrs["actual_source"] = "eastmoney:curl_cffi:stock_zh_a_spot_em"
        return result

    def _fetch_assets_code_name(self) -> DataFrame:
        """从 AKShare 全 A 代码名册获取资产基础列表。"""

        frames: list[DataFrame] = []
        source_errors: list[dict[str, str]] = []

        def append_frame(source: str, df: DataFrame, code_column: str, name_column: str) -> None:
            frame = df[[code_column, name_column]].copy()
            frame.columns = ["code", "name"]
            frame["source"] = source
            frames.append(frame)

        for source, loader, code_column, name_column in [
            (
                "akshare:stock_info_sz_name_code",
                lambda: ak.stock_info_sz_name_code(symbol="A股列表"),
                "A股代码",
                "A股简称",
            ),
            (
                "akshare:stock_info_sh_name_code:main",
                lambda: ak.stock_info_sh_name_code(symbol="主板A股"),
                "证券代码",
                "证券简称",
            ),
            (
                "akshare:stock_info_sh_name_code:kcb",
                lambda: ak.stock_info_sh_name_code(symbol="科创板"),
                "证券代码",
                "证券简称",
            ),
            (
                "akshare:stock_info_bj_name_code",
                ak.stock_info_bj_name_code,
                "证券代码",
                "证券简称",
            ),
        ]:
            try:
                append_frame(source, loader(), code_column, name_column)
            except Exception as exc:
                source_errors.append({"source": source, "error_message": str(exc)})

        if not frames:
            return ak.stock_info_a_code_name()

        result = pd.concat(frames, ignore_index=True)
        result["code"] = (
            result["code"].astype(str).str.split(".", expand=True).iloc[:, 0].str.zfill(6)
        )
        result = result.dropna(subset=["code"]).drop_duplicates(subset=["code"], keep="first")
        result.attrs["source_errors"] = source_errors
        return result

    def _fetch_assets_tencent(self) -> DataFrame:
        """从腾讯接口获取 A 股实时行情。"""

        url = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList"
        page_size = 200
        max_pages = 40
        rows: list[dict[str, Any]] = []
        total: int | None = None
        for page in range(max_pages):
            offset = page * page_size
            try:
                response = curl_requests.get(
                    url,
                    params={
                        "_appver": "11.17.0",
                        "board_code": "aStock",
                        "sort_type": "price",
                        "direct": "down",
                        "offset": str(offset),
                        "count": str(page_size),
                    },
                    timeout=15,
                    impersonate="chrome120",
                )
                response.raise_for_status()
                data = response.json().get("data") or {}
                page_rows = data.get("rank_list") or []
                if total is None:
                    total_value = data.get("total")
                    total = int(total_value) if total_value is not None else None
                if not page_rows:
                    break
                rows.extend(page_rows)
                if total is not None and len(rows) >= total:
                    break
            except Exception:
                if rows:
                    break
                raise
        return pd.DataFrame(rows)

    def _fetch_ohlcv_eastmoney(
        self,
        *,
        symbol: str,
        timeframe: str,
        start: str | None,
        end: str | None,
        adjust: str,
    ) -> DataFrame:
        """从东方财富接口获取 A 股历史 K 线。"""

        return ak.stock_zh_a_hist(
            symbol=symbol,
            period=self._to_ak_period(timeframe),
            start_date=start or "20000101",
            end_date=end or "20991231",
            adjust=adjust,
            timeout=self.request_timeout_seconds,
        )

    def _fetch_ohlcv_tencent(
        self,
        *,
        symbol: str,
        start: str | None,
        end: str | None,
        adjust: str,
    ) -> DataFrame:
        """从腾讯接口获取 A 股历史日线。"""

        return fetch_tencent_kline_direct(
            symbol=symbol,
            start=start,
            end=end,
            adjust=adjust,
            timeout=self.request_timeout_seconds,
        )

    def _fetch_ohlcv_eastmoney_curl_cffi(
        self,
        *,
        symbol: str,
        timeframe: str,
        start: str | None,
        end: str | None,
        adjust: str,
    ) -> DataFrame:
        """使用 curl_cffi 直连东方财富 K 线接口。

        AKShare 当前东方财富历史行情实现使用 `requests`。在部分网络环境下，
        上游会直接断开普通 requests 连接；这里保留 repo-side fallback，
        不修改 `.venv` 里的 AKShare 源码。
        """

        return fetch_eastmoney_kline_direct(
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            adjust=adjust,
            timeout=self.request_timeout_seconds,
        )

    def fetch_trade_dates(self, *, start_date: date, end_date: date) -> list[date]:
        """获取 A 股交易日历，用于 K 线缺口补采和调度对齐。"""

        df = ak.tool_trade_date_hist_sina()
        if "trade_date" in df.columns:
            column = "trade_date"
        elif "交易日" in df.columns:
            column = "交易日"
        else:
            column = df.columns[0]
        parsed = pd.to_datetime(df[column], errors="coerce").dt.date
        return sorted(
            trade_date
            for trade_date in parsed.dropna().tolist()
            if start_date <= trade_date <= end_date
        )

    def health_check(self) -> dict[str, Any]:
        """轻量健康检查。"""

        result = self.fetch_ohlcv(symbol="000001", timeframe="1d", limit=1)
        return {
            "provider_name": self.provider_name,
            "status": result.status,
            "error_message": result.error_message,
        }
