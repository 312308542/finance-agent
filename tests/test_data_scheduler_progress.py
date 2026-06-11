import json
import threading
import time
from pathlib import Path
from typing import Any
from datetime import UTC, datetime, timedelta

from finance_agent.application.data_sync_control_service import DataSyncControlService
from finance_agent.cache.null_cache import NullCacheClient
from finance_agent.data.collection_runtime import CollectionTaskResult
from finance_agent.scheduler.base_data_scheduler import import_collection_module


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


def test_job_cancelled_marks_current_run_as_failed_with_cancelled_event() -> None:
    """用户取消任务后，Redis 快照不应继续停留在 running。"""

    from finance_agent.scheduler.base_data_progress import BaseDataTaskProgressRecorder

    cache = RecordingCache()
    job_name = "ashare.bars.1d.bootstrap"
    recorder = BaseDataTaskProgressRecorder(cache=cache, cache_backend="redis")
    run_id = recorder.job_started(
        job_name=job_name,
        title="A 股 10 年历史日 K 初始化",
        market="ashare",
        task_type="market_bars_full_history_backfill",
        interval_seconds=0,
        max_workers=2,
        total_items=100,
    )

    recorder.job_cancelled(job_name=job_name, error_message="用户取消")

    snapshot = cache.values[f"base_data:task:{job_name}:run:{run_id}:snapshot"]
    events = cache.lists[f"base_data:task:{job_name}:run:{run_id}:events"]
    assert snapshot["status"] == "failed"
    assert snapshot["running_items"] == 0
    assert snapshot["remaining_items"] == 100
    assert snapshot["error_message"] == "用户取消"
    assert events[-1]["event_type"] == "job_failed"
    assert events[-1]["status"] == "cancelled"


def test_job_cancelled_falls_back_to_active_index_when_current_key_expired() -> None:
    """current key 过期但 active 索引仍存在时，取消仍应更新运行快照。"""

    from finance_agent.scheduler.base_data_progress import (
        BaseDataTaskProgressRecorder,
        current_task_key,
    )

    cache = RecordingCache()
    job_name = "ashare.bars.1d.bootstrap"
    recorder = BaseDataTaskProgressRecorder(cache=cache, cache_backend="redis")
    run_id = recorder.job_started(
        job_name=job_name,
        title="A 股 10 年历史日 K 初始化",
        market="ashare",
        task_type="market_bars_full_history_backfill",
        interval_seconds=0,
        max_workers=2,
        total_items=100,
    )
    cache.delete(current_task_key(job_name))

    recorder.job_cancelled(job_name=job_name, error_message="用户取消")

    snapshot = cache.values[f"base_data:task:{job_name}:run:{run_id}:snapshot"]
    assert snapshot["status"] == "failed"
    assert snapshot["error_message"] == "用户取消"


def test_scheduler_progress_response_reads_snapshots_events_and_waiting_jobs(
    tmp_path: Path,
) -> None:
    """进度接口应聚合 Redis 快照、最近事件和配置里的等待队列。"""

    from finance_agent.scheduler.base_data_progress import BaseDataTaskProgressRecorder

    cache = RecordingCache()
    recorder = BaseDataTaskProgressRecorder(cache=cache, cache_backend="redis")
    running_job = "ashare.bars.1d"
    waiting_job = "crypto.bars.1h"
    manual_job = "ashare.bars.1d.bootstrap"
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
                    {
                        "name": manual_job,
                        "group": "ashare-p0",
                        "enabled": False,
                        "interval_seconds": 0,
                        "market": "ashare",
                        "schedule_type": "manual",
                        "params": {
                            "lookback": "10y",
                            "sync_task_type": "market_bars_full_history_backfill",
                        },
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
    assert data["metrics"]["waiting_count"] == 2
    task = data["tasks"][0]
    assert task["job_name"] == running_job
    assert task["run_id"] == run_id
    assert task["max_workers"] == 2
    assert task["throughput_per_minute"] >= 0
    assert task["summary"] == {
        "total_items": 2,
        "completed_items": 1,
        "running_items": 1,
        "failed_items": 0,
        "retry_items": 0,
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
        },
        {
            "job_name": manual_job,
            "title": manual_job,
            "status": "waiting",
            "interval_seconds": 0,
        }
    ]


