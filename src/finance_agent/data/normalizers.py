"""第三方数据源归一化函数。"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from hashlib import sha1
from typing import Any

import pandas as pd

from finance_agent.data.models import (
    AssetData,
    CapitalFlowSnapshotData,
    CryptoDerivativeSnapshotData,
    EventRecordData,
    EvidenceData,
    FundNavSnapshotData,
    FundamentalSnapshotData,
    MarketBarData,
    RiskFindingData,
    UniverseSeedData,
)


def to_decimal(value: Any) -> Decimal:
    """把第三方返回的数值安全转成 Decimal。"""

    if value is None or pd.isna(value):
        return Decimal("0")
    return Decimal(str(value).replace(",", ""))


def nullable_decimal(value: Any) -> Decimal | None:
    """把第三方可缺失数值安全转成 Decimal 或 None。"""

    if value is None or pd.isna(value):
        return None
    normalized = str(value).replace(",", "").strip()
    if not normalized or normalized in {"-", "--"}:
        return None
    try:
        return Decimal(normalized)
    except Exception:
        return None


def normalize_ashare_spot(df: pd.DataFrame, *, limit: int | None = None) -> list[AssetData]:
    """归一化 AKShare A 股实时列表。"""

    assets: list[AssetData] = []
    rows = df.head(limit) if limit else df
    for row in rows.to_dict("records"):
        symbol = str(row.get("代码", "")).strip()
        if not symbol:
            continue
        tradable = _ashare_spot_row_is_tradable(row)
        assets.append(
            AssetData(
                asset_id=f"ashare:{symbol}",
                symbol=symbol,
                name=str(row.get("名称", symbol)).strip(),
                market="ashare",
                asset_type="stock",
                exchange=infer_ashare_exchange(symbol),
                currency="CNY",
                tradable=tradable,
                status="available" if tradable else "unavailable",
                payload={"raw": row},
            )
        )
    return assets


def _ashare_spot_row_is_tradable(row: dict[str, Any]) -> bool:
    """根据实时行情行判断是否仍有可交易报价。"""

    if "最新价" not in row:
        return True
    return nullable_decimal(row.get("最新价")) is not None


def normalize_ashare_spot_tx(df: pd.DataFrame, *, limit: int | None = None) -> list[AssetData]:
    """归一化腾讯 A 股实时列表。"""

    assets: list[AssetData] = []
    rows = df.head(limit) if limit else df
    for row in rows.to_dict("records"):
        raw_code = str(row.get("code", "")).strip().lower()
        symbol = strip_ashare_exchange_prefix(raw_code)
        if not symbol:
            continue
        assets.append(
            AssetData(
                asset_id=f"ashare:{symbol}",
                symbol=symbol,
                name=str(row.get("name", symbol)).strip(),
                market="ashare",
                asset_type="stock",
                exchange=(
                    infer_ashare_exchange_from_prefixed(raw_code) or infer_ashare_exchange(symbol)
                ),
                currency="CNY",
                tradable=True,
                payload={"raw": row, "source_symbol": raw_code},
            )
        )
    return assets


def normalize_ashare_code_name(df: pd.DataFrame, *, limit: int | None = None) -> list[AssetData]:
    """归一化 AKShare 全 A 代码名册。"""

    assets: list[AssetData] = []
    rows = df.head(limit) if limit else df
    for row in rows.to_dict("records"):
        symbol = normalize_ashare_symbol(str(row.get("code", "")).strip())
        if not symbol:
            continue
        name = str(row.get("name", symbol)).strip() or symbol
        assets.append(
            AssetData(
                asset_id=f"ashare:{symbol}",
                symbol=symbol,
                name=name,
                market="ashare",
                asset_type="stock",
                exchange=infer_ashare_exchange(symbol),
                currency="CNY",
                tradable=True,
                payload={"raw": row},
            )
        )
    return assets


def normalize_ashare_hist(
    df: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    source: str,
    adjustment: str,
    is_closed: bool = True,
    status: str = "available",
) -> list[MarketBarData]:
    """归一化 AKShare A 股历史行情。"""

    bars: list[MarketBarData] = []
    for row in df.to_dict("records"):
        timestamp = pd.Timestamp(row["日期"]).to_pydatetime().replace(tzinfo=UTC)
        bars.append(
            MarketBarData(
                asset_id=f"ashare:{symbol}",
                symbol=symbol,
                market="ashare",
                timeframe=timeframe,
                timestamp=timestamp,
                open_price=to_decimal(row.get("开盘")),
                high=to_decimal(row.get("最高")),
                low=to_decimal(row.get("最低")),
                close=to_decimal(row.get("收盘")),
                volume=to_decimal(row.get("成交量")),
                amount=to_decimal(row.get("成交额")),
                source=source,
                adjustment=adjustment,
                is_closed=is_closed,
                status=status,
            )
        )
    return bars


def normalize_ashare_hist_tx(
    df: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    source: str,
    adjustment: str,
    is_closed: bool = True,
    status: str = "available",
) -> list[MarketBarData]:
    """归一化腾讯 A 股历史行情。

    腾讯历史行情返回的 `amount` 更接近成交量字段，但官方列名只叫 amount。
    为避免把语义写错，第一版同时写入 `volume` 和 `amount`，并在
    `source` 中保留腾讯来源，后续可按真实含义再做字段修正。
    """

    bars: list[MarketBarData] = []
    clean_symbol = strip_ashare_exchange_prefix(symbol)
    for row in df.to_dict("records"):
        timestamp = pd.Timestamp(row["date"]).to_pydatetime().replace(tzinfo=UTC)
        amount = to_decimal(row.get("amount"))
        bars.append(
            MarketBarData(
                asset_id=f"ashare:{clean_symbol}",
                symbol=clean_symbol,
                market="ashare",
                timeframe=timeframe,
                timestamp=timestamp,
                open_price=to_decimal(row.get("open")),
                high=to_decimal(row.get("high")),
                low=to_decimal(row.get("low")),
                close=to_decimal(row.get("close")),
                volume=amount,
                amount=amount,
                source=source,
                adjustment=adjustment,
                is_closed=is_closed,
                status=status,
            )
        )
    return bars


def normalize_fund_etf_spot_em(df: pd.DataFrame, *, limit: int | None = None) -> list[AssetData]:
    """归一化东方财富 ETF 实时列表。"""

    return _normalize_fund_spot_assets(df, asset_type="etf", limit=limit)


def normalize_fund_lof_spot_em(df: pd.DataFrame, *, limit: int | None = None) -> list[AssetData]:
    """归一化东方财富 LOF 实时列表。"""

    return _normalize_fund_spot_assets(df, asset_type="lof", limit=limit)


def _is_exchange_traded_fund_row(row: dict[str, Any]) -> bool:
    """识别被开放式基金接口混入的场内 ETF/LOF 行。"""

    name = str(_first_present(row, ["基金简称", "基金名称", "名称", "fund_name"]) or "").strip().upper()
    purchase_status = str(_first_present(row, ["申购状态", "purchase_status"]) or "").strip()
    redeem_status = str(_first_present(row, ["赎回状态", "redeem_status"]) or "").strip()
    if "ETF" in name or "LOF" in name:
        return True
    return "场内交易" in purchase_status or "场内交易" in redeem_status


def normalize_fund_open_fund_daily_em(
    df: pd.DataFrame,
    *,
    limit: int | None = None,
) -> list[AssetData]:
    """归一化开放式基金日净值列表为资产身份。"""

    assets: list[AssetData] = []
    rows = df.head(limit) if limit else df
    for row in rows.to_dict("records"):
        symbol = str(
            _first_present(
                row,
                ["基金代码", "代码", "基金编号", "fund_code"],
            )
            or ""
        ).strip()
        if not symbol:
            continue
        if _is_exchange_traded_fund_row(row):
            continue
        name = str(
            _first_present(
                row,
                ["基金简称", "基金名称", "名称", "fund_name"],
            )
            or symbol
        ).strip()
        assets.append(
            AssetData(
                asset_id=f"fund:open:{symbol}",
                symbol=symbol,
                name=name,
                market="fund",
                asset_type="open_fund",
                exchange=None,
                currency="CNY",
                tradable=True,
                payload={"raw": row},
            )
        )
    return assets


def normalize_fund_etf_hist_em(
    df: pd.DataFrame,
    *,
    symbol: str,
    asset_type: str = "etf",
    timeframe: str,
    source: str,
    is_closed: bool = True,
    status: str = "available",
) -> list[MarketBarData]:
    """归一化 ETF 历史日 K。"""

    return _normalize_fund_hist(
        df,
        symbol=symbol,
        asset_type=asset_type,
        timeframe=timeframe,
        source=source,
        is_closed=is_closed,
        status=status,
    )


def normalize_fund_lof_hist_em(
    df: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    source: str,
    is_closed: bool = True,
    status: str = "available",
) -> list[MarketBarData]:
    """归一化 LOF 历史日 K。"""

    return _normalize_fund_hist(
        df,
        symbol=symbol,
        asset_type="lof",
        timeframe=timeframe,
        source=source,
        is_closed=is_closed,
        status=status,
    )


def normalize_fund_open_nav_em(
    df: pd.DataFrame,
    *,
    symbol: str,
    source: str,
    limit: int | None = None,
) -> list[FundNavSnapshotData]:
    """归一化开放式基金历史净值。"""

    snapshots: list[FundNavSnapshotData] = []
    rows = df.head(limit) if limit else df
    for row in rows.to_dict("records"):
        nav_date = parse_fund_nav_date(
            _first_present(
                row,
                ["净值日期", "日期", "净值时间", "trade_date"],
            )
        )
        if nav_date is None:
            continue
        snapshots.append(
            FundNavSnapshotData(
                snapshot_id=stable_id("fund_nav", source, symbol, nav_date.isoformat()),
                asset_id=f"fund:open:{symbol}",
                symbol=symbol,
                market="fund",
                source=source,
                nav_date=nav_date,
                unit_nav=_first_decimal(
                    row,
                    ["单位净值", "最新净值", "净值"],
                ),
                accumulated_nav=_first_decimal(
                    row,
                    ["累计净值", "累计单位净值"],
                ),
                daily_return=_normalize_percent_decimal(
                    _first_present(row, ["日增长率", "日涨跌幅", "涨跌幅"])
                ),
                purchase_status=_normalize_optional_text(
                    _first_present(row, ["申购状态", "申购", "购买状态"])
                ),
                redeem_status=_normalize_optional_text(
                    _first_present(row, ["赎回状态", "赎回", "卖出状态"])
                ),
                status="available",
                payload={"raw": row},
            )
        )
    return snapshots


def normalize_ashare_board_members(
    df: pd.DataFrame,
    *,
    source_name: str,
    source_type: str,
    as_of: datetime,
    limit: int | None = None,
) -> list[UniverseSeedData]:
    """归一化 AKShare 行业/概念板块成分。"""

    seeds: list[UniverseSeedData] = []
    rows = df.head(limit) if limit else df
    for index, row in enumerate(rows.to_dict("records"), start=1):
        symbol = _first_present(row, ["代码", "股票代码", "code"])
        symbol = strip_ashare_exchange_prefix(str(symbol or ""))
        if not symbol:
            continue
        name = str(_first_present(row, ["名称", "股票名称", "name"]) or symbol).strip()
        seeds.append(
            UniverseSeedData(
                seed_id=stable_id("seed", source_type, source_name, symbol),
                source_name=source_name,
                source_type=source_type,
                symbol=symbol,
                name=name,
                market="ashare",
                asset_id=f"ashare:{symbol}",
                rank_hint=index,
                as_of=as_of,
                payload={"raw": row},
            )
        )
    return seeds


def normalize_ashare_index_members(
    df: pd.DataFrame,
    *,
    index_code: str,
    index_name: str,
    source: str,
    as_of: datetime,
    limit: int | None = None,
) -> list[UniverseSeedData]:
    """归一化 AKShare 指数成分股。"""

    seeds: list[UniverseSeedData] = []
    rows = df.head(limit) if limit else df
    for index, row in enumerate(rows.to_dict("records"), start=1):
        raw_symbol = _first_present(row, ["成分券代码", "品种代码", "代码", "股票代码"])
        symbol = strip_ashare_exchange_prefix(str(raw_symbol or ""))
        if not symbol:
            continue
        name = str(
            _first_present(row, ["成分券名称", "品种名称", "名称", "股票名称"])
            or symbol
        ).strip()
        effective_index_name = str(
            _first_present(row, ["指数名称"]) or index_name or index_code
        ).strip()
        seeds.append(
            UniverseSeedData(
                seed_id=stable_id("seed", "index", index_code, symbol),
                source_name=effective_index_name,
                source_type="index",
                symbol=symbol,
                name=name,
                market="ashare",
                asset_id=f"ashare:{symbol}",
                rank_hint=index,
                as_of=parse_ashare_datetime(_first_present(row, ["日期", "纳入日期"])) or as_of,
                payload={
                    "raw": row,
                    "index_code": index_code,
                    "index_name": effective_index_name,
                    "source": source,
                },
            )
        )
    return seeds


def normalize_ashare_fund_flow_rank(
    df: pd.DataFrame,
    *,
    source: str,
    window: str,
    as_of: datetime,
    limit: int | None = None,
) -> list[CapitalFlowSnapshotData]:
    """归一化 AKShare 个股资金流排名。"""

    snapshots: list[CapitalFlowSnapshotData] = []
    rows = df.head(limit) if limit else df
    rank_total = len(rows.index)
    for index, row in enumerate(rows.to_dict("records"), start=1):
        symbol = normalize_ashare_symbol(str(_first_present(row, ["代码", "股票代码"]) or ""))
        if not is_main_board_ashare_stock_symbol(symbol):
            continue
        main_net_inflow = _first_decimal(
            row,
            [
                "主力净流入",
                "今日主力净流入-净额",
                "5日主力净流入-净额",
                "10日主力净流入-净额",
                "净额",
            ],
        )
        amount = _first_decimal(row, ["成交额", "今日成交额"])
        turnover_rate = _first_decimal(row, ["换手率", "今日换手率"])
        snapshots.append(
            CapitalFlowSnapshotData(
                snapshot_id=stable_id("capital_flow", source, window, symbol, as_of.isoformat()),
                asset_id=f"ashare:{symbol}",
                symbol=symbol,
                market="ashare",
                window=window,
                source=source,
                as_of=as_of,
                main_net_inflow=main_net_inflow,
                turnover_rate=turnover_rate,
                amount=amount,
                status="available",
                payload={"raw": row, "rank_hint": index, "rank_total": rank_total},
            )
        )
    return snapshots


def normalize_ashare_northbound_market_flow(
    df: pd.DataFrame,
    *,
    source: str,
    symbol: str,
    as_of: datetime,
    limit: int | None = None,
) -> list[CapitalFlowSnapshotData]:
    """归一化沪深港通市场级北向资金历史。"""

    snapshots: list[CapitalFlowSnapshotData] = []
    rows = df.tail(limit) if limit else df
    for row in rows.to_dict("records"):
        snapshot_at = parse_ashare_datetime(_first_present(row, ["日期", "数据日期"])) or as_of
        northbound_net_inflow = _first_decimal(
            row,
            [
                "北向资金",
                "当日资金流入",
                "当日成交净买额",
                "净买额",
                "资金净流入",
            ],
        )
        snapshots.append(
            CapitalFlowSnapshotData(
                snapshot_id=stable_id("capital_flow", source, symbol, snapshot_at.isoformat()),
                asset_id="market:ashare:northbound",
                symbol="northbound",
                market="ashare",
                window="daily",
                source=source,
                as_of=snapshot_at,
                northbound_net_inflow=northbound_net_inflow,
                status="available" if northbound_net_inflow is not None else "partial",
                payload={"raw": row, "source_symbol": symbol},
            )
        )
    return snapshots


def normalize_ashare_northbound_individual_flow(
    df: pd.DataFrame,
    *,
    source: str,
    symbol: str,
    as_of: datetime,
    limit: int | None = None,
) -> list[CapitalFlowSnapshotData]:
    """归一化沪深港通个股持仓/资金数据。"""

    snapshots: list[CapitalFlowSnapshotData] = []
    rows = df.tail(limit) if limit else df
    for row in rows.to_dict("records"):
        clean_symbol = normalize_ashare_symbol(
            str(_first_present(row, ["股票代码", "证券代码", "代码"]) or symbol)
        )
        if not is_main_board_ashare_stock_symbol(clean_symbol):
            continue
        snapshot_at = (
            parse_ashare_datetime(_first_present(row, ["持股日期", "日期", "数据日期"])) or as_of
        )
        northbound_net_inflow = _first_decimal(
            row,
            [
                "当日净买额",
                "净买额",
                "买入成交净额",
                "今日增持资金",
                "持股市值变化-1日",
                "今日持股市值变化",
                "持股市值变化",
            ],
        )
        amount = _first_decimal(row, ["持股市值", "持股金额", "市值"])
        snapshots.append(
            CapitalFlowSnapshotData(
                snapshot_id=stable_id(
                    "capital_flow",
                    source,
                    clean_symbol,
                    snapshot_at.isoformat(),
                ),
                asset_id=f"ashare:{clean_symbol}",
                symbol=clean_symbol,
                market="ashare",
                window="daily",
                source=source,
                as_of=snapshot_at,
                northbound_net_inflow=northbound_net_inflow,
                amount=amount,
                status="available"
                if northbound_net_inflow is not None or amount is not None
                else "partial",
                payload={"raw": row, "source_symbol": symbol},
            )
        )
    return snapshots


def normalize_ashare_stock_news(
    df: pd.DataFrame,
    *,
    symbol: str,
    source: str,
    collected_at: datetime,
    limit: int | None = None,
) -> tuple[list[EventRecordData], list[EvidenceData]]:
    """归一化 AKShare 个股新闻。"""

    events: list[EventRecordData] = []
    evidence: list[EvidenceData] = []
    clean_symbol = strip_ashare_exchange_prefix(symbol)
    rows = df.head(limit) if limit else df
    for row in rows.to_dict("records"):
        title = str(_first_present(row, ["新闻标题", "标题", "title"]) or "").strip()
        if not title:
            continue
        summary = str(_first_present(row, ["新闻内容", "摘要", "内容"]) or "").strip() or None
        url = str(_first_present(row, ["新闻链接", "链接", "url"]) or "").strip() or None
        published_at = parse_ashare_datetime(_first_present(row, ["发布时间", "时间", "日期"]))
        event_id = stable_id("event", source, clean_symbol, title, published_at or collected_at)
        evidence_id = stable_id("evidence", source, event_id)
        events.append(
            EventRecordData(
                event_id=event_id,
                asset_id=f"ashare:{clean_symbol}",
                symbol=clean_symbol,
                market="ashare",
                event_type="news",
                title=title[:255],
                summary=summary,
                sentiment="unknown",
                importance="medium",
                source=source,
                url=url,
                published_at=published_at,
                collected_at=collected_at,
                payload={"raw": row},
            )
        )
        evidence.append(
            EvidenceData(
                evidence_id=evidence_id,
                evidence_type="news",
                asset_id=f"ashare:{clean_symbol}",
                source=source,
                title=title[:255],
                summary=summary,
                data_ref=event_id,
                url=url,
                reliability="medium",
                as_of=published_at,
                collected_at=collected_at,
                payload={"event_id": event_id},
            )
        )
    return events, evidence


def normalize_ashare_notice_reports(
    df: pd.DataFrame,
    *,
    source: str,
    collected_at: datetime,
    limit: int | None = None,
) -> tuple[list[EventRecordData], list[EvidenceData]]:
    """归一化 AKShare 公告披露。"""

    events: list[EventRecordData] = []
    evidence: list[EvidenceData] = []
    rows = df.head(limit) if limit else df
    for row in rows.to_dict("records"):
        symbol = strip_ashare_exchange_prefix(str(_first_present(row, ["代码", "股票代码"]) or ""))
        title = str(_first_present(row, ["公告标题", "标题"]) or "").strip()
        if not title:
            continue
        url = str(_first_present(row, ["公告链接", "链接", "url"]) or "").strip() or None
        published_at = parse_ashare_datetime(_first_present(row, ["公告日期", "发布时间", "日期"]))
        asset_id = f"ashare:{symbol}" if symbol else None
        event_id = stable_id(
            "event",
            source,
            symbol or "market",
            title,
            published_at or collected_at,
        )
        evidence_id = stable_id("evidence", source, event_id)
        events.append(
            EventRecordData(
                event_id=event_id,
                asset_id=asset_id,
                symbol=symbol or None,
                market="ashare",
                event_type="announcement",
                title=title[:255],
                summary=None,
                sentiment="unknown",
                importance="medium",
                source=source,
                url=url,
                published_at=published_at,
                collected_at=collected_at,
                payload={"raw": row},
            )
        )
        evidence.append(
            EvidenceData(
                evidence_id=evidence_id,
                evidence_type="announcement",
                asset_id=asset_id,
                source=source,
                title=title[:255],
                summary=None,
                data_ref=event_id,
                url=url,
                reliability="high",
                as_of=published_at,
                collected_at=collected_at,
                payload={"event_id": event_id},
            )
        )
    return events, evidence


def normalize_ashare_financial_indicator(
    df: pd.DataFrame,
    *,
    symbol: str,
    source: str,
    as_of: datetime,
    limit: int | None = None,
) -> list[FundamentalSnapshotData]:
    """归一化 AKShare 东方财富主要财务指标。"""

    snapshots: list[FundamentalSnapshotData] = []
    rows = df.head(limit) if limit else df
    for row in rows.to_dict("records"):
        clean_symbol = normalize_ashare_symbol(
            str(_first_present(row, ["SECURITY_CODE", "股票代码", "代码"]) or symbol)
        )
        if not clean_symbol:
            continue
        report_at = parse_ashare_datetime(_first_present(row, ["REPORT_DATE", "报告期", "日期"]))
        report_period = report_at.strftime("%Y%m%d") if report_at else None
        revenue_growth_yoy = _first_decimal(row, ["TOTALOPERATEREVETZ", "营业总收入同比增长"])
        net_profit_growth_yoy = _first_decimal(row, ["PARENTNETPROFITTZ", "净利润同比增长"])
        roe = _first_decimal(row, ["ROE_DILUTED", "净资产收益率", "加权净资产收益率"])
        operating_cashflow = _first_decimal(row, ["PER_NETCASH", "每股经营性现金流"])
        missing_fields = _missing_fields(
            {
                "roe": roe,
                "revenue_growth_yoy": revenue_growth_yoy,
                "net_profit_growth_yoy": net_profit_growth_yoy,
                "operating_cashflow": operating_cashflow,
            }
        )
        snapshots.append(
            FundamentalSnapshotData(
                snapshot_id=stable_id(
                    "fundamental",
                    source,
                    clean_symbol,
                    report_period or as_of.isoformat(),
                ),
                asset_id=f"ashare:{clean_symbol}",
                symbol=clean_symbol,
                report_period=report_period,
                roe=roe,
                revenue_growth_yoy=revenue_growth_yoy,
                net_profit_growth_yoy=net_profit_growth_yoy,
                operating_cashflow=operating_cashflow,
                source=source,
                status="partial" if missing_fields else "available",
                missing_fields=missing_fields,
                as_of=report_at or as_of,
                payload={"raw": row},
            )
        )
    return snapshots


def normalize_ashare_valuation(
    df: pd.DataFrame,
    *,
    symbol: str,
    source: str,
    as_of: datetime,
    limit: int | None = None,
) -> list[FundamentalSnapshotData]:
    """归一化 AKShare 个股估值序列。"""

    snapshots: list[FundamentalSnapshotData] = []
    clean_symbol = normalize_ashare_symbol(symbol)
    rows = df.tail(limit) if limit else df
    for row in rows.to_dict("records"):
        value_at = parse_ashare_datetime(_first_present(row, ["数据日期", "日期"]))
        pe_ttm = _first_decimal(row, ["PE(TTM)", "市盈率TTM", "滚动市盈率"])
        pb = _first_decimal(row, ["市净率", "PB"])
        missing_fields = _missing_fields({"pe_ttm": pe_ttm, "pb": pb})
        snapshots.append(
            FundamentalSnapshotData(
                snapshot_id=stable_id(
                    "fundamental",
                    source,
                    clean_symbol,
                    value_at.isoformat() if value_at else as_of.isoformat(),
                ),
                asset_id=f"ashare:{clean_symbol}",
                symbol=clean_symbol,
                pe_ttm=pe_ttm,
                pb=pb,
                source=source,
                status="partial" if missing_fields else "available",
                missing_fields=missing_fields,
                as_of=value_at or as_of,
                payload={
                    "raw": row,
                    "market_value": _first_decimal(row, ["总市值"]),
                    "float_market_value": _first_decimal(row, ["流通市值"]),
                    "ps": _first_decimal(row, ["市销率"]),
                    "pcf": _first_decimal(row, ["市现率"]),
                    "peg": _first_decimal(row, ["PEG值"]),
                },
            )
        )
    return snapshots


def normalize_ashare_performance_report(
    df: pd.DataFrame,
    *,
    source: str,
    as_of: datetime,
    limit: int | None = None,
) -> list[FundamentalSnapshotData]:
    """归一化 AKShare 业绩报表/快报/预告列表。"""

    snapshots: list[FundamentalSnapshotData] = []
    rows = df.head(limit) if limit else df
    for row in rows.to_dict("records"):
        symbol = normalize_ashare_symbol(
            str(_first_present(row, ["股票代码", "SECURITY_CODE", "代码"]) or "")
        )
        if not symbol:
            continue
        report_at = parse_ashare_datetime(
            _first_present(row, ["REPORT_DATE", "报告期", "公告日期", "最新公告日期"])
        )
        report_period = report_at.strftime("%Y%m%d") if report_at else None
        revenue_growth_yoy = _first_decimal(
            row,
            ["营业收入-同比增长", "营业总收入同比增长", "TOTALOPERATEREVETZ"],
        )
        net_profit_growth_yoy = _first_decimal(
            row,
            ["净利润-同比增长", "归属净利润同比增长", "PARENTNETPROFITTZ"],
        )
        missing_fields = _missing_fields(
            {
                "revenue_growth_yoy": revenue_growth_yoy,
                "net_profit_growth_yoy": net_profit_growth_yoy,
            }
        )
        snapshots.append(
            FundamentalSnapshotData(
                snapshot_id=stable_id(
                    "fundamental",
                    source,
                    symbol,
                    report_period or as_of.isoformat(),
                ),
                asset_id=f"ashare:{symbol}",
                symbol=symbol,
                report_period=report_period,
                revenue_growth_yoy=revenue_growth_yoy,
                net_profit_growth_yoy=net_profit_growth_yoy,
                source=source,
                status="partial" if missing_fields else "available",
                missing_fields=missing_fields,
                as_of=report_at or as_of,
                payload={"raw": row},
            )
        )
    return snapshots


def normalize_ashare_dividend_yield(
    df: pd.DataFrame,
    *,
    source: str,
    as_of: datetime,
    limit: int | None = None,
) -> list[FundamentalSnapshotData]:
    """归一化 AKShare A 股股息率数据。"""

    snapshots: list[FundamentalSnapshotData] = []
    rows = df.head(limit) if limit else df
    for row in rows.to_dict("records"):
        symbol = normalize_ashare_symbol(
            str(_first_present(row, ["股票代码", "代码", "symbol"]) or "")
        )
        if not symbol:
            continue
        snapshot_at = parse_ashare_datetime(_first_present(row, ["日期", "数据日期", "报告期"]))
        dividend_yield = _first_decimal(row, ["股息率", "股息率TTM", "股利支付率"])
        missing_fields = _missing_fields({"dividend_yield": dividend_yield})
        snapshots.append(
            FundamentalSnapshotData(
                snapshot_id=stable_id(
                    "fundamental",
                    source,
                    symbol,
                    snapshot_at.isoformat() if snapshot_at else as_of.isoformat(),
                ),
                asset_id=f"ashare:{symbol}",
                symbol=symbol,
                source=source,
                status="partial" if missing_fields else "available",
                missing_fields=missing_fields,
                as_of=snapshot_at or as_of,
                payload={"raw": row, "dividend_yield": dividend_yield},
            )
        )
    return snapshots


def normalize_ashare_stop_list(
    df: pd.DataFrame,
    *,
    source: str,
    collected_at: datetime,
    limit: int | None = None,
) -> tuple[list[RiskFindingData], list[EventRecordData]]:
    """归一化 A 股停牌/两网及退市列表为交易状态风险和事件。"""

    risks: list[RiskFindingData] = []
    events: list[EventRecordData] = []
    rows = df.head(limit) if limit else df
    for row in rows.to_dict("records"):
        symbol = normalize_ashare_symbol(str(_first_present(row, ["代码", "股票代码"]) or ""))
        if not symbol or not is_standard_ashare_stock_symbol(symbol):
            continue
        name = str(_first_present(row, ["名称", "股票简称", "股票名称"]) or symbol).strip()
        stop_at = parse_ashare_datetime(
            _first_present(row, ["停牌时间", "停牌日期", "日期", "公告日期"])
        )
        as_of = stop_at or collected_at
        reason = str(_first_present(row, ["停牌原因", "原因"]) or "交易状态异常").strip()
        title = f"{name}({symbol}) 交易状态异常"
        event_id = stable_id("event", source, "stop", symbol, as_of.isoformat())
        risk_id = stable_id("risk", source, "trading_status", symbol, as_of.isoformat())
        asset_id = f"ashare:{symbol}"
        events.append(
            EventRecordData(
                event_id=event_id,
                asset_id=asset_id,
                symbol=symbol,
                market="ashare",
                event_type="trading_status",
                title=title[:255],
                summary=reason,
                sentiment="negative",
                importance="high",
                source=source,
                published_at=stop_at,
                collected_at=collected_at,
                payload={"raw": row},
            )
        )
        risks.append(
            RiskFindingData(
                risk_id=risk_id,
                asset_id=asset_id,
                scope="asset",
                risk_type="trading_status",
                severity="high",
                score=Decimal("1.0"),
                title=title[:255],
                description=reason,
                as_of=as_of,
                evidence_ids=[event_id],
                payload={"raw": row, "event_id": event_id},
            )
        )
    return risks, events


def normalize_ashare_st_list(
    df: pd.DataFrame,
    *,
    source: str,
    collected_at: datetime,
    limit: int | None = None,
) -> tuple[list[RiskFindingData], list[EventRecordData]]:
    """归一化 A 股 ST/风险警示列表为回避风险。"""

    risks: list[RiskFindingData] = []
    events: list[EventRecordData] = []
    rows = df.head(limit) if limit else df
    for row in rows.to_dict("records"):
        symbol = normalize_ashare_symbol(str(_first_present(row, ["代码", "股票代码"]) or ""))
        if not symbol:
            continue
        name = str(_first_present(row, ["名称", "股票简称", "股票名称"]) or symbol).strip()
        risk_at = (
            parse_ashare_datetime(_first_present(row, ["日期", "公告日期", "交易日期"]))
            or collected_at
        )
        title = f"{name}({symbol}) 风险警示"
        description = str(_first_present(row, ["原因", "风险原因", "备注"]) or "ST/风险警示")
        asset_id = f"ashare:{symbol}"
        event_id = stable_id("event", source, "st_risk", symbol, risk_at.isoformat())
        risk_id = stable_id("risk", source, "st_risk", symbol, risk_at.isoformat())
        events.append(
            EventRecordData(
                event_id=event_id,
                asset_id=asset_id,
                symbol=symbol,
                market="ashare",
                event_type="st_risk",
                title=title[:255],
                summary=description,
                sentiment="negative",
                importance="high",
                source=source,
                published_at=risk_at,
                collected_at=collected_at,
                payload={"raw": row},
            )
        )
        risks.append(
            RiskFindingData(
                risk_id=risk_id,
                asset_id=asset_id,
                scope="asset",
                risk_type="st_risk",
                severity="high",
                score=Decimal("0.9"),
                title=title[:255],
                description=description,
                as_of=risk_at,
                evidence_ids=[event_id],
                payload={"raw": row, "event_id": event_id},
            )
        )
    return risks, events


def normalize_ashare_delist_list(
    df: pd.DataFrame,
    *,
    source: str,
    collected_at: datetime,
    limit: int | None = None,
) -> tuple[list[RiskFindingData], list[EventRecordData]]:
    """归一化 A 股退市相关列表为退市风险和事件。"""

    risks: list[RiskFindingData] = []
    events: list[EventRecordData] = []
    rows = df.head(limit) if limit else df
    for row in rows.to_dict("records"):
        symbol = normalize_ashare_symbol(
            str(_first_present(row, ["代码", "证券代码", "股票代码"]) or "")
        )
        if not symbol:
            continue
        name = str(
            _first_present(row, ["名称", "证券简称", "股票简称", "股票名称"]) or symbol
        ).strip()
        delist_at = (
            parse_ashare_datetime(
                _first_present(row, ["退市日期", "终止上市日期", "摘牌日期", "日期", "公告日期"])
            )
            or collected_at
        )
        reason = str(
            _first_present(row, ["原因", "退市原因", "终止上市原因", "备注"]) or "退市风险"
        )
        title = f"{name}({symbol}) 退市相关风险"
        asset_id = f"ashare:{symbol}"
        event_id = stable_id("event", source, "delist_risk", symbol, delist_at.isoformat())
        risk_id = stable_id("risk", source, "delist_risk", symbol, delist_at.isoformat())
        events.append(
            EventRecordData(
                event_id=event_id,
                asset_id=asset_id,
                symbol=symbol,
                market="ashare",
                event_type="delist_risk",
                title=title[:255],
                summary=reason,
                sentiment="negative",
                importance="high",
                source=source,
                published_at=delist_at,
                collected_at=collected_at,
                payload={"raw": row},
            )
        )
        risks.append(
            RiskFindingData(
                risk_id=risk_id,
                asset_id=asset_id,
                scope="asset",
                risk_type="delist_risk",
                severity="critical",
                score=Decimal("1.0"),
                title=title[:255],
                description=reason,
                as_of=delist_at,
                evidence_ids=[event_id],
                payload={"raw": row, "event_id": event_id},
            )
        )
    return risks, events


def normalize_ashare_restricted_release_detail(
    df: pd.DataFrame,
    *,
    source: str,
    collected_at: datetime,
    limit: int | None = None,
    risk_window_days: int = 30,
    risk_ratio_threshold: Decimal = Decimal("0.05"),
) -> tuple[list[RiskFindingData], list[EventRecordData]]:
    """归一化限售解禁详情为事件和临近解禁风险。"""

    risks: list[RiskFindingData] = []
    events: list[EventRecordData] = []
    rows = df.head(limit) if limit else df
    for row in rows.to_dict("records"):
        symbol = normalize_ashare_symbol(
            str(_first_present(row, ["股票代码", "证券代码", "代码"]) or "")
        )
        if not symbol or not is_main_board_ashare_stock_symbol(symbol):
            continue
        name = str(_first_present(row, ["股票简称", "证券简称", "名称"]) or symbol).strip()
        release_at = (
            parse_ashare_datetime(_first_present(row, ["解禁时间", "解禁日期", "日期"]))
            or collected_at
        )
        release_type = str(_first_present(row, ["限售股类型", "解禁类型"]) or "限售股解禁")
        release_quantity = _first_decimal(row, ["实际解禁数量", "解禁数量"])
        release_value = _first_decimal(row, ["实际解禁市值", "解禁市值"])
        release_ratio = _first_decimal(row, ["占解禁前流通市值比例", "占总股本比例", "占流通股比例"])
        asset_id = f"ashare:{symbol}"
        event_id = stable_id("event", source, "restricted_release", symbol, release_at.isoformat())
        summary = (
            f"{release_type}；实际解禁数量：{release_quantity}；"
            f"实际解禁市值：{release_value}；占解禁前流通市值比例：{release_ratio}。"
        )
        importance = (
            "high"
            if release_ratio is not None and release_ratio >= risk_ratio_threshold
            else "medium"
        )
        events.append(
            EventRecordData(
                event_id=event_id,
                asset_id=asset_id,
                symbol=symbol,
                market="ashare",
                event_type="restricted_release",
                title=f"{name}({symbol}) 限售股解禁",
                summary=summary,
                sentiment="negative",
                importance=importance,
                source=source,
                published_at=release_at,
                collected_at=collected_at,
                payload={
                    "raw": row,
                    "release_type": release_type,
                    "release_quantity": str(release_quantity)
                    if release_quantity is not None
                    else None,
                    "release_value": str(release_value) if release_value is not None else None,
                    "release_ratio": str(release_ratio) if release_ratio is not None else None,
                },
            )
        )
        days_until_release = (release_at.date() - collected_at.date()).days
        if (
            0 <= days_until_release <= risk_window_days
            and release_ratio is not None
            and release_ratio >= risk_ratio_threshold
        ):
            severity = "high" if release_ratio >= Decimal("0.05") else "medium"
            risks.append(
                RiskFindingData(
                    risk_id=stable_id(
                        "risk",
                        source,
                        "restricted_release",
                        symbol,
                        release_at.isoformat(),
                    ),
                    asset_id=asset_id,
                    scope="asset",
                    risk_type="restricted_release",
                    severity=severity,
                    score=Decimal("0.8") if severity == "high" else Decimal("0.45"),
                    title=f"{name}({symbol}) 临近限售股解禁",
                    description=summary,
                    as_of=release_at,
                    evidence_ids=[event_id],
                    payload={
                        "raw": row,
                        "event_id": event_id,
                        "days_until_release": days_until_release,
                        "release_ratio": str(release_ratio),
                    },
                )
            )
    return risks, events


def normalize_ashare_pledge_ratio(
    df: pd.DataFrame,
    *,
    source: str,
    collected_at: datetime,
    limit: int | None = None,
    risk_ratio_threshold: Decimal = Decimal("0.30"),
) -> list[RiskFindingData]:
    """归一化上市公司股权质押比例为资产级风险。"""

    risks: list[RiskFindingData] = []
    rows = df.head(limit) if limit else df
    for row in rows.to_dict("records"):
        symbol = normalize_ashare_symbol(
            str(_first_present(row, ["股票代码", "证券代码", "代码"]) or "")
        )
        if not symbol or not is_main_board_ashare_stock_symbol(symbol):
            continue
        name = str(_first_present(row, ["股票简称", "证券简称", "名称"]) or symbol).strip()
        pledge_ratio = _normalize_percent_ratio(
            _first_decimal(row, ["质押比例", "质押总比例", "股权质押比例"])
        )
        if pledge_ratio is None or pledge_ratio < risk_ratio_threshold:
            continue
        as_of = (
            parse_ashare_datetime(_first_present(row, ["交易日期", "统计日期", "日期"]))
            or collected_at
        )
        pledge_count = _first_decimal(row, ["质押笔数", "质押次数"])
        pledged_shares = _first_decimal(row, ["质押股数", "质押数量"])
        pledge_value = _first_decimal(row, ["质押市值", "质押金额"])
        industry = str(_first_present(row, ["所属行业", "行业"]) or "").strip() or None
        severity = "critical" if pledge_ratio >= Decimal("0.50") else "high"
        score = min(Decimal("1.0"), pledge_ratio * Decimal("2"))
        description = (
            f"股权质押比例 {pledge_ratio:.2%}，质押笔数 {pledge_count}，"
            f"质押市值 {pledge_value}。"
        )
        risks.append(
            RiskFindingData(
                risk_id=stable_id("risk", source, "pledge_ratio", symbol, as_of.isoformat()),
                asset_id=f"ashare:{symbol}",
                scope="asset",
                risk_type="pledge_ratio",
                severity=severity,
                score=score.quantize(Decimal("0.001")),
                title=f"{name}({symbol}) 股权质押比例偏高",
                description=description,
                as_of=as_of,
                payload={
                    "raw": row,
                    "pledge_ratio": str(pledge_ratio),
                    "pledge_count": str(pledge_count) if pledge_count is not None else None,
                    "pledged_shares": str(pledged_shares) if pledged_shares is not None else None,
                    "pledge_value": str(pledge_value) if pledge_value is not None else None,
                    "industry": industry,
                    "risk_ratio_threshold": str(risk_ratio_threshold),
                },
            )
        )
    return risks


def normalize_ashare_hot_rank(
    df: pd.DataFrame,
    *,
    source: str,
    as_of: datetime,
    limit: int | None = None,
) -> tuple[list[UniverseSeedData], list[EventRecordData]]:
    """归一化东方财富人气榜为热度种子和情绪事件。"""

    seeds: list[UniverseSeedData] = []
    events: list[EventRecordData] = []
    rows = df.head(limit) if limit else df
    for index, row in enumerate(rows.to_dict("records"), start=1):
        symbol = normalize_ashare_symbol(str(_first_present(row, ["代码", "股票代码"]) or ""))
        if not symbol:
            continue
        name = str(_first_present(row, ["股票名称", "名称"]) or symbol).strip()
        rank_hint = int(_first_present(row, ["当前排名", "排名"]) or index)
        asset_id = f"ashare:{symbol}"
        seeds.append(
            UniverseSeedData(
                seed_id=stable_id("seed", "sentiment_hot_rank", symbol, as_of.isoformat()),
                source_name="东方财富人气榜",
                source_type="sentiment_hot_rank",
                symbol=symbol,
                name=name,
                market="ashare",
                asset_id=asset_id,
                rank_hint=rank_hint,
                as_of=as_of,
                payload={"raw": row},
            )
        )
        events.append(
            EventRecordData(
                event_id=stable_id("event", source, "hot_rank", symbol, as_of.isoformat()),
                asset_id=asset_id,
                symbol=symbol,
                market="ashare",
                event_type="sentiment_hot_rank",
                title=f"{name}({symbol}) 人气榜排名第 {rank_hint}",
                summary="热度榜用于候选池种子和情绪拥挤度参考，不直接构成推荐。",
                sentiment="positive",
                importance="medium",
                source=source,
                published_at=as_of,
                collected_at=as_of,
                payload={"raw": row, "rank_hint": rank_hint},
            )
        )
    return seeds, events


def normalize_ashare_zt_pool(
    df: pd.DataFrame,
    *,
    date: str,
    source: str,
    collected_at: datetime,
    limit: int | None = None,
) -> tuple[list[UniverseSeedData], list[EventRecordData], list[RiskFindingData]]:
    """归一化涨停池为强势种子、情绪事件和过热风险。"""

    seeds: list[UniverseSeedData] = []
    events: list[EventRecordData] = []
    risks: list[RiskFindingData] = []
    rows = df.head(limit) if limit else df
    as_of = parse_ashare_datetime(date) or collected_at
    for index, row in enumerate(rows.to_dict("records"), start=1):
        symbol = normalize_ashare_symbol(str(_first_present(row, ["代码", "股票代码"]) or ""))
        if not symbol:
            continue
        name = str(_first_present(row, ["名称", "股票名称"]) or symbol).strip()
        asset_id = f"ashare:{symbol}"
        consecutive = _first_decimal(row, ["连板数"])
        burst_count = _first_decimal(row, ["炸板次数"])
        industry = _first_present(row, ["所属行业"])
        seeds.append(
            UniverseSeedData(
                seed_id=stable_id("seed", "zt_pool", symbol, as_of.isoformat()),
                source_name="涨停池",
                source_type="sentiment_zt_pool",
                symbol=symbol,
                name=name,
                market="ashare",
                asset_id=asset_id,
                rank_hint=index,
                as_of=as_of,
                payload={"raw": row},
            )
        )
        event_id = stable_id("event", source, "zt_pool", symbol, as_of.isoformat())
        events.append(
            EventRecordData(
                event_id=event_id,
                asset_id=asset_id,
                symbol=symbol,
                market="ashare",
                event_type="limit_up",
                title=f"{name}({symbol}) 进入涨停池",
                summary=(
                    f"连板数 {consecutive}，炸板次数 {burst_count}，行业 {industry}。"
                    "该事件用于短线情绪解释。"
                ),
                sentiment="positive",
                importance="medium",
                source=source,
                published_at=as_of,
                collected_at=collected_at,
                payload={"raw": row},
            )
        )
        if (consecutive is not None and consecutive >= Decimal("2")) or (
            burst_count is not None and burst_count > Decimal("0")
        ):
            severity = (
                "high"
                if consecutive is not None and consecutive >= Decimal("3")
                else "medium"
            )
            risks.append(
                RiskFindingData(
                    risk_id=stable_id(
                        "risk",
                        source,
                        "sentiment_crowding",
                        symbol,
                        as_of.isoformat(),
                    ),
                    asset_id=asset_id,
                    scope="asset",
                    risk_type="sentiment_crowding",
                    severity=severity,
                    score=Decimal("0.7") if severity == "high" else Decimal("0.45"),
                    title=f"{name}({symbol}) 短线情绪拥挤",
                    description="连续涨停或炸板记录提示短线交易拥挤，后续推荐需降低追高置信度。",
                    as_of=as_of,
                    evidence_ids=[event_id],
                    payload={"raw": row, "event_id": event_id},
                )
            )
    return seeds, events, risks


def normalize_ashare_lhb_detail(
    df: pd.DataFrame,
    *,
    source: str,
    collected_at: datetime,
    limit: int | None = None,
) -> tuple[list[RiskFindingData], list[EvidenceData]]:
    """归一化龙虎榜明细为异常交易风险和证据。"""

    risks: list[RiskFindingData] = []
    evidence: list[EvidenceData] = []
    rows = df.head(limit) if limit else df
    for row in rows.to_dict("records"):
        symbol = normalize_ashare_symbol(str(_first_present(row, ["代码", "股票代码"]) or ""))
        if not symbol or not is_standard_ashare_stock_symbol(symbol):
            continue
        name = str(_first_present(row, ["名称", "股票名称"]) or symbol).strip()
        as_of = parse_ashare_datetime(_first_present(row, ["上榜日", "日期"])) or collected_at
        reason = str(_first_present(row, ["上榜原因", "解读"]) or "龙虎榜上榜").strip()
        net_buy = _first_decimal(row, ["龙虎榜净买额"])
        turnover_ratio = _first_decimal(row, ["成交额占总成交比"])
        severity = (
            "high"
            if turnover_ratio is not None and turnover_ratio >= Decimal("30")
            else "medium"
        )
        evidence_id = stable_id("evidence", source, "lhb", symbol, as_of.isoformat(), reason)
        risk_id = stable_id("risk", source, "lhb_activity", symbol, as_of.isoformat(), reason)
        asset_id = f"ashare:{symbol}"
        summary = (
            f"龙虎榜原因：{reason}；净买额：{net_buy}；"
            f"成交额占总成交比：{turnover_ratio}。"
        )
        evidence.append(
            EvidenceData(
                evidence_id=evidence_id,
                evidence_type="lhb",
                asset_id=asset_id,
                source=source,
                title=f"{name}({symbol}) 龙虎榜记录",
                summary=summary,
                data_ref=risk_id,
                reliability="high",
                as_of=as_of,
                collected_at=collected_at,
                payload={"raw": row},
            )
        )
        risks.append(
            RiskFindingData(
                risk_id=risk_id,
                asset_id=asset_id,
                scope="asset",
                risk_type="lhb_activity",
                severity=severity,
                score=Decimal("0.65") if severity == "high" else Decimal("0.4"),
                title=f"{name}({symbol}) 龙虎榜交易活跃",
                description=summary,
                as_of=as_of,
                evidence_ids=[evidence_id],
                payload={"raw": row},
            )
        )
    return risks, evidence


def normalize_ashare_block_trades(
    df: pd.DataFrame,
    *,
    source: str,
    collected_at: datetime,
    limit: int | None = None,
) -> tuple[list[RiskFindingData], list[EvidenceData]]:
    """归一化大宗交易明细为折溢价风险和证据。"""

    risks: list[RiskFindingData] = []
    evidence: list[EvidenceData] = []
    rows = df.head(limit) if limit else df
    for row in rows.to_dict("records"):
        symbol = normalize_ashare_symbol(str(_first_present(row, ["证券代码", "代码"]) or ""))
        if not symbol:
            continue
        name = str(_first_present(row, ["证券简称", "名称"]) or symbol).strip()
        as_of = parse_ashare_datetime(_first_present(row, ["交易日期", "日期"])) or collected_at
        premium = _first_decimal(row, ["折溢率"])
        amount = _first_decimal(row, ["成交额"])
        severity = "medium"
        score = Decimal("0.35")
        if premium is not None and premium <= Decimal("-0.05"):
            severity = "high"
            score = Decimal("0.65")
        elif premium is not None and abs(premium) >= Decimal("0.03"):
            severity = "medium"
            score = Decimal("0.45")
        evidence_id = stable_id("evidence", source, "block_trade", symbol, as_of.isoformat())
        risk_id = stable_id("risk", source, "block_trade", symbol, as_of.isoformat())
        asset_id = f"ashare:{symbol}"
        summary = f"大宗交易成交额：{amount}；折溢率：{premium}。"
        evidence.append(
            EvidenceData(
                evidence_id=evidence_id,
                evidence_type="block_trade",
                asset_id=asset_id,
                source=source,
                title=f"{name}({symbol}) 大宗交易记录",
                summary=summary,
                data_ref=risk_id,
                reliability="high",
                as_of=as_of,
                collected_at=collected_at,
                payload={"raw": row},
            )
        )
        risks.append(
            RiskFindingData(
                risk_id=risk_id,
                asset_id=asset_id,
                scope="asset",
                risk_type="block_trade",
                severity=severity,
                score=score,
                title=f"{name}({symbol}) 大宗交易异动",
                description=summary,
                as_of=as_of,
                evidence_ids=[evidence_id],
                payload={"raw": row},
            )
        )
    return risks, evidence


def normalize_ashare_margin_summary(
    df: pd.DataFrame,
    *,
    source: str,
    market_scope: str,
    collected_at: datetime,
    limit: int | None = None,
) -> list[RiskFindingData]:
    """归一化交易所融资融券汇总为市场杠杆情绪风险。"""

    risks: list[RiskFindingData] = []
    rows = df.head(limit) if limit else df
    for row in rows.to_dict("records"):
        as_of = parse_ashare_datetime(_first_present(row, ["信用交易日期", "日期"])) or collected_at
        balance = _first_decimal(row, ["融资融券余额"])
        margin_balance = _first_decimal(row, ["融资余额"])
        short_balance = _first_decimal(row, ["融券余额", "融券余量金额"])
        risk_id = stable_id("risk", source, market_scope, as_of.isoformat())
        risks.append(
            RiskFindingData(
                risk_id=risk_id,
                asset_id=None,
                scope="market",
                risk_type="margin_leverage",
                severity="medium",
                score=Decimal("0.35"),
                title=f"{market_scope} 融资融券余额快照",
                description=(
                    f"融资融券余额：{balance}；融资余额：{margin_balance}；"
                    f"融券余额/余量金额：{short_balance}。"
                ),
                as_of=as_of,
                evidence_ids=[],
                payload={"raw": row, "market_scope": market_scope},
            )
        )
    return risks


def normalize_crypto_markets(
    markets: dict[str, dict[str, Any]],
    *,
    limit: int | None = None,
    market_type: str = "spot",
) -> list[AssetData]:
    """归一化 ccxt markets 为资产列表。"""

    assets: list[AssetData] = []
    market_name = "crypto_future" if market_type in {"future", "swap"} else "crypto_spot"
    selected = list(markets.values())
    if limit:
        selected = selected[:limit]

    for item in selected:
        symbol = compact_crypto_symbol(str(item.get("symbol") or ""))
        base_asset = item.get("base")
        quote_asset = item.get("quote")
        if not symbol or not base_asset or not quote_asset:
            continue
        active = bool(item.get("active", True))
        assets.append(
            AssetData(
                asset_id=f"{market_name}:{symbol}",
                symbol=symbol,
                name=f"{base_asset} / {quote_asset}",
                market=market_name,
                asset_type="crypto",
                exchange="Binance",
                currency=str(quote_asset),
                base_asset=str(base_asset),
                quote_asset=str(quote_asset),
                tradable=active,
                status="available" if active else "stale",
                payload={"raw": item},
            )
        )
    return assets


def normalize_crypto_ohlcv(
    rows: list[list[Any]],
    *,
    symbol: str,
    timeframe: str,
    source: str,
    market_type: str = "spot",
) -> list[MarketBarData]:
    """归一化 ccxt OHLCV。"""

    market_name = "crypto_future" if market_type in {"future", "swap"} else "crypto_spot"
    compact_symbol = compact_crypto_symbol(symbol)
    bars: list[MarketBarData] = []
    duration = timeframe_to_timedelta(timeframe)
    for timestamp_ms, open_price, high, low, close, volume in rows:
        timestamp = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
        bars.append(
            MarketBarData(
                asset_id=f"{market_name}:{compact_symbol}",
                symbol=compact_symbol,
                market=market_name,
                timeframe=timeframe,
                timestamp=timestamp,
                end_timestamp=timestamp + duration if duration else None,
                open_price=to_decimal(open_price),
                high=to_decimal(high),
                low=to_decimal(low),
                close=to_decimal(close),
                volume=to_decimal(volume),
                source=source,
                adjustment="",
                is_closed=True,
            )
        )
    return bars


def normalize_binance_derivative_snapshot(
    *,
    symbol: str,
    source: str,
    premium_index: dict[str, Any] | None,
    open_interest: dict[str, Any] | None,
    long_short_ratio: dict[str, Any] | None,
    collected_at: datetime,
) -> CryptoDerivativeSnapshotData:
    """归一化 Binance U 本位合约衍生品快照。"""

    compact_symbol = compact_crypto_symbol(symbol)
    premium_index = premium_index or {}
    open_interest = open_interest or {}
    long_short_ratio = long_short_ratio or {}

    premium_time = _datetime_from_milliseconds(premium_index.get("time"))
    oi_time = _datetime_from_milliseconds(open_interest.get("time"))
    ratio_time = _datetime_from_milliseconds(long_short_ratio.get("timestamp"))
    as_of = max(
        [value for value in [premium_time, oi_time, ratio_time, collected_at] if value is not None]
    )

    funding_rate = _nullable_decimal(premium_index.get("lastFundingRate"))
    mark_price = _nullable_decimal(premium_index.get("markPrice"))
    index_price = _nullable_decimal(premium_index.get("indexPrice"))
    oi_amount = _nullable_decimal(open_interest.get("openInterest"))
    open_interest_value = None
    if oi_amount is not None and mark_price is not None:
        open_interest_value = oi_amount * mark_price

    basis_rate = None
    if mark_price is not None and index_price not in {None, Decimal("0")}:
        basis_rate = (mark_price - index_price) / index_price

    snapshot_id = (
        f"crypto_derivative:{compact_symbol}:{source}:{as_of.strftime('%Y%m%dT%H%M%SZ')}"
    )
    return CryptoDerivativeSnapshotData(
        snapshot_id=snapshot_id,
        asset_id=f"crypto_future:{compact_symbol}",
        symbol=compact_symbol,
        market="crypto_future",
        source=source,
        as_of=as_of,
        funding_rate=funding_rate,
        next_funding_time=_datetime_from_milliseconds(premium_index.get("nextFundingTime")),
        open_interest=oi_amount,
        open_interest_value=open_interest_value,
        long_short_ratio=_nullable_decimal(long_short_ratio.get("longShortRatio")),
        basis_rate=basis_rate,
        liquidation_risk_score=None,
        status="available",
        payload={
            "schema_version": "1.0",
            "premium_index": premium_index,
            "open_interest": open_interest,
            "long_short_ratio": long_short_ratio,
        },
    )


def compact_crypto_symbol(symbol: str) -> str:
    """把 ccxt 的 BTC/USDT:USDT 等格式统一压缩为库内交易对 BTCUSDT。"""

    normalized = str(symbol or "").strip().upper().replace("/", "")
    if ":" in normalized:
        base_quote, settlement = normalized.split(":", 1)
        if "-" in settlement:
            normalized = f"{base_quote}-{settlement.split('-', 1)[1]}"
        else:
            normalized = base_quote
    return normalized


def infer_ashare_exchange(symbol: str) -> str:
    """根据 A 股代码推断交易所。"""

    if symbol.startswith(("6", "9")):
        return "SSE"
    if symbol.startswith(("0", "2", "3")):
        return "SZSE"
    if symbol.startswith(("4", "8")):
        return "BSE"
    return "UNKNOWN"


def stable_id(*parts: Any) -> str:
    """生成稳定短 ID。"""

    normalized = ":".join(str(part) for part in parts if part is not None)
    digest = sha1(normalized.encode("utf-8")).hexdigest()[:16]
    prefix = str(parts[0]) if parts else "id"
    return f"{prefix}:{digest}"


def _normalize_fund_spot_assets(
    df: pd.DataFrame,
    *,
    asset_type: str,
    limit: int | None = None,
) -> list[AssetData]:
    """归一化场内基金实时列表。"""

    assets: list[AssetData] = []
    rows = df.head(limit) if limit else df
    for row in rows.to_dict("records"):
        symbol = str(_first_present(row, ["代码", "基金代码", "symbol"]) or "").strip()
        if not symbol:
            continue
        name = str(_first_present(row, ["名称", "基金名称", "name"]) or symbol).strip()
        assets.append(
            AssetData(
                asset_id=f"fund:{asset_type}:{symbol}",
                symbol=symbol,
                name=name,
                market="fund",
                asset_type=asset_type,
                exchange=infer_fund_exchange(symbol),
                currency="CNY",
                tradable=True,
                payload={"raw": row},
            )
        )
    return assets


def _normalize_fund_hist(
    df: pd.DataFrame,
    *,
    symbol: str,
    asset_type: str,
    timeframe: str,
    source: str,
    is_closed: bool,
    status: str,
) -> list[MarketBarData]:
    """归一化场内基金历史 K 线。"""

    bars: list[MarketBarData] = []
    for row in df.to_dict("records"):
        timestamp_value = _first_present(row, ["日期", "date", "净值日期"])
        if timestamp_value is None:
            continue
        timestamp = pd.Timestamp(timestamp_value).to_pydatetime().replace(tzinfo=UTC)
        bars.append(
            MarketBarData(
                asset_id=f"fund:{asset_type}:{symbol}",
                symbol=symbol,
                market="fund",
                timeframe=timeframe,
                timestamp=timestamp,
                open_price=to_decimal(_first_present(row, ["开盘", "open"])),
                high=to_decimal(_first_present(row, ["最高", "high"])),
                low=to_decimal(_first_present(row, ["最低", "low"])),
                close=to_decimal(_first_present(row, ["收盘", "close"])),
                volume=to_decimal(_first_present(row, ["成交量", "volume"])),
                amount=nullable_decimal(_first_present(row, ["成交额", "amount"])),
                source=source,
                adjustment="",
                is_closed=is_closed,
                status=status,
            )
        )
    return bars


def infer_fund_exchange(symbol: str) -> str:
    """根据基金代码推断交易所。"""

    if symbol.startswith(("5", "6")):
        return "SSE"
    if symbol.startswith(("0", "1", "3", "4")):
        return "SZSE"
    return "UNKNOWN"


def parse_fund_nav_date(value: Any) -> date | None:
    """把基金净值日期解析为 date。"""

    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return pd.Timestamp(text).date()
    except Exception:
        return None


def _normalize_percent_decimal(value: Any) -> Decimal | None:
    """把 1.23% 或 1.23 转成 0.0123。"""

    if value is None or pd.isna(value):
        return None
    text = str(value).strip().replace("%", "")
    if not text or text in {"-", "--"}:
        return None
    try:
        return Decimal(text) / Decimal("100")
    except Exception:
        return None


def _normalize_optional_text(value: Any) -> str | None:
    """把可选文本字段转换为空或标准字符串。"""

    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def parse_ashare_datetime(value: Any) -> datetime | None:
    """解析 AKShare 常见日期时间字段。"""

    if value is None or pd.isna(value):
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    if isinstance(parsed, pd.Timestamp):
        if parsed.tzinfo is not None:
            return parsed.to_pydatetime().astimezone(UTC)
        return datetime.combine(parsed.date(), parsed.time() or time.min, tzinfo=UTC)
    return None


def infer_ashare_exchange_from_prefixed(symbol: str) -> str | None:
    """根据带市场前缀的 A 股代码推断交易所。"""

    normalized = symbol.lower().strip()
    if normalized.startswith("sh"):
        return "SSE"
    if normalized.startswith("sz"):
        return "SZSE"
    if normalized.startswith("bj"):
        return "BSE"
    return None


def with_ashare_exchange_prefix(symbol: str) -> str:
    """转换为腾讯接口需要的带交易所前缀代码。"""

    normalized = symbol.strip().lower()
    if normalized.startswith(("sh", "sz", "bj")):
        return normalized
    exchange = infer_ashare_exchange(normalized)
    if exchange == "SSE":
        return f"sh{normalized}"
    if exchange == "SZSE":
        return f"sz{normalized}"
    if exchange == "BSE":
        return f"bj{normalized}"
    return normalized


def strip_ashare_exchange_prefix(symbol: str) -> str:
    """去掉腾讯等接口返回的 A 股市场前缀。"""

    normalized = symbol.strip().lower()
    if normalized.startswith(("sh", "sz", "bj")):
        return normalized[2:]
    return normalized


def normalize_ashare_symbol(symbol: str) -> str:
    """统一 A 股代码格式，输出不带市场前后缀的 6 位代码。"""

    normalized = strip_ashare_exchange_prefix(symbol)
    for suffix in (".sh", ".sz", ".bj"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    return normalized.strip()


def is_standard_ashare_stock_symbol(symbol: str) -> bool:
    """判断是否为普通 A 股股票代码，过滤可转债、B 股等非股票标的。"""

    normalized = normalize_ashare_symbol(symbol)
    if len(normalized) != 6 or not normalized.isdigit():
        return False
    non_stock_prefixes = (
        "110",
        "111",
        "113",
        "118",
        "123",
        "127",
        "128",
        "200",
        "201",
        "202",
        "900",
    )
    if normalized.startswith(non_stock_prefixes):
        return False
    return normalized.startswith(("0", "3", "4", "6", "8", "920"))


def is_main_board_ashare_stock_symbol(symbol: str) -> bool:
    """判断是否为用户可交易范围内的 A 股主板股票代码。"""

    normalized = normalize_ashare_symbol(symbol)
    if not is_standard_ashare_stock_symbol(normalized):
        return False
    return normalized.startswith(("000", "001", "002", "003", "600", "601", "603", "605"))


def _normalize_percent_ratio(value: Decimal | None) -> Decimal | None:
    """把第三方百分数或小数比例统一成 0~1 口径。"""

    if value is None:
        return None
    return value / Decimal("100") if value > Decimal("1") else value


def timeframe_to_timedelta(timeframe: str) -> timedelta | None:
    """将常见 K 线周期转换成时间长度。"""

    unit = timeframe[-1]
    try:
        amount = int(timeframe[:-1])
    except ValueError:
        return None
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)
    if unit == "w":
        return timedelta(weeks=amount)
    return None


def _nullable_decimal(value: Any) -> Decimal | None:
    """把可缺失的第三方数值转成 Decimal。"""

    if value is None or pd.isna(value):
        return None
    normalized = str(value).replace(",", "").strip()
    if not normalized:
        return None
    return Decimal(normalized)


def _first_present(row: dict[str, Any], names: list[str]) -> Any:
    """按候选列名读取第一个非空值。"""

    for name in names:
        if name in row and row[name] is not None and not pd.isna(row[name]):
            return row[name]
    return None


def _first_decimal(row: dict[str, Any], names: list[str]) -> Decimal | None:
    """按候选列名读取第一个可转 Decimal 的值。"""

    for name in names:
        if name not in row:
            continue
        value = nullable_decimal(row[name])
        if value is not None:
            return value
    return None


def _missing_fields(values: dict[str, Any]) -> list[str]:
    """返回缺失字段名列表。"""

    return [name for name, value in values.items() if value is None]


def _datetime_from_milliseconds(value: Any) -> datetime | None:
    """把毫秒时间戳转换为 UTC datetime。"""

    if value is None or pd.isna(value):
        return None
    timestamp_ms = int(value)
    if timestamp_ms <= 0:
        return None
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
