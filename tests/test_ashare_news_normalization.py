from datetime import UTC, datetime

import pandas as pd

from finance_agent.data.normalizers import normalize_ashare_stock_news, stable_id


def test_stock_news_naive_published_at_uses_shanghai_timezone() -> None:
    """东财新闻的无时区发布时间应按北京时间解释后再转为 UTC。"""

    events, evidence = normalize_ashare_stock_news(
        pd.DataFrame(
            [
                {
                    "新闻标题": "融资客看好个股一览",
                    "发布时间": "2026-07-15 10:21:00",
                }
            ]
        ),
        symbol="600428",
        source="akshare:stock_news_em",
        collected_at=datetime(2026, 7, 15, 2, 48, tzinfo=UTC),
    )

    expected = datetime(2026, 7, 15, 2, 21, tzinfo=UTC)
    assert events[0].published_at == expected
    assert evidence[0].as_of == expected


def test_stock_news_timezone_fix_keeps_existing_event_identity() -> None:
    """时区修正不能让已入库的同一条新闻在下次采集时生成重复事件。"""

    events, _ = normalize_ashare_stock_news(
        pd.DataFrame(
            [
                {
                    "新闻标题": "融资客看好个股一览",
                    "发布时间": "2026-07-15 10:21:00",
                }
            ]
        ),
        symbol="600428",
        source="akshare:stock_news_em",
        collected_at=datetime(2026, 7, 15, 2, 48, tzinfo=UTC),
    )

    legacy_identity_at = datetime(2026, 7, 15, 10, 21, tzinfo=UTC)
    assert events[0].event_id == stable_id(
        "event",
        "akshare:stock_news_em",
        "600428",
        "融资客看好个股一览",
        legacy_identity_at,
    )
