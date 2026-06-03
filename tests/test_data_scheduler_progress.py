import json
from pathlib import Path
from typing import Any

from finance_agent.application.data_sync_control_service import DataSyncControlService
from finance_agent.cache.null_cache import NullCacheClient
from finance_agent.data.collection_runtime import CollectionTaskResult


class RecordingCache:
    """记录 JSON、列表和 TTL 操作，避免测试依赖真实 Redis。"""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.lists: dict[str, list[Any]] = {}
        self.ttls: dict[str, int] = {}
        self.deleted: list[str] = []

    def get_json(self, key: str) -> Any:
        return self.values.get(key)

    def set_json(self, key: str, value: object, *, ttl_seconds: int | None = None) -> None:
        self.values[key] = value
        if ttl_seconds is not None:
            self.ttls[key] = ttl_seconds

    def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.values.pop(key, None)
        self.lists.pop(key, None)
        self.ttls.pop(key, None)

    def append_json(
        self,
        key: str,
        value: object,
        *,
        ttl_seconds: int | None = None,
        max_length: int | None = None,
    ) -> None:
        items = self.lists.setdefault(key, [])
        items.append(value)
        if max_length is not None and max_length > 0:
            del items[:-max_length]
        if ttl_seconds is not None:
            self.ttls[key] = ttl_seconds

    def list_json(self, key: str, *, limit: int | None = None) -> list[Any]:
        items = list(self.lists.get(key, []))
        if limit is None:
            return items
        return items[-limit:]

    def expire(self, key: str, ttl_seconds: int) -> None:
        self.ttls[key] = ttl_seconds


def test_job_start_replaces_previous_run_and_sets_ttl() -> None:
    """同名任务新一轮启动时应清理旧运行态，并按 interval+grace 设置 TTL。"""

    from finance_agent.scheduler.base_data_progress import BaseDataTaskProgressRecorder

    cache = RecordingCache()
    job_name = "ashare.bars.1d"
    old_run_id = "old-run"
    cache.set_json(f"base_data:task:{job_name}:current", old_run_id)
    cache.set_json(f"base_data:task:{job_name}:run:{old_run_id}:snapshot", {"status": "running"})
    cache.append_json(
        f"base_data:task:{job_name}:run:{old_run_id}:events",
        {"event_type": "job_started"},
    )

    recorder = BaseDataTaskProgressRecorder(cache=cache, cache_backend="redis")
    run_id = recorder.job_started(
        job_name=job_name,
        title="补采 A 股 1d K 线",
        market="ashare",
        task_type="market_bars_backfill",
        interval_seconds=60,
        max_workers=4,
    )

    ttl = 60 + 1800
    assert cache.deleted == [
        f"base_data:task:{job_name}:run:{old_run_id}:snapshot",
        f"base_data:task:{job_name}:run:{old_run_id}:events",
    ]
    assert cache.values[f"base_data:task:{job_name}:current"] == run_id
    assert cache.ttls[f"base_data:task:{job_name}:current"] == ttl
    snapshot = cache.values[f"base_data:task:{job_name}:run:{run_id}:snapshot"]
    assert snapshot["status"] == "running"
    assert snapshot["task_type"] == "market_bars_backfill"
    assert snapshot["metrics"]["cache_backend"] == "redis"
    events = cache.lists[f"base_data:task:{job_name}:run:{run_id}:events"]
    assert events[-1]["event_type"] == "job_started"


