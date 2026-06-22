"""基础数据调度器运行态进度读写。"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from finance_agent.cache import create_cache_client

JsonDict = dict[str, Any]

CURRENT_KEY_PREFIX = "base_data:task:{job_name}:current"
SNAPSHOT_KEY_PREFIX = "base_data:task:{job_name}:run:{run_id}:snapshot"
EVENTS_KEY_PREFIX = "base_data:task:{job_name}:run:{run_id}:events"
ACTIVE_KEY = "base_data:task:active"
SOURCE_RATE_KEY_PREFIX = "base_data:source_rate:{source_key}:snapshot"
SOURCE_RATE_ACTIVE_KEY = "base_data:source_rate:active"
EVENT_LIMIT_DEFAULT = 80
EVENT_LIMIT_MAX = 200
PROGRESS_TTL_GRACE_SECONDS = 1800
PROGRESS_TTL_MIN_SECONDS = 900
PROGRESS_EVENT_LIMIT = 200


class ProgressCacheClient(Protocol):
    """进度缓存所需的最小接口。"""

    def get_json(self, key: str) -> dict | list | str | int | float | bool | None:
        """读取 JSON 值。"""

    def set_json(self, key: str, value: object, *, ttl_seconds: int | None = None) -> None:
        """写入 JSON 值。"""

    def delete(self, key: str) -> None:
        """删除键。"""

    def append_json(
        self,
        key: str,
        value: object,
        *,
        ttl_seconds: int | None = None,
        max_length: int | None = None,
    ) -> None:
        """追加 JSON 列表项。"""

    def list_json(self, key: str, *, limit: int | None = None) -> list[Any]:
        """读取 JSON 列表。"""

    def expire(self, key: str, ttl_seconds: int) -> None:
        """设置 TTL。"""


@dataclass
class BaseDataTaskProgressRecorder:
    """基础数据调度器的 Redis 进度记录器。"""

    cache: Any
    cache_backend: str = "redis"
    event_limit: int = PROGRESS_EVENT_LIMIT
    now_func: Any = datetime.now
    _disabled: bool = False
    _last_run_ids: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_cache_client(cls, cache: Any, *, cache_backend: str) -> BaseDataTaskProgressRecorder:
        """根据缓存后端构建记录器。"""

        disabled = cache_backend != "redis" or cache is None
        return cls(cache=cache, cache_backend=cache_backend, _disabled=disabled)

    @classmethod
    def create(cls, *, backend: str = "auto") -> BaseDataTaskProgressRecorder:
        """创建可直接使用的记录器。"""

        cache, _, cache_status = create_cache_client(backend=backend)
        return cls.from_cache_client(cache, cache_backend=cache_status.backend)

    def job_started(
        self,
        *,
        job_name: str,
        title: str,
        market: str | None,
        task_type: str,
        interval_seconds: int,
        max_workers: int | None = None,
        total_items: int | None = None,
    ) -> str | None:
        """记录任务启动，并清理同名旧运行。"""

        run_id = self._build_run_id(job_name)
        if self._disabled:
            return run_id
        now = self._now()
        ttl_seconds = progress_ttl_seconds(interval_seconds)
        current_key = current_task_key(job_name)
        snapshot_key = snapshot_task_key(job_name, run_id)

        old_run_id = self._read_current_run_id(job_name)
        if old_run_id and old_run_id != run_id:
            self._delete_run(job_name=job_name, run_id=old_run_id)

        snapshot = self._build_snapshot(
            job_name=job_name,
            run_id=run_id,
            title=title,
            market=market,
            task_type=task_type,
            interval_seconds=interval_seconds,
            status="running",
            started_at=now,
            updated_at=now,
            finished_at=None,
            total_items=total_items or 0,
            completed_items=0,
            running_items=0,
            failed_items=0,
            remaining_items=total_items or 0,
            max_workers=max_workers,
        )
        self._set_json(current_key, run_id, ttl_seconds=ttl_seconds)
        self._set_json(snapshot_key, snapshot, ttl_seconds=ttl_seconds)
        self._append_event(
            job_name=job_name,
            run_id=run_id,
            event_type="job_started",
            created_at=now,
            ttl_seconds=ttl_seconds,
            payload={
                "title": title,
                "market": market,
                "task_type": task_type,
                "interval_seconds": interval_seconds,
                "max_workers": max_workers,
                "total_items": total_items,
            },
        )
        self._update_active_index(
            job_name=job_name,
            run_id=run_id,
            status="running",
            ttl_seconds=ttl_seconds,
            updated_at=now,
        )
        return run_id

    def batch_started(
        self,
        *,
        job_name: str,
        run_id: str | None,
        stage_key: str,
        total_items: int,
        batch_index: int,
        batch_count: int,
        batch_size: int,
        max_workers: int | None = None,
    ) -> None:
        """记录批次开始。"""

        if self._disabled or not run_id:
            return
        now = self._now()
        snapshot = self._load_snapshot(job_name, run_id)
        if not snapshot:
            return
        ttl_seconds = self._snapshot_ttl(snapshot)
        stage = self._stage_entry(
            snapshot,
            stage_key=stage_key,
            status="running",
            updated_at=now,
            total_items=total_items,
        )
        stage_pending_items = max(
            0,
            int(stage.get("total_items") or total_items)
            - int(stage.get("completed_items") or 0)
            - int(stage.get("failed_items") or 0),
        )
        running_items = min(max(int(batch_size), 0), stage_pending_items)
        snapshot["updated_at"] = now.isoformat()
        snapshot["status"] = "running"
        snapshot["batch_index"] = batch_index
        snapshot["batch_count"] = batch_count
        snapshot["batch_size"] = batch_size
        if max_workers is not None:
            snapshot["max_workers"] = max(int(max_workers), 1)
        stage["running_items"] = running_items
        snapshot["stages"] = self._replace_stage(snapshot.get("stages"), stage)
        self._refresh_aggregate_from_stages(snapshot)
        self._refresh_metrics(snapshot, now=now)
        self._save_snapshot(job_name, run_id, snapshot, ttl_seconds=ttl_seconds)
        self._append_event(
            job_name=job_name,
            run_id=run_id,
            event_type="batch_started",
            created_at=now,
            ttl_seconds=ttl_seconds,
            payload={
                "stage_key": stage_key,
                "total_items": total_items,
                "batch_index": batch_index,
                "batch_count": batch_count,
                "batch_size": batch_size,
                "max_workers": max_workers,
            },
        )

    def stage_planned(
        self,
        *,
        job_name: str,
        run_id: str | None,
        stage_key: str,
        total_items: int,
    ) -> None:
        """预先登记阶段总量，用于多阶段任务展示完整总进度。"""

        if self._disabled or not run_id or total_items <= 0:
            return
        now = self._now()
        snapshot = self._load_snapshot(job_name, run_id)
        if not snapshot:
            return
        ttl_seconds = self._snapshot_ttl(snapshot)
        stage = self._stage_entry(
            snapshot,
            stage_key=stage_key,
            status="waiting",
            updated_at=now,
            total_items=total_items,
        )
        stage["running_items"] = 0
        snapshot["stages"] = self._replace_stage(snapshot.get("stages"), stage)
        self._refresh_aggregate_from_stages(snapshot)
        snapshot["updated_at"] = now.isoformat()
        self._refresh_metrics(snapshot, now=now)
        self._save_snapshot(job_name, run_id, snapshot, ttl_seconds=ttl_seconds)
        self._append_event(
            job_name=job_name,
            run_id=run_id,
            event_type="stage_planned",
            created_at=now,
            ttl_seconds=ttl_seconds,
            payload={"stage_key": stage_key, "total_items": total_items},
        )

    def symbol_started(
        self,
        *,
        job_name: str,
        run_id: str | None,
        stage_key: str,
        symbol: str,
        batch_index: int,
        batch_count: int,
    ) -> None:
        """记录单个标的开始。"""

        if self._disabled or not run_id:
            return
        now = self._now()
        snapshot = self._load_snapshot(job_name, run_id)
        if not snapshot:
            return
        ttl_seconds = self._snapshot_ttl(snapshot)
        current_items = list(snapshot.get("current_items") or [])
        current_items.append(
            {
                "symbol": symbol,
                "status": "running",
                "started_at": now.isoformat(),
                "stage_key": stage_key,
                "batch_index": batch_index,
                "batch_count": batch_count,
            }
        )
        snapshot["current_items"] = current_items
        snapshot["running_items"] = int(snapshot.get("running_items") or 0) + 1
        snapshot["updated_at"] = now.isoformat()
        stage = self._stage_entry(
            snapshot,
            stage_key=stage_key,
            status="running",
            updated_at=now,
            delta_running=1,
        )
        snapshot["stages"] = self._replace_stage(snapshot.get("stages"), stage)
        self._save_snapshot(job_name, run_id, snapshot, ttl_seconds=ttl_seconds)
        self._append_event(
            job_name=job_name,
            run_id=run_id,
            event_type="symbol_started",
            created_at=now,
            ttl_seconds=ttl_seconds,
            payload={
                "stage_key": stage_key,
                "symbol": symbol,
                "batch_index": batch_index,
                "batch_count": batch_count,
            },
        )

    def symbol_completed(
        self,
        *,
        job_name: str,
        run_id: str | None,
        stage_key: str,
        symbol: str,
        status: str,
        item_count: int,
        batch_index: int,
        batch_count: int,
        error_message: str | None = None,
        retry_count: int | None = None,
        retry_after_seconds: int | float | None = None,
        next_retry_at: str | None = None,
        provider_key: str | None = None,
        error_category: str | None = None,
    ) -> None:
        """记录单个标的完成或失败。"""

        if self._disabled or not run_id:
            return
        now = self._now()
        snapshot = self._load_snapshot(job_name, run_id)
        if not snapshot:
            return
        ttl_seconds = self._snapshot_ttl(snapshot)
        completed = int(snapshot.get("completed_items") or 0)
        failed = int(snapshot.get("failed_items") or 0)
        retry_items = int(snapshot.get("retry_items") or 0)
        running = max(0, int(snapshot.get("running_items") or 0) - 1)
        if status in {"completed", "available", "skipped", "locked"}:
            completed += 1
            event_type = "symbol_completed"
            stage_status = "completed"
        else:
            failed += 1
            event_type = "symbol_failed"
            stage_status = "failed"
            if retry_after_seconds is not None or next_retry_at or (retry_count or 0) > 0:
                retry_items += 1
        total_items = int(snapshot.get("total_items") or 0)
        current_items = [
            item
            for item in list(snapshot.get("current_items") or [])
            if str(item.get("symbol")) != symbol
        ]
        snapshot["current_items"] = current_items
        snapshot["completed_items"] = completed
        snapshot["failed_items"] = failed
        snapshot["retry_items"] = retry_items
        snapshot["running_items"] = running
        snapshot["remaining_items"] = max(total_items - completed - failed, 0) if total_items else 0
        snapshot["progress_ratio"] = self._progress_ratio(total_items, completed, failed)
        snapshot["updated_at"] = now.isoformat()
        snapshot["stages"] = self._replace_stage(
            snapshot.get("stages"),
            self._stage_entry(
                snapshot,
                stage_key=stage_key,
                status=stage_status,
                updated_at=now,
                delta_running=-1,
                delta_completed=1 if stage_status == "completed" else 0,
                delta_failed=1 if stage_status == "failed" else 0,
            ),
        )
        self._refresh_aggregate_from_stages(snapshot)
        self._refresh_metrics(snapshot, now=now)
        self._save_snapshot(job_name, run_id, snapshot, ttl_seconds=ttl_seconds)
        self._append_event(
            job_name=job_name,
            run_id=run_id,
            event_type=event_type,
            created_at=now,
            ttl_seconds=ttl_seconds,
            payload={
                "stage_key": stage_key,
                "symbol": symbol,
                "status": status,
                "item_count": item_count,
                "batch_index": batch_index,
                "batch_count": batch_count,
                "error_message": error_message,
                "retry_count": retry_count,
                "retry_after_seconds": retry_after_seconds,
                "next_retry_at": next_retry_at,
                "provider_key": provider_key,
                "error_category": error_category,
            },
        )

    def job_completed(
        self,
        *,
        job_name: str,
        run_id: str | None,
        status: str,
        summary: JsonDict | None = None,
        error_message: str | None = None,
    ) -> None:
        """记录任务完成或失败。"""

        if self._disabled or not run_id:
            return
        now = self._now()
        snapshot = self._load_snapshot(job_name, run_id)
        if not snapshot:
            return
        ttl_seconds = self._snapshot_ttl(snapshot)
        summary = summary or {}
        if "total_items" in summary and not snapshot.get("total_items"):
            snapshot["total_items"] = int(summary.get("total_items") or 0)
        snapshot["completed_items"] = max(
            int(snapshot.get("completed_items") or 0),
            int(summary.get("completed_items") or summary.get("available_items") or 0),
        )
        snapshot["failed_items"] = max(
            int(snapshot.get("failed_items") or 0),
            int(summary.get("failed_items") or 0),
        )
        snapshot["running_items"] = 0
        total_items = int(snapshot.get("total_items") or 0)
        completed = int(snapshot.get("completed_items") or 0)
        failed = int(snapshot.get("failed_items") or 0)
        snapshot["remaining_items"] = max(total_items - completed - failed, 0)
        snapshot["progress_ratio"] = self._progress_ratio(total_items, completed, failed)
        snapshot["status"] = "completed" if status == "completed" else "failed"
        snapshot["finished_at"] = now.isoformat()
        snapshot["updated_at"] = now.isoformat()
        snapshot["error_message"] = error_message
        self._refresh_metrics(snapshot, now=now)
        self._save_snapshot(job_name, run_id, snapshot, ttl_seconds=ttl_seconds)
        self._append_event(
            job_name=job_name,
            run_id=run_id,
            event_type="job_completed" if status == "completed" else "job_failed",
            created_at=now,
            ttl_seconds=ttl_seconds,
            payload={
                "status": status,
                "summary": summary,
                "error_message": error_message,
            },
        )
        self._update_active_index(
            job_name=job_name,
            run_id=run_id,
            status=snapshot["status"],
            ttl_seconds=ttl_seconds,
            updated_at=now,
        )

    def job_failed(
        self,
        *,
        job_name: str,
        run_id: str | None,
        error_message: str,
        summary: JsonDict | None = None,
    ) -> None:
        """记录任务失败。"""

        self.job_completed(
            job_name=job_name,
            run_id=run_id,
            status="failed",
            summary=summary,
            error_message=error_message,
        )

    def job_cancelled(self, *, job_name: str, error_message: str) -> None:
        """把当前运行中的任务标记为用户取消，避免任务监控页长期停留在 running。"""

        if self._disabled:
            return
        run_id = self._read_current_run_id(job_name) or self._read_active_run_id(job_name)
        self.job_completed(
            job_name=job_name,
            run_id=run_id,
            status="cancelled",
            summary={},
            error_message=error_message,
        )

    def job_paused(self, *, job_name: str, message: str) -> None:
        """把当前任务标记为暂停，但不写 finished_at，便于后续断点继续。"""

        self._set_current_job_runtime_status(
            job_name=job_name,
            status="paused",
            event_type="job_paused",
            message=message,
        )

    def job_resumed(self, *, job_name: str, message: str) -> None:
        """把暂停任务恢复为运行中，并追加继续事件。"""

        self._set_current_job_runtime_status(
            job_name=job_name,
            status="running",
            event_type="job_resumed",
            message=message,
        )

    def source_rate_updated(
        self,
        *,
        source_key: str,
        snapshot: JsonDict,
        ttl_seconds: int | None = None,
    ) -> None:
        """记录数据源运行期限频和退避状态。"""

        if self._disabled:
            return
        normalized_source = str(source_key or "default").strip() or "default"
        now = self._now()
        payload = {"source_key": normalized_source, **dict(snapshot), "updated_at": now.isoformat()}
        ttl = ttl_seconds or PROGRESS_TTL_MIN_SECONDS
        self._set_json(source_rate_key(normalized_source), payload, ttl_seconds=ttl)
        active_payload = self._safe_get_json(SOURCE_RATE_ACTIVE_KEY)
        active_items = [
            item
            for item in active_items_from_payload(active_payload)
            if str(item.get("source_key") or "") != normalized_source
        ]
        active_items.append(
            {
                "source_key": normalized_source,
                "updated_at": now.isoformat(),
            }
        )
        self._set_json(SOURCE_RATE_ACTIVE_KEY, active_items, ttl_seconds=ttl)

    def build_snapshot_task_view(
        self,
        *,
        job_name: str,
        run_id: str,
        snapshot: JsonDict,
        events: list[Any],
    ) -> JsonDict:
        """将快照转换为 API 输出。"""

        summary = {
            "total_items": int(snapshot.get("total_items") or 0),
            "completed_items": int(snapshot.get("completed_items") or 0),
            "running_items": int(snapshot.get("running_items") or 0),
            "failed_items": int(snapshot.get("failed_items") or 0),
            "retry_items": int(snapshot.get("retry_items") or 0),
            "remaining_items": int(snapshot.get("remaining_items") or 0),
            "progress_ratio": float(snapshot.get("progress_ratio") or 0.0),
        }
        return {
            "job_name": job_name,
            "run_id": run_id,
            "title": snapshot.get("title") or job_name,
            "market": snapshot.get("market"),
            "task_type": snapshot.get("task_type"),
            "status": snapshot.get("status") or "unknown",
            "interval_seconds": snapshot.get("interval_seconds"),
            "started_at": snapshot.get("started_at"),
            "updated_at": snapshot.get("updated_at"),
            "finished_at": snapshot.get("finished_at"),
            "batch_index": snapshot.get("batch_index"),
            "batch_count": snapshot.get("batch_count"),
            "batch_size": snapshot.get("batch_size"),
            "max_workers": int(snapshot.get("max_workers") or 0),
            "throughput_per_minute": float(
                (snapshot.get("metrics") or {}).get("throughput_per_minute") or 0.0
            ),
            "summary": summary,
            "stages": list(snapshot.get("stages") or []),
            "recent_events": events[-self._event_limit_for_read():],
            "metrics": snapshot.get("metrics") or {},
        }

    def read_source_rate_states(self) -> list[JsonDict]:
        """读取当前数据源限频和退避状态。"""

        active_payload = self._safe_get_json(SOURCE_RATE_ACTIVE_KEY)
        states: list[JsonDict] = []
        for item in active_items_from_payload(active_payload):
            source_key = str(item.get("source_key") or "").strip()
            if not source_key:
                continue
            snapshot = self._safe_get_json(source_rate_key(source_key))
            if isinstance(snapshot, dict):
                states.append(dict(snapshot))
        return states

    def read_scheduler_progress(
        self,
        *,
        event_limit: int = EVENT_LIMIT_DEFAULT,
        scheduler_jobs: list[Any] | tuple[Any, ...] | None = None,
        redis_error_message: str | None = None,
    ) -> JsonDict:
        """读取当前运行态进度。"""

        if self._disabled:
            return {
                "status": "degraded",
                "message": redis_error_message or "Redis 不可用，无法读取实时进度。",
                "data": {
                    "cache_backend": self.cache_backend,
                    "tasks": [],
                    "waiting": [],
                },
            }

        limit = clamp_event_limit(event_limit)
        self.event_limit = limit
        now = self._now()
        active_payload = self._safe_get_json(ACTIVE_KEY)
        active_items = active_items_from_payload(active_payload)
        tasks: list[JsonDict] = []
        running_job_names: set[str] = set()
        for item in active_items:
            job_name = str(item.get("job_name") or "").strip()
            run_id = str(item.get("run_id") or "").strip()
            if not job_name or not run_id:
                continue
            snapshot = self._safe_get_json(snapshot_task_key(job_name, run_id))
            if not isinstance(snapshot, dict):
                continue
            events = self._safe_list_json(events_task_key(job_name, run_id), limit=limit)
            tasks.append(
                self.build_snapshot_task_view(
                    job_name=job_name,
                    run_id=run_id,
                    snapshot=snapshot,
                    events=events,
                )
            )
            if snapshot.get("status") == "running":
                running_job_names.add(job_name)
        waiting = self._build_waiting_jobs(
            scheduler_jobs=scheduler_jobs or (),
            running_job_names=running_job_names,
        )
        metrics = {
            "running_count": len(running_job_names),
            "waiting_count": len(waiting),
            "failed_count": sum(1 for task in tasks if task["status"] == "failed"),
            "completed_recent_count": sum(1 for task in tasks if task["status"] == "completed"),
        }
        return {
            "status": "ok",
            "data": {
                "cache_backend": self.cache_backend,
                "generated_at": now.isoformat(),
                "tasks": tasks,
                "waiting": waiting,
                "source_rate_states": self.read_source_rate_states(),
                "metrics": metrics,
            },
        }

    def _build_waiting_jobs(
        self,
        *,
        scheduler_jobs: list[Any] | tuple[Any, ...],
        running_job_names: set[str],
    ) -> list[JsonDict]:
        waiting: list[JsonDict] = []
        for job in scheduler_jobs:
            schedule_type = str(getattr(job, "schedule_type", "interval") or "interval")
            is_manual = schedule_type == "manual"
            if not getattr(job, "enabled", True) and not is_manual:
                continue
            job_name = str(getattr(job, "name", "") or "").strip()
            if not job_name or job_name in running_job_names:
                continue
            waiting.append(
                {
                    "job_name": job_name,
                    "title": str(getattr(job, "title", None) or job_name),
                    "status": "waiting",
                    "interval_seconds": int(getattr(job, "interval_seconds", 0) or 0),
                }
            )
        return waiting

    def _event_limit_for_read(self) -> int:
        return clamp_event_limit(self.event_limit)

    def _build_run_id(self, job_name: str) -> str:
        timestamp = self._now().strftime("%Y%m%dT%H%M%S")
        short_hash = secrets.token_hex(2)
        return f"{job_name}:{timestamp}:{short_hash}"

    def _now(self) -> datetime:
        return datetime.now(tz=UTC)

    def _read_current_run_id(self, job_name: str) -> str | None:
        current = self._safe_get_json(current_task_key(job_name))
        return current if isinstance(current, str) and current else None

    def _read_active_run_id(self, job_name: str) -> str | None:
        active_payload = self._safe_get_json(ACTIVE_KEY)
        active_items = active_items_from_payload(active_payload)
        for item in reversed(active_items):
            if str(item.get("job_name") or "").strip() != job_name:
                continue
            run_id = item.get("run_id")
            if isinstance(run_id, str) and run_id:
                return run_id
        return None

    def _delete_run(self, *, job_name: str, run_id: str) -> None:
        self._safe_delete(snapshot_task_key(job_name, run_id))
        self._safe_delete(events_task_key(job_name, run_id))

    def _save_snapshot(
        self,
        job_name: str,
        run_id: str,
        snapshot: JsonDict,
        *,
        ttl_seconds: int,
    ) -> None:
        self._set_json(snapshot_task_key(job_name, run_id), snapshot, ttl_seconds=ttl_seconds)
        self._update_active_index(
            job_name=job_name,
            run_id=run_id,
            status=str(snapshot.get("status") or "unknown"),
            ttl_seconds=ttl_seconds,
            updated_at=parse_iso_datetime(snapshot.get("updated_at")),
        )

    def _set_current_job_runtime_status(
        self,
        *,
        job_name: str,
        status: str,
        event_type: str,
        message: str,
    ) -> None:
        """更新当前任务的运行期状态；暂停/继续不改变完成时间。"""

        if self._disabled:
            return
        run_id = self._read_current_run_id(job_name) or self._read_active_run_id(job_name)
        if not run_id:
            return
        snapshot = self._load_snapshot(job_name, run_id)
        if not snapshot:
            return
        now = self._now()
        ttl_seconds = self._snapshot_ttl(snapshot)
        snapshot["status"] = status
        snapshot["updated_at"] = now.isoformat()
        snapshot["error_message"] = message if status == "paused" else None
        self._refresh_metrics(snapshot, now=now)
        self._save_snapshot(job_name, run_id, snapshot, ttl_seconds=ttl_seconds)
        self._append_event(
            job_name=job_name,
            run_id=run_id,
            event_type=event_type,
            created_at=now,
            ttl_seconds=ttl_seconds,
            payload={"status": status, "message": message},
        )

    def _append_event(
        self,
        *,
        job_name: str,
        run_id: str,
        event_type: str,
        created_at: datetime,
        ttl_seconds: int,
        payload: JsonDict,
    ) -> None:
        event_payload = {
            "event_type": event_type,
            "job_name": job_name,
            "run_id": run_id,
            "created_at": created_at.isoformat(),
        }
        event_payload.update({key: value for key, value in payload.items() if value is not None})
        self._append_json(
            events_task_key(job_name, run_id),
            event_payload,
            ttl_seconds=ttl_seconds,
            max_length=self.event_limit,
        )

    def _update_active_index(
        self,
        *,
        job_name: str,
        run_id: str,
        status: str,
        ttl_seconds: int,
        updated_at: datetime | None,
    ) -> None:
        active_payload = self._safe_get_json(ACTIVE_KEY)
        active_items = active_items_from_payload(active_payload)
        next_items = [
            item for item in active_items if str(item.get("job_name") or "") != job_name
        ]
        next_items.append(
            {
                "job_name": job_name,
                "run_id": run_id,
                "status": status,
                "updated_at": (updated_at or self._now()).isoformat(),
            }
        )
        self._set_json(ACTIVE_KEY, next_items, ttl_seconds=ttl_seconds)

    def _load_snapshot(self, job_name: str, run_id: str) -> JsonDict | None:
        snapshot = self._safe_get_json(snapshot_task_key(job_name, run_id))
        return snapshot if isinstance(snapshot, dict) else None

    def _build_snapshot(
        self,
        *,
        job_name: str,
        run_id: str,
        title: str,
        market: str | None,
        task_type: str,
        interval_seconds: int,
        status: str,
        started_at: datetime,
        updated_at: datetime,
        finished_at: datetime | None,
        total_items: int,
        completed_items: int,
        running_items: int,
        failed_items: int,
        remaining_items: int,
        max_workers: int | None,
    ) -> JsonDict:
        snapshot: JsonDict = {
            "job_name": job_name,
            "run_id": run_id,
            "title": title,
            "market": market,
            "task_type": task_type,
            "status": status,
            "interval_seconds": interval_seconds,
            "started_at": started_at.isoformat(),
            "updated_at": updated_at.isoformat(),
            "finished_at": finished_at.isoformat() if finished_at else None,
            "total_items": total_items,
            "completed_items": completed_items,
            "running_items": running_items,
            "failed_items": failed_items,
            "retry_items": 0,
            "remaining_items": remaining_items,
            "progress_ratio": self._progress_ratio(total_items, completed_items, failed_items),
            "batch_index": None,
            "batch_count": None,
            "batch_size": None,
            "max_workers": max_workers,
            "current_items": [],
            "stages": [],
            "metrics": {
                "duration_seconds": 0,
                "error_rate": 0.0,
                "max_workers": max_workers or 0,
                "throughput_per_minute": 0.0,
                "node": "local",
                "cache_backend": self.cache_backend,
            },
            "error_message": None,
        }
        return snapshot

    def _stage_entry(
        self,
        snapshot: JsonDict,
        *,
        stage_key: str,
        status: str,
        updated_at: datetime,
        total_items: int | None = None,
        delta_running: int = 0,
        delta_completed: int = 0,
        delta_failed: int = 0,
    ) -> JsonDict:
        stages = list(snapshot.get("stages") or [])
        current = None
        for item in stages:
            if item.get("stage_key") == stage_key:
                current = dict(item)
                break
        if current is None:
            current = {
                "stage_key": stage_key,
                "title": stage_key,
                "status": status,
                "total_items": total_items or 0,
                "completed_items": 0,
                "failed_items": 0,
                "running_items": 0,
                "progress_ratio": 0.0,
            }
        current["status"] = status
        if total_items is not None:
            current["total_items"] = max(int(current.get("total_items") or 0), total_items)
        current["completed_items"] = int(current.get("completed_items") or 0) + delta_completed
        current["failed_items"] = int(current.get("failed_items") or 0) + delta_failed
        current["running_items"] = max(0, int(current.get("running_items") or 0) + delta_running)
        total = int(current.get("total_items") or 0)
        current["progress_ratio"] = self._progress_ratio(
            total,
            int(current.get("completed_items") or 0),
            int(current.get("failed_items") or 0),
        )
        current["updated_at"] = updated_at.isoformat()
        return current

    def _replace_stage(self, stages: Any, stage: JsonDict) -> list[JsonDict]:
        items = [dict(item) for item in stages or [] if isinstance(item, dict)]
        replaced = False
        for index, existing in enumerate(items):
            if existing.get("stage_key") == stage.get("stage_key"):
                items[index] = stage
                replaced = True
                break
        if not replaced:
            items.append(stage)
        return items

    def _refresh_aggregate_from_stages(self, snapshot: JsonDict) -> None:
        stages = [dict(item) for item in snapshot.get("stages") or [] if isinstance(item, dict)]
        if not stages:
            total = int(snapshot.get("total_items") or 0)
            completed = int(snapshot.get("completed_items") or 0)
            failed = int(snapshot.get("failed_items") or 0)
            snapshot["remaining_items"] = max(total - completed - failed, 0)
            snapshot["progress_ratio"] = self._progress_ratio(total, completed, failed)
            return
        total = sum(max(int(stage.get("total_items") or 0), 0) for stage in stages)
        completed = sum(max(int(stage.get("completed_items") or 0), 0) for stage in stages)
        failed = sum(max(int(stage.get("failed_items") or 0), 0) for stage in stages)
        running = sum(max(int(stage.get("running_items") or 0), 0) for stage in stages)
        snapshot["total_items"] = total
        snapshot["completed_items"] = completed
        snapshot["failed_items"] = failed
        snapshot["running_items"] = running
        snapshot["remaining_items"] = max(total - completed - failed, 0)
        snapshot["progress_ratio"] = self._progress_ratio(total, completed, failed)

    def _refresh_metrics(self, snapshot: JsonDict, *, now: datetime) -> None:
        started_at = parse_iso_datetime(snapshot.get("started_at")) or now
        duration_seconds = max(0, int((now - started_at).total_seconds()))
        total = max(int(snapshot.get("total_items") or 0), 0)
        completed = max(int(snapshot.get("completed_items") or 0), 0)
        failed = max(int(snapshot.get("failed_items") or 0), 0)
        processed = completed + failed
        error_rate = round(failed / total, 6) if total else 0.0
        throughput_per_minute = round((processed / duration_seconds) * 60, 3) if duration_seconds else 0.0
        snapshot["metrics"] = {
            "duration_seconds": duration_seconds,
            "error_rate": error_rate,
            "max_workers": int(snapshot.get("max_workers") or 0),
            "throughput_per_minute": throughput_per_minute,
            "node": "local",
            "cache_backend": self.cache_backend,
        }

    def _progress_ratio(self, total: int, completed: int, failed: int) -> float:
        if total <= 0:
            return 0.0
        return round(min(1.0, (completed + failed) / total), 3)

    def _snapshot_ttl(self, snapshot: JsonDict) -> int:
        interval_seconds = int(snapshot.get("interval_seconds") or 0)
        return progress_ttl_seconds(interval_seconds)

    def _safe_get_json(self, key: str) -> Any:
        try:
            return self.cache.get_json(key)
        except Exception:
            return None

    def _safe_list_json(self, key: str, *, limit: int) -> list[Any]:
        try:
            if hasattr(self.cache, "list_json"):
                return list(self.cache.list_json(key, limit=limit))
            payload = self.cache.get_json(key)
            if isinstance(payload, list):
                return payload[-limit:]
            return []
        except Exception:
            return []

    def _safe_set(self, key: str, value: object, *, ttl_seconds: int | None) -> None:
        try:
            self.cache.set_json(key, value, ttl_seconds=ttl_seconds)
        except Exception:
            return

    def _safe_delete(self, key: str) -> None:
        try:
            self.cache.delete(key)
        except Exception:
            return

    def _set_json(self, key: str, value: object, *, ttl_seconds: int | None) -> None:
        self._safe_set(key, value, ttl_seconds=ttl_seconds)

    def _append_json(
        self,
        key: str,
        value: object,
        *,
        ttl_seconds: int | None,
        max_length: int | None,
    ) -> None:
        try:
            if hasattr(self.cache, "append_json"):
                self.cache.append_json(key, value, ttl_seconds=ttl_seconds, max_length=max_length)
                return
            existing = self.cache.get_json(key)
            items = list(existing) if isinstance(existing, list) else []
            items.append(value)
            if max_length is not None and max_length > 0:
                items = items[-max_length:]
            self.cache.set_json(key, items, ttl_seconds=ttl_seconds)
        except Exception:
            return


def progress_ttl_seconds(interval_seconds: int) -> int:
    """根据任务频率计算进度 TTL。"""

    return max(int(interval_seconds) + PROGRESS_TTL_GRACE_SECONDS, PROGRESS_TTL_MIN_SECONDS)


def clamp_event_limit(value: int) -> int:
    """限制返回事件数量，避免一次拉取过大。"""

    try:
        number = int(value)
    except (TypeError, ValueError):
        number = EVENT_LIMIT_DEFAULT
    return max(1, min(number, EVENT_LIMIT_MAX))


def current_task_key(job_name: str) -> str:
    """生成 current 键。"""

    return CURRENT_KEY_PREFIX.format(job_name=job_name)


def snapshot_task_key(job_name: str, run_id: str) -> str:
    """生成 snapshot 键。"""

    return SNAPSHOT_KEY_PREFIX.format(job_name=job_name, run_id=run_id)


def events_task_key(job_name: str, run_id: str) -> str:
    """生成 events 键。"""

    return EVENTS_KEY_PREFIX.format(job_name=job_name, run_id=run_id)


def source_rate_key(source_key: str) -> str:
    """生成数据源限频状态键。"""

    return SOURCE_RATE_KEY_PREFIX.format(source_key=source_key)


def active_items_from_payload(payload: Any) -> list[JsonDict]:
    """把 active 索引标准化为字典列表。"""

    if isinstance(payload, dict):
        items: list[JsonDict] = []
        for job_name, value in payload.items():
            if isinstance(value, dict):
                item = dict(value)
                item.setdefault("job_name", job_name)
                items.append(item)
        return items
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def parse_iso_datetime(value: Any) -> datetime | None:
    """解析 ISO 时间。"""

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def build_progress_snapshot_response(
    *,
    cache: Any,
    cache_backend: str,
    scheduler_jobs: list[Any] | tuple[Any, ...],
    event_limit: int,
    redis_error_message: str | None = None,
) -> JsonDict:
    """读取 Scheduler 运行态进度并输出接口结构。"""

    recorder = BaseDataTaskProgressRecorder.from_cache_client(cache, cache_backend=cache_backend)
    return recorder.read_scheduler_progress(
        event_limit=event_limit,
        scheduler_jobs=list(scheduler_jobs),
        redis_error_message=redis_error_message,
    )
