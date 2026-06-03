from datetime import UTC, datetime

import pandas as pd

from finance_agent.data.normalizers import normalize_ashare_lhb_detail


def test_lhb_detail_filters_non_standard_stock_symbols() -> None:
    """龙虎榜明细应过滤可转债、B 股等非普通 A 股代码，避免 evidence 孤儿引用。"""

    rows = [
        {
            "代码": "600519",
            "名称": "贵州茅台",
            "上榜日": "2026-06-03",
            "上榜原因": "日涨幅偏离值达7%",
        },
        {
            "代码": "113702",
            "名称": "可转债样例",
            "上榜日": "2026-06-03",
            "上榜原因": "债券异常波动",
        },
        {
            "代码": "123267",
            "名称": "可转债样例2",
            "上榜日": "2026-06-03",
            "上榜原因": "债券异常波动",
        },
        {"代码": "200016", "名称": "B股样例", "上榜日": "2026-06-03", "上榜原因": "B股异常波动"},
        {"代码": "900915", "名称": "B股样例2", "上榜日": "2026-06-03", "上榜原因": "B股异常波动"},
    ]

    risks, evidence = normalize_ashare_lhb_detail(
        pd.DataFrame(rows),
        source="akshare:stock_lhb_detail_em",
        collected_at=datetime(2026, 6, 4, tzinfo=UTC),
    )

    assert [risk.asset_id for risk in risks] == ["ashare:600519"]
    assert [item.asset_id for item in evidence] == ["ashare:600519"]
