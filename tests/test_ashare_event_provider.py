from datetime import UTC, datetime

import pandas as pd
import pytest

from finance_agent.data.providers import akshare_p1_provider as provider_module
from finance_agent.data.providers.akshare_p1_provider import AshareEventProvider


def test_provider_queries_code_and_asset_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """关键词新闻必须使用代码和规范公司名组合查询。"""

    calls: list[str] = []

    def fake_stock_news_em(*, symbol: str) -> pd.DataFrame:
        calls.append(symbol)
        return pd.DataFrame(
            [
                {
                    "新闻标题": "中山公用发布经营数据",
                    "发布时间": datetime(2026, 7, 15, 10, 21, tzinfo=UTC),
                }
            ]
        )

    monkeypatch.setattr(provider_module.ak, "stock_news_em", fake_stock_news_em)

    result = AshareEventProvider().fetch_stock_news(
        symbol="000685",
        asset_name="中山公用",
    )

    assert calls == ["000685 中山公用"]
    assert result.status == "available"
    assert result.payload["query"] == "000685 中山公用"
    assert result.payload["entity_validation"]["passed_count"] == 1


def test_provider_missing_asset_name_does_not_fallback_to_symbol_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺少公司名时必须 fail-closed，不得调用纯代码搜索。"""

    calls: list[str] = []
    monkeypatch.setattr(
        provider_module.ak,
        "stock_news_em",
        lambda *, symbol: calls.append(symbol) or pd.DataFrame(),
    )

    result = AshareEventProvider().fetch_stock_news(symbol="000685", asset_name="")

    assert calls == []
    assert result.status == "unavailable"
    assert result.payload["reason"] == "missing_asset_name"
    assert result.payload["symbol"] == "000685"


def test_provider_all_filtered_preserves_source_row_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """源端有返回但全部过滤时不能伪装成源端空列表。"""

    monkeypatch.setattr(
        provider_module.ak,
        "stock_news_em",
        lambda *, symbol: pd.DataFrame(
            [
                {
                    "新闻标题": "上证科创板芯片指数 000685.SH 上涨",
                    "发布时间": "2026-07-15 10:21:00",
                }
            ]
        ),
    )

    result = AshareEventProvider().fetch_stock_news(
        symbol="000685",
        asset_name="中山公用",
    )

    assert result.status == "unavailable"
    assert result.payload["entity_validation"]["source_row_count"] == 1
    assert result.payload["entity_validation"]["failed_count"] == 1
