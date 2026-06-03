from typing import Any

import finance_agent.data.providers.ccxt_binance_provider as provider_module
from finance_agent.data.providers.ccxt_binance_provider import CcxtBinanceProvider


def test_future_provider_uses_binance_usdm_exchange(
    monkeypatch: Any,
) -> None:
    """合约市场应使用 ccxt.binanceusdm，避免走到易被 418 的入口。"""

    created: list[tuple[str, dict[str, Any]]] = []

    class FakeExchange:
        pass

    def fake_binance(config: dict[str, Any]) -> FakeExchange:
        created.append(("binance", config))
        return FakeExchange()

    def fake_binanceusdm(config: dict[str, Any]) -> FakeExchange:
        created.append(("binanceusdm", config))
        return FakeExchange()

    monkeypatch.setattr(provider_module.ccxt, "binance", fake_binance)
    monkeypatch.setattr(provider_module.ccxt, "binanceusdm", fake_binanceusdm)

    CcxtBinanceProvider(default_type="future")

    assert [name for name, _config in created] == ["binanceusdm"]
    assert created[0][1]["options"]["defaultType"] == "future"


def test_spot_provider_keeps_binance_exchange(
    monkeypatch: Any,
) -> None:
    """现货市场仍使用 ccxt.binance，保持现有 spot 行为不变。"""

    created: list[tuple[str, dict[str, Any]]] = []

    class FakeExchange:
        pass

    def fake_binance(config: dict[str, Any]) -> FakeExchange:
        created.append(("binance", config))
        return FakeExchange()

    def fake_binanceusdm(config: dict[str, Any]) -> FakeExchange:
        created.append(("binanceusdm", config))
        return FakeExchange()

    monkeypatch.setattr(provider_module.ccxt, "binance", fake_binance)
    monkeypatch.setattr(provider_module.ccxt, "binanceusdm", fake_binanceusdm)

    CcxtBinanceProvider(default_type="spot")

    assert [name for name, _config in created] == ["binance"]
    assert created[0][1]["options"]["defaultType"] == "spot"


def test_future_provider_expands_compact_symbol_with_settlement_suffix(
    monkeypatch: Any,
) -> None:
    """合约请求应把库内紧凑交易对 BTCUSDT 转成 ccxt 需要的 BTC/USDT:USDT。"""

    class FakeExchange:
        pass

    monkeypatch.setattr(provider_module.ccxt, "binanceusdm", lambda _config: FakeExchange())

    provider = CcxtBinanceProvider(default_type="future")

    assert provider._to_ccxt_symbol("BTCUSDT") == "BTC/USDT:USDT"


def test_future_provider_expands_delivery_symbol_with_settlement_suffix(
    monkeypatch: Any,
) -> None:
    """交割合约请求应把 BTCUSDT-260626 转成 ccxt 的 BTC/USDT:USDT-260626。"""

    class FakeExchange:
        pass

    monkeypatch.setattr(provider_module.ccxt, "binanceusdm", lambda _config: FakeExchange())

    provider = CcxtBinanceProvider(default_type="future")

    assert provider._to_ccxt_symbol("BTCUSDT-260626") == "BTC/USDT:USDT-260626"
