"""验证数据层生产化增强策略。

本脚本不访问外部数据源，也不连接数据库，只验证服务层纯策略：
- A 股交易日历与数字货币 7x24 日历。
- 数据质量缺口转补采任务。
- Binance 限流错误识别和备用端点选择。
- 同市场多候选池合并。
- 回避池策略。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from finance_agent.application.data_production_service import (
    AvoidPoolPolicy,
    BinanceRateLimitPolicy,
    DataBackfillPlanner,
    MarketCalendarService,
    UniverseMergeService,
)
from finance_agent.data.providers import akshare_risk_sentiment_provider as risk_provider_module
from finance_agent.data.providers.akshare_risk_sentiment_provider import AshareRiskProvider


def main() -> None:
    """执行离线生产化策略 smoke。"""

    calendar_service = MarketCalendarService()
    ashare_entries = calendar_service.build_ashare_calendar_entries(
        trading_dates=[date(2026, 5, 18), date(2026, 5, 19)],
        start_date=date(2026, 5, 18),
        end_date=date(2026, 5, 20),
        source="smoke",
    )
    if len(ashare_entries) != 3:
        raise AssertionError(f"A 股日历必须补齐交易日和非交易日：{ashare_entries}")
    if not calendar_service.is_trading_day(ashare_entries, date(2026, 5, 18)):
        raise AssertionError("2026-05-18 应识别为 A 股交易日")
    if calendar_service.is_trading_day(ashare_entries, date(2026, 5, 20)):
        raise AssertionError("2026-05-20 未出现在交易日源中，应识别为非交易日")
    missing_dates = calendar_service.missing_trading_dates(
        ashare_entries,
        existing_dates={date(2026, 5, 18)},
    )
    if missing_dates != [date(2026, 5, 19)]:
        raise AssertionError(f"缺口补采应只返回缺失交易日，不包含休市日：{missing_dates}")

    crypto_entries = calendar_service.build_crypto_calendar_entries(
        start_date=date(2026, 5, 18),
        end_date=date(2026, 5, 20),
        market="crypto_spot",
        source="smoke",
    )
    if not all(entry.is_trading_day for entry in crypto_entries):
        raise AssertionError("数字货币日历必须按 7x24 交易处理")

    backfill_jobs = DataBackfillPlanner().build_backfill_jobs(
        health_summary=build_health_summary(),
        now=datetime(2026, 5, 21, 9, 30, tzinfo=UTC),
    )
    job_keys = {job.task_key for job in backfill_jobs}
    required_backfill_jobs = {
        "ashare_calendar_refresh",
        "crypto_spot_calendar_refresh",
        "crypto_future_calendar_refresh",
        "ashare_market_bars_backfill",
        "crypto_spot_market_bars_backfill",
        "crypto_future_market_bars_backfill",
        "ashare_risk_refresh",
    }
    if not required_backfill_jobs.issubset(job_keys):
        raise AssertionError(f"缺口补采必须生成双市场行情和风险任务：{job_keys}")

    with tempfile.TemporaryDirectory() as temp_dir:
        health_file = Path(temp_dir) / "health.json"
        health_file.write_text(
            json.dumps(build_health_summary(), ensure_ascii=False),
            encoding="utf-8",
        )
        cli_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "finance_agent.cli",
                "data",
                "production",
                "backfill-plan",
                "--health-file",
                str(health_file),
            ],
            check=True,
            capture_output=True,
            encoding="utf-8",
            text=True,
        )
        cli_payload = json.loads(cli_result.stdout)
        cli_job_names = {job["name"] for job in cli_payload["data"]["jobs"]}
        if not required_backfill_jobs.issubset(cli_job_names):
            raise AssertionError(f"CLI 必须导出双市场补采任务：{cli_payload}")

    rate_policy = BinanceRateLimitPolicy(
        base_urls=(
            "https://fapi.binance.com",
            "https://fapi1.binance.com",
            "https://fapi2.binance.com",
        ),
    )
    decision = rate_policy.plan_retry(
        RuntimeError("HTTP 429 Too many requests; retry later"),
        current_base_url="https://fapi.binance.com",
        attempt=1,
    )
    if not decision.should_retry or decision.next_base_url != "https://fapi1.binance.com":
        raise AssertionError(f"Binance 限流必须切到下一个备用端点：{decision}")
    if not rate_policy.is_rate_limited(RuntimeError("418 IP banned by rate limit")):
        raise AssertionError("Binance 418 必须识别为限流/封禁类错误")

    merge_service = UniverseMergeService()
    merged = merge_service.merge_members(
        target_universe_id="universe:merged:ashare",
        market="ashare",
        sources=[
            {
                "universe_id": "u:flow",
                "source": "capital_flow_rank",
                "weight": 2.0,
                "members": [
                    {"asset_id": "ashare:600519", "symbol": "600519", "rank_hint": 2},
                    {"asset_id": "ashare:000001", "symbol": "000001", "rank_hint": 5},
                ],
            },
            {
                "universe_id": "u:hot",
                "source": "hot_rank",
                "weight": 1.0,
                "members": [
                    {"asset_id": "ashare:600519", "symbol": "600519", "rank_hint": 1},
                    {"asset_id": "ashare:300750", "symbol": "300750", "rank_hint": 3},
                ],
            },
        ],
        as_of=datetime(2026, 5, 21, 9, 30, tzinfo=UTC),
    )
    if [member.asset_id for member in merged] != [
        "ashare:600519",
        "ashare:000001",
        "ashare:300750",
    ]:
        raise AssertionError(f"合并候选池必须按权重和排名去重排序：{merged}")
    try:
        merge_service.merge_members(
            target_universe_id="universe:bad",
            market="ashare",
            sources=[
                {
                    "universe_id": "u:crypto",
                    "source": "binance_usdt",
                    "market": "crypto_spot",
                    "members": [{"asset_id": "crypto_spot:BTCUSDT", "symbol": "BTCUSDT"}],
                }
            ],
            as_of=datetime(2026, 5, 21, 9, 30, tzinfo=UTC),
        )
    except ValueError:
        pass
    else:
        raise AssertionError("候选池合并必须拒绝跨市场来源")

    avoid_members = AvoidPoolPolicy().build_avoid_members(
        universe_id="universe:avoid:ashare",
        market="ashare",
        assets=[
            {
                "asset_id": "ashare:600001",
                "symbol": "600001",
                "name": "*ST 样例",
                "status": "available",
                "tradable": True,
            },
            {
                "asset_id": "ashare:000002",
                "symbol": "000002",
                "name": "停牌样例",
                "status": "suspended",
                "tradable": False,
            },
            {
                "asset_id": "ashare:300001",
                "symbol": "300001",
                "name": "风险样例",
                "status": "available",
                "tradable": True,
            },
        ],
        risks=[
            {
                "asset_id": "ashare:300001",
                "risk_type": "delist_risk",
                "severity": "critical",
                "title": "退市风险",
            }
        ],
        as_of=datetime(2026, 5, 21, 9, 30, tzinfo=UTC),
    )
    reasons = {member.asset_id: member.removed_reason for member in avoid_members}
    if "ST/风险警示名称" not in (reasons.get("ashare:600001") or ""):
        raise AssertionError(f"ST 名称必须进入回避池：{reasons}")
    if "不可交易" not in (reasons.get("ashare:000002") or ""):
        raise AssertionError(f"停牌/不可交易资产必须进入回避池：{reasons}")
    if "退市风险" not in (reasons.get("ashare:300001") or ""):
        raise AssertionError(f"高风险发现必须进入回避池：{reasons}")

    assert_stop_list_supplemental_sources_work_when_primary_fails()

    print(
        {
            "status": "ok",
            "ashare_calendar_entries": len(ashare_entries),
            "crypto_calendar_entries": len(crypto_entries),
            "backfill_jobs": sorted(job_keys),
            "merged_members": [member.asset_id for member in merged],
            "avoid_members": sorted(reasons),
        }
    )


def build_health_summary() -> dict[str, object]:
    """构造带缺口和替代源失败提示的健康检查摘要。"""

    return {
        "refresh_hints": [
            {
                "table_name": "market_calendars",
                "action": "refresh",
                "reason": "market_calendars 暂无数据",
            },
            {
                "table_name": "market_bars",
                "action": "refresh",
                "reason": "market_bars 超过 freshness 阈值",
            },
            {
                "table_name": "event_records",
                "action": "fallback",
                "reason": "停复牌源失败",
            },
        ],
        "gaps": ["risk_findings 暂无数据"],
    }


def assert_stop_list_supplemental_sources_work_when_primary_fails() -> None:
    """验证停复牌主源失败时，ST/退市补充源仍能产出风险。"""

    original_stop = risk_provider_module.ak.stock_zh_a_stop_em
    original_fallback = risk_provider_module.eastmoney_curl.fetch_stop_list
    original_st = getattr(risk_provider_module.ak, "stock_zh_a_st_em", None)
    original_sh_delist = getattr(risk_provider_module.ak, "stock_info_sh_delist", None)
    original_sz_delist = getattr(risk_provider_module.ak, "stock_info_sz_delist", None)
    original_staq = getattr(risk_provider_module.ak, "stock_staq_net_stop", None)
    try:
        risk_provider_module.ak.stock_zh_a_stop_em = lambda: (_raise(RuntimeError("主源失败")))
        risk_provider_module.eastmoney_curl.fetch_stop_list = lambda limit=None: _raise(
            RuntimeError("fallback 失败")
        )
        risk_provider_module.ak.stock_zh_a_st_em = lambda: pd.DataFrame(
            [{"代码": "600001", "名称": "*ST 样例", "原因": "风险警示"}]
        )
        risk_provider_module.ak.stock_info_sh_delist = lambda: pd.DataFrame(
            [{"证券代码": "600002", "证券简称": "退市样例", "退市原因": "终止上市"}]
        )
        risk_provider_module.ak.stock_info_sz_delist = lambda: pd.DataFrame()
        risk_provider_module.ak.stock_staq_net_stop = lambda: pd.DataFrame()

        result = AshareRiskProvider().fetch_stop_list(limit=10)
        risk_types = {risk.risk_type for risk in result.risks}
        if result.status != "partial":
            raise AssertionError(f"替代源兜底时状态应为 partial：{result.status}")
        if not {"st_risk", "delist_risk"}.issubset(risk_types):
            raise AssertionError(f"替代源必须产出 ST 和退市风险：{risk_types}")
    finally:
        risk_provider_module.ak.stock_zh_a_stop_em = original_stop
        risk_provider_module.eastmoney_curl.fetch_stop_list = original_fallback
        if original_st is not None:
            risk_provider_module.ak.stock_zh_a_st_em = original_st
        if original_sh_delist is not None:
            risk_provider_module.ak.stock_info_sh_delist = original_sh_delist
        if original_sz_delist is not None:
            risk_provider_module.ak.stock_info_sz_delist = original_sz_delist
        if original_staq is not None:
            risk_provider_module.ak.stock_staq_net_stop = original_staq


def _raise(error: Exception) -> None:
    """在 lambda 中抛出异常。"""

    raise error


if __name__ == "__main__":
    main()
