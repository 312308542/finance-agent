"""基础数据采集运行控制。

这里封装任务锁、Provider 熔断状态和采集摘要，避免脚本、调度器和后续
后台任务各自直接操作 Redis 键。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from finance_agent.data.collectors import ArchivedProviderResult
from finance_agent.ports.cache import CacheClient, LockClient

JsonDict = dict[str, Any]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CollectionTaskResult:
    """单个采集任务的摘要结果。"""

    task: str
    status: str
    raw_record_id: str | None
    item_count: int
    error_message: str | None
    payload: JsonDict


@dataclass(frozen=True)
class ProviderCircuitPolicy:
    """Provider 熔断策略。"""

    failure_threshold: int = 3
    cooldown_seconds: int = 900
    state_ttl_seconds: int = 86400


@dataclass(frozen=True)
class CollectionRuntime:
    """采集任务运行控制器。"""

    cache: CacheClient
    locks: LockClient
    lock_ttl_seconds: int = 600
    circuit_policy: ProviderCircuitPolicy = field(default_factory=ProviderCircuitPolicy)

    def run_task(
        self,
        *,
        task: str,
        provider_key: str,
        parameters: JsonDict,
        collect: Any,
        force: bool = False,
    ) -> CollectionTaskResult:
        """带任务锁和 Provider 熔断保护地执行采集任务。"""

        logger.info(
            "采集任务开始 task=%s provider=%s parameters=%s force=%s",
            task,
            provider_key,
            parameters,
            force,
        )
        circuit_state = self.get_provider_state(provider_key)
        if self.is_circuit_open(circuit_state) and not force:
            result = CollectionTaskResult(
                task=task,
                status="skipped",
                raw_record_id=None,
                item_count=0,
                error_message="Provider 熔断中，跳过本次采集",
                payload={
                    "provider_key": provider_key,
                    "circuit_state": circuit_state,
                },
            )
            logger.warning(
                "采集任务跳过 task=%s provider=%s status=%s error=%s",
                task,
                provider_key,
                result.status,
                result.error_message,
            )
            return result

        lock_key = collection_lock_key(task=task, parameters=parameters)
        if not self.locks.acquire_lock(lock_key, ttl_seconds=self.lock_ttl_seconds):
            result = CollectionTaskResult(
                task=task,
                status="locked",
                raw_record_id=None,
                item_count=0,
                error_message="同参数采集任务正在运行",
                payload={"provider_key": provider_key, "lock_key": lock_key},
            )
            logger.warning(
                "采集任务被锁定 task=%s provider=%s lock_key=%s status=%s",
                task,
                provider_key,
                lock_key,
                result.status,
            )
            return result

        try:
            archive: ArchivedProviderResult | Sequence[ArchivedProviderResult] = collect()
            result = summarize_archive(task, archive)
            self.record_provider_result(provider_key=provider_key, result=result)
            log_method = logger.info if result.status == "available" else logger.warning
            log_method(
                "采集任务完成 task=%s provider=%s status=%s item_count=%s raw_record_id=%s "
                "source=%s error=%s",
                task,
                provider_key,
                result.status,
                result.item_count,
                result.raw_record_id,
                result.payload.get("actual_source"),
                result.error_message,
            )
            return result
        except Exception:
            logger.exception(
                "采集任务异常 task=%s provider=%s parameters=%s",
                task,
                provider_key,
                parameters,
            )
            raise
        finally:
            self.locks.release_lock(lock_key)

    def get_provider_state(self, provider_key: str) -> JsonDict:
        """读取 Provider 熔断状态。"""

        cached = self.cache.get_json(provider_state_key(provider_key))
        return cached if isinstance(cached, dict) else {}

    def is_circuit_open(self, state: JsonDict) -> bool:
        """判断 Provider 是否处于熔断冷却期。"""

        if state.get("status") != "open":
            return False
        opened_until = parse_datetime(state.get("opened_until"))
        if opened_until is None:
            return False
        return opened_until > datetime.now(tz=UTC)

    def record_provider_result(
        self,
        *,
        provider_key: str,
        result: CollectionTaskResult,
    ) -> None:
        """根据采集结果更新 Provider 熔断状态。"""

        state = self.get_provider_state(provider_key)
        now = datetime.now(tz=UTC)
        if result.status == "available":
            next_state = {
                "status": "closed",
                "failure_count": 0,
                "last_status": result.status,
                "last_raw_record_id": result.raw_record_id,
                "last_error_message": None,
                "updated_at": now.isoformat(),
            }
        elif result.status == "error":
            failure_count = int(state.get("failure_count") or 0) + 1
            next_state = {
                "status": "closed",
                "failure_count": failure_count,
                "last_status": result.status,
                "last_raw_record_id": result.raw_record_id,
                "last_error_message": result.error_message,
                "updated_at": now.isoformat(),
            }
            if failure_count >= self.circuit_policy.failure_threshold:
                opened_until = now.timestamp() + self.circuit_policy.cooldown_seconds
                next_state["status"] = "open"
                next_state["opened_until"] = datetime.fromtimestamp(
                    opened_until,
                    tz=UTC,
                ).isoformat()
        else:
            next_state = state | {
                "status": "closed",
                "failure_count": int(state.get("failure_count") or 0),
                "last_status": result.status,
                "last_raw_record_id": result.raw_record_id,
                "last_error_message": result.error_message,
                "updated_at": now.isoformat(),
            }

        self.cache.set_json(
            provider_state_key(provider_key),
            next_state,
            ttl_seconds=self.circuit_policy.state_ttl_seconds,
        )

    def list_provider_states(self, provider_keys: list[str]) -> list[JsonDict]:
        """批量读取 Provider 熔断状态。"""

        states: list[JsonDict] = []
        for provider_key in provider_keys:
            state = self.get_provider_state(provider_key)
            states.append(
                {
                    "provider_key": provider_key,
                    "status": state.get("status", "unknown"),
                    "failure_count": state.get("failure_count", 0),
                    "opened_until": state.get("opened_until"),
                    "last_status": state.get("last_status"),
                    "last_raw_record_id": state.get("last_raw_record_id"),
                    "last_error_message": state.get("last_error_message"),
                    "updated_at": state.get("updated_at"),
                }
            )
        return states


def summarize_archive(
    task: str,
    archive: ArchivedProviderResult | Sequence[ArchivedProviderResult],
) -> CollectionTaskResult:
    """把带归档编号的 Provider 结果压缩成命令输出摘要。"""

    if not is_single_archived_provider_result(archive):
        return summarize_archives(task, archive)

    result = archive.result
    return CollectionTaskResult(
        task=task,
        status=result.status,
        raw_record_id=archive.raw_record_id,
        item_count=infer_item_count(result),
        error_message=result.error_message,
        payload={
            "actual_source": result.payload.get("actual_source"),
            "fallback_used": result.payload.get("fallback_used"),
            "source_coverage": result.payload.get("source_coverage"),
            "rate_limited": result.payload.get("rate_limited"),
        }
        | infer_time_range_payload(result),
    )


def is_single_archived_provider_result(value: Any) -> bool:
    """判断对象是否为单个已归档 Provider 结果，兼容脚本内轻量包装。"""

    return hasattr(value, "result") and hasattr(value, "raw_record_id")


def summarize_archives(
    task: str,
    archives: Sequence[ArchivedProviderResult],
) -> CollectionTaskResult:
    """把多 Provider 归档结果合并成一个任务摘要。"""

    if not archives:
        return CollectionTaskResult(
            task=task,
            status="unavailable",
            raw_record_id=None,
            item_count=0,
            error_message="没有 Provider 归档结果",
            payload={"raw_record_ids": [], "source_results": []},
        )

    source_results: list[JsonDict] = []
    raw_record_ids: list[str] = []
    actual_sources: list[str] = []
    error_messages: list[str] = []
    item_count = 0
    statuses: list[str] = []

    for archived in archives:
        result = archived.result
        count = infer_item_count(result)
        actual_source = result.payload.get("actual_source")
        source_results.append(
            {
                "provider_name": result.provider_name,
                "status": result.status,
                "raw_record_id": archived.raw_record_id,
                "item_count": count,
                "actual_source": actual_source,
                "error_message": result.error_message,
            }
            | infer_time_range_payload(result)
        )
        raw_record_ids.append(archived.raw_record_id)
        statuses.append(result.status)
        if result.status == "available":
            item_count += count
            if actual_source:
                actual_sources.append(str(actual_source))
        elif result.error_message:
            error_messages.append(result.error_message)

    status = merge_archive_statuses(statuses)
    return CollectionTaskResult(
        task=task,
        status=status,
        raw_record_id=None,
        item_count=item_count,
        error_message=None if status == "available" else "; ".join(error_messages) or None,
        payload={
            "actual_source": actual_sources,
            "fallback_used": any(
                bool(archived.result.payload.get("fallback_used")) for archived in archives
            ),
            "source_coverage": [
                archived.result.payload.get("source_coverage")
                for archived in archives
                if archived.result.payload.get("source_coverage") is not None
            ],
            "rate_limited": any(
                bool(archived.result.payload.get("rate_limited")) for archived in archives
            ),
            "raw_record_ids": raw_record_ids,
            "source_results": source_results,
        },
    )


def merge_archive_statuses(statuses: Sequence[str]) -> str:
    """合并多个 Provider 状态，任一可用源即可让任务摘要可用。"""

    if any(status == "available" for status in statuses):
        return "available"
    if any(status == "error" for status in statuses):
        return "error"
    if any(status == "unavailable" for status in statuses):
        return "unavailable"
    return statuses[0] if statuses else "unavailable"


def infer_item_count(result: Any) -> int:
    """根据 ProviderResult 子类型推断采集条数。"""

    for attr_name in ("assets", "bars", "seeds", "snapshots", "risks", "events", "evidence"):
        value = getattr(result, attr_name, None)
        if value:
            return len(value)
    if getattr(result, "snapshot", None) is not None:
        return 1
    return 0


def infer_time_range_payload(result: Any) -> JsonDict:
    """从 Provider 结果中提取最早和最新数据日期。"""

    bars = getattr(result, "bars", None)
    if bars:
        timestamps = [bar.timestamp for bar in bars if getattr(bar, "timestamp", None) is not None]
        if timestamps:
            return {
                "earliest_at": min(timestamps).isoformat(),
                "latest_at": max(timestamps).isoformat(),
            }
    snapshots = getattr(result, "snapshots", None)
    if snapshots:
        nav_dates = [
            snapshot.nav_date
            for snapshot in snapshots
            if getattr(snapshot, "nav_date", None) is not None
        ]
        if nav_dates:
            return {
                "earliest_at": min(nav_dates).isoformat(),
                "latest_at": max(nav_dates).isoformat(),
            }
    return {}


def provider_state_key(provider_key: str) -> str:
    """生成 Provider 熔断状态缓存键。"""

    return f"provider_state:{provider_key}"


def collection_lock_key(*, task: str, parameters: JsonDict) -> str:
    """生成采集任务锁键。"""

    normalized = ":".join(f"{key}={parameters[key]}" for key in sorted(parameters))
    return f"base_data_collect:{task}:{normalized}"


def parse_datetime(value: Any) -> datetime | None:
    """解析 ISO datetime 字符串。"""

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
