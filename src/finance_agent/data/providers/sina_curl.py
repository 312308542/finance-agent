"""新浪行业与概念板块 curl_cffi 免费数据源。"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any

import pandas as pd
from curl_cffi import requests as curl_requests

JsonDict = dict[str, Any]

SINA_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://finance.sina.com.cn/stock/sl/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    ),
}

SINA_CATALOG_URL = "https://money.finance.sina.com.cn/q/view/newFLJK.php"
SINA_LEGACY_INDUSTRY_CATALOG_URL = (
    "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
)
SINA_DETAIL_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)


def fetch_industry_names(*, limit: int | None = None) -> pd.DataFrame:
    """获取新浪行业分类目录。"""

    return _copy_catalog("industry", limit=limit)


def fetch_concept_names(*, limit: int | None = None) -> pd.DataFrame:
    """获取新浪概念分类目录。"""

    return _copy_catalog("concept", limit=limit)


def has_industry(symbol: str) -> bool:
    """判断行业名称或标签是否存在于新浪目录。"""

    return _has_sector(symbol, board_type="industry")


def has_concept(symbol: str) -> bool:
    """判断概念名称或标签是否存在于新浪目录。"""

    return _has_sector(symbol, board_type="concept")


def fetch_industry_members(symbol: str, *, limit: int | None = None) -> pd.DataFrame:
    """按行业名称或新浪标签获取完整成员。"""

    return _fetch_sector_members(symbol, board_type="industry", limit=limit)


def fetch_concept_members(symbol: str, *, limit: int | None = None) -> pd.DataFrame:
    """按概念名称或新浪标签获取完整成员。"""

    return _fetch_sector_members(symbol, board_type="concept", limit=limit)


def _copy_catalog(board_type: str, *, limit: int | None) -> pd.DataFrame:
    catalog = _fetch_sector_catalog(board_type).copy()
    if limit is not None:
        catalog = catalog.head(limit)
    result = catalog.reset_index(drop=True)
    if board_type == "industry":
        result.attrs.update(
            {
                "actual_source": (
                    "sina:curl_cffi:stock_sector_spot:industry_combined"
                ),
                "sources": [
                    "sina:curl_cffi:stock_sector_spot:industry",
                    "sina:curl_cffi:stock_sector_spot:sina_industry",
                ],
            }
        )
    else:
        result.attrs["actual_source"] = f"sina:curl_cffi:stock_sector_spot:{board_type}"
    return result


@lru_cache(maxsize=2)
def _fetch_sector_catalog(board_type: str) -> pd.DataFrame:
    if board_type == "industry":
        official = _fetch_sector_catalog("industry_official")
        legacy = _fetch_sector_catalog("sina_industry")
        return (
            pd.concat([official, legacy], ignore_index=True)
            .drop_duplicates(subset=["板块名称"], keep="first")
            .reset_index(drop=True)
        )
    if board_type == "industry_official":
        indicator = "industry"
        url = SINA_CATALOG_URL
        params: JsonDict | None = {"param": indicator}
    elif board_type == "sina_industry":
        url = SINA_LEGACY_INDUSTRY_CATALOG_URL
        params = None
    elif board_type == "concept":
        indicator = "class"
        url = SINA_CATALOG_URL
        params = {"param": indicator}
    else:
        raise ValueError(f"不支持的新浪板块类型: {board_type}")

    text = _sina_get_text(url, params=params)
    payload = _decode_json_value(text, expected_type=dict)
    rows: list[JsonDict] = []
    for fallback_label, raw_value in payload.items():
        fields = str(raw_value or "").split(",")
        if len(fields) < 3:
            continue
        board_code = str(fields[0] or fallback_label).strip()
        board_name = str(fields[1]).strip()
        if not board_code or not board_name:
            continue
        rows.append(
            {
                "板块名称": board_name,
                "板块代码": board_code,
                "公司家数": int(float(fields[2] or 0)),
            }
        )
    if not rows:
        raise ValueError(f"新浪{board_type}目录未返回有效板块")
    return pd.DataFrame(rows, columns=["板块名称", "板块代码", "公司家数"])


def _fetch_sector_members(
    symbol: str,
    *,
    board_type: str,
    limit: int | None,
) -> pd.DataFrame:
    catalog = _fetch_sector_catalog(board_type)
    board_code, expected_count = _resolve_board(catalog, symbol)
    rows = _fetch_member_rows(board_code=board_code, expected_count=expected_count)
    fetched_count = len(rows)
    count_mismatch = fetched_count != expected_count
    if count_mismatch and not board_code.startswith("new_"):
        raise ValueError(
            f"新浪板块 {symbol} 目录声明 {expected_count}，只返回 {fetched_count}"
        )

    frame = pd.DataFrame(rows)
    if frame.empty:
        frame = pd.DataFrame(columns=["代码", "名称"])
    else:
        frame.rename(columns={"code": "代码", "name": "名称"}, inplace=True)
        frame["代码"] = (
            frame["代码"]
            .astype(str)
            .str.replace(r"^(?:sh|sz|bj)", "", regex=True, flags=re.IGNORECASE)
            .str.replace(r"\.0$", "", regex=True)
            .str.zfill(6)
        )
    if limit is not None:
        frame = frame.head(limit)
    result = frame.reset_index(drop=True)
    detail_source = "sina_industry" if board_code.startswith("new_") else board_type
    if count_mismatch:
        source_coverage = "partial"
    elif limit is not None and limit < fetched_count:
        source_coverage = "limited"
    else:
        source_coverage = "full"
    result.attrs.update(
        {
            "actual_source": f"sina:curl_cffi:stock_sector_detail:{detail_source}",
            "source_coverage": source_coverage,
            "board_code": board_code,
            "expected_count": expected_count,
            "fetched_count": fetched_count,
        }
    )
    return result


def _fetch_member_rows(*, board_code: str, expected_count: int) -> list[JsonDict]:
    rows: list[JsonDict] = []
    seen_codes: set[str] = set()
    for page in range(1, 1001):
        text = _sina_get_text(
            SINA_DETAIL_URL,
            params={
                "page": str(page),
                "num": "5000",
                "sort": "symbol",
                "asc": "1",
                "node": board_code,
                "symbol": "",
                "_s_r_a": "page",
            },
        )
        page_rows = _decode_json_value(text, expected_type=list)
        if not page_rows:
            break
        added = 0
        for row in page_rows:
            code = str(row.get("code") or row.get("symbol") or "").strip()
            dedupe_key = code or json.dumps(row, ensure_ascii=True, sort_keys=True)
            if dedupe_key in seen_codes:
                continue
            seen_codes.add(dedupe_key)
            rows.append(row)
            added += 1
        if len(rows) >= expected_count or added == 0:
            break
    return rows


def _resolve_board(catalog: pd.DataFrame, symbol: str) -> tuple[str, int]:
    normalized = symbol.strip()
    matches = catalog[
        (catalog["板块名称"].astype(str) == normalized)
        | (catalog["板块代码"].astype(str) == normalized)
    ]
    if matches.empty:
        raise KeyError(f"新浪目录未找到板块: {symbol}")
    row = matches.iloc[0]
    return str(row["板块代码"]), int(row["公司家数"])


def _has_sector(symbol: str, *, board_type: str) -> bool:
    catalog = _fetch_sector_catalog(board_type)
    normalized = symbol.strip()
    return bool(
        (
            (catalog["板块名称"].astype(str) == normalized)
            | (catalog["板块代码"].astype(str) == normalized)
        ).any()
    )


def _decode_json_value(text: str, *, expected_type: type[Any]) -> Any:
    start = min(
        (position for position in (text.find("{"), text.find("[")) if position >= 0),
        default=-1,
    )
    if start < 0:
        raise ValueError("新浪报文未找到 JSON 数据")
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(value, expected_type):
        raise ValueError(f"新浪报文类型异常: {type(value).__name__}")
    return value


def _sina_get_text(url: str, *, params: JsonDict | None = None) -> str:
    response = curl_requests.get(
        url,
        params=params,
        headers=SINA_HEADERS,
        timeout=20,
        impersonate="chrome120",
    )
    response.raise_for_status()
    return _decode_sina_response(response.content)


def _decode_sina_response(content: bytes) -> str:
    for encoding in ("utf-8", "gbk"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="ignore")
