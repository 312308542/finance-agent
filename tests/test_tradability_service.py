from __future__ import annotations

from finance_agent.application.tradability_service import TradabilityInput, TradabilityService


def test_tradability_blocks_one_word_limit_up() -> None:
    result = TradabilityService().evaluate(
        TradabilityInput(
            asset_id="ashare:600519",
            symbol="600519",
            market="ashare",
            open_price=110,
            high_price=110,
            low_price=110,
            close_price=110,
            previous_close=100,
            volume=120000,
            status_flags=(),
            leadership_flags=("sector_leader",),
        )
    )

    assert result.tradable is False
    assert result.blocking_level == "blocked"
    assert "one_word_limit_up" in result.reasons
    assert result.action_override == "watch"


def test_tradability_blocks_suspended_or_st_assets() -> None:
    result = TradabilityService().evaluate(
        TradabilityInput(
            asset_id="ashare:000001",
            symbol="000001",
            market="ashare",
            open_price=10,
            high_price=10.2,
            low_price=9.9,
            close_price=10.1,
            previous_close=10,
            volume=0,
            status_flags=("suspended", "st"),
        )
    )

    assert result.tradable is False
    assert result.blocking_level == "blocked"
    assert {"suspended", "st", "no_volume"}.issubset(set(result.reasons))


def test_tradability_warns_low_liquidity_without_blocking() -> None:
    result = TradabilityService().evaluate(
        TradabilityInput(
            asset_id="ashare:000001",
            symbol="000001",
            market="ashare",
            open_price=10,
            high_price=10.2,
            low_price=9.9,
            close_price=10.1,
            previous_close=10,
            volume=800,
            status_flags=(),
            min_volume=1000,
        )
    )

    assert result.tradable is True
    assert result.blocking_level == "warning"
    assert result.reasons == ("low_liquidity",)