def test_scheduler_progress_metrics_include_workers_and_throughput() -> None:
    """进度快照应输出并发数和吞吐量，避免前端运行指标显示为空。"""

    from finance_agent.scheduler.base_data_progress import BaseDataTaskProgressRecorder

    cache = RecordingCache()
    job_name = "ashare.bars.1d"
    recorder = BaseDataTaskProgressRecorder(cache=cache, cache_backend="redis")
    run_id = recorder.job_started(
        job_name=job_name,
        title="补采 A 股 1d K 线",
        market="ashare",
        task_type="market_bars_backfill",
        interval_seconds=3600,
        max_workers=4,
    )
    snapshot_key = f"base_data:task:{job_name}:run:{run_id}:snapshot"
    cache.values[snapshot_key]["started_at"] = (
        datetime.now(tz=UTC) - timedelta(minutes=2)
    ).isoformat()
    recorder.batch_started(
        job_name=job_name,
        run_id=run_id,
        stage_key="sync_symbols",
        total_items=10,
        batch_index=1,
        batch_count=1,
        batch_size=4,
    )
    recorder.symbol_completed(
        job_name=job_name,
        run_id=run_id,
        stage_key="sync_symbols",
        symbol="000001",
        status="completed",
        item_count=5,
        batch_index=1,
        batch_count=1,
    )
    task = recorder.build_snapshot_task_view(
        job_name=job_name,
        run_id=run_id,
        snapshot=cache.values[snapshot_key],
        events=cache.lists[f"base_data:task:{job_name}:run:{run_id}:events"],
    )

    assert task["max_workers"] == 4
    assert task["throughput_per_minute"] > 0
    assert task["metrics"]["max_workers"] == 4
    assert task["metrics"]["throughput_per_minute"] == task["throughput_per_minute"]


def test_scheduler_progress_batch_started_updates_hot_reloaded_workers() -> None:
    """批次开始时应写回本批最新 worker，页面才能展示热加载后的真实有效并发。"""

    from finance_agent.scheduler.base_data_progress import BaseDataTaskProgressRecorder

    cache = RecordingCache()
    job_name = "ashare.bars.1d.bootstrap"
    recorder = BaseDataTaskProgressRecorder(cache=cache, cache_backend="redis")
    run_id = recorder.job_started(
        job_name=job_name,
        title="A 股历史 K 线初始化",
        market="ashare",
        task_type="market_bars_full_history_backfill",
        interval_seconds=0,
        max_workers=4,
    )
    recorder.batch_started(
        job_name=job_name,
        run_id=run_id,
        stage_key="sync_symbols",
        total_items=100,
        batch_index=2,
        batch_count=10,
        batch_size=20,
        max_workers=8,
    )

    snapshot = cache.values[f"base_data:task:{job_name}:run:{run_id}:snapshot"]
    events = cache.lists[f"base_data:task:{job_name}:run:{run_id}:events"]
    task = recorder.build_snapshot_task_view(
        job_name=job_name,
        run_id=run_id,
        snapshot=snapshot,
        events=events,
    )

    assert snapshot["max_workers"] == 8
    assert snapshot["metrics"]["max_workers"] == 8
    assert events[-1]["max_workers"] == 8
    assert task["max_workers"] == 8


def test_scheduler_progress_includes_source_rate_states() -> None:
    """进度接口应输出数据源退避状态，便于判断是否被限流或断连。"""

    from finance_agent.scheduler.base_data_progress import BaseDataTaskProgressRecorder

    cache = RecordingCache()
    recorder = BaseDataTaskProgressRecorder(cache=cache, cache_backend="redis")
    recorder.source_rate_updated(
        source_key="stock_zh_a_hist_tx",
        snapshot={
            "failure_count": 2,
            "disconnect_count": 2,
            "effective_max_concurrency": 1,
            "effective_min_interval_seconds": 2.0,
            "next_recover_at": 1000.0,
        },
        ttl_seconds=900,
    )

    response = recorder.read_scheduler_progress()

    assert response["status"] == "ok"
    state = response["data"]["source_rate_states"][0]
    assert state["source_key"] == "stock_zh_a_hist_tx"
    assert state["failure_count"] == 2
    assert state["disconnect_count"] == 2
    assert state["effective_max_concurrency"] == 1
    assert state["effective_min_interval_seconds"] == 2.0
    assert state["next_recover_at"] == 1000.0
    assert state["updated_at"]


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
    collect_base_data = import_collection_module()

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


