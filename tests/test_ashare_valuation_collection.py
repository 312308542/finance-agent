"""A 股估值采集测试。"""

from finance_agent.data.providers.akshare_p2_provider import AshareValuationProvider


def test_valuation_none_payload_is_unavailable(monkeypatch) -> None:
    """源端返回空对象时应标记不可用，不能记为采集错误。"""

    def raise_no_data(*, symbol: str) -> None:
        assert symbol == "001235"
        raise TypeError("'NoneType' object is not subscriptable")

    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p2_provider.ak.stock_value_em",
        raise_no_data,
    )

    result = AshareValuationProvider().fetch_valuation(symbol="001235")

    assert result.status == "unavailable"
    assert result.error_message is None
    assert result.snapshots == []
    assert result.payload["unavailable_reason"] == "source_returned_no_data"


def test_valuation_unexpected_type_error_remains_error(monkeypatch) -> None:
    """非空数据语义的类型错误仍应保留为错误，避免掩盖真实故障。"""

    def raise_unexpected(*, symbol: str) -> None:
        assert symbol == "600357"
        raise TypeError("unexpected response shape")

    monkeypatch.setattr(
        "finance_agent.data.providers.akshare_p2_provider.ak.stock_value_em",
        raise_unexpected,
    )

    result = AshareValuationProvider().fetch_valuation(symbol="600357")

    assert result.status == "error"
    assert result.error_message == "unexpected response shape"
    assert result.payload == {"endpoint": "stock_value_em", "symbol": "600357"}
