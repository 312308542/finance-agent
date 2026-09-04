"""冻结并校验收盘推荐使用的统一点时决策输入。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from finance_agent.storage.snapshot_contracts import (
    DataSnapshot,
    build_data_snapshot,
    canonical_json,
    json_safe,
    normalize_datetime,
)

JsonDict = dict[str, Any]


class DecisionSnapshotCollisionError(RuntimeError):
    """同一决策快照 ID 已存在不同内容。"""


class DecisionSnapshotRepository(Protocol):
    def get_snapshot(self, data_snapshot_id: str) -> Any | None: ...

    def insert_snapshot(self, snapshot: DataSnapshot) -> Any: ...


@dataclass(frozen=True)
class DecisionFact:
    """参与收盘决策的一份带点时与质量信息的冻结事实。"""

    data_snapshot_id: str
    as_of: datetime
    quality_status: str
    payload: Any


@dataclass(frozen=True)
class DecisionSnapshotInputs:
    """构建统一决策快照所需的全部显式输入。"""

    market: str
    as_of: datetime
    market_regime: DecisionFact | None
    sector_opportunities: DecisionFact | None
    structure: DecisionFact | None
    risk: DecisionFact | None
    assets: tuple[Mapping[str, Any], ...]
    data_versions: Mapping[str, Any]
    previous_assets: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class DecisionSnapshot:
    """通过全局点时门控后的不可变推荐输入。"""

    decision_snapshot_id: str
    market: str
    as_of: datetime
    market_regime: JsonDict
    sector_opportunities: tuple[JsonDict, ...]
    assets: tuple[JsonDict, ...]
    data_versions: JsonDict
    quality_status: Literal["available", "partial"]

    def to_storage_snapshot(self) -> DataSnapshot:
        """转换为现有 data_snapshots 仓储可直接保存的对象。"""

        payload = {
            "decision_snapshot_id": self.decision_snapshot_id,
            "market_regime": self.market_regime,
            "sector_opportunities": self.sector_opportunities,
            "assets": self.assets,
            "data_versions": self.data_versions,
        }
        snapshot = build_data_snapshot(
            snapshot_type="recommendation_decision_input",
            market=self.market,
            as_of=self.as_of,
            captured_at=self.as_of,
            provider="finance_agent:decision_snapshot",
            provider_version="decision-v1",
            quality_status=self.quality_status,
            payload=payload,
            metadata={"decision_snapshot_id": self.decision_snapshot_id},
        )
        return replace(snapshot, data_snapshot_id=self.decision_snapshot_id)


@dataclass(frozen=True)
class DecisionSnapshotBuildResult:
    """快照构建成功、部分可用或全局阻断结果。"""

    status: Literal["available", "partial", "blocked"]
    reason_codes: tuple[str, ...]
    snapshot: DecisionSnapshot | None


class DecisionSnapshotBuilder:
    """校验时点和质量后生成不可变决策快照。"""

    def __init__(
        self,
        *,
        maximum_skew: timedelta = timedelta(minutes=5),
        repository: DecisionSnapshotRepository | None = None,
    ) -> None:
        if maximum_skew < timedelta(0):
            raise ValueError("maximum_skew 不能小于 0")
        self.maximum_skew = maximum_skew
        self.repository = repository

    def build(self, inputs: DecisionSnapshotInputs) -> DecisionSnapshotBuildResult:
        """全局事实失败时阻断，单资产失败时保留隔离后的部分快照。"""

        market = str(inputs.market or "").strip()
        if not market:
            raise ValueError("market 不能为空")
        as_of = normalize_datetime(inputs.as_of, field_name="as_of")
        facts = {
            "market": inputs.market_regime,
            "sector": inputs.sector_opportunities,
            "structure": inputs.structure,
            "risk": inputs.risk,
        }
        global_reasons = tuple(
            reason
            for name, fact in facts.items()
            for reason in self._fact_reasons(name, fact, as_of=as_of)
        )
        if global_reasons:
            return DecisionSnapshotBuildResult(
                status="blocked",
                reason_codes=global_reasons,
                snapshot=None,
            )

        normalized_assets, asset_reasons = _normalize_assets(
            inputs.assets,
            previous_assets=inputs.previous_assets,
            decision_as_of=as_of,
            maximum_skew=self.maximum_skew,
        )
        quality_status: Literal["available", "partial"] = (
            "partial" if asset_reasons else "available"
        )
        market_fact = facts["market"]
        sector_fact = facts["sector"]
        assert market_fact is not None
        assert sector_fact is not None
        market_regime = _mapping_payload(market_fact.payload, field_name="market_regime")
        sectors = _sequence_payload(
            sector_fact.payload,
            field_name="sector_opportunities",
        )
        data_versions = {
            **dict(json_safe(dict(inputs.data_versions))),
            **{
                f"{name}_snapshot_id": fact.data_snapshot_id
                for name, fact in facts.items()
                if fact is not None
            },
        }
        identity = {
            "market": market,
            "as_of": as_of.isoformat(),
            "market_regime": market_regime,
            "sector_opportunities": sectors,
            "assets": normalized_assets,
            "data_versions": data_versions,
            "quality_status": quality_status,
        }
        digest = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()[:24]
        snapshot = DecisionSnapshot(
            decision_snapshot_id=f"decision:{market}:{as_of.date().isoformat()}:{digest}",
            market=market,
            as_of=as_of,
            market_regime=market_regime,
            sector_opportunities=sectors,
            assets=normalized_assets,
            data_versions=data_versions,
            quality_status=quality_status,
        )
        self._persist(snapshot)
        return DecisionSnapshotBuildResult(
            status=quality_status,
            reason_codes=asset_reasons,
            snapshot=snapshot,
        )

    def _fact_reasons(
        self,
        name: str,
        fact: DecisionFact | None,
        *,
        as_of: datetime,
    ) -> tuple[str, ...]:
        if fact is None:
            return (f"{name}_missing",)
        fact_as_of = normalize_datetime(fact.as_of, field_name=f"{name}.as_of")
        quality = str(fact.quality_status or "").strip()
        if fact_as_of > as_of:
            return (f"{name}_future",)
        if as_of - fact_as_of > self.maximum_skew:
            return (f"{name}_stale",)
        if quality != "available":
            return (f"{name}_quality_{quality or 'missing'}",)
        return ()

    def _persist(self, snapshot: DecisionSnapshot) -> None:
        if self.repository is None:
            return
        storage_snapshot = snapshot.to_storage_snapshot()
        existing = self.repository.get_snapshot(snapshot.decision_snapshot_id)
        if existing is not None:
            if str(existing.content_hash) != storage_snapshot.content_hash:
                raise DecisionSnapshotCollisionError(
                    f"决策快照 ID 内容冲突: {snapshot.decision_snapshot_id}"
                )
            return
        self.repository.insert_snapshot(storage_snapshot)


def _normalize_assets(
    assets: Sequence[Mapping[str, Any]],
    *,
    previous_assets: Mapping[str, Mapping[str, Any]],
    decision_as_of: datetime,
    maximum_skew: timedelta,
) -> tuple[tuple[JsonDict, ...], tuple[str, ...]]:
    normalized: list[JsonDict] = []
    all_reasons: list[str] = []
    for index, raw in enumerate(assets):
        row = dict(json_safe(dict(raw)))
        asset_id = str(row.get("asset_id") or f"invalid:{index}").strip()
        symbol = str(row.get("symbol") or "").strip()
        quality = str(row.get("quality_status") or "unavailable").strip()
        reasons: list[str] = []
        if not symbol:
            reasons.append("symbol_missing")
        if quality != "available":
            reasons.append(f"asset_quality_{quality or 'missing'}")
        asset_as_of_raw = row.get("as_of")
        if asset_as_of_raw is not None:
            parsed_asset_as_of = (
                datetime.fromisoformat(asset_as_of_raw.replace("Z", "+00:00"))
                if isinstance(asset_as_of_raw, str)
                else asset_as_of_raw
            )
            asset_as_of = normalize_datetime(
                parsed_asset_as_of,
                field_name=f"assets[{asset_id}].as_of",
            )
            if asset_as_of > decision_as_of:
                reasons.append("asset_future")
            elif decision_as_of - asset_as_of > maximum_skew:
                reasons.append("asset_stale")
        if reasons:
            previous = previous_assets.get(asset_id)
            if previous is not None:
                row = dict(json_safe(dict(previous)))
                row["asset_id"] = asset_id
                row["data_quality"] = "stale"
            else:
                row["asset_id"] = asset_id
                row["symbol"] = symbol
                row["data_quality"] = "unavailable"
            row["reason_codes"] = reasons
            all_reasons.extend(f"{asset_id}:{reason}" for reason in reasons)
        else:
            row["data_quality"] = "available"
            row["reason_codes"] = []
        normalized.append(row)
    normalized.sort(key=lambda item: (str(item.get("asset_id") or ""), canonical_json(item)))
    return tuple(normalized), tuple(dict.fromkeys(all_reasons))


def _mapping_payload(payload: Any, *, field_name: str) -> JsonDict:
    if not isinstance(payload, Mapping):
        raise ValueError(f"{field_name} 必须是对象")
    return dict(json_safe(dict(payload)))


def _sequence_payload(payload: Any, *, field_name: str) -> tuple[JsonDict, ...]:
    if not isinstance(payload, Sequence) or isinstance(payload, str | bytes | bytearray):
        raise ValueError(f"{field_name} 必须是数组")
    rows = [
        dict(json_safe(dict(item)))
        for item in payload
        if isinstance(item, Mapping)
    ]
    rows.sort(key=canonical_json)
    return tuple(rows)
