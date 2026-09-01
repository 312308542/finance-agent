"""补跑模块的生产装配（组合根）。

规格 6.1：数据库交易日历未覆盖当前日期时，``preview`` 必须通过现有
交易日历 Provider 做只读获取；本模块把该回调统一装配进
:dataclass:`DataRecoveryModule`，供 API / CLI / MCP / 调度桥共用，
避免各入口裸构造导致 ``database_stale`` 误判。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

SHANGHAI_TZ = timezone(timedelta(hours=8))


def _calendar_refresh() -> list[dict[str, Any]]:
    """经 AkshareProvider 只读拉取最近交易日，规范化为 cutoff 消费格式。

    失败时直接抛异常：resolve_cutoff 会捕获并结构化为 provider_error，
    计划保持不可执行（规格 6.1 禁止按工作日猜测交易日）。
    """

    from finance_agent.data.providers.akshare_provider import AkshareProvider

    provider = AkshareProvider(request_timeout_seconds=10.0)
    today = datetime.now(SHANGHAI_TZ).date()
    trade_dates: list[date] = provider.fetch_trade_dates(
        start_date=today - timedelta(days=45), end_date=today
    )
    return [
        {
            "trade_date": day,
            "is_trading_day": True,
            # akshare 日历只给交易日；收盘时刻按 A 股规则取当日 15:00 上海时间。
            "close_at": datetime.combine(day, time(15, 0), tzinfo=SHANGHAI_TZ),
            "status": "available",
        }
        for day in trade_dates
    ]


def _universe_refresh() -> dict[str, Any] | None:
    """经 AkshareProvider 只读拉取 A 股资产池成员（规格 6.2）。

    返回 {"asset_ids": [...]}；无可用资产时返回 None 让调用方标记
    provider_unavailable。
    """

    from finance_agent.data.providers.akshare_provider import AkshareProvider

    provider = AkshareProvider(request_timeout_seconds=20.0)
    result = provider.fetch_assets()
    asset_ids = [
        str(asset.asset_id)
        for asset in (result.assets or [])
        if getattr(asset, "asset_id", None)
    ]
    if not asset_ids:
        return None
    return {"asset_ids": asset_ids}


def build_default_recovery_module(session):
    """构造带日历/资产池只读刷新回调的补跑门面（生产标准入口）。"""

    from finance_agent.data_recovery.service import DataRecoveryModule

    return DataRecoveryModule(
        session,
        calendar_refresh=_calendar_refresh,
        universe_refresh=_universe_refresh,
    )