def test_scheduler_progress_response_reads_snapshots_events_and_waiting_jobs(
    tmp_path: Path,
) -> None:
    """进度接口应聚合 Redis 快照、最近事件和配置里的等待队列。"""

    from finance_agent.scheduler.base_data_progress import BaseDataTaskProgressRecorder

    cache = RecordingCache()
    recorder = BaseDataTaskProgressRecorder(cache=cache, cache_backend="redis")
    running_job = "ashare.bars.1d"
    waiting_job = "crypto.bars.1h"
    run_id = recorder.job_started(
        job_name=running_job,
        title="补采 A 股 1d K 线",
        market="ashare",
        task_type="market_bars_backfill",
        interval_seconds=3600,
        max_workers=2,
    )
    recorder.batch_started(
        job_name=running_job,
        run_id=run_id,
        stage_key="sync_symbols",
        total_items=2,
        batch_index=1,
        batch_count=1,
        batch_size=2,
    )
    recorder.symbol_completed(
        job_name=running_job,
        run_id=run_id,
        stage_key="sync_symbols",
        symbol="000001",
        status="completed",
        item_count=5,
        batch_index=1,
        batch_count=1,
    )
    config_file = tmp_path / "base_data_scheduler.json"
    config_file.write_text(
        json.dumps(
            {
                "schema_version": "data-sync-scheduler-v1",
                "enabled": True,
                "cache_backend": "redis",
                "jobs": [
                    {
                        "name": running_job,
                        "group": "ashare-p0",
                        "interval_seconds": 3600,
                        "market": "ashare",
                        "params": {"sync_task_type": "market_bars_backfill"},
                    },
                    {
                        "name": waiting_job,
                        "group": "crypto",
                        "interval_seconds": 300,
                        "market": "crypto_spot",
                        "params": {"sync_task_type": "market_bars_backfill"},
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    response = DataSyncControlService().read_scheduler_progress(
        event_limit=80,
        cache=cache,
        cache_backend="redis",
        scheduler_config_file=config_file,
    )

    assert response["status"] == "ok"
    data = response["data"]
    assert data["cache_backend"] == "redis"
    assert data["metrics"]["running_count"] == 1
    assert data["metrics"]["waiting_count"] == 1
    task = data["tasks"][0]
    assert task["job_name"] == running_job
    assert task["run_id"] == run_id
    assert task["summary"] == {
        "total_items": 2,
        "completed_items": 1,
        "running_items": 1,
        "failed_items": 0,
        "remaining_items": 1,
        "progress_ratio": 0.5,
    }
    assert [event["event_type"] for event in task["recent_events"]] == [
        "job_started",
        "batch_started",
        "symbol_completed",
    ]
    assert data["waiting"] == [
        {
            "job_name": waiting_job,
            "title": waiting_job,
            "status": "waiting",
            "interval_seconds": 300,
        }
    ]


def test_scheduler_progress_degrades_when_redis_is_unavailable() -> None:
    """Redis 不可用时接口返回 degraded，且不尝试从 PostgreSQL 补进度。"""

    response = DataSyncControlService().read_scheduler_progress(
        cache=NullCacheClient(),
        cache_backend="null",
        redis_error_message="Redis unavailable",
    )

    assert response == {
        "status": "degraded",
        "message": "Redis 不可用，无法读取实时进度。",
        "data": {
            "cache_backend": "null",
            "tasks": [],
            "waiting": [],
        },
    }


def test_symbol_task_batch_emits_symbol_completed_event() -> None:
    """按 symbol 批处理完成后应写入 symbol_completed 事件并推进计数。"""

    from finance_agent.scheduler.base_data_progress import BaseDataTaskProgressRecorder
    from scripts.data import collect_base_data

    cache = RecordingCache()
    job_name = "ashare.bars.1d"
    recorder = BaseDataTaskProgressRecorder(cache=cache, cache_backend="redis")
    run_id = recorder.job_started(
        job_name=job_name,
        title=job_name,
        market="ashare",
        task_type="market_bars_backfill",
        interval_seconds=3600,
    )
    recorder.batch_started(
        job_name=job_name,
        run_id=run_id,
        stage_key="sync_symbols",
        total_items=1,
        batch_index=1,
        batch_count=1,
        batch_size=1,
    )

    def collect_symbol(symbol: str) -> CollectionTaskResult:
        return CollectionTaskResult(
            task="ashare_p0_ohlcv",
            status="available",
            raw_record_id="raw-1",
            item_count=3,
            error_message=None,
            payload={},
        )

    collect_base_data.run_symbol_task_batch(
        ["000001"],
        max_workers=1,
        collect_symbol=collect_symbol,
        progress=recorder,
        job_name=job_name,
        run_id=run_id,
        stage_key="sync_symbols",
        batch_index=1,
        batch_count=1,
    )

    snapshot = cache.values[f"base_data:task:{job_name}:run:{run_id}:snapshot"]
    assert snapshot["completed_items"] == 1
    assert snapshot["remaining_items"] == 0
    events = cache.lists[f"base_data:task:{job_name}:run:{run_id}:events"]
    assert events[-1]["event_type"] == "symbol_completed"
    assert events[-1]["symbol"] == "000001"


def test_ashare_full_asset_refresh_emits_progress_events(monkeypatch) -> None:
    """A 股全市场资产池刷新应写入 Redis 进度，避免任务监控页只能看到 job_started。"""

    from finance_agent.scheduler.base_data_progress import BaseDataTaskProgressRecorder
    from scripts.data import collect_base_data

    cache = RecordingCache()
    job_name = "ashare.universe.all"
    recorder = BaseDataTaskProgressRecorder(cache=cache, cache_backend="redis")
    run_id = recorder.job_started(
        job_name=job_name,
        title=job_name,
        market="ashare",
        task_type="universe_refresh",
        interval_seconds=86400,
    )

    class RecordingRuntime:
        def run_task(self, *, task, provider_key, parameters, collect, force=False):
            return CollectionTaskResult(
                task=task,
                status="available",
                raw_record_id="raw-assets",
                item_count=5530,
                error_message=None,
                payload={},
            )

    args = collect_base_data.default_collection_args(
        progress_job_name=job_name,
        progress_run_id=run_id,
    )
    monkeypatch.setattr(collect_base_data, "COLLECTION_PROGRESS_RECORDER", recorder)

    collect_base_data.build_ashare_full_asset_refresh_task(
        collector=object(),
        args=args,
        runtime=RecordingRuntime(),
    )

    events = cache.lists[f"base_data:task:{job_name}:run:{run_id}:events"]
    event_types = [event["event_type"] for event in events]
    assert "batch_started" in event_types
    assert events[-1]["event_type"] == "symbol_completed"
    assert events[-1]["symbol"] == "全市场资产池"
    assert events[-1]["item_count"] == 5530
    snapshot = cache.values[f"base_data:task:{job_name}:run:{run_id}:snapshot"]
    assert snapshot["stages"][0]["stage_key"] == "ashare_p0_assets"


def test_api_registers_scheduler_progress_route() -> None:
    """FastAPI 应注册 GET /api/data/scheduler/progress。"""

    from finance_agent.api.app import create_app

    app = create_app()

    assert any(
        route.path == "/api/data/scheduler/progress" and "GET" in route.methods
        for route in app.routes
    )
