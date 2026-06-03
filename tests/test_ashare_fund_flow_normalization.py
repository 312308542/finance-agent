from datetime import UTC, datetime

import pandas as pd

from finance_agent.data.normalizers import normalize_ashare_fund_flow_rank


def test_fund_flow_rank_normalizes_and_filters_invalid_ashare_symbols() -> None:
    """资金流排名只应产出规范 6 位 A 股代码，避免短代码污染资产池。"""

    rows = pd.DataFrame(
        [
            {"代码": "1400", "主力净流入": "100", "成交额": "2000", "换手率": "1.2"},
            {"代码": "000001", "主力净流入": "300", "成交额": "4000", "换手率": "2.3"},
            {"代码": "sz000002", "主力净流入": "500", "成交额": "6000", "换手率": "3.4"},
            {"代码": "600519.SH", "主力净流入": "700", "成交额": "8000", "换手率": "4.5"},
        ]
    )

    snapshots = normalize_ashare_fund_flow_rank(
        rows,
        source="ths:curl_cffi:stock_fund_flow_individual",
        window="5日",
        as_of=datetime(2026, 6, 3, tzinfo=UTC),
    )

    assert [item.symbol for item in snapshots] == ["000001", "000002", "600519"]
    assert [item.asset_id for item in snapshots] == [
        "ashare:000001",
        "ashare:000002",
        "ashare:600519",
    ]
