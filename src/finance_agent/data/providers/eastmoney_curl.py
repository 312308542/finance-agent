"""东方财富 curl_cffi 兜底接口。

AKShare 某些东方财富接口在当前网络下会被上游断开普通 requests 连接。
这里保留仓库侧 fallback：复用 AKShare 的公开接口参数和字段映射，只替换
传输层为 curl_cffi，不修改虚拟环境中的 AKShare 源码。
"""

from __future__ import annotations

import math
import re
from io import StringIO
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


def fetch_fund_flow_rank(indicator: str, *, limit: int | None = None) -> pd.DataFrame:
    """获取个股资金流排名。"""

    try:
        return _fetch_eastmoney_fund_flow_rank(indicator, limit=limit)
    except Exception:
        return _fetch_ths_fund_flow_rank(indicator, limit=limit)


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
        headers=EASTMONEY_HEADERS | {"Referer": "https://guba.eastmoney.com/rank/"},
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

    first_url = "http://data.10jqka.com.cn/funds/ggzjl/field/code/order/desc/ajax/1/free/1/"
    first_html = _ths_get_text(first_url, referer="http://data.10jqka.com.cn/funds/ggzjl/")
    page_count = _parse_ths_page_count(first_html)
    page_size = 60
    max_pages = page_count
    if limit is not None:
        max_pages = min(max_pages, max(math.ceil(limit / page_size), 1))

    path = board_map[indicator]
    url_template = (
        f"http://data.10jqka.com.cn/funds/ggzjl/{path}"
        "field/zdf/order/desc/page/{}/ajax/1/free/1/"
    )
    frames: list[pd.DataFrame] = []
    for page in range(1, max_pages + 1):
        html = _ths_get_text(url_template.format(page), referer="http://data.10jqka.com.cn/funds/ggzjl/")
        tables = pd.read_html(StringIO(html))
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
        headers=EASTMONEY_HEADERS,
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
