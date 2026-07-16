from datetime import UTC, datetime

import pandas as pd

from finance_agent.data.normalizers import normalize_ashare_stock_news, stable_id


def test_stock_news_filters_conflicting_index_entity_and_keeps_raw_audit() -> None:
    """同代码异交易所指数新闻必须被过滤并保留原始报文。"""

    normalized = normalize_ashare_stock_news(
        pd.DataFrame(
            [
                {
                    "新闻标题": "科创芯片 ETF 跟踪上证科创板芯片指数 000685.SH 上涨",
                    "新闻内容": "指数成分股走强",
                    "发布时间": "2026-07-15 10:21:00",
                }
            ]
        ),
        symbol="000685",
        asset_name="中山公用",
        source="akshare:stock_news_em",
        collected_at=datetime(2026, 7, 15, 2, 48, tzinfo=UTC),
    )

    assert normalized.events == []
    assert normalized.evidence == []
    validation = normalized.entity_validation
    assert validation["source_row_count"] == 1
    assert validation["passed_count"] == 0
    assert validation["failed_count"] == 1
    assert validation["ambiguous_count"] == 0
    filtered = validation["filtered_rows"][0]
    assert filtered["reason"] == "conflicting_exchange_suffix"
    assert filtered["raw"]["新闻标题"].startswith("科创芯片 ETF")


def test_stock_news_passed_event_and_evidence_include_validation_payload() -> None:
    """通过门控的事件和证据必须保存同一规则版本及匹配依据。"""

    normalized = normalize_ashare_stock_news(
        pd.DataFrame(
            [
                {
                    "新闻标题": "中山公用发布经营数据",
                    "新闻内容": "公司披露主营业务进展",
                    "发布时间": "2026-07-15 10:21:00",
                }
            ]
        ),
        symbol="000685",
        asset_name="中山公用",
        source="akshare:stock_news_em",
        collected_at=datetime(2026, 7, 15, 2, 48, tzinfo=UTC),
    )

    assert len(normalized.events) == 1
    assert len(normalized.evidence) == 1
    event_validation = normalized.events[0].payload["entity_validation"]
    evidence_validation = normalized.evidence[0].payload["entity_validation"]
    assert event_validation["status"] == "passed"
    assert event_validation["reason"] == "canonical_name"
    assert event_validation["rule_version"] == "ashare_news_entity_v1"
    assert evidence_validation == event_validation


def test_stock_news_naive_published_at_uses_shanghai_timezone() -> None:
    """东财新闻的无时区发布时间应按北京时间解释后再转为 UTC。"""

    normalized = normalize_ashare_stock_news(
        pd.DataFrame(
            [
                {
                    "新闻标题": "融资客看好个股一览",
                    "新闻内容": "航天电子获得融资客关注",
                    "发布时间": "2026-07-15 10:21:00",
                }
            ]
        ),
        symbol="600428",
        asset_name="航天电子",
        source="akshare:stock_news_em",
        collected_at=datetime(2026, 7, 15, 2, 48, tzinfo=UTC),
    )

    expected = datetime(2026, 7, 15, 2, 21, tzinfo=UTC)
    assert normalized.events[0].published_at == expected
    assert normalized.evidence[0].as_of == expected


def test_stock_news_timezone_fix_keeps_existing_event_identity() -> None:
    """时区修正不能让已入库的同一条新闻在下次采集时生成重复事件。"""

    normalized = normalize_ashare_stock_news(
        pd.DataFrame(
            [
                {
                    "新闻标题": "融资客看好个股一览",
                    "新闻内容": "航天电子获得融资客关注",
                    "发布时间": "2026-07-15 10:21:00",
                }
            ]
        ),
        symbol="600428",
        asset_name="航天电子",
        source="akshare:stock_news_em",
        collected_at=datetime(2026, 7, 15, 2, 48, tzinfo=UTC),
    )

    legacy_identity_at = datetime(2026, 7, 15, 10, 21, tzinfo=UTC)
    assert normalized.events[0].event_id == stable_id(
        "event",
        "akshare:stock_news_em",
        "600428",
        "融资客看好个股一览",
        legacy_identity_at,
    )
