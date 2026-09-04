from finance_agent.intraday.models import QuoteQualityResult, quote_channel_policy


def test_quote_channels_have_fixed_frequency_and_batch_limits() -> None:
    held = quote_channel_policy("held")
    radar = quote_channel_policy("radar")
    market = quote_channel_policy("market")
    verification = quote_channel_policy("verification")

    assert (held.interval_seconds, held.batch_size, held.primary_source) == (1, 50, "gotdx")
    assert held.maximum_freshness_seconds == 3
    assert (radar.interval_seconds, radar.batch_size, radar.primary_source) == (5, 50, "gotdx")
    assert radar.maximum_freshness_seconds == 10
    assert (market.interval_seconds, market.batch_size, market.primary_source) == (300, 500, "akshare")
    assert market.maximum_freshness_seconds == 300
    assert (verification.interval_seconds, verification.batch_size, verification.primary_source) == (
        30,
        50,
        "akshare",
    )
    assert verification.maximum_freshness_seconds == 90


def test_only_available_quote_quality_is_executable() -> None:
    available = QuoteQualityResult(
        status="available",
        requested_count=2,
        received_count=2,
        fresh_count=2,
        maximum_lag_seconds=0.8,
        duplicate_timestamp_count=0,
        clock_regression_count=0,
        source_errors=(),
    )
    partial = QuoteQualityResult(
        status="partial",
        requested_count=2,
        received_count=1,
        fresh_count=1,
        maximum_lag_seconds=0.8,
        duplicate_timestamp_count=0,
        clock_regression_count=0,
        source_errors=("gotdx:timeout",),
    )

    assert available.is_executable is True
    assert partial.is_executable is False
