from datetime import date

import pytest

from finance_agent.research.structural_event_study import (
    events_from_structural_payload,
    forward_return,
    maximum_adverse_excursion,
    summarize_events,
)


def test_events_from_payload_uses_confirmed_at_and_skips_unknown_dates() -> None:
    events = events_from_structural_payload(
        asset_id="a",
        payloads=[
            {
                "horizon": "smc_lite_v2",
                "structure_events": [
                    {"name": "bos_bullish", "direction": "bullish", "confirmed_at": "2026-01-02T08:00:00+08:00"},
                    {"name": "bos_bearish", "direction": "bearish", "confirmed_at": "2026-01-09T08:00:00+08:00"},
                ],
            }
        ],
        date_indexes={"2026-01-02": 3},
    )
    assert len(events) == 1
    assert events[0]["event_index"] == 3
    assert events[0]["event_date"] == date(2026, 1, 2)


def test_forward_return_and_adverse_excursion_respect_direction() -> None:
    closes = [100.0, 98.0, 105.0, 95.0]
    assert forward_return(closes, 0, 2) == pytest.approx(0.05)
    assert forward_return(closes, 0, 2, direction="bearish") == pytest.approx(-0.05)
    assert maximum_adverse_excursion(closes, 0, 2) == pytest.approx(-0.02)
    assert maximum_adverse_excursion(closes, 0, 2, direction="bearish") == pytest.approx(-0.05)


def test_summary_separates_out_of_sample_and_reports_excess() -> None:
    events = [
        {
            "asset_id": "a",
            "signal": "bos_bullish",
            "direction": "bullish",
            "event_index": 0,
            "event_date": date(2026, 1, 1),
        },
        {
            "asset_id": "a",
            "signal": "bos_bullish",
            "direction": "bullish",
            "event_index": 1,
            "event_date": date(2026, 2, 1),
        },
    ]
    result = summarize_events(
        events,
        {"a": [100.0, 101.0, 105.0]},
        {(str(date(2026, 1, 1)), 1): 0.01, (str(date(2026, 2, 1)), 1): 0.01},
        windows=(1,),
        split_date=date(2026, 2, 1),
    )
    assert result["signals"]["bos_bullish"]["in_sample"]["1"]["sample_count"] == 1
    assert result["signals"]["bos_bullish"]["out_of_sample"]["1"]["median_excess_return"] == pytest.approx(0.02960396)


def test_summary_supports_two_ordered_out_of_sample_periods() -> None:
    events = [
        {
            "asset_id": "a",
            "signal": "bos_bullish",
            "direction": "bullish",
            "event_index": 0,
            "event_date": date(2026, 1, 1),
        },
        {
            "asset_id": "a",
            "signal": "bos_bullish",
            "direction": "bullish",
            "event_index": 1,
            "event_date": date(2026, 2, 1),
        },
        {
            "asset_id": "a",
            "signal": "bos_bullish",
            "direction": "bullish",
            "event_index": 2,
            "event_date": date(2026, 3, 1),
        },
    ]
    result = summarize_events(
        events,
        {"a": [100.0, 101.0, 102.0, 104.0]},
        {},
        windows=(1,),
        period_splits=(date(2026, 2, 1), date(2026, 3, 1)),
    )
    signal = result["signals"]["bos_bullish"]
    assert signal["in_sample"]["1"]["sample_count"] == 1
    assert signal["out_of_sample_period_1"]["1"]["sample_count"] == 1
    assert signal["out_of_sample_period_2"]["1"]["sample_count"] == 1
