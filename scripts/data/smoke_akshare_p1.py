"""AKShare P1 数据链路冒烟验证。

验证内容：
- 行业/概念种子源 Provider 能返回结构化结果。
- 个股资金流排名能归一化并写入 `capital_flow_snapshots`。
- 个股新闻能归一化并写入 `event_records` 和 `evidence`。
- 每个 Provider 调用都会写入 `raw_records`，成功和失败都可审计。

部分东方财富接口在当前网络环境下可能被上游断开；脚本会输出结构化
错误，不把单个 Provider 失败当成整个冒烟脚本失败。
"""

from __future__ import annotations

from datetime import UTC, datetime

from finance_agent.data.collectors import AshareP1Collector
from finance_agent.storage.db import create_session_factory, session_scope


def main() -> None:
    """执行 AKShare P1 冒烟验证。"""

    as_of = datetime.now(tz=UTC)
    session_factory = create_session_factory()
    with session_scope(session_factory) as session:
        collector = AshareP1Collector(session)
        industry_archive = collector.collect_industry_members(
            industry_name="银行",
            universe_id="universe:akshare_p1_smoke:industry_bank",
            universe_name="AKShare P1 冒烟行业种子池",
            strategy_context="p1_smoke",
            limit=5,
        )
        flow_archive = collector.collect_flow_rank(indicator="5日", limit=5)
        news_archive = collector.collect_stock_news(symbol="000001", asset_name="平安银行", limit=3)

    industry = industry_archive.result
    flow_rank = flow_archive.result
    news = news_archive.result
    raw_record_ids = [
        industry_archive.raw_record_id,
        flow_archive.raw_record_id,
        news_archive.raw_record_id,
    ]

    print(
        {
            "as_of": as_of.isoformat(),
            "industry_status": industry.status,
            "industry_count": len(industry.seeds),
            "industry_error": industry.error_message,
            "flow_status": flow_rank.status,
            "flow_count": len(flow_rank.snapshots),
            "flow_error": flow_rank.error_message,
            "news_status": news.status,
            "news_count": len(news.events),
            "news_evidence_count": len(news.evidence),
            "news_error": news.error_message,
            "raw_record_count": len(raw_record_ids),
            "raw_record_ids": raw_record_ids,
        }
    )


if __name__ == "__main__":
    main()
