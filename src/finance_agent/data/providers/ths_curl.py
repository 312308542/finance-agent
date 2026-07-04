"""同花顺 curl_cffi 备用数据源。

当前 AKShare 暴露了同花顺行业/概念名称、简介和指数接口，但本环境中的
AKShare 包没有暴露同花顺板块成分接口。这里用同花顺公开详情页做轻量
fallback：只采集详情页首屏成分，用于东财板块成分接口断连时继续生成
候选池种子。
"""

from __future__ import annotations

import re
from functools import lru_cache
from io import StringIO
from typing import Any

import pandas as pd
from curl_cffi import requests as curl_requests

JsonDict = dict[str, Any]

THS_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    ),
}

BOARD_LINK_RE = re.compile(
    r'href=["\'][^"\']*/(?:thshy|gn)/detail/code/(\d+)/?["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)


def fetch_industry_members(symbol: str, *, limit: int | None = None) -> pd.DataFrame:
    """获取同花顺行业详情页首屏成分股。"""

    board_code = symbol if _looks_like_ths_board_code(symbol) else _find_board_code(
        symbol,
        board_type="industry",
    )
    url = f"http://q.10jqka.com.cn/thshy/detail/code/{board_code}/"
    df = _fetch_detail_members(
        url,
        referer="http://q.10jqka.com.cn/thshy/",
        limit=limit,
    )
    df.attrs["actual_source"] = "ths:curl_cffi:stock_board_industry_detail_first_page"
    df.attrs["source_coverage"] = "first_page"
    df.attrs["board_code"] = board_code
    return df


def fetch_concept_members(symbol: str, *, limit: int | None = None) -> pd.DataFrame:
    """获取同花顺概念详情页首屏成分股。"""

    board_code = symbol if _looks_like_ths_board_code(symbol) else _find_board_code(
        symbol,
        board_type="concept",
    )
    url = f"http://q.10jqka.com.cn/gn/detail/code/{board_code}/"
    df = _fetch_detail_members(
        url,
        referer="http://q.10jqka.com.cn/gn/",
        limit=limit,
    )
    df.attrs["actual_source"] = "ths:curl_cffi:stock_board_concept_detail_first_page"
    df.attrs["source_coverage"] = "first_page"
    df.attrs["board_code"] = board_code
    return df


def fetch_industry_names(*, limit: int | None = None) -> pd.DataFrame:
    """获取同花顺行业板块目录。"""

    board_map = _fetch_board_name_map("industry")
    df = _board_name_map_to_frame(board_map)
    if limit is not None:
        df = df.head(limit)
    result = df.reset_index(drop=True)
    result.attrs["actual_source"] = "ths:curl_cffi:stock_board_industry_name"
    return result


def fetch_concept_names(*, limit: int | None = None) -> pd.DataFrame:
    """获取同花顺概念板块目录。"""

    board_map = _fetch_concept_name_map_from_summary()
    df = _board_name_map_to_frame(board_map)
    if limit is not None:
        df = df.head(limit)
    result = df.reset_index(drop=True)
    result.attrs["actual_source"] = "ths:curl_cffi:stock_board_concept_name"
    return result


def _fetch_detail_members(
    url: str,
    *,
    referer: str,
    limit: int | None,
) -> pd.DataFrame:
    html = _q_ths_get_text(url, referer=referer)
    tables = pd.read_html(StringIO(html))
    for table in tables:
        columns = {str(column) for column in table.columns}
        if {"代码", "名称"}.issubset(columns):
            df = table.copy()
            if "代码" in df.columns:
                df["代码"] = (
                    df["代码"]
                    .astype(str)
                    .str.replace(r"\.0$", "", regex=True)
                    .str.zfill(6)
                )
            if limit is not None:
                df = df.head(limit)
            return df.reset_index(drop=True)
    raise ValueError(f"同花顺详情页未找到成分表: {url}")


def _find_board_code(symbol: str, *, board_type: str) -> str:
    symbol = symbol.strip()
    board_map = _fetch_board_name_map(board_type)
    if symbol in board_map:
        return board_map[symbol]

    if board_type == "concept":
        summary_code = _find_concept_code_from_summary(symbol)
        if summary_code:
            return summary_code
    raise ValueError(f"未找到同花顺{board_type}板块代码: {symbol}")


@lru_cache(maxsize=2)
def _fetch_board_name_map(board_type: str) -> dict[str, str]:
    if board_type == "industry":
        url = "https://q.10jqka.com.cn/thshy/detail/code/881272/"
        referer = "https://q.10jqka.com.cn/thshy/"
    elif board_type == "concept":
        url = "https://q.10jqka.com.cn/gn/detail/code/307822/"
        referer = "https://q.10jqka.com.cn/gn/"
    else:
        raise ValueError(f"不支持的同花顺板块类型: {board_type}")

    html = _q_ths_get_text(url, referer=referer)
    return _extract_board_links(html)


def _find_concept_code_from_summary(symbol: str) -> str | None:
    """从同花顺概念时间表中按名称查找代码。

    部分较新的概念不在概念分类详情页首屏链接中，需要查概念列表页。
    这里按页顺序查找，命中即停，避免每次都全量拉取。
    """

    first_url = "http://q.10jqka.com.cn/gn/index/field/addtime/order/desc/page/1/ajax/1/"
    first_html = _q_ths_get_text(first_url, referer="http://q.10jqka.com.cn/gn/")
    first_links = _extract_board_links(first_html)
    if symbol in first_links:
        return first_links[symbol]

    page_count = _parse_page_count(first_html)
    for page in range(2, page_count + 1):
        url = f"http://q.10jqka.com.cn/gn/index/field/addtime/order/desc/page/{page}/ajax/1/"
        html = _q_ths_get_text(url, referer="http://q.10jqka.com.cn/gn/")
        links = _extract_board_links(html)
        if symbol in links:
            return links[symbol]
    return None


def _fetch_concept_name_map_from_summary() -> dict[str, str]:
    """从同花顺概念列表页全量展开概念名称到代码的映射。"""

    first_url = "http://q.10jqka.com.cn/gn/index/field/addtime/order/desc/page/1/ajax/1/"
    first_html = _q_ths_get_text(first_url, referer="http://q.10jqka.com.cn/gn/")
    board_map = _extract_board_links(first_html)
    page_count = _parse_page_count(first_html)
    for page in range(2, page_count + 1):
        url = f"http://q.10jqka.com.cn/gn/index/field/addtime/order/desc/page/{page}/ajax/1/"
        html = _q_ths_get_text(url, referer="http://q.10jqka.com.cn/gn/")
        board_map.update(_extract_board_links(html))
    return board_map


def _board_name_map_to_frame(board_map: dict[str, str]) -> pd.DataFrame:
    """把名称到代码的映射转换为与 AKShare/东财一致的目录表结构。"""

    rows = [
        {"板块名称": name, "板块代码": code}
        for name, code in board_map.items()
        if str(name).strip()
    ]
    return pd.DataFrame(rows, columns=["板块名称", "板块代码"])


def _extract_board_links(html: str) -> dict[str, str]:
    links: dict[str, str] = {}
    for code, raw_name in BOARD_LINK_RE.findall(html):
        name = re.sub(r"<.*?>", "", raw_name).strip()
        if name:
            links[name] = code
    return links


def _q_ths_get_text(url: str, *, referer: str) -> str:
    response = curl_requests.get(
        url,
        headers=THS_HEADERS | {"Referer": referer},
        timeout=20,
        impersonate="chrome120",
    )
    response.raise_for_status()
    return _decode_ths_response(response.content)


def _decode_ths_response(content: bytes) -> str:
    for encoding in ("gbk", "utf-8"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("gbk", errors="ignore")


def _looks_like_ths_board_code(symbol: str) -> bool:
    return bool(re.fullmatch(r"\d{6}", symbol.strip()))


def _parse_page_count(html: str) -> int:
    match = re.search(r'<span class="page_info">\s*\d+/(\d+)\s*</span>', html)
    if not match:
        return 1
    return int(match.group(1))
