"""AKShare P2 财务估值数据链路冒烟验证。

验证内容：
- 个股主要财务指标能归一化并写入 `fundamental_snapshots`。
- 个股估值序列能归一化并写入 `fundamental_snapshots`。
- 业绩报表接口即使在当前网络下失败，也会写入 `raw_records` 便于审计。
"""

from __future__ import annotations

from finance_agent.data.collectors import AshareP2Collector
from finance_agent.storage.db import create_session_factory, session_scope


def main() -> None:
    """执行 AKShare P2 财务估值冒烟验证。"""

    session_factory = create_session_factory()
    with session_scope(session_factory) as session:
        collector = AshareP2Collector(session)
        indicators = collector.collect_financial_indicators(
            symbol="000001",
            asset_name="平安银行",
            limit=3,
        )
        valuation = collector.collect_valuation(
            symbol="000001",
            asset_name="平安银行",
            limit=3,
        )
        performance = collector.collect_performance_report(
            date="20250331",
            report_type="业绩报表",
            limit=3,
        )

    print(
        {
            "indicators_status": indicators.result.status,
            "indicators_count": len(indicators.result.snapshots),
            "indicators_error": indicators.result.error_message,
            "valuation_status": valuation.result.status,
            "valuation_count": len(valuation.result.snapshots),
            "valuation_error": valuation.result.error_message,
            "performance_status": performance.result.status,
            "performance_count": len(performance.result.snapshots),
            "performance_error": performance.result.error_message,
            "raw_record_ids": [
                indicators.raw_record_id,
                valuation.raw_record_id,
                performance.raw_record_id,
            ],
        }
    )


if __name__ == "__main__":
    main()
