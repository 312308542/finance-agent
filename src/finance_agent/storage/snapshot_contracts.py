"""跨链路数据快照契约。

数据快照是推荐、风控和 Hermes 复核共同引用的只读事实版本。快照 ID
由规范化后的输入内容计算得到，同一批输入重复提交时得到相同 ID，避免
重试生成多个无法关联的事实版本。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

JsonDict = dict[str, Any]
SnapshotQuality = Literal[
    "available",
    "partial",
    "stale",
    "conflict",
    "unavailable",
    "invalid_server_time",
    "after_hours_snapshot",
    "clock_skew",
]

ALLOWED_SNAPSHOT_QUALITIES = frozenset(
    {
        "available",
        "partial",
        "stale",
        "conflict",
        "unavailable",
        "invalid_server_time",
        "after_hours_snapshot",
        "clock_skew",
    }
)


class SnapshotValidationError(ValueError):
    """快照输入不满足跨链路契约。"""


def normalize_datetime(value: datetime, *, field_name: str) -> datetime:
    """将时间统一为 UTC；无时区输入按 UTC 解释并保留可复现语义。"""

    if not isinstance(value, datetime):
        raise SnapshotValidationError(f"{field_name} 必须是 datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def json_safe(value: Any) -> Any:
    """把常见金融数据类型转换为稳定 JSON 值。"""

    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return normalize_datetime(value, field_name="datetime").isoformat()
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def canonical_json(value: Any) -> str:
    """生成跨进程稳定的 JSON 表示。"""

    return json.dumps(json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class DataSnapshot:
    """不可变的数据事实版本。"""

    data_snapshot_id: str
    snapshot_type: str
    market: str
    as_of: datetime
    captured_at: datetime
    provider: str
    provider_version: str | None
    quality_status: SnapshotQuality
    schema_version: str
    content_hash: str
    raw_record_ids: tuple[str, ...]
    payload: JsonDict
    metadata: JsonDict

    def to_record(self) -> JsonDict:
        """转换为 ORM 仓储使用的追加写入记录。"""

        return {
            "data_snapshot_id": self.data_snapshot_id,
            "snapshot_type": self.snapshot_type,
            "market": self.market,
            "as_of": self.as_of,
            "captured_at": self.captured_at,
            "provider": self.provider,
            "provider_version": self.provider_version,
            "quality_status": self.quality_status,
            "schema_version": self.schema_version,
            "content_hash": self.content_hash,
            "raw_record_ids": list(self.raw_record_ids),
            "payload": json_safe(self.payload),
            "metadata": json_safe(self.metadata),
        }


def build_data_snapshot(
    *,
    snapshot_type: str,
    market: str,
    as_of: datetime,
    captured_at: datetime,
    provider: str,
    quality_status: str,
    payload: JsonDict,
    provider_version: str | None = None,
    schema_version: str = "1",
    raw_record_ids: tuple[str, ...] | list[str] = (),
    metadata: JsonDict | None = None,
) -> DataSnapshot:
    """校验并构造确定性数据快照。"""

    normalized_type = str(snapshot_type).strip()
    normalized_market = str(market).strip()
    normalized_provider = str(provider).strip()
    normalized_quality = str(quality_status).strip()
    normalized_schema = str(schema_version).strip()
    if not normalized_type:
        raise SnapshotValidationError("snapshot_type 不能为空")
    if not normalized_market:
        raise SnapshotValidationError("market 不能为空")
    if not normalized_provider:
        raise SnapshotValidationError("provider 不能为空")
    if normalized_quality not in ALLOWED_SNAPSHOT_QUALITIES:
        raise SnapshotValidationError(f"quality_status 不支持: {normalized_quality}")
    if not normalized_schema:
        raise SnapshotValidationError("schema_version 不能为空")
    if not isinstance(payload, dict):
        raise SnapshotValidationError("payload 必须是 dict")

    normalized_as_of = normalize_datetime(as_of, field_name="as_of")
    normalized_captured_at = normalize_datetime(captured_at, field_name="captured_at")
    if normalized_captured_at < normalized_as_of:
        raise SnapshotValidationError("captured_at 不能早于 as_of")

    normalized_raw_ids = tuple(sorted({str(value).strip() for value in raw_record_ids if str(value).strip()}))
    normalized_metadata = dict(metadata or {})
    identity = {
        "snapshot_type": normalized_type,
        "market": normalized_market,
        "as_of": normalized_as_of.isoformat(),
        "captured_at": normalized_captured_at.isoformat(),
        "provider": normalized_provider,
        "provider_version": provider_version,
        "quality_status": normalized_quality,
        "schema_version": normalized_schema,
        "raw_record_ids": normalized_raw_ids,
        "payload": payload,
        "metadata": normalized_metadata,
    }
    content_hash = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
    snapshot_id = f"snapshot:{normalized_type}:{normalized_market}:{content_hash[:32]}"
    return DataSnapshot(
        data_snapshot_id=snapshot_id,
        snapshot_type=normalized_type,
        market=normalized_market,
        as_of=normalized_as_of,
        captured_at=normalized_captured_at,
        provider=normalized_provider,
        provider_version=str(provider_version).strip() if provider_version else None,
        quality_status=normalized_quality,  # type: ignore[arg-type]
        schema_version=normalized_schema,
        content_hash=content_hash,
        raw_record_ids=normalized_raw_ids,
        payload=json_safe(payload),
        metadata=json_safe(normalized_metadata),
    )


def snapshot_from_orm(record: Any) -> DataSnapshot:
    """把已持久化快照转换回只读领域对象。"""

    return DataSnapshot(
        data_snapshot_id=str(record.data_snapshot_id),
        snapshot_type=str(record.snapshot_type),
        market=str(record.market),
        as_of=normalize_datetime(record.as_of, field_name="as_of"),
        captured_at=normalize_datetime(record.captured_at, field_name="captured_at"),
        provider=str(record.provider),
        provider_version=record.provider_version,
        quality_status=str(record.quality_status),  # type: ignore[arg-type]
        schema_version=str(record.schema_version),
        content_hash=str(record.content_hash),
        raw_record_ids=tuple(str(item) for item in (record.raw_record_ids or ())),
        payload=dict(record.payload or {}),
        metadata=dict(getattr(record, "snapshot_metadata", {}) or {}),
    )
