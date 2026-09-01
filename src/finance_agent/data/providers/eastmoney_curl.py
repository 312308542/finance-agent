"""东方财富 curl_cffi 兜底接口。

AKShare 某些东方财富接口在当前网络下会被上游断开普通 requests 连接。
这里保留仓库侧 fallback：复用 AKShare 的公开接口参数和字段映射，只替换
传输层为 curl_cffi，不修改虚拟环境中的 AKShare 源码。
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import string
import threading
import time
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any

import pandas as pd
from curl_cffi import requests as curl_requests

JsonDict = dict[str, Any]

EASTMONEY_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://quote.eastmoney.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    ),
}
EASTMONEY_COOKIE_ENV = "FINANCE_AGENT_EASTMONEY_COOKIE"
EASTMONEY_COOKIE_FILE_ENV = "FINANCE_AGENT_EASTMONEY_COOKIE_FILE"
EASTMONEY_COOKIE_AUTO_REFRESH_ENV = "FINANCE_AGENT_EASTMONEY_COOKIE_AUTO_REFRESH"
EASTMONEY_COOKIE_MAX_AGE_SECONDS_ENV = "FINANCE_AGENT_EASTMONEY_COOKIE_MAX_AGE_SECONDS"
EASTMONEY_KLINE_COOKIE_COOLDOWN_SECONDS_ENV = "FINANCE_AGENT_EASTMONEY_KLINE_COOKIE_COOLDOWN_SECONDS"
EASTMONEY_KLINE_COOKIE_PROBE_TTL_SECONDS_ENV = "FINANCE_AGENT_EASTMONEY_KLINE_COOKIE_PROBE_TTL_SECONDS"
DEFAULT_EASTMONEY_COOKIE_FILE = Path("runtime/secrets/eastmoney_cookie.json")
DEFAULT_EASTMONEY_COOKIE_MAX_AGE_SECONDS = 12 * 60 * 60
DEFAULT_EASTMONEY_KLINE_COOKIE_COOLDOWN_SECONDS = 15 * 60
DEFAULT_EASTMONEY_KLINE_COOKIE_PROBE_TTL_SECONDS = 10 * 60
_SYNTHETIC_EASTMONEY_COOKIE: str | None = None
_EASTMONEY_COOKIE_REFRESH_LOCK = threading.Lock()
_EASTMONEY_COOKIE_REFRESH_GENERATION = 0
_EASTMONEY_KLINE_COOKIE_HEALTH_LOCK = threading.Lock()
_EASTMONEY_KLINE_COOKIE_HEALTH: dict[str, Any] = {
    "state": "unknown",
    "last_checked_at": 0.0,
    "unavailable_until": 0.0,
    "last_error_message": None,
}


def eastmoney_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """生成东方财富浏览器态请求头；配置 cookie 时自动追加 Cookie。"""

    headers = dict(EASTMONEY_HEADERS)
    if extra:
        headers.update(extra)
    headers["Cookie"] = _eastmoney_cookie()
    return headers


def _eastmoney_cookie() -> str:
    """读取用户配置的 Cookie；未配置时生成匿名浏览器 Cookie。"""

    global _EASTMONEY_COOKIE_REFRESH_GENERATION

    configured_cookie = os.getenv(EASTMONEY_COOKIE_ENV, "").strip()
    if configured_cookie:
        return configured_cookie
    ensure_eastmoney_cookie()
    file_cookie = _eastmoney_cookie_from_file()
    if file_cookie:
        return file_cookie
    return _synthetic_eastmoney_cookie()


def _eastmoney_cookie_from_file() -> str | None:
    """从本地 secret 文件读取东方财富 Cookie。

    文件可以是 JSON：{"cookie": "..."}，也可以是纯 Cookie 字符串。这个文件只用于本机
    运行态，不应提交到版本库。
    """

    cookie_path = Path(os.getenv(EASTMONEY_COOKIE_FILE_ENV, "") or DEFAULT_EASTMONEY_COOKIE_FILE)
    if not cookie_path.exists():
        return None
    raw_text = cookie_path.read_text(encoding="utf-8").strip()
    if not raw_text:
        return None
    if raw_text.startswith("{"):
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            return None
        cookie = str(payload.get("cookie") or "").strip()
        return cookie or None
    return raw_text


def eastmoney_cookie_status() -> dict[str, Any]:
    """返回东方财富 Cookie 的来源和可用性，供任务监控或诊断脚本展示。"""

    configured_cookie = os.getenv(EASTMONEY_COOKIE_ENV, "").strip()
    if configured_cookie:
        return {"available": True, "source": "env", "cookie_length": len(configured_cookie)}
    file_cookie = _eastmoney_cookie_from_file()
    if file_cookie:
        return {"available": True, "source": "file", "cookie_length": len(file_cookie)}
    return {"available": True, "source": "synthetic", "cookie_length": len(_synthetic_eastmoney_cookie())}


def ensure_eastmoney_cookie(*, force: bool = False) -> dict[str, Any]:
    """确保本地有可复用的东方财富 Cookie。

    环境变量 Cookie 永远优先，不会被自动刷新覆盖。未配置环境变量时，若本地 Cookie 文件
    缺失或超过最大年龄，则尝试用 Playwright 自动刷新。刷新失败不会阻断采集，调用方仍可
    退回合成 Cookie 或其他数据源。
    """

    configured_cookie = os.getenv(EASTMONEY_COOKIE_ENV, "").strip()
    if configured_cookie:
        return {"available": True, "source": "env", "refreshed": False}
    cookie_path = _eastmoney_cookie_file_path()
    max_age_seconds = _eastmoney_cookie_max_age_seconds()
    if not force and cookie_path.exists() and _cookie_file_is_fresh(cookie_path, max_age_seconds):
        return {"available": True, "source": "file", "refreshed": False}
    if not _eastmoney_auto_refresh_enabled():
        return {
            "available": bool(_eastmoney_cookie_from_file()),
            "source": "file" if _eastmoney_cookie_from_file() else "synthetic",
            "refreshed": False,
            "auto_refresh": False,
        }
    global _EASTMONEY_COOKIE_REFRESH_GENERATION

    observed_refresh_generation = _EASTMONEY_COOKIE_REFRESH_GENERATION
    with _EASTMONEY_COOKIE_REFRESH_LOCK:
        if cookie_path.exists() and _cookie_file_is_fresh(cookie_path, max_age_seconds):
            if not force or _EASTMONEY_COOKIE_REFRESH_GENERATION != observed_refresh_generation:
                return {
                    "available": True,
                    "source": "file",
                    "refreshed": False,
                    "singleflight": force,
                }
        if force and _EASTMONEY_COOKIE_REFRESH_GENERATION != observed_refresh_generation:
            if _eastmoney_cookie_from_file():
                return {
                    "available": True,
                    "source": "file",
                    "refreshed": False,
                    "singleflight": True,
                }
        try:
            payload = refresh_eastmoney_cookie_file(output_path=cookie_path)
        except Exception as exc:  # noqa: BLE001 - 自动保活失败时不能阻断采集兜底
            return {
                "available": bool(_eastmoney_cookie_from_file()),
                "source": "file" if _eastmoney_cookie_from_file() else "synthetic",
                "refreshed": False,
                "error_message": str(exc),
            }
        _EASTMONEY_COOKIE_REFRESH_GENERATION += 1
        return {
            "available": True,
            "source": "file",
            "refreshed": True,
            "cookie_count": payload.get("cookie_count"),
        }


def ensure_eastmoney_kline_cookie(*, force: bool = False) -> dict[str, Any]:
    """确保东方财富 K 线 Cookie 对 push2his 接口真实可用。

    通用 Cookie 只证明本机能生成浏览器态 Cookie；K 线采集还必须通过 push2his
    探测。探测失败时进入短冷却，后续标的会快速降级到腾讯，冷却到期后再自动探测
    并恢复东方财富。
    """

    now = time.time()
    with _EASTMONEY_KLINE_COOKIE_HEALTH_LOCK:
        unavailable_until = float(_EASTMONEY_KLINE_COOKIE_HEALTH.get("unavailable_until") or 0.0)
        if unavailable_until > now:
            remaining = max(0, int(unavailable_until - now))
            raise RuntimeError(f"东方财富 K 线 Cookie 冷却中，约 {remaining} 秒后重试")

        probe_ttl_seconds = _eastmoney_kline_cookie_probe_ttl_seconds()
        last_checked_at = float(_EASTMONEY_KLINE_COOKIE_HEALTH.get("last_checked_at") or 0.0)
        if (
            not force
            and _EASTMONEY_KLINE_COOKIE_HEALTH.get("state") == "healthy"
            and now - last_checked_at <= probe_ttl_seconds
        ):
            return {
                "available": True,
                "source": eastmoney_cookie_status()["source"],
                "refreshed": False,
                "probe_ok": True,
                "cached": True,
            }

        cookie_status = ensure_eastmoney_cookie(force=force)
        try:
            probe_status = _probe_eastmoney_kline_cookie()
        except Exception as exc:  # noqa: BLE001 - 这里需要把任意探测失败收敛成冷却状态
            reason = str(exc)
            _set_eastmoney_kline_cookie_unavailable(reason, now=now)
            raise RuntimeError(f"东方财富 K 线 Cookie 校验失败，已进入冷却：{reason}") from exc

        _EASTMONEY_KLINE_COOKIE_HEALTH.update(
            {
                "state": "healthy",
                "last_checked_at": now,
                "unavailable_until": 0.0,
                "last_error_message": None,
            }
        )
        return {
            **cookie_status,
            "available": True,
            "source": eastmoney_cookie_status()["source"],
            "probe_ok": True,
            "probe_row_count": probe_status["row_count"],
        }


def mark_eastmoney_kline_cookie_unavailable(reason: str) -> dict[str, Any]:
    """主动标记东方财富 K 线 Cookie 不可用，等待冷却后自动探测恢复。"""

    with _EASTMONEY_KLINE_COOKIE_HEALTH_LOCK:
        return _set_eastmoney_kline_cookie_unavailable(reason, now=time.time())


def eastmoney_kline_cookie_health_status() -> dict[str, Any]:
    """返回东方财富 K 线 Cookie 健康状态，用于诊断和任务监控展示。"""

    now = time.time()
    with _EASTMONEY_KLINE_COOKIE_HEALTH_LOCK:
        unavailable_until = float(_EASTMONEY_KLINE_COOKIE_HEALTH.get("unavailable_until") or 0.0)
        status = dict(_EASTMONEY_KLINE_COOKIE_HEALTH)
    status["cooldown_remaining_seconds"] = max(0, int(unavailable_until - now))
    return status


def reset_eastmoney_kline_cookie_health_for_tests() -> None:
    """重置东方财富 K 线 Cookie 健康状态；仅供测试隔离使用。"""

    with _EASTMONEY_KLINE_COOKIE_HEALTH_LOCK:
        _EASTMONEY_KLINE_COOKIE_HEALTH.update(
            {
                "state": "unknown",
                "last_checked_at": 0.0,
                "unavailable_until": 0.0,
                "last_error_message": None,
            }
        )


def refresh_eastmoney_cookie_file(
    *,
    output_path: Path | None = None,
    headed: bool = False,
    timeout_ms: int = 45_000,
) -> dict[str, Any]:
    """使用浏览器刷新东方财富 Cookie 并写入本地 secret 文件。"""

    payload = collect_eastmoney_cookie(headed=headed, timeout_ms=timeout_ms)
    target = output_path or _eastmoney_cookie_file_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def collect_eastmoney_cookie(*, headed: bool = False, timeout_ms: int = 45_000) -> dict[str, Any]:
    """打开东方财富页面并导出匿名访问 Cookie。"""

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - 依赖由运行环境决定
        raise RuntimeError("当前环境缺少 playwright，无法自动刷新东方财富 Cookie") from exc

    with sync_playwright() as playwright:
        browser = _launch_cookie_browser(playwright, headed=headed)
        context = browser.new_context(
            user_agent=EASTMONEY_HEADERS["User-Agent"],
            locale="zh-CN",
        )
        page = context.new_page()
        page.goto("https://quote.eastmoney.com/", wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(1500)
        page.goto(
            "https://quote.eastmoney.com/sh603507.html",
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )
        page.wait_for_timeout(1500)
        cookies = context.cookies(
            [
                "https://quote.eastmoney.com/",
                "https://push2.eastmoney.com/",
                "https://push2his.eastmoney.com/",
            ]
        )
        browser.close()

    cookie_items = [
        f"{cookie['name']}={cookie['value']}"
        for cookie in cookies
        if "eastmoney.com" in str(cookie.get("domain", ""))
    ]
    cookie = "; ".join(dict.fromkeys(cookie_items))
    if not cookie:
        raise RuntimeError("未能从浏览器会话中提取东方财富 Cookie")
    return {
        "cookie": cookie,
        "cookie_count": len(cookie_items),
        "updated_at": datetime.now(tz=UTC).isoformat(),
        "source": "playwright",
        "domains": sorted({str(item.get("domain")) for item in cookies}),
    }


def _eastmoney_cookie_file_path() -> Path:
    return Path(os.getenv(EASTMONEY_COOKIE_FILE_ENV, "") or DEFAULT_EASTMONEY_COOKIE_FILE)


def _eastmoney_cookie_max_age_seconds() -> int:
    raw_value = os.getenv(EASTMONEY_COOKIE_MAX_AGE_SECONDS_ENV, "").strip()
    if not raw_value:
        return DEFAULT_EASTMONEY_COOKIE_MAX_AGE_SECONDS
    try:
        return max(0, int(raw_value))
    except ValueError:
        return DEFAULT_EASTMONEY_COOKIE_MAX_AGE_SECONDS


def _eastmoney_kline_cookie_cooldown_seconds() -> int:
    raw_value = os.getenv(EASTMONEY_KLINE_COOKIE_COOLDOWN_SECONDS_ENV, "").strip()
    if not raw_value:
        return DEFAULT_EASTMONEY_KLINE_COOKIE_COOLDOWN_SECONDS
    try:
        return max(0, int(raw_value))
    except ValueError:
        return DEFAULT_EASTMONEY_KLINE_COOKIE_COOLDOWN_SECONDS


def _eastmoney_kline_cookie_probe_ttl_seconds() -> int:
    raw_value = os.getenv(EASTMONEY_KLINE_COOKIE_PROBE_TTL_SECONDS_ENV, "").strip()
    if not raw_value:
        return DEFAULT_EASTMONEY_KLINE_COOKIE_PROBE_TTL_SECONDS
    try:
        return max(0, int(raw_value))
    except ValueError:
        return DEFAULT_EASTMONEY_KLINE_COOKIE_PROBE_TTL_SECONDS


def _set_eastmoney_kline_cookie_unavailable(reason: str, *, now: float) -> dict[str, Any]:
    cooldown_seconds = _eastmoney_kline_cookie_cooldown_seconds()
    unavailable_until = now + cooldown_seconds
    _EASTMONEY_KLINE_COOKIE_HEALTH.update(
        {
            "state": "cooling",
            "last_checked_at": now,
            "unavailable_until": unavailable_until,
            "last_error_message": reason,
        }
    )
    return dict(_EASTMONEY_KLINE_COOKIE_HEALTH)


def _probe_eastmoney_kline_cookie() -> dict[str, Any]:
    """用真实 push2his K 线接口校验当前 Cookie 是否可用。"""

    response = curl_requests.get(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        params={
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
            "klt": "101",
            "fqt": "1",
            "secid": "1.603507",
            "beg": "20240101",
            "end": "20251231",
        },
        timeout=10,
        impersonate="chrome120",
        headers=eastmoney_headers(),
    )
    response.raise_for_status()
    payload = response.json()
    klines = (payload.get("data") or {}).get("klines") or []
    if payload.get("rc") != 0 or not klines:
        raise RuntimeError(f"push2his probe returned rc={payload.get('rc')} rows={len(klines)}")
    return {"row_count": len(klines)}


def _cookie_file_is_fresh(cookie_path: Path, max_age_seconds: int) -> bool:
    if max_age_seconds <= 0:
        return False
    age_seconds = time.time() - cookie_path.stat().st_mtime
    return age_seconds <= max_age_seconds


def _eastmoney_auto_refresh_enabled() -> bool:
    if os.getenv("PYTEST_CURRENT_TEST"):
        return False
    value = os.getenv(EASTMONEY_COOKIE_AUTO_REFRESH_ENV, "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _launch_cookie_browser(playwright: Any, *, headed: bool) -> Any:
    for channel in ["msedge", "chrome"]:
        try:
            return playwright.chromium.launch(channel=channel, headless=not headed)
        except Exception:
            continue
    return playwright.chromium.launch(headless=not headed)


def _synthetic_eastmoney_cookie() -> str:
    """生成东方财富匿名访问 Cookie，用于没有浏览器 Cookie 的后台采集。"""

    global _SYNTHETIC_EASTMONEY_COOKIE
    if _SYNTHETIC_EASTMONEY_COOKIE:
        return _SYNTHETIC_EASTMONEY_COOKIE

    now_ms = int(time.time() * 1000)
    first_seen = datetime.now().strftime("%Y-%m-%d%%20%H%%3A%M%%3A%S")
    cookies = [
        f"st_nvi={_random_alnum(26)}",
        f"qgqp_b_id={secrets.token_hex(16)}",
        f"nid18=000{secrets.token_hex(15)[:29]}",
        f"nid18_create_time={now_ms}",
        f"gviem={_random_alnum(22)}",
        f"gviem_create_time={now_ms}",
        f"st_pvi={secrets.randbelow(90_000_000_000_000) + 10_000_000_000_000}",
        f"st_sp={first_seen}",
        "st_inirUrl=https%3A%2F%2Fquote.eastmoney.com%2F",
    ]
    _SYNTHETIC_EASTMONEY_COOKIE = "; ".join(cookies)
    return _SYNTHETIC_EASTMONEY_COOKIE


def _random_alnum(length: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def fetch_industry_members(symbol: str) -> pd.DataFrame:
    """获取行业板块成分。"""

    board_code = symbol if re.match(pattern=r"^BK\d+", string=symbol) else _find_board_code(
        symbol,
        board_type="industry",
    )
    df = _fetch_board_members(board_code, sort_field="f3")
    df.attrs["actual_source"] = "eastmoney:curl_cffi:stock_board_industry_cons_em"
    return df


def fetch_concept_members(symbol: str) -> pd.DataFrame:
    """获取概念板块成分。"""

    board_code = symbol if re.match(pattern=r"^BK\d+", string=symbol) else _find_board_code(
        symbol,
        board_type="concept",
    )
    df = _fetch_board_members(board_code, sort_field="f12")
    df.attrs["actual_source"] = "eastmoney:curl_cffi:stock_board_concept_cons_em"
    return df


def fetch_industry_names(*, limit: int | None = None) -> pd.DataFrame:
    """获取东方财富行业板块目录。"""

    df = _fetch_board_names("industry")
    if limit is not None:
        df = df.head(limit)
    result = df.reset_index(drop=True)
    result.attrs["actual_source"] = "eastmoney:curl_cffi:stock_board_industry_name_em"
    return result


def fetch_concept_names(*, limit: int | None = None) -> pd.DataFrame:
    """获取东方财富概念板块目录。"""

    df = _fetch_board_names("concept")
    if limit is not None:
        df = df.head(limit)
    result = df.reset_index(drop=True)
    result.attrs["actual_source"] = "eastmoney:curl_cffi:stock_board_concept_name_em"
    return result


def fetch_fund_flow_rank(indicator: str, *, limit: int | None = None) -> pd.DataFrame:
    """获取个股资金流排名。"""

    try:
        return _fetch_eastmoney_fund_flow_rank(indicator, limit=limit)
    except Exception:
        return _fetch_ths_fund_flow_rank(indicator, limit=limit)


def fetch_individual_fund_flow(symbol: str, market: str) -> pd.DataFrame:
    """使用 curl_cffi 获取单只股票历史资金流。"""

    market_map = {"sh": 1, "sz": 0, "bj": 0}
    if market not in market_map:
        raise ValueError(f"不支持的资金流市场: {market}")
    payload = _curl_get_json(
        "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
        params={
            "lmt": "0",
            "klt": "101",
            "secid": f"{market_map[market]}.{symbol}",
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
            "ut": "b2884a393a59ad64002292a3e90d46a5",
            "_": int(time.time() * 1000),
        },
    )
    klines = ((payload.get("data") or {}).get("klines") or [])
    columns = [
        "日期",
        "主力净流入-净额",
        "小单净流入-净额",
        "中单净流入-净额",
        "大单净流入-净额",
        "超大单净流入-净额",
        "主力净流入-净占比",
        "小单净流入-净占比",
        "中单净流入-净占比",
        "大单净流入-净占比",
        "超大单净流入-净占比",
        "收盘价",
        "涨跌幅",
        "-1",
        "-2",
    ]
    rows = [str(item).split(",") for item in klines]
    frame = pd.DataFrame(rows, columns=columns) if rows else pd.DataFrame(columns=columns)
    frame.attrs["actual_source"] = "eastmoney:curl_cffi:stock_individual_fund_flow"
    return frame


def fetch_stop_list(*, limit: int | None = None) -> pd.DataFrame:
    """获取两网及退市/交易状态异常列表。"""

    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1",
        "pz": "100",
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": "f3",
        "fs": "m:0 s:3",
        "fields": (
            "f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,"
            "f18,f20,f21,f23,f24,f25,f22,f11,f62,f128,f136,f115,f152"
        ),
    }
    temp_df = _fetch_clist_pages(url, params, max_rows=limit)
    temp_df.columns = [
        "序号",
        "最新价",
        "涨跌幅",
        "涨跌额",
        "成交量",
        "成交额",
        "振幅",
        "换手率",
        "市盈率-动态",
        "量比",
        "_",
        "代码",
        "_",
        "名称",
        "最高",
        "最低",
        "今开",
        "昨收",
        "_",
        "_",
        "_",
        "市净率",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
    ]
    result = temp_df[
        [
            "序号",
            "代码",
            "名称",
            "最新价",
            "涨跌幅",
            "涨跌额",
            "成交量",
            "成交额",
            "振幅",
            "最高",
            "最低",
            "今开",
            "昨收",
            "量比",
            "换手率",
            "市盈率-动态",
            "市净率",
        ]
    ]
    result.attrs["actual_source"] = "eastmoney:curl_cffi:stock_zh_a_stop_em"
    return _to_numeric(
        result,
        [
            "最新价",
            "涨跌幅",
            "涨跌额",
            "成交量",
            "成交额",
            "振幅",
            "最高",
            "最低",
            "今开",
            "量比",
            "换手率",
            "市盈率-动态",
            "市净率",
        ],
    )


def fetch_hot_rank(*, limit: int | None = None) -> pd.DataFrame:
    """获取东方财富个股人气榜。"""

    page_size = limit or 100
    rank_response = curl_requests.post(
        "https://emappdata.eastmoney.com/stockrank/getAllCurrentList",
        json={
            "appId": "appId01",
            "globalId": "786e4c21-70dc-435a-93bb-38",
            "marketType": "",
            "pageNo": 1,
            "pageSize": page_size,
        },
        timeout=20,
        impersonate="chrome120",
        headers=eastmoney_headers({"Referer": "https://guba.eastmoney.com/rank/"}),
    )
    rank_response.raise_for_status()
    rank_df = pd.DataFrame((rank_response.json() or {}).get("data") or [])
    if rank_df.empty:
        return pd.DataFrame()
    if limit is not None:
        rank_df = rank_df.head(limit)

    rank_df["mark"] = [
        "0" + "." + item[2:] if "SZ" in item else "1" + "." + item[2:]
        for item in rank_df["sc"]
    ]
    quote_params = {
        "ut": "f057cbcbce2a86e2866ab8877db1d059",
        "fltt": "2",
        "invt": "2",
        "fields": "f14,f3,f12,f2",
        "secids": ",".join(rank_df["mark"]),
    }
    try:
        quote_json = _curl_get_json_any(
            [
                "https://push2.eastmoney.com/api/qt/ulist.np/get",
                "https://20.push2.eastmoney.com/api/qt/ulist.np/get",
                "https://29.push2.eastmoney.com/api/qt/ulist.np/get",
                "https://push2his.eastmoney.com/api/qt/ulist.np/get",
            ],
            params=quote_params,
        )
        quote_df = pd.DataFrame(((quote_json.get("data") or {}).get("diff")) or [])
    except Exception:
        quote_df = pd.DataFrame()
    if quote_df.empty:
        clean_codes = [str(item)[2:] for item in rank_df["sc"]]
        result = pd.DataFrame(
            {
                "当前排名": pd.to_numeric(rank_df["rk"], errors="coerce"),
                "代码": clean_codes,
                "股票名称": clean_codes,
                "最新价": None,
                "涨跌额": None,
                "涨跌幅": None,
            }
        )
        result.attrs["actual_source"] = "eastmoney:curl_cffi:stock_hot_rank_em"
        result.attrs["source_coverage"] = "rank_only"
        return result
    quote_df.columns = ["股票名称", "涨跌幅", "代码", "最新价"]
    quote_df["最新价"] = pd.to_numeric(quote_df["最新价"], errors="coerce")
    quote_df["涨跌幅"] = pd.to_numeric(quote_df["涨跌幅"], errors="coerce")
    quote_df["涨跌额"] = quote_df["最新价"] * quote_df["涨跌幅"] / 100
    quote_df["当前排名"] = pd.to_numeric(rank_df["rk"], errors="coerce").to_list()
    quote_df["代码"] = rank_df["sc"].to_list()
    result = quote_df[
        [
            "当前排名",
            "代码",
            "股票名称",
            "最新价",
            "涨跌额",
            "涨跌幅",
        ]
    ]
    result.attrs["actual_source"] = "eastmoney:curl_cffi:stock_hot_rank_em"
    result.attrs["source_coverage"] = "rank_with_quote"
    return result


def _fetch_eastmoney_fund_flow_rank(
    indicator: str,
    *,
    limit: int | None = None,
) -> pd.DataFrame:
    """使用东方财富个股资金流排名接口。"""

    indicator_map = {
        "今日": [
            "f62",
            "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f204,f205,f124",
        ],
        "3日": [
            "f267",
            "f12,f14,f2,f127,f267,f268,f269,f270,f271,f272,f273,f274,f275,f276,f257,f258,f124",
        ],
        "5日": [
            "f164",
            "f12,f14,f2,f109,f164,f165,f166,f167,f168,f169,f170,f171,f172,f173,f257,f258,f124",
        ],
        "10日": [
            "f174",
            "f12,f14,f2,f160,f174,f175,f176,f177,f178,f179,f180,f181,f182,f183,f260,f261,f124",
        ],
    }
    if indicator not in indicator_map:
        raise ValueError(f"不支持的资金流周期: {indicator}")

    url = "https://push2.eastmoney.com/api/qt/clist/get"
    params = {
        "fid": indicator_map[indicator][0],
        "po": "1",
        "pz": "100",
        "pn": "1",
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "ut": "b2884a393a59ad64002292a3e90d46a5",
        "fs": (
            "m:0+t:6+f:!2,m:0+t:13+f:!2,m:0+t:80+f:!2,"
            "m:1+t:2+f:!2,m:1+t:23+f:!2,m:0+t:7+f:!2,m:1+t:3+f:!2"
        ),
        "fields": indicator_map[indicator][1],
    }
    temp_df = _fetch_clist_pages(url, params, sort_by=None, max_rows=limit)
    result = _rename_fund_flow_columns(temp_df, indicator=indicator)
    result.attrs["actual_source"] = "eastmoney:curl_cffi:stock_individual_fund_flow_rank"
    return result


def fetch_performance_report(
    date: str,
    *,
    report_type: str,
    limit: int | None = None,
) -> pd.DataFrame:
    """获取业绩报表、快报或预告。"""

    if report_type == "业绩报表":
        df = _fetch_yjbb(date, limit=limit)
        df.attrs["actual_source"] = "eastmoney:curl_cffi:stock_yjbb_em"
        return df
    if report_type == "业绩快报":
        df = _fetch_yjkb(date, limit=limit)
        df.attrs["actual_source"] = "eastmoney:curl_cffi:stock_yjkb_em"
        return df
    if report_type == "业绩预告":
        df = _fetch_yjyg(date, limit=limit)
        df.attrs["actual_source"] = "eastmoney:curl_cffi:stock_yjyg_em"
        return df
    raise ValueError(f"不支持的业绩报告类型: {report_type}")


def _fetch_board_members(board_code: str, *, sort_field: str) -> pd.DataFrame:
    url = "https://29.push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1",
        "pz": "100",
        "po": "1",
        "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": "2",
        "invt": "2",
        "fid": sort_field,
        "fs": f"b:{board_code} f:!50",
        "fields": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,f20,f21,"
        "f23,f24,f25,f22,f11,f62,f128,f136,f115,f152,f45",
    }
    temp_df = _fetch_clist_pages(url, params)
    temp_df.columns = [
        "序号",
        "_",
        "最新价",
        "涨跌幅",
        "涨跌额",
        "成交量",
        "成交额",
        "振幅",
        "换手率",
        "市盈率-动态",
        "_",
        "_",
        "代码",
        "_",
        "名称",
        "最高",
        "最低",
        "今开",
        "昨收",
        "_",
        "_",
        "_",
        "市净率",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
    ]
    temp_df = temp_df[
        [
            "序号",
            "代码",
            "名称",
            "最新价",
            "涨跌幅",
            "涨跌额",
            "成交量",
            "成交额",
            "振幅",
            "最高",
            "最低",
            "今开",
            "昨收",
            "换手率",
            "市盈率-动态",
            "市净率",
        ]
    ]
    return _to_numeric(
        temp_df,
        [
            "最新价",
            "涨跌幅",
            "涨跌额",
            "成交量",
            "成交额",
            "振幅",
            "最高",
            "最低",
            "今开",
            "昨收",
            "换手率",
            "市盈率-动态",
            "市净率",
        ],
    )


def _fetch_ths_fund_flow_rank(indicator: str, *, limit: int | None = None) -> pd.DataFrame:
    """使用同花顺个股资金流作为非东财备用源。"""

    board_map = {
        "今日": "",
        "3日": "board/3/",
        "5日": "board/5/",
        "10日": "board/10/",
    }
    if indicator not in board_map:
        raise ValueError(f"同花顺资金流暂不支持周期: {indicator}")

    path = board_map[indicator]
    url_template = (
        f"http://data.10jqka.com.cn/funds/ggzjl/{path}"
        "field/zdf/order/desc/page/{}/ajax/1/free/1/"
    )
    first_html = _ths_get_text(
        url_template.format(1),
        referer="http://data.10jqka.com.cn/funds/ggzjl/",
    )
    page_count = _parse_ths_page_count(first_html)
    page_size = 60
    max_pages = page_count
    if limit is not None:
        max_pages = min(max_pages, max(math.ceil(limit / page_size), 1))

    frames: list[pd.DataFrame] = []
    first_tables = _read_ths_html_tables(first_html)
    if first_tables:
        frames.append(first_tables[0])
    for page in range(2, max_pages + 1):
        html = _ths_get_text(url_template.format(page), referer="http://data.10jqka.com.cn/funds/ggzjl/")
        tables = _read_ths_html_tables(html)
        if not tables:
            continue
        frames.append(tables[0])
        if limit is not None and sum(len(item) for item in frames) >= limit:
            break
    if not frames:
        return pd.DataFrame()

    big_df = pd.concat(frames, ignore_index=True)
    if "序号" in big_df.columns:
        del big_df["序号"]
    big_df.reset_index(inplace=True)
    big_df["index"] = range(1, len(big_df) + 1)
    if indicator == "今日":
        big_df.columns = [
            "序号",
            "代码",
            "名称",
            "最新价",
            "今日涨跌幅",
            "今日换手率",
            "流入资金",
            "流出资金",
            "今日主力净流入-净额",
            "成交额",
        ]
        big_df = big_df[
            [
                "序号",
                "代码",
                "名称",
                "最新价",
                "今日涨跌幅",
                "今日主力净流入-净额",
                "成交额",
            ]
        ]
    else:
        big_df.columns = [
            "序号",
            "代码",
            "名称",
            "最新价",
            f"{indicator}涨跌幅",
            "连续换手率",
            f"{indicator}主力净流入-净额",
        ]
        big_df = big_df[
            [
                "序号",
                "代码",
                "名称",
                "最新价",
                f"{indicator}涨跌幅",
                f"{indicator}主力净流入-净额",
            ]
        ]
    big_df["代码"] = big_df["代码"].map(_normalize_ths_stock_code)
    if limit is not None:
        big_df = big_df.head(limit)
    big_df.attrs["actual_source"] = "ths:curl_cffi:stock_fund_flow_individual"
    return big_df


def _find_board_code(symbol: str, *, board_type: str) -> str:
    board_df = _fetch_board_names(board_type)
    matched = board_df[board_df["板块名称"] == symbol]
    if matched.empty:
        raise ValueError(f"未找到{board_type}板块代码: {symbol}")
    return str(matched["板块代码"].iloc[0])


def _fetch_board_names(board_type: str) -> pd.DataFrame:
    if board_type == "industry":
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1",
            "pz": "500",
            "po": "1",
            "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2",
            "invt": "2",
            "fid": "f3",
            "fs": "m:90 t:2 f:!50",
            "fields": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,f20,f21,"
            "f23,f24,f25,f26,f22,f33,f11,f62,f128,f136,f115,f152,f124,f107,f104,f105,"
            "f140,f141,f207,f208,f209,f222",
        }
        temp_df = _fetch_clist_pages(url, params)
        temp_df.columns = [
            "排名",
            "-",
            "最新价",
            "涨跌幅",
            "涨跌额",
            "-",
            "_",
            "-",
            "换手率",
            "-",
            "-",
            "-",
            "板块代码",
            "-",
            "板块名称",
            "-",
            "-",
            "-",
            "-",
            "总市值",
            "-",
            "-",
            "-",
            "-",
            "-",
            "-",
            "-",
            "-",
            "上涨家数",
            "下跌家数",
            "-",
            "-",
            "-",
            "领涨股票",
            "-",
            "-",
            "领涨股票-涨跌幅",
            "-",
            "-",
            "-",
            "-",
            "-",
        ]
    else:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1",
            "pz": "500",
            "po": "1",
            "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2",
            "invt": "2",
            "fid": "f12",
            "fs": "m:90 t:3 f:!50",
            "fields": (
                "f2,f3,f4,f8,f12,f14,f15,f16,f17,f18,f20,f21,f24,"
                "f25,f22,f33,f11,f62,f128,f124,f107,f104,f105,f136"
            ),
        }
        temp_df = _fetch_clist_pages(url, params)
        temp_df.columns = [
            "排名",
            "最新价",
            "涨跌幅",
            "涨跌额",
            "换手率",
            "_",
            "板块代码",
            "板块名称",
            "_",
            "_",
            "_",
            "_",
            "总市值",
            "_",
            "_",
            "_",
            "_",
            "_",
            "_",
            "上涨家数",
            "下跌家数",
            "_",
            "_",
            "领涨股票",
            "_",
            "_",
            "领涨股票-涨跌幅",
        ]
    return temp_df[["排名", "板块名称", "板块代码"]]


def _fetch_yjbb(date: str, *, limit: int | None) -> pd.DataFrame:
    params = {
        "sortColumns": "UPDATE_DATE,SECURITY_CODE",
        "sortTypes": "-1,-1",
        "pageSize": "500",
        "pageNumber": "1",
        "reportName": "RPT_LICO_FN_CPD",
        "columns": "ALL",
        "filter": f"(REPORTDATE='{_format_report_date(date)}')",
    }
    big_df = _fetch_datacenter_pages(
        "https://datacenter-web.eastmoney.com/api/data/v1/get",
        params,
        max_rows=limit,
    )
    big_df.reset_index(inplace=True)
    big_df["index"] = range(1, len(big_df) + 1)
    big_df.columns = [
        "序号",
        "股票代码",
        "股票简称",
        "_",
        "_",
        "_",
        "_",
        "最新公告日期",
        "_",
        "每股收益",
        "_",
        "营业总收入-营业总收入",
        "净利润-净利润",
        "净资产收益率",
        "营业总收入-同比增长",
        "净利润-同比增长",
        "每股净资产",
        "每股经营现金流量",
        "销售毛利率",
        "营业总收入-季度环比增长",
        "净利润-季度环比增长",
        "_",
        "_",
        "所处行业",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
    ]
    big_df = big_df[
        [
            "序号",
            "股票代码",
            "股票简称",
            "每股收益",
            "营业总收入-营业总收入",
            "营业总收入-同比增长",
            "营业总收入-季度环比增长",
            "净利润-净利润",
            "净利润-同比增长",
            "净利润-季度环比增长",
            "每股净资产",
            "净资产收益率",
            "每股经营现金流量",
            "销售毛利率",
            "所处行业",
            "最新公告日期",
        ]
    ]
    _to_numeric(
        big_df,
        [
            "每股收益",
            "营业总收入-营业总收入",
            "营业总收入-同比增长",
            "营业总收入-季度环比增长",
            "净利润-净利润",
            "净利润-同比增长",
            "净利润-季度环比增长",
            "每股净资产",
            "净资产收益率",
            "每股经营现金流量",
            "销售毛利率",
        ],
    )
    big_df["最新公告日期"] = pd.to_datetime(big_df["最新公告日期"], errors="coerce").dt.date
    return big_df


def _fetch_yjkb(date: str, *, limit: int | None) -> pd.DataFrame:
    params = {
        "sortColumns": "UPDATE_DATE,SECURITY_CODE",
        "sortTypes": "-1,-1",
        "pageSize": "500",
        "pageNumber": "1",
        "reportName": "RPT_FCI_PERFORMANCEE",
        "columns": "ALL",
        "filter": (
            '(SECURITY_TYPE_CODE in ("058001001","058001008"))'
            '(TRADE_MARKET_CODE!="069001017")'
            f"(REPORT_DATE='{_format_report_date(date)}')"
        ),
    }
    big_df = _fetch_datacenter_pages(
        "https://datacenter.eastmoney.com/securities/api/data/v1/get",
        params,
        max_rows=limit,
    )
    big_df.reset_index(inplace=True)
    big_df["index"] = range(1, len(big_df) + 1)
    big_df.columns = [
        "序号",
        "股票代码",
        "股票简称",
        "市场板块",
        "_",
        "证券类型",
        "_",
        "公告日期",
        "_",
        "每股收益",
        "营业收入-营业收入",
        "营业收入-去年同期",
        "净利润-净利润",
        "净利润-去年同期",
        "每股净资产",
        "净资产收益率",
        "营业收入-同比增长",
        "净利润-同比增长",
        "营业收入-季度环比增长",
        "净利润-季度环比增长",
        "所处行业",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
        "_",
    ]
    big_df = big_df[
        [
            "序号",
            "股票代码",
            "股票简称",
            "每股收益",
            "营业收入-营业收入",
            "营业收入-去年同期",
            "营业收入-同比增长",
            "营业收入-季度环比增长",
            "净利润-净利润",
            "净利润-去年同期",
            "净利润-同比增长",
            "净利润-季度环比增长",
            "每股净资产",
            "净资产收益率",
            "所处行业",
            "公告日期",
        ]
    ]
    _to_numeric(
        big_df,
        [
            "每股收益",
            "营业收入-营业收入",
            "营业收入-去年同期",
            "营业收入-同比增长",
            "营业收入-季度环比增长",
            "净利润-净利润",
            "净利润-去年同期",
            "净利润-同比增长",
            "净利润-季度环比增长",
            "每股净资产",
            "净资产收益率",
        ],
    )
    big_df["公告日期"] = pd.to_datetime(big_df["公告日期"], errors="coerce").dt.date
    return big_df


def _fetch_yjyg(date: str, *, limit: int | None) -> pd.DataFrame:
    params = {
        "sortColumns": "NOTICE_DATE,SECURITY_CODE",
        "sortTypes": "-1,-1",
        "pageSize": "500",
        "pageNumber": "1",
        "reportName": "RPT_PUBLIC_OP_NEWPREDICT",
        "columns": "ALL",
        "filter": f" (REPORT_DATE='{_format_report_date(date)}')",
    }
    big_df = _fetch_datacenter_pages(
        "https://datacenter.eastmoney.com/securities/api/data/v1/get",
        params,
        max_rows=limit,
    )
    big_df.reset_index(inplace=True)
    big_df["index"] = range(1, len(big_df) + 1)
    big_df.columns = [
        "序号",
        "_",
        "股票代码",
        "股票简称",
        "_",
        "公告日期",
        "报告日期",
        "_",
        "预测指标",
        "_",
        "_",
        "_",
        "_",
        "业绩变动",
        "业绩变动原因",
        "预告类型",
        "上年同期值",
        "_",
        "_",
        "_",
        "_",
        "业绩变动幅度",
        "预测数值",
        "_",
        "_",
        "_",
        "_",
        "_",
    ]
    big_df = big_df[
        [
            "序号",
            "股票代码",
            "股票简称",
            "预测指标",
            "业绩变动",
            "预测数值",
            "业绩变动幅度",
            "业绩变动原因",
            "预告类型",
            "上年同期值",
            "公告日期",
        ]
    ]
    big_df["公告日期"] = pd.to_datetime(big_df["公告日期"], errors="coerce").dt.date
    return _to_numeric(big_df, ["业绩变动幅度", "预测数值", "上年同期值"])


def _fetch_clist_pages(
    url: str,
    params: JsonDict,
    *,
    sort_by: str | None = "f3",
    max_rows: int | None = None,
) -> pd.DataFrame:
    first_page = _curl_get_json(url, params=params)
    data = first_page.get("data") or {}
    rows = data.get("diff") or []
    if not rows:
        return pd.DataFrame()
    total = int(data.get("total") or len(rows))
    page_size = max(len(rows), 1)
    total_page = math.ceil(total / page_size)
    page_rows = [pd.DataFrame(rows)]
    for page in range(2, total_page + 1):
        if max_rows is not None and sum(len(item) for item in page_rows) >= max_rows:
            break
        page_params = params | {"pn": page}
        data_json = _curl_get_json(url, params=page_params)
        inner_rows = (data_json.get("data") or {}).get("diff") or []
        if not inner_rows:
            break
        page_rows.append(pd.DataFrame(inner_rows))
    temp_df = pd.concat(page_rows, ignore_index=True)
    if sort_by and sort_by in temp_df.columns:
        temp_df[sort_by] = pd.to_numeric(temp_df[sort_by], errors="coerce")
        temp_df.sort_values(by=[sort_by], ascending=False, inplace=True, ignore_index=True)
    if max_rows is not None:
        temp_df = temp_df.head(max_rows)
    temp_df.reset_index(inplace=True)
    temp_df["index"] = temp_df["index"].astype(int) + 1
    return temp_df


def _fetch_datacenter_pages(
    url: str,
    params: JsonDict,
    *,
    max_rows: int | None = None,
) -> pd.DataFrame:
    first_page = _curl_get_json(url, params=params)
    result = first_page.get("result") or {}
    total_page = int(result.get("pages") or 0)
    if total_page <= 0:
        return pd.DataFrame()
    page_rows: list[pd.DataFrame] = []
    for page in range(1, total_page + 1):
        if max_rows is not None and sum(len(item) for item in page_rows) >= max_rows:
            break
        page_params = params | {"pageNumber": page}
        data_json = _curl_get_json(url, params=page_params)
        rows = ((data_json.get("result") or {}).get("data")) or []
        if not rows:
            continue
        page_rows.append(pd.DataFrame(rows))
    if not page_rows:
        return pd.DataFrame()
    big_df = pd.concat(page_rows, ignore_index=True)
    return big_df.head(max_rows) if max_rows is not None else big_df


def _curl_get_json(url: str, *, params: JsonDict) -> JsonDict:
    response = curl_requests.get(
        url,
        params=params,
        timeout=20,
        impersonate="chrome120",
        headers=eastmoney_headers(),
    )
    response.raise_for_status()
    return response.json()


def _curl_get_json_any(urls: list[str], *, params: JsonDict) -> JsonDict:
    """按顺序尝试多个 Eastmoney 子域名，全部失败时抛出最后一个错误。"""

    last_error: Exception | None = None
    for url in urls:
        try:
            return _curl_get_json(url, params=params)
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise ValueError("未提供可请求的 Eastmoney URL")


def _rename_fund_flow_columns(temp_df: pd.DataFrame, *, indicator: str) -> pd.DataFrame:
    if indicator == "今日":
        temp_df.columns = [
            "序号",
            "最新价",
            "今日涨跌幅",
            "代码",
            "名称",
            "今日主力净流入-净额",
            "今日超大单净流入-净额",
            "今日超大单净流入-净占比",
            "今日大单净流入-净额",
            "今日大单净流入-净占比",
            "今日中单净流入-净额",
            "今日中单净流入-净占比",
            "今日小单净流入-净额",
            "今日小单净流入-净占比",
            "_",
            "今日主力净流入-净占比",
            "_",
            "_",
            "_",
        ]
        columns = [
            "序号",
            "代码",
            "名称",
            "最新价",
            "今日涨跌幅",
            "今日主力净流入-净额",
            "今日主力净流入-净占比",
            "今日超大单净流入-净额",
            "今日超大单净流入-净占比",
            "今日大单净流入-净额",
            "今日大单净流入-净占比",
            "今日中单净流入-净额",
            "今日中单净流入-净占比",
            "今日小单净流入-净额",
            "今日小单净流入-净占比",
        ]
    elif indicator == "3日":
        temp_df.columns = [
            "序号",
            "最新价",
            "代码",
            "名称",
            "_",
            "3日涨跌幅",
            "_",
            "_",
            "_",
            "3日主力净流入-净额",
            "3日主力净流入-净占比",
            "3日超大单净流入-净额",
            "3日超大单净流入-净占比",
            "3日大单净流入-净额",
            "3日大单净流入-净占比",
            "3日中单净流入-净额",
            "3日中单净流入-净占比",
            "3日小单净流入-净额",
            "3日小单净流入-净占比",
        ]
        columns = [
            "序号",
            "代码",
            "名称",
            "最新价",
            "3日涨跌幅",
            "3日主力净流入-净额",
            "3日主力净流入-净占比",
            "3日超大单净流入-净额",
            "3日超大单净流入-净占比",
            "3日大单净流入-净额",
            "3日大单净流入-净占比",
            "3日中单净流入-净额",
            "3日中单净流入-净占比",
            "3日小单净流入-净额",
            "3日小单净流入-净占比",
        ]
    elif indicator == "5日":
        temp_df.columns = [
            "序号",
            "最新价",
            "代码",
            "名称",
            "5日涨跌幅",
            "_",
            "5日主力净流入-净额",
            "5日主力净流入-净占比",
            "5日超大单净流入-净额",
            "5日超大单净流入-净占比",
            "5日大单净流入-净额",
            "5日大单净流入-净占比",
            "5日中单净流入-净额",
            "5日中单净流入-净占比",
            "5日小单净流入-净额",
            "5日小单净流入-净占比",
            "_",
            "_",
            "_",
        ]
        columns = [
            "序号",
            "代码",
            "名称",
            "最新价",
            "5日涨跌幅",
            "5日主力净流入-净额",
            "5日主力净流入-净占比",
            "5日超大单净流入-净额",
            "5日超大单净流入-净占比",
            "5日大单净流入-净额",
            "5日大单净流入-净占比",
            "5日中单净流入-净额",
            "5日中单净流入-净占比",
            "5日小单净流入-净额",
            "5日小单净流入-净占比",
        ]
    else:
        temp_df.columns = [
            "序号",
            "最新价",
            "代码",
            "名称",
            "_",
            "10日涨跌幅",
            "10日主力净流入-净额",
            "10日主力净流入-净占比",
            "10日超大单净流入-净额",
            "10日超大单净流入-净占比",
            "10日大单净流入-净额",
            "10日大单净流入-净占比",
            "10日中单净流入-净额",
            "10日中单净流入-净占比",
            "10日小单净流入-净额",
            "10日小单净流入-净占比",
            "_",
            "_",
            "_",
            "_",
        ]
        columns = [
            "序号",
            "代码",
            "名称",
            "最新价",
            "10日涨跌幅",
            "10日主力净流入-净额",
            "10日主力净流入-净占比",
            "10日超大单净流入-净额",
            "10日超大单净流入-净占比",
            "10日大单净流入-净额",
            "10日大单净流入-净占比",
            "10日中单净流入-净额",
            "10日中单净流入-净占比",
            "10日小单净流入-净额",
            "10日小单净流入-净占比",
        ]
    return temp_df[columns]


def _to_numeric(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    for column in columns:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    return df


def _format_report_date(date: str) -> str:
    return "-".join([date[:4], date[4:6], date[6:]])


def _ths_get_text(url: str, *, referer: str) -> str:
    response = curl_requests.get(
        url,
        headers={
            "Accept": "text/html, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Host": "data.10jqka.com.cn",
            "Pragma": "no-cache",
            "Referer": referer,
            "User-Agent": EASTMONEY_HEADERS["User-Agent"],
            "X-Requested-With": "XMLHttpRequest",
            "hexin-v": _ths_v_code(),
        },
        timeout=20,
        impersonate="chrome120",
    )
    response.raise_for_status()
    return response.text


def _ths_v_code() -> str:
    import py_mini_racer
    from akshare.datasets import get_ths_js

    js_code = py_mini_racer.MiniRacer()
    with open(get_ths_js("ths.js"), encoding="utf-8") as file:
        js_code.eval(file.read())
    return str(js_code.call("v"))


def _parse_ths_page_count(html: str) -> int:
    match = re.search(r'<span class="page_info">\s*\d+/(\d+)\s*</span>', html)
    if not match:
        return 1
    return int(match.group(1))


def _read_ths_html_tables(html: str) -> list[pd.DataFrame]:
    """读取同花顺 HTML 表格，便于隔离测试替换网络报文解析。"""

    return pd.read_html(StringIO(html))


def _normalize_ths_stock_code(value: object) -> str:
    """恢复同花顺 HTML 表格读取时丢失的 A 股代码前导零。"""

    text = str(value).strip()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text.zfill(6) if text.isdigit() else text