def test_symbol_task_batch_calls_result_callback_as_each_future_finishes() -> None:
    """批内低并发任务应在单只股票完成后立即回调，方便采集方及时写入水位。"""

    collect_base_data = import_collection_module()

    first_callback_seen = threading.Event()
    active_lock = threading.Lock()
    active_count = 0
    max_active_count = 0
    callback_symbols: list[str] = []

    def collect_symbol(symbol: str) -> CollectionTaskResult:
        nonlocal active_count, max_active_count
        with active_lock:
            active_count += 1
            max_active_count = max(max_active_count, active_count)
        try:
            if symbol == "600519":
                assert first_callback_seen.wait(timeout=2)
            else:
                time.sleep(0.05)
            return CollectionTaskResult(
                task="ashare_p0_ohlcv",
                status="available",
                raw_record_id=f"raw-{symbol}",
                item_count=3,
                error_message=None,
                payload={},
            )
        finally:
            with active_lock:
                active_count -= 1

    def on_symbol_result(symbol: str, result: CollectionTaskResult, index: int) -> None:
        callback_symbols.append(symbol)
        if symbol == "000001":
            first_callback_seen.set()

    results = collect_base_data.run_symbol_task_batch(
        ["000001", "600519", "300750"],
        max_workers=2,
        collect_symbol=collect_symbol,
        on_symbol_result=on_symbol_result,
    )

    assert max_active_count == 2
    assert [result.raw_record_id for result in results] == [
        "raw-000001",
        "raw-600519",
        "raw-300750",
    ]
    assert set(callback_symbols) == {"000001", "600519", "300750"}


def test_symbol_failed_event_exposes_retry_watermark_metadata() -> None:
    """失败的按标的采集应把水位重试信息写入任务进度，方便前端排障。"""

    from finance_agent.scheduler.base_data_progress import BaseDataTaskProgressRecorder
    collect_base_data = import_collection_module()

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
        stage_key="ashare_p0_ohlcv",
        total_items=1,
        batch_index=1,
        batch_count=1,
        batch_size=1,
    )

    def collect_symbol(symbol: str) -> CollectionTaskResult:
        return CollectionTaskResult(
            task="ashare_p0_ohlcv",
            status="error",
            raw_record_id=None,
            item_count=0,
            error_message="curl: (56) Connection closed abruptly",
            payload={
                "provider_key": "akshare:stock_zh_a_hist_tx",
                "error_category": "network",
                "retry_after_seconds": 900,
                "next_retry_at": "2026-06-04T10:15:00+00:00",
            },
        )

    collect_base_data.run_symbol_task_batch(
        ["301611"],
        max_workers=1,
        collect_symbol=collect_symbol,
        progress=recorder,
        job_name=job_name,
        run_id=run_id,
        stage_key="ashare_p0_ohlcv",
        batch_index=1,
        batch_count=1,
    )

    snapshot = cache.values[f"base_data:task:{job_name}:run:{run_id}:snapshot"]
    assert snapshot["retry_items"] == 1
    view = recorder.build_snapshot_task_view(
        job_name=job_name,
        run_id=run_id,
        snapshot=snapshot,
        events=cache.lists[f"base_data:task:{job_name}:run:{run_id}:events"],
    )
    assert view["summary"]["retry_items"] == 1
    event = cache.lists[f"base_data:task:{job_name}:run:{run_id}:events"][-1]
    assert event["event_type"] == "symbol_failed"
    assert event["provider_key"] == "akshare:stock_zh_a_hist_tx"
    assert event["error_category"] == "network"
    assert event["retry_after_seconds"] == 900
    assert event["next_retry_at"] == "2026-06-04T10:15:00+00:00"


def test_ashare_full_asset_refresh_emits_progress_events(monkeypatch) -> None:
    """A 股全市场资产池刷新应写入 Redis 进度，避免任务监控页只能看到 job_started。"""

    from finance_agent.scheduler.base_data_progress import BaseDataTaskProgressRecorder
    collect_base_data = import_collection_module()

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


def test_api_registers_scheduler_job_control_routes() -> None:
    """FastAPI 应注册任务目录、单任务配置和单任务执行接口。"""

    from finance_agent.api.app import create_app

    app = create_app()

    assert any(route.path == "/api/data/scheduler/jobs" and "GET" in route.methods for route in app.routes)
    assert any(
        route.path == "/api/data/scheduler/jobs/{job_name}" and "PUT" in route.methods
        for route in app.routes
    )
    assert any(
        route.path == "/api/data/scheduler/jobs/{job_name}/run" and "POST" in route.methods
        for route in app.routes
    )
    assert any(
        route.path == "/api/data/scheduler/jobs/{job_name}/rerun-failed" and "POST" in route.methods
        for route in app.routes
    )
    assert any(
        route.path == "/api/data/scheduler/jobs/{job_name}/cancel" and "POST" in route.methods
        for route in app.routes
    )
