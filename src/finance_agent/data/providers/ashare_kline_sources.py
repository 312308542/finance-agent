"""A 股 K 线直连数据源适配层。

这里隔离第三方 HTTP 请求、Cookie、切片和解析逻辑。上层 Provider 只接收 DataFrame，
从而保持原有入库链路不感知具体数据源。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import pandas as pd
import requests
from curl_cffi import requests as curl_requests
from pandas import DataFrame

from finance_agent.data.normalizers import with_ashare_exchange_prefix
from finance_agent.data.providers.eastmoney_curl import (
    eastmoney_headers,
    ensure_eastmoney_kline_cookie,
)

TENCENT_KLINE_BROWSER_HEADERS = {
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
TENCENT_KLINE_WINDOW_MAX_ATTEMPTS = 3
TENCENT_KLINE_WINDOW_RETRY_DELAY_SECONDS = 0.5


@dataclass(frozen=True)
class DateWindow:
    """第三方 K 线接口请求窗口。"""

    start: date
    end: date


def fetch_tencent_kline_direct(
    *,
    symbol: str,
    start: str | None,
    end: str | None,
    adjust: str,
    timeout: float,
) -> DataFrame:
    """直连腾讯 K 线接口，并由本适配层控制请求切片。"""

    prefixed_symbol = with_ashare_exchange_prefix(symbol)
    start_date = _parse_ashare_date(start, default=date(2000, 1, 1))
    end_date = _parse_ashare_date(end, default=date.today())
    if end_date > date.today():
        end_date = date.today()
    if start_date > end_date:
        return _empty_tencent_frame()

    rows: list[list[Any]] = []
    for window in two_year_windows(start_date, end_date):
        rows.extend(
            _fetch_tencent_kline_window(
                prefixed_symbol=prefixed_symbol,
                window=window,
                adjust=adjust,
                timeout=timeout,
            )
        )

    if not rows:
        return _empty_tencent_frame()

    frame = pd.DataFrame(rows).iloc[:, :6]
    frame.columns = ["date", "open", "close", "high", "low", "amount"]
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    for column in ["open", "close", "high", "low", "amount"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["date"]).drop_duplicates(subset=["date"], keep="last")
    frame = frame[(frame["date"] >= start_date) & (frame["date"] <= end_date)]
    frame = frame.sort_values("date").reset_index(drop=True)
    frame.attrs["source"] = "tencent:direct:kline"
    frame.attrs["window_count"] = len(two_year_windows(start_date, end_date))
    return frame


def fetch_eastmoney_kline_direct(
    *,
    symbol: str,
    timeframe: str,
    start: str | None,
    end: str | None,
    adjust: str,
    timeout: float,
) -> DataFrame:
    """直连东方财富 K 线接口，使用统一 Cookie/headers 管理。"""

    ensure_eastmoney_kline_cookie()
    try:
        return _fetch_eastmoney_kline_direct_once(
            symbol=symbol,
            timeframe=timeframe,
            start=start,
            end=end,
            adjust=adjust,
            timeout=timeout,
        )
    except Exception:
        refresh_status = ensure_eastmoney_kline_cookie(force=True)
        if refresh_status.get("probe_ok"):
            return _fetch_eastmoney_kline_direct_once(
                symbol=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
                adjust=adjust,
                timeout=timeout,
            )
        raise


def _fetch_eastmoney_kline_direct_once(
    *,
    symbol: str,
    timeframe: str,
    start: str | None,
    end: str | None,
    adjust: str,
    timeout: float,
) -> DataFrame:
    """执行一次东方财富 K 线请求，不在此函数内处理刷新。"""

    period = _to_eastmoney_period(timeframe)
    adjust_code = {"qfq": "1", "hfq": "2", "": "0"}[adjust]
    market_code = 1 if symbol.startswith(("6", "9")) else 0
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "klt": period,
        "fqt": adjust_code,
        "secid": f"{market_code}.{symbol}",
        "beg": start or "20000101",
        "end": end or _format_compact(date.today()),
    }
    response = curl_requests.get(
        url,
        params=params,
        timeout=timeout,
        impersonate="chrome120",
        headers=eastmoney_headers(),
    )
    response.raise_for_status()
    data_json = response.json()
    klines = (data_json.get("data") or {}).get("klines") or []
    if not klines:
        return _empty_eastmoney_frame()
    frame = pd.DataFrame([item.split(",") for item in klines])
    frame["股票代码"] = symbol
    frame.columns = [
        "日期",
        "开盘",
        "收盘",
        "最高",
        "最低",
        "成交量",
        "成交额",
        "振幅",
        "涨跌幅",
        "涨跌额",
        "换手率",
        "股票代码",
    ]
    frame["日期"] = pd.to_datetime(frame["日期"], errors="coerce").dt.date
    for column in ["开盘", "收盘", "最高", "最低", "成交量", "成交额", "振幅", "涨跌幅", "涨跌额", "换手率"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame.attrs["source"] = "eastmoney:direct:kline"
    return frame[
        [
            "日期",
            "股票代码",
            "开盘",
            "收盘",
            "最高",
            "最低",
            "成交量",
            "成交额",
            "振幅",
            "涨跌幅",
            "涨跌额",
            "换手率",
        ]
    ]


def two_year_windows(start_date: date, end_date: date) -> list[DateWindow]:
    """按两年一组生成腾讯 K 线请求窗口，避免 AKShare 内部逐年重叠循环。"""

    windows: list[DateWindow] = []
    cursor = start_date
    while cursor <= end_date:
        window_end = min(date(cursor.year + 1, 12, 31), end_date)
        windows.append(DateWindow(start=cursor, end=window_end))
        cursor = date(window_end.year + 1, 1, 1)
    return windows


def _fetch_tencent_kline_window(
    *,
    prefixed_symbol: str,
    window: DateWindow,
    adjust: str,
    timeout: float,
) -> list[list[Any]]:
    url = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
    var_name = f"kline_day{adjust}{window.start.year}"
    params = {
        "_var": var_name,
        "param": (
            f"{prefixed_symbol},day,{_format_hyphen(window.start)},"
            f"{_format_hyphen(window.end)},640,{adjust}"
        ),
        "r": "0.8205512681390605",
    }
    response = None
    for attempt in range(1, TENCENT_KLINE_WINDOW_MAX_ATTEMPTS + 1):
        try:
            response = requests.get(
                url,
                params=params,
                timeout=timeout,
                headers=TENCENT_KLINE_BROWSER_HEADERS,
            )
            response.raise_for_status()
            break
        except requests.RequestException:
            if attempt >= TENCENT_KLINE_WINDOW_MAX_ATTEMPTS:
                raise
            time.sleep(TENCENT_KLINE_WINDOW_RETRY_DELAY_SECONDS)
    if response is None:
        raise RuntimeError("腾讯 K 线请求未返回响应")
    payload = _loads_jsonp(response.text)
    symbol_payload = (payload.get("data") or {}).get(prefixed_symbol) or {}
    return (
        symbol_payload.get("qfqday")
        or symbol_payload.get("hfqday")
        or symbol_payload.get("day")
        or []
    )


def _loads_jsonp(text: str) -> dict[str, Any]:
    payload = text[text.find("={") + 1 :] if "={" in text else text
    return json.loads(payload)


def _parse_ashare_date(value: str | None, *, default: date) -> date:
    if not value:
        return default
    text = value.strip()
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d").date()
    return datetime.strptime(text, "%Y-%m-%d").date()


def _format_hyphen(value: date) -> str:
    return value.strftime("%Y-%m-%d")


def _format_compact(value: date) -> str:
    return value.strftime("%Y%m%d")


def _to_eastmoney_period(timeframe: str) -> str:
    mapping = {"1d": "101", "1w": "102", "1M": "103"}
    if timeframe not in mapping:
        raise ValueError(f"东方财富 A 股 K 线暂不支持周期: {timeframe}")
    return mapping[timeframe]


def _empty_tencent_frame() -> DataFrame:
    return pd.DataFrame(columns=["date", "open", "close", "high", "low", "amount"])


def _empty_eastmoney_frame() -> DataFrame:
    return pd.DataFrame(
        columns=[
            "日期",
            "股票代码",
            "开盘",
            "收盘",
            "最高",
            "最低",
            "成交量",
            "成交额",
            "振幅",
            "涨跌幅",
            "涨跌额",
            "换手率",
        ]
    )
