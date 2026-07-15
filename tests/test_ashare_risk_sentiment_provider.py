"""A 股风险情绪 Provider 测试。"""

from finance_agent.data.providers.akshare_risk_sentiment_provider import AshareRiskProvider


def test_margin_szse_empty_table_is_unavailable(monkeypatch) -> None:
    """深交所当日数据未发布时应标记不可用，不能记为采集错误。"""

    def raise_no_data(*, date: str) -> None:
        assert date == "20260715"
        raise ValueError("Length mismatch: Expected axis has 0 elements, new values have 6 elements")

    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_risk_sentiment_provider.ak.stock_margin_szse",
        raise_no_data,
    )

    result = AshareRiskProvider().fetch_margin_szse(date="20260715")

    assert result.status == "unavailable"
    assert result.error_message is None
    assert result.risks == []
    assert result.payload["unavailable_reason"] == "source_returned_no_data"


def test_margin_szse_unexpected_value_error_remains_error(monkeypatch) -> None:
    """非空数据语义的值错误仍应保留为错误，避免掩盖真实故障。"""

    def raise_unexpected(*, date: str) -> None:
        assert date == "20260715"
        raise ValueError("unexpected response shape")

    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_risk_sentiment_provider.ak.stock_margin_szse",
        raise_unexpected,
    )

    result = AshareRiskProvider().fetch_margin_szse(date="20260715")

    assert result.status == "error"
    assert result.error_message == "unexpected response shape"
    assert result.payload == {"endpoint": "stock_margin_szse", "date": "20260715"}
