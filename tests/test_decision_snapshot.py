"""统一点时推荐决策快照测试。"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from finance_agent.recommendations.decision_snapshot import (
    DecisionFact,
    DecisionSnapshotBuilder,
    DecisionSnapshotCollisionError,
    DecisionSnapshotInputs,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 9, 4, 15, 0, tzinfo=SHANGHAI)


def _fact(payload: object, *, as_of: datetime = AS_OF, quality: str = "available") -> DecisionFact:
    return DecisionFact(
        data_snapshot_id=f"fact:{hash(str(payload))}",
        as_of=as_of,
        quality_status=quality,
        payload=payload,
    )


def _inputs(
    *,
    structure_as_of: datetime = AS_OF,
    risk: DecisionFact | None = None,
    assets: tuple[dict[str, object], ...] | None = None,
    previous_assets: dict[str, dict[str, object]] | None = None,
) -> DecisionSnapshotInputs:
    return DecisionSnapshotInputs(
        market="ashare",
        as_of=AS_OF,
        market_regime=_fact({"regime": "trend_up"}),
        sector_opportunities=_fact([{"sector_id": "bank", "score": 80}]),
        structure=_fact({"coverage": 1.0}, as_of=structure_as_of),
        risk=risk or _fact({"status": "available"}),
        assets=assets
        or (
            {
                "asset_id": "ashare:600519",
                "symbol": "600519",
                "quality_status": "available",
                "score": 80,
            },
        ),
        data_versions={"market": "fact:market", "structure": "fact:structure"},
        previous_assets=previous_assets or {},
    )


def test_decision_snapshot_rejects_mixed_market_dates() -> None:
    inputs = _inputs(structure_as_of=AS_OF - timedelta(days=1))

    result = DecisionSnapshotBuilder(maximum_skew=timedelta(minutes=5)).build(inputs)

    assert result.status == "blocked"
    assert result.reason_codes == ("structure_stale",)
    assert result.snapshot is None


def test_decision_snapshot_requires_global_risk_fact() -> None:
    inputs = _inputs()
    inputs = DecisionSnapshotInputs(**{**inputs.__dict__, "risk": None})

    result = DecisionSnapshotBuilder().build(inputs)

    assert result.status == "blocked"
    assert result.reason_codes == ("risk_missing",)


def test_normalized_inputs_generate_stable_id_independent_of_asset_order() -> None:
    first_asset = {
        "asset_id": "ashare:600519",
        "symbol": "600519",
        "quality_status": "available",
        "payload": {"b": 2, "a": 1},
    }
    second_asset = {
        "asset_id": "ashare:000001",
        "symbol": "000001",
        "quality_status": "available",
        "payload": {"a": 1, "b": 2},
    }

    first = DecisionSnapshotBuilder().build(_inputs(assets=(first_asset, second_asset)))
    second = DecisionSnapshotBuilder().build(_inputs(assets=(second_asset, first_asset)))

    assert first.snapshot is not None
    assert second.snapshot is not None
    assert first.snapshot.decision_snapshot_id == second.snapshot.decision_snapshot_id
    assert first.snapshot.decision_snapshot_id.startswith("decision:ashare:2026-09-04:")


def test_one_invalid_asset_does_not_block_other_assets() -> None:
    result = DecisionSnapshotBuilder().build(
        _inputs(
            assets=(
                {
                    "asset_id": "ashare:600519",
                    "symbol": "600519",
                    "quality_status": "available",
                },
                {"asset_id": "ashare:000001", "quality_status": "available"},
            )
        )
    )

    assert result.status == "partial"
    assert result.snapshot is not None
    by_asset = {item["asset_id"]: item for item in result.snapshot.assets}
    assert by_asset["ashare:600519"]["data_quality"] == "available"
    assert by_asset["ashare:000001"]["data_quality"] == "unavailable"
    assert by_asset["ashare:000001"]["reason_codes"] == ["symbol_missing"]


def test_invalid_asset_preserves_previous_valid_state_as_stale() -> None:
    previous = {
        "asset_id": "ashare:600519",
        "symbol": "600519",
        "score": 76,
        "data_quality": "available",
    }
    result = DecisionSnapshotBuilder().build(
        _inputs(
            assets=(
                {
                    "asset_id": "ashare:600519",
                    "symbol": "600519",
                    "quality_status": "partial",
                    "score": 10,
                },
            ),
            previous_assets={"ashare:600519": previous},
        )
    )

    assert result.snapshot is not None
    asset = result.snapshot.assets[0]
    assert asset["score"] == 76
    assert asset["data_quality"] == "stale"
    assert asset["reason_codes"] == ["asset_quality_partial"]


def test_future_asset_fact_isolated_without_blocking_same_snapshot() -> None:
    result = DecisionSnapshotBuilder(maximum_skew=timedelta(minutes=5)).build(
        _inputs(
            assets=(
                {
                    "asset_id": "ashare:600519",
                    "symbol": "600519",
                    "quality_status": "available",
                    "as_of": AS_OF + timedelta(minutes=1),
                },
                {
                    "asset_id": "ashare:000001",
                    "symbol": "000001",
                    "quality_status": "available",
                    "as_of": AS_OF,
                },
            )
        )
    )

    assert result.status == "partial"
    assert result.snapshot is not None
    by_asset = {item["asset_id"]: item for item in result.snapshot.assets}
    assert by_asset["ashare:600519"]["data_quality"] == "unavailable"
    assert by_asset["ashare:600519"]["reason_codes"] == ["asset_future"]
    assert by_asset["ashare:000001"]["data_quality"] == "available"


def test_stale_asset_fact_isolated_at_maximum_skew() -> None:
    result = DecisionSnapshotBuilder(maximum_skew=timedelta(minutes=5)).build(
        _inputs(
            assets=(
                {
                    "asset_id": "ashare:600519",
                    "symbol": "600519",
                    "quality_status": "available",
                    "as_of": AS_OF - timedelta(minutes=6),
                },
            )
        )
    )

    assert result.status == "partial"
    assert result.snapshot is not None
    assert result.snapshot.assets[0]["data_quality"] == "unavailable"
    assert result.snapshot.assets[0]["reason_codes"] == ["asset_stale"]


def test_builder_persists_once_and_rejects_same_id_with_different_hash() -> None:
    class _Repository:
        def __init__(self) -> None:
            self.existing: object | None = None
            self.inserted: list[object] = []

        def get_snapshot(self, _snapshot_id: str) -> object | None:
            return self.existing

        def insert_snapshot(self, snapshot: object) -> object:
            self.inserted.append(snapshot)
            self.existing = snapshot
            return snapshot

    repository = _Repository()
    builder = DecisionSnapshotBuilder(repository=repository)

    first = builder.build(_inputs())
    second = builder.build(_inputs())

    assert first.snapshot is not None
    assert second.snapshot is not None
    assert len(repository.inserted) == 1
    assert repository.inserted[0].data_snapshot_id == first.snapshot.decision_snapshot_id

    repository.existing = SimpleNamespace(content_hash="different")
    with pytest.raises(DecisionSnapshotCollisionError):
        builder.build(_inputs())
