from datetime import UTC, datetime, timedelta

import pytest

from finance_agent.storage.snapshot_contracts import (
    SnapshotValidationError,
    build_data_snapshot,
)


def _captured_at() -> datetime:
    return datetime(2026, 7, 20, 9, 31, tzinfo=UTC)


def test_snapshot_id_is_stable_for_same_normalized_input() -> None:
    kwargs = {
        "snapshot_type": "ashare_realtime_quotes",
        "market": "ashare",
        "as_of": datetime(2026, 7, 20, 9, 30, tzinfo=UTC),
        "captured_at": _captured_at(),
        "provider": "gotdx:tdx_main",
        "provider_version": "gateway-v1",
        "quality_status": "available",
        "schema_version": "1",
        "raw_record_ids": ("raw:quote:1",),
        "payload": {"quotes": [{"symbol": "600519.SH", "last_price": "1500.00"}]},
        "metadata": {"received_at": _captured_at().isoformat()},
    }

    first = build_data_snapshot(**kwargs)
    second = build_data_snapshot(**kwargs)

    assert first.data_snapshot_id == second.data_snapshot_id
    assert first.content_hash == second.content_hash
    assert first.to_record()["data_snapshot_id"] == first.data_snapshot_id


def test_snapshot_contract_requires_provider_and_quality() -> None:
    base = {
        "snapshot_type": "ashare_realtime_quotes",
        "market": "ashare",
        "as_of": datetime(2026, 7, 20, 9, 30, tzinfo=UTC),
        "captured_at": _captured_at(),
        "provider": "gotdx:tdx_main",
        "quality_status": "available",
        "payload": {},
    }

    with pytest.raises(SnapshotValidationError, match="provider"):
        build_data_snapshot(**{**base, "provider": ""})
    with pytest.raises(SnapshotValidationError, match="quality_status"):
        build_data_snapshot(**{**base, "quality_status": ""})


def test_snapshot_normalizes_naive_datetime_to_utc() -> None:
    snapshot = build_data_snapshot(
        snapshot_type="ashare_realtime_quotes",
        market="ashare",
        as_of=datetime(2026, 7, 20, 9, 30),
        captured_at=datetime(2026, 7, 20, 9, 31),
        provider="gotdx:tdx_main",
        quality_status="available",
        payload={},
    )

    assert snapshot.as_of.tzinfo is UTC
    assert snapshot.captured_at.tzinfo is UTC


def test_snapshot_rejects_capture_before_as_of() -> None:
    with pytest.raises(SnapshotValidationError, match="captured_at"):
        build_data_snapshot(
            snapshot_type="ashare_realtime_quotes",
            market="ashare",
            as_of=datetime(2026, 7, 20, 9, 31, tzinfo=UTC),
            captured_at=datetime(2026, 7, 20, 9, 30, tzinfo=UTC),
            provider="gotdx:tdx_main",
            quality_status="available",
            payload={},
        )


def test_snapshot_id_changes_when_payload_changes() -> None:
    common = {
        "snapshot_type": "ashare_realtime_quotes",
        "market": "ashare",
        "as_of": datetime(2026, 7, 20, 9, 30, tzinfo=UTC),
        "captured_at": _captured_at(),
        "provider": "gotdx:tdx_main",
        "quality_status": "available",
    }
    first = build_data_snapshot(**common, payload={"last_price": "1"})
    second = build_data_snapshot(**common, payload={"last_price": "2"})

    assert first.data_snapshot_id != second.data_snapshot_id
    assert first.captured_at - first.as_of == timedelta(minutes=1)
