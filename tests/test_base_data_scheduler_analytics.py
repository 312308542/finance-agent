import logging
import sys
import threading
import time
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from finance_agent.scheduler import (
    BaseDataScheduler,
    BaseDataSchedulerConfig,
    BaseDataSchedulerJob,
    parse_scheduler_config,
)
from finance_agent.scheduler.base_data_scheduler import (
    import_collection_module,
    replace_file_with_retry,
    seconds_until_next_run,
)


def test_parse_scheduler_config_accepts_recommendation_pipeline_job() -> None:
    """调度配置应能表达采集之外的真实推荐流水线任务。"""

    config = parse_scheduler_config(
        {
            "enabled": True,
            "jobs": [
                {
                    "name": "analytics.recommendations.ashare.all_a",
                    "job_type": "recommendation_pipeline",
                    "group": "analytics",
                    "enabled": True,
                    "interval_seconds": 3600,
                    "limit": 20,
                    "market": "ashare",
                    "params": {
                        "universe_id": "universe:base:ashare:p0:all_a",
                        "strategy": "balanced_swing_v1",
                        "horizon": "swing",
                    },
                }
            ],
        }
    )

    assert config.jobs[0].job_type == "recommendation_pipeline"
    assert config.jobs[0].group == "analytics"


def test_parse_scheduler_config_accepts_data_quality_refresh_job() -> None:
    """调度配置应能表达数据质量快照刷新任务。"""

    config = parse_scheduler_config(
        {
            "enabled": True,
            "jobs": [
                {
                    "name": "quality.ashare",
                    "job_type": "data_quality_refresh",
                    "group": "analytics",
                    "enabled": True,
                    "interval_seconds": 3600,
                    "limit": 200,
                    "market": "ashare",
                    "params": {
                        "market": "ashare",
                        "timeframe": "1d",
                        "min_bars": 60,
                        "data_domains": ["market_bars", "indicator_frames"],
                    },
                }
            ],
        }
    )

    assert config.jobs[0].job_type == "data_quality_refresh"
    assert config.jobs[0].group == "analytics"


def test_scheduler_executes_recommendation_pipeline_job_without_collection() -> None:
    """推荐流水线任务应调用 analytics 执行器，而不是误走基础采集入口。"""

    calls: list[dict[str, Any]] = []

    def run_recommendation_pipeline(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "status": "available",
            "universe_id": kwargs["universe_id"],
            "recommendation_count": 3,
            "recommendation_run_id": "run:real",
        }

    def collect_base_data(_: Any) -> dict[str, Any]:
        raise AssertionError("推荐流水线任务不应调用基础采集入口")

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        jobs=(
            BaseDataSchedulerJob(
                name="analytics.recommendations.ashare.all_a",
                job_type="recommendation_pipeline",
                group="analytics",
                interval_seconds=3600,
                limit=20,
                market="ashare",
                params={
                    "universe_id": "universe:base:ashare:p0:all_a",
                    "strategy": "balanced_swing_v1",
                    "horizon": "swing",
                    "timeframe": "1d",
                    "min_bars": 60,
                    "min_indicator_coverage_ratio": 0.7,
                    "min_factor_coverage_ratio": 0.5,
                    "min_available_factor_groups": 3,
                },
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=collect_base_data,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        run_recommendation_pipeline_func=run_recommendation_pipeline,
    )

    result = scheduler.run_once()

    assert result["jobs"][0]["status"] == "executed"
    assert result["jobs"][0]["summary"]["recommendation_run_id"] == "run:real"
    assert calls == [
        {
            "universe_id": "universe:base:ashare:p0:all_a",
            "strategy": "balanced_swing_v1",
            "horizon": "swing",
            "timeframe": "1d",
            "limit": 20,
            "min_bars": 60,
            "min_available_factor_groups": 3,
            "min_indicator_coverage_ratio": 0.7,
            "min_factor_coverage_ratio": 0.5,
        }
    ]


def test_scheduler_runs_data_quality_refresh_without_collection() -> None:
    """数据质量任务应调用质量刷新执行器，而不是误走基础采集入口。"""

    calls: list[dict[str, Any]] = []

    def run_data_quality_refresh(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"status": "available", "snapshot_count": 2}

    def collect_base_data(_: Any) -> dict[str, Any]:
        raise AssertionError("数据质量任务不应调用基础采集入口")

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        jobs=(
            BaseDataSchedulerJob(
                name="quality.ashare",
                job_type="data_quality_refresh",
                group="analytics",
                interval_seconds=3600,
                limit=200,
                market="ashare",
                params={
                    "market": "ashare",
                    "timeframe": "1d",
                    "min_bars": 60,
                    "stale_after_seconds": 86400,
                    "data_domains": ["market_bars", "indicator_frames"],
                },
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=collect_base_data,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        run_data_quality_refresh_func=run_data_quality_refresh,
    )

    result = scheduler.run_once()

    assert result["jobs"][0]["status"] == "executed"
    assert result["jobs"][0]["summary"]["snapshot_count"] == 2
    assert calls == [
        {
            "market": "ashare",
            "timeframe": "1d",
            "limit": 200,
            "min_bars": 60,
            "stale_after_seconds": 86400,
            "data_domains": ["market_bars", "indicator_frames"],
        }
    ]


def test_recommendation_job_passes_watchlist_intake_options() -> None:
    """推荐任务的观察池入池选项应透传给执行器。"""

    calls: list[dict[str, Any]] = []

    def run_recommendation_pipeline(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"status": "available", "recommendation_run_id": "run:real"}

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        jobs=(
            BaseDataSchedulerJob(
                name="analytics.recommendations.ashare.all_a",
                job_type="recommendation_pipeline",
                group="analytics",
                interval_seconds=3600,
                limit=20,
                market="ashare",
                params={
                    "universe_id": "universe:base:ashare:p0:all_a",
                    "auto_sync_watchlist": True,
                    "owner_id": "default-owner",
                    "watchlist_id": "watchlist:default-owner:ashare:recommendations",
                    "recommendation_intake_limit": 20,
                },
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=lambda _args: {"status": "unexpected"},
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        run_recommendation_pipeline_func=run_recommendation_pipeline,
    )

    result = scheduler.run_once()

    assert result["jobs"][0]["status"] == "executed"
    assert calls == [
        {
            "universe_id": "universe:base:ashare:p0:all_a",
            "strategy": "balanced_swing_v1",
            "horizon": "swing",
            "limit": 20,
            "auto_sync_watchlist": True,
            "owner_id": "default-owner",
            "watchlist_id": "watchlist:default-owner:ashare:recommendations",
            "recommendation_intake_limit": 20,
        }
    ]


def test_scheduler_logs_job_progress_to_standard_logging(caplog) -> None:
    """调度任务应输出标准日志，方便 PyCharm 控制台直接观察执行进度。"""

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        jobs=(
            BaseDataSchedulerJob(
                name="ashare.test.job",
                group="ashare-p0",
                interval_seconds=3600,
                market="ashare",
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=lambda _args: {"status": "ok", "total_tasks": 1, "available": 1},
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
    )
    caplog.set_level(logging.INFO, logger="finance_agent.scheduler.base_data_scheduler")

    result = scheduler.run_once()

    messages = [record.getMessage() for record in caplog.records]
    assert result["jobs"][0]["status"] == "executed"
    assert any(
        "调度任务开始" in message and "job=ashare.test.job" in message
        for message in messages
    )
    assert any(
        "调度任务完成" in message and "job=ashare.test.job" in message
        for message in messages
    )


def test_scheduler_converts_ashare_market_bars_lookback_to_collection_dates() -> None:
    """A 股 K 线调度任务应把 lookback 转换成动态采集日期。"""

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        jobs=(
            BaseDataSchedulerJob(
                name="ashare.bars.1d",
                job_type="collection",
                group="ashare-p0",
                interval_seconds=3600,
                limit=200,
                market="ashare",
                params={
                    "sync_task_type": "market_bars_backfill",
                    "lookback": "30d",
                    "symbol_source": "market_assets",
                    "ashare_timeframe": "1d",
                },
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
    )

    args = scheduler.build_collection_args(config.jobs[0])

    assert args.ashare_end != "20260514"
    assert int(args.ashare_end) > int(args.ashare_start)
    start_date = datetime.strptime(args.ashare_start, "%Y%m%d").replace(tzinfo=UTC)
    end_date = datetime.strptime(args.ashare_end, "%Y%m%d").replace(tzinfo=UTC)
    assert (end_date - start_date).days == 30
    assert args.symbol_source == "market_assets"


def test_scheduler_loop_runs_due_jobs_concurrently() -> None:
    """loop 模式应把到期任务提交到后台线程，避免慢任务阻塞其它任务。"""

    started: list[str] = []
    release_slow_job = threading.Event()
    fast_job_finished = threading.Event()

    def collect_base_data(args: Namespace) -> dict[str, Any]:
        started.append(args.name)
        if args.name == "slow":
            release_slow_job.wait(timeout=2)
        if args.name == "fast":
            fast_job_finished.set()
            release_slow_job.set()
        return {"status": "ok", "name": args.name}

    def sleep(_: float) -> None:
        if fast_job_finished.wait(timeout=2):
            release_slow_job.set()

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        max_concurrent_jobs=2,
        loop_idle_seconds=1,
        jobs=(
            BaseDataSchedulerJob(
                name="slow",
                group="ashare-p0",
                interval_seconds=60,
                params={"name": "slow"},
            ),
            BaseDataSchedulerJob(
                name="fast",
                group="ashare-p1",
                interval_seconds=60,
                params={"name": "fast"},
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=collect_base_data,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        sleep_func=sleep,
    )

    started_at = time.perf_counter()
    result = scheduler.run_loop(max_cycles=1)

    assert result["cycles"] == 1
    assert {job["job"] for job in result["jobs"]} == {"slow", "fast"}
    assert set(started) == {"slow", "fast"}
    assert time.perf_counter() - started_at < 1.5


def test_scheduler_loop_runs_market_universe_before_asset_dependents() -> None:
    """同一市场的资产池刷新应先于依赖资产池的采集任务执行，避免并发抢写资产主表。"""

    started: list[str] = []

    def collect_base_data(args: Namespace) -> dict[str, Any]:
        started.append(args.name)
        return {"status": "ok", "name": args.name}

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        max_concurrent_jobs=2,
        loop_idle_seconds=0.01,
        jobs=(
            BaseDataSchedulerJob(
                name="ashare.universe.all",
                group="ashare-p0",
                interval_seconds=3600,
                market="ashare",
                params={
                    "name": "ashare.universe.all",
                    "sync_task_type": "universe_refresh",
                },
            ),
            BaseDataSchedulerJob(
                name="ashare.fundamentals",
                group="ashare-p2",
                interval_seconds=3600,
                market="ashare",
                params={
                    "name": "ashare.fundamentals",
                    "sync_task_type": "fundamental_refresh",
                },
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=collect_base_data,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        sleep_func=lambda _: None,
    )

    result = scheduler.run_loop(max_cycles=1)

    assert result["cycles"] == 1
    assert started == ["ashare.universe.all"]


def test_status_writes_from_multiple_scheduler_instances_do_not_share_temp_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多个调度器实例并发写同一状态文件时，不应抢同一个临时文件。"""

    status_file = tmp_path / "status.json"
    first_temp_written = threading.Event()
    allow_first_replace = threading.Event()
    original_write_text = Path.write_text

    def delayed_first_temp_write(path: Path, data: str, *args: Any, **kwargs: Any) -> int:
        result = original_write_text(path, data, *args, **kwargs)
        if path.name.startswith("status.json.") and path.name.endswith(".tmp") and "first" in data:
            first_temp_written.set()
            assert allow_first_replace.wait(timeout=2)
        return result

    monkeypatch.setattr(Path, "write_text", delayed_first_temp_write)

    config = BaseDataSchedulerConfig(job_timeout_seconds=0)
    first = BaseDataScheduler(config, status_file=status_file)
    second = BaseDataScheduler(config, status_file=status_file)
    errors: list[BaseException] = []

    def write_first() -> None:
        try:
            first.write_status(state="running", writer="first")
        except BaseException as exc:  # pragma: no cover - 失败时由断言展示
            errors.append(exc)

    thread = threading.Thread(target=write_first)
    thread.start()
    assert first_temp_written.wait(timeout=2)

    second.write_status(state="running", writer="second")
    allow_first_replace.set()
    thread.join(timeout=2)

    assert not errors
    assert status_file.exists()


def test_status_replace_retries_transient_os_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """状态文件替换遇到短暂占用时应重试，而不是立即让调度任务失败。"""

    source = tmp_path / "status.tmp"
    target = tmp_path / "status.json"
    source.write_text("{}", encoding="utf-8")
    attempts = 0
    sleeps: list[float] = []
    original_replace = Path.replace

    def flaky_replace(path: Path, target_path: Path) -> Path:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("文件被暂时占用")
        return original_replace(path, target_path)

    monkeypatch.setattr(Path, "replace", flaky_replace)

    replace_file_with_retry(source, target, sleep_func=sleeps.append, max_attempts=2)

    assert attempts == 2
    assert sleeps
    assert target.read_text(encoding="utf-8") == "{}"


def test_seconds_until_next_run_handles_empty_waiting_states() -> None:
    """所有任务都在运行或排队时，调度循环等待应退回 idle 秒数而不是崩溃。"""

    assert seconds_until_next_run([], idle_seconds=5) == 5.0


def test_ashare_market_bars_backfill_registers_one_task_per_market_asset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股 K 线补采应按资产池批量登记任务，而不是固定只采 000001。"""

    collect_base_data = import_collection_module()

    calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append(
                {
                    "task": task,
                    "provider_key": provider_key,
                    "parameters": parameters,
                    "force": force,
                }
            )
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data,
        "batch_ashare_symbols",
        lambda session, limit, fallback_symbol: ["000001", "600519"],
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p0"],
        sync_task_type="market_bars_backfill",
        symbol_source="market_assets",
        limit=2,
        ashare_start="20260501",
        ashare_end="20260514",
    )

    collect_base_data.run_ashare_p0(object(), args, RecordingRuntime())

    ohlcv_calls = [item for item in calls if item["task"] == "ashare_p0_ohlcv"]
    assert [item["parameters"]["symbol"] for item in ohlcv_calls] == ["000001", "600519"]
    assert [item["provider_key"] for item in ohlcv_calls] == [
        "stock_zh_a_hist_tx:000001",
        "stock_zh_a_hist_tx:600519",
    ]


def test_ashare_universe_refresh_fetches_complete_asset_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股 Universe 刷新应拉完整资产列表，不能被调度批大小截断。"""

    collect_base_data = import_collection_module()

    collect_limits: list[int | None] = []
    task_parameters: list[dict[str, Any]] = []

    def fake_collect_assets(self: Any, **kwargs: Any) -> Any:
        collect_limits.append(kwargs["limit"])
        return Namespace()

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            if task == "ashare_p0_assets":
                task_parameters.append(parameters)
                collect()
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(collect_base_data.AshareP0Collector, "collect_assets", fake_collect_assets)

    args = collect_base_data.default_collection_args(
        group=["ashare-p0"],
        sync_task_type="universe_refresh",
        limit=2,
        batch_size=2,
    )

    collect_base_data.run_ashare_p0(object(), args, RecordingRuntime())

    assert collect_limits == [None]
    assert task_parameters == [{"limit": None}]


def test_symbol_task_batch_runner_limits_concurrency_and_preserves_order() -> None:
    """按标的采集的批内执行器应限制并发，并保持结果顺序稳定。"""

    collect_base_data = import_collection_module()
    symbols = ["000001", "000002", "000003", "000004"]
    active_count = 0
    max_active_count = 0
    lock = threading.Lock()

    def collect_symbol(symbol: str) -> Namespace:
        nonlocal active_count, max_active_count
        with lock:
            active_count += 1
            max_active_count = max(max_active_count, active_count)
        time.sleep(0.05)
        with lock:
            active_count -= 1
        return Namespace(
            task=symbol,
            status="planned",
            raw_record_id=None,
            item_count=0,
            error_message=None,
            payload={},
        )

    results = collect_base_data.run_symbol_task_batch(
        symbols,
        max_workers=2,
        collect_symbol=collect_symbol,
    )

    assert [result.task for result in results] == symbols
    assert max_active_count == 2


def test_ashare_p1_list_refreshes_use_unbounded_source_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股 P1 列表型来源应完整刷新，不应把调度 batch_size 当作总量上限。"""

    collect_base_data = import_collection_module()

    calls: list[dict[str, Any]] = []

    class FakeProvider:
        def fetch_index_catalog(self, limit: int | None = None) -> Namespace:
            return Namespace(payload={"indexes": [{"code": "000300", "name": "沪深300"}]})

        def fetch_industry_names(self, limit: int | None = None) -> Namespace:
            return Namespace(payload={"names": []})

        def fetch_concept_names(self, limit: int | None = None) -> Namespace:
            return Namespace(payload={"names": []})

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append(
                {
                    "task": task,
                    "provider_key": provider_key,
                    "parameters": parameters,
                    "force": force,
                }
            )
            collect()
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data.AshareP1Collector,
        "__init__",
        lambda self, session: setattr(self, "sector_provider", FakeProvider()),
    )
    monkeypatch.setattr(
        collect_base_data.AshareP1Collector,
        "collect_flow_rank",
        lambda self, indicator, limit: calls.append(
            {"task": "collect_flow_rank", "indicator": indicator, "limit": limit}
        ),
    )
    monkeypatch.setattr(
        collect_base_data.AshareP1Collector,
        "collect_index_members",
        lambda self,
        index_code,
        index_name,
        universe_id,
        universe_name,
        strategy_context,
        limit: calls.append(
            {"task": "collect_index_members", "index_code": index_code, "limit": limit}
        ),
    )
    monkeypatch.setattr(
        collect_base_data.AshareP1Collector,
        "collect_industry_members",
        lambda self,
        industry_name,
        universe_id,
        universe_name,
        strategy_context,
        limit: calls.append(
            {"task": "collect_industry_members", "industry": industry_name, "limit": limit}
        ),
    )
    monkeypatch.setattr(
        collect_base_data.AshareP1Collector,
        "collect_concept_members",
        lambda self,
        concept_name,
        universe_id,
        universe_name,
        strategy_context,
        limit: calls.append(
            {"task": "collect_concept_members", "concept": concept_name, "limit": limit}
        ),
    )
    monkeypatch.setattr(
        collect_base_data.AshareP1Collector,
        "collect_stock_news",
        lambda self, symbol, asset_name, limit: calls.append(
            {"task": "collect_stock_news", "symbol": symbol, "limit": limit}
        ),
    )
    monkeypatch.setattr(
        collect_base_data.AshareP1Collector,
        "collect_notice_reports",
        lambda self, symbol, date, limit: calls.append(
            {"task": "collect_notice_reports", "symbol": symbol, "limit": limit}
        ),
    )
    monkeypatch.setattr(
        collect_base_data,
        "should_refresh_asset_universe_before_incremental",
        lambda session, *, market, min_asset_count=None: False,
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p1"],
        sync_task_type="event_refresh",
        limit=2,
        batch_size=2,
    )

    collect_base_data.run_ashare_p1(object(), args, RecordingRuntime())

    event_call_limits = {
        item["task"]: item["limit"]
        for item in calls
        if item["task"] in {"collect_stock_news", "collect_notice_reports"}
    }
    assert event_call_limits == {
        "collect_stock_news": None,
        "collect_notice_reports": None,
    }

    calls.clear()
    args = collect_base_data.default_collection_args(
        group=["ashare-p1"],
        sync_task_type="universe_refresh",
        limit=2,
        batch_size=2,
        index_catalog_limit=0,
        industry_catalog_limit=0,
        concept_catalog_limit=0,
        catalog_member_limit=0,
    )

    collect_base_data.run_ashare_p1(object(), args, RecordingRuntime())

    flow_call = next(item for item in calls if item["task"] == "collect_flow_rank")
    flow_task = next(item for item in calls if item["task"] == "ashare_p1_flow_rank")
    assert flow_call["limit"] is None
    assert flow_task["parameters"]["limit"] is None


def test_ashare_event_refresh_runs_stock_news_for_all_market_assets_in_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股新闻增量应从完整资产池展开并按批跑完，而不是只采样例股票。"""

    collect_base_data = import_collection_module()

    calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append(
                {
                    "task": task,
                    "provider_key": provider_key,
                    "parameters": parameters,
                    "force": force,
                }
            )
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data,
        "batch_ashare_symbols",
        lambda session, limit, fallback_symbol: ["000001", "000002", "000003"],
    )
    monkeypatch.setattr(
        collect_base_data,
        "should_refresh_asset_universe_before_incremental",
        lambda session, *, market, min_asset_count=None: False,
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p1"],
        sync_task_type="event_refresh",
        symbol_source="market_assets",
        limit=2,
        batch_size=2,
    )

    results = collect_base_data.run_ashare_p1(object(), args, RecordingRuntime())

    news_calls = [item for item in calls if item["task"] == "ashare_p1_stock_news"]
    assert [item["parameters"]["symbol"] for item in news_calls] == ["000001", "000002", "000003"]
    assert [item["parameters"]["limit"] for item in news_calls] == [None, None, None]
    assert [
        item.payload.get("batch_index")
        for item in results
        if item.task == "ashare_p1_stock_news"
    ] == [
        1,
        1,
        2,
    ]
    notice_call = next(item for item in calls if item["task"] == "ashare_p1_notice_reports")
    assert notice_call["parameters"]["limit"] is None


def test_ashare_risk_sentiment_refreshes_use_unbounded_source_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股风险和情绪列表应完整刷新，不应只采一批样本。"""

    collect_base_data = import_collection_module()

    collected_limits: dict[str, int | None] = {}

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            collect()
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload=dict(parameters),
            )

    def record_limit(name: str):
        def inner(self: Any, *args: Any, **kwargs: Any) -> Any:
            collected_limits[name] = kwargs.get("limit")
            return Namespace()

        return inner

    for method_name in [
        "collect_stop_list",
        "collect_hot_rank",
        "collect_zt_pool",
        "collect_lhb_detail",
        "collect_block_trades",
        "collect_margin_sse",
        "collect_margin_szse",
    ]:
        monkeypatch.setattr(
            collect_base_data.AshareRiskSentimentCollector,
            method_name,
            record_limit(method_name),
        )

    args = collect_base_data.default_collection_args(
        group=["ashare-risk"],
        sync_task_type="risk_sentiment_refresh",
        limit=2,
        batch_size=2,
    )

    results = collect_base_data.run_ashare_risk(object(), args, RecordingRuntime())

    assert set(collected_limits) == {
        "collect_stop_list",
        "collect_hot_rank",
        "collect_zt_pool",
        "collect_lhb_detail",
        "collect_block_trades",
        "collect_margin_sse",
        "collect_margin_szse",
    }
    assert all(limit is None for limit in collected_limits.values())
    assert all(result.payload.get("limit") is None for result in results)


def test_ashare_market_bars_backfill_refreshes_complete_universe_when_pool_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """库内没有 A 股资产时，按资产补采应先刷新完整全 A Universe。"""

    collect_base_data = import_collection_module()

    collect_limits: list[int | None] = []
    calls: list[str] = []

    class FakeAssetRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def find_by_market(self, market: str, *, only_tradable: bool = True) -> list[Any]:
            assert market == "ashare"
            return []

    def fake_collect_assets(self: Any, **kwargs: Any) -> Any:
        collect_limits.append(kwargs["limit"])
        return Namespace()

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append(task)
            if task == "ashare_p0_assets":
                collect()
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(collect_base_data, "AssetRepository", FakeAssetRepository)
    monkeypatch.setattr(collect_base_data.AshareP0Collector, "collect_assets", fake_collect_assets)

    args = collect_base_data.default_collection_args(
        group=["ashare-p0"],
        sync_task_type="market_bars_backfill",
        symbol_source="market_assets",
        limit=2,
        batch_size=2,
    )

    collect_base_data.run_ashare_p0(object(), args, RecordingRuntime())

    assert collect_limits == [None]
    assert calls[:2] == ["ashare_p0_calendar", "ashare_p0_assets"]


def test_ashare_market_bars_backfill_runs_all_market_assets_in_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股 K 线补采的一次调度应按批跑完整个待补资产池。"""

    collect_base_data = import_collection_module()

    calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append(
                {
                    "task": task,
                    "provider_key": provider_key,
                    "parameters": parameters,
                    "force": force,
                }
            )
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data,
        "batch_ashare_symbols",
        lambda session, limit, fallback_symbol: [
            "000001",
            "000002",
            "000003",
            "000004",
            "000005",
        ],
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p0"],
        sync_task_type="market_bars_backfill",
        symbol_source="market_assets",
        limit=2,
        batch_size=2,
        ashare_start="20260501",
        ashare_end="20260514",
    )

    results = collect_base_data.run_ashare_p0(object(), args, RecordingRuntime())

    ohlcv_calls = [item for item in calls if item["task"] == "ashare_p0_ohlcv"]
    assert [item["parameters"]["symbol"] for item in ohlcv_calls] == [
        "000001",
        "000002",
        "000003",
        "000004",
        "000005",
    ]
    assert [item["parameters"]["limit"] for item in ohlcv_calls] == [2, 2, 2, 2, 2]
    assert [
        item.payload.get("batch_index")
        for item in results
        if item.task == "ashare_p0_ohlcv"
    ] == [
        1,
        1,
        2,
        2,
        3,
    ]
    assert [
        item.payload.get("batch_count")
        for item in results
        if item.task == "ashare_p0_ohlcv"
    ] == [
        3,
        3,
        3,
        3,
        3,
    ]


def test_ashare_market_bars_uses_symbol_scoped_provider_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股 K 线批量补采的熔断应按标的隔离，避免少数股票断连导致整批跳过。"""

    collect_base_data = import_collection_module()

    calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append(
                {
                    "task": task,
                    "provider_key": provider_key,
                    "parameters": parameters,
                    "force": force,
                }
            )
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data,
        "batch_ashare_symbols",
        lambda session, limit, fallback_symbol: ["000001", "600519"],
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p0"],
        sync_task_type="market_bars_backfill",
        symbol_source="market_assets",
        limit=2,
    )

    collect_base_data.run_ashare_p0(object(), args, RecordingRuntime())

    ohlcv_calls = [item for item in calls if item["task"] == "ashare_p0_ohlcv"]
    assert [item["provider_key"] for item in ohlcv_calls] == [
        "stock_zh_a_hist_tx:000001",
        "stock_zh_a_hist_tx:600519",
    ]


def test_batch_ashare_symbols_prefers_assets_with_less_market_bar_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股 K 线批次应优先补没有 K 线或 K 线较少的资产，避免反复覆盖代码靠前的股票。"""

    collect_base_data = import_collection_module()

    class FakeAssetRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def find_by_market(self, market: str, *, only_tradable: bool = True) -> list[Any]:
            assert market == "ashare"
            assert only_tradable is True
            return [
                Namespace(asset_id="ashare:000001", symbol="000001"),
                Namespace(asset_id="ashare:300750", symbol="300750"),
                Namespace(asset_id="ashare:600519", symbol="600519"),
            ]

    monkeypatch.setattr(collect_base_data, "AssetRepository", FakeAssetRepository)
    monkeypatch.setattr(
        collect_base_data,
        "_fetch_ashare_bar_coverage",
        lambda session, asset_ids, timeframe: {
            "ashare:000001": (40, None),
            "ashare:300750": (8, None),
        },
        raising=False,
    )

    symbols = collect_base_data.batch_ashare_symbols(
        object(),
        limit=2,
        fallback_symbol="000001",
    )

    assert symbols == ["600519", "300750"]


def test_resolve_ashare_collection_symbols_keeps_full_backfill_universe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """market_assets 模式下 limit 表示单标的拉取条数，不应截断资产全集。"""

    collect_base_data = import_collection_module()

    class FakeAssetRepository:
        def __init__(self, session: Any) -> None:
            self.session = session

        def find_by_market(self, market: str, *, only_tradable: bool = True) -> list[Any]:
            assert market == "ashare"
            return [
                Namespace(asset_id=f"ashare:00000{index}", symbol=f"00000{index}")
                for index in range(1, 6)
            ]

    monkeypatch.setattr(collect_base_data, "AssetRepository", FakeAssetRepository)
    monkeypatch.setattr(
        collect_base_data,
        "_fetch_ashare_bar_coverage",
        lambda session, asset_ids, timeframe: {},
        raising=False,
    )
    args = collect_base_data.default_collection_args(
        sync_task_type="market_bars_backfill",
        symbol_source="market_assets",
        limit=2,
    )

    symbols = collect_base_data.resolve_ashare_collection_symbols(object(), args)

    assert symbols == ["000001", "000002", "000003", "000004", "000005"]


def test_ashare_market_bars_backfill_commits_after_each_symbol_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """长时间全市场补采应每批提交一次，避免超时后整轮写入回滚。"""

    collect_base_data = import_collection_module()

    class FakeSession:
        def __init__(self) -> None:
            self.commit_count = 0

        def commit(self) -> None:
            self.commit_count += 1

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data,
        "batch_ashare_symbols",
        lambda session, limit, fallback_symbol: [
            "000001",
            "000002",
            "000003",
            "000004",
            "000005",
        ],
    )
    monkeypatch.setattr(
        collect_base_data,
        "should_refresh_asset_universe_before_incremental",
        lambda session, market: False,
    )

    session = FakeSession()
    args = collect_base_data.default_collection_args(
        group=["ashare-p0"],
        sync_task_type="market_bars_backfill",
        symbol_source="market_assets",
        limit=2,
        batch_size=2,
    )

    collect_base_data.run_ashare_p0(session, args, RecordingRuntime())

    assert session.commit_count == 3


def test_ashare_fundamentals_run_all_market_assets_in_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 股基本面和估值刷新应按完整资产池分批跑完。"""

    collect_base_data = import_collection_module()

    calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append({"task": task, "parameters": parameters})
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data,
        "batch_ashare_symbols",
        lambda session, limit, fallback_symbol: [
            "000001",
            "000002",
            "000003",
            "000004",
            "000005",
        ],
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p2"],
        sync_task_type="fundamental_refresh",
        symbol_source="market_assets",
        limit=2,
        batch_size=2,
    )

    results = collect_base_data.run_ashare_p2(object(), args, RecordingRuntime())

    per_symbol_calls = [
        item
        for item in calls
        if item["task"] in {"ashare_p2_financial_indicators", "ashare_p2_valuation"}
    ]
    assert [item["parameters"]["symbol"] for item in per_symbol_calls] == [
        "000001",
        "000001",
        "000002",
        "000002",
        "000003",
        "000003",
        "000004",
        "000004",
        "000005",
        "000005",
    ]
    assert [
        item.payload.get("batch_index")
        for item in results
        if item.task == "ashare_p2_valuation"
    ] == [
        1,
        1,
        2,
        2,
        3,
    ]


def test_ashare_fundamentals_does_not_fallback_to_single_symbol_when_universe_locked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """资产池刷新被锁住时，基本面任务应等待下一轮，而不是静默退化为只同步默认股票。"""

    collect_base_data = import_collection_module()

    calls: list[str] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append(task)
            if task == "ashare_p0_assets":
                return Namespace(
                    task=task,
                    status="locked",
                    raw_record_id=None,
                    item_count=0,
                    error_message="同参数采集任务正在运行",
                    payload={},
                )
            raise AssertionError(f"资产池未就绪时不应继续执行 {task}")

    monkeypatch.setattr(
        collect_base_data,
        "should_refresh_asset_universe_before_incremental",
        lambda session, market: True,
    )
    monkeypatch.setattr(
        collect_base_data,
        "batch_ashare_symbols",
        lambda session, limit, fallback_symbol: [fallback_symbol],
    )

    args = collect_base_data.default_collection_args(
        group=["ashare-p2"],
        sync_task_type="fundamental_refresh",
        symbol_source="market_assets",
        ashare_symbol="000001",
    )

    results = collect_base_data.run_ashare_p2(object(), args, RecordingRuntime())

    assert calls == ["ashare_p0_assets"]
    assert [result.task for result in results] == ["ashare_p0_assets"]
    assert results[0].status == "locked"


def test_crypto_market_bars_backfill_registers_one_task_per_market_asset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """数字货币 K 线补采应按市场资产批量注册任务，而不是固定只采 BTCUSDT。"""

    collect_base_data = import_collection_module()

    calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append(
                {
                    "task": task,
                    "provider_key": provider_key,
                    "parameters": parameters,
                    "force": force,
                }
            )
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data,
        "batch_crypto_symbols",
        lambda session, market, timeframe, limit, fallback_symbol: ["BTCUSDT", "ETHUSDT"],
        raising=False,
    )

    args = collect_base_data.default_collection_args(
        group=["crypto"],
        sync_task_type="market_bars_backfill",
        symbol_source="market_assets",
        crypto_market_type="spot",
        crypto_timeframe="1h",
        limit=2,
    )

    collect_base_data.run_crypto(object(), args, RecordingRuntime())

    ohlcv_calls = [item for item in calls if item["task"] == "crypto_ohlcv"]
    assert [item["parameters"]["symbol"] for item in ohlcv_calls] == ["BTCUSDT", "ETHUSDT"]
    assert [item["provider_key"] for item in ohlcv_calls] == [
        "ccxt_binance_fetch_ohlcv:spot:BTCUSDT",
        "ccxt_binance_fetch_ohlcv:spot:ETHUSDT",
    ]


def test_crypto_universe_refresh_fetches_complete_market_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Binance Universe 刷新应拉完整交易对列表，不能被调度批大小截断。"""

    collect_base_data = import_collection_module()

    collect_limits: list[int | None] = []
    task_parameters: list[dict[str, Any]] = []

    def fake_collect_markets(self: Any, **kwargs: Any) -> Any:
        collect_limits.append(kwargs["limit"])
        return Namespace()

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            if task == "crypto_markets":
                task_parameters.append(parameters)
                collect()
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data.CryptoDataCollector,
        "collect_markets",
        fake_collect_markets,
    )

    args = collect_base_data.default_collection_args(
        group=["crypto"],
        sync_task_type="universe_refresh",
        crypto_market_type="spot",
        limit=2,
        batch_size=2,
    )

    collect_base_data.run_crypto(object(), args, RecordingRuntime())

    assert collect_limits == [None]
    assert task_parameters == [{"market_type": "spot", "limit": None}]


def test_parse_collect_base_data_args_includes_crypto_lookback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """命令行入口应给数字货币日历任务提供 lookback，避免 universe_refresh 进入采集前崩溃。"""

    collect_base_data = import_collection_module()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "collect_base_data.py",
            "--group",
            "crypto",
            "--sync-task-type",
            "universe_refresh",
            "--crypto-market-type",
            "spot",
        ],
    )

    args = collect_base_data.parse_args()

    assert hasattr(args, "lookback")
    assert args.lookback is None


def test_crypto_market_bars_backfill_runs_all_market_assets_in_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """数字货币 K 线补采的一次调度应按批跑完整个待补交易对集合。"""

    collect_base_data = import_collection_module()

    calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append(
                {
                    "task": task,
                    "provider_key": provider_key,
                    "parameters": parameters,
                    "force": force,
                }
            )
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data,
        "batch_crypto_symbols",
        lambda session, market, timeframe, limit, fallback_symbol: [
            "BTCUSDT",
            "ETHUSDT",
            "SOLUSDT",
            "BNBUSDT",
        ],
        raising=False,
    )

    args = collect_base_data.default_collection_args(
        group=["crypto"],
        sync_task_type="market_bars_backfill",
        symbol_source="market_assets",
        crypto_market_type="spot",
        crypto_timeframe="1h",
        limit=2,
        batch_size=2,
    )

    results = collect_base_data.run_crypto(object(), args, RecordingRuntime())

    ohlcv_calls = [item for item in calls if item["task"] == "crypto_ohlcv"]
    assert [item["parameters"]["symbol"] for item in ohlcv_calls] == [
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "BNBUSDT",
    ]
    assert [item["parameters"]["limit"] for item in ohlcv_calls] == [2, 2, 2, 2]
    assert [item.payload.get("batch_index") for item in results if item.task == "crypto_ohlcv"] == [
        1,
        1,
        2,
        2,
    ]
    assert [item.payload.get("batch_count") for item in results if item.task == "crypto_ohlcv"] == [
        2,
        2,
        2,
        2,
    ]


def test_crypto_derivative_refresh_runs_all_future_assets_in_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """合约衍生品快照应按完整合约资产池分批跑完。"""

    collect_base_data = import_collection_module()

    calls: list[dict[str, Any]] = []

    class RecordingRuntime:
        def run_task(
            self,
            *,
            task: str,
            provider_key: str,
            parameters: dict[str, Any],
            collect: Any,
            force: bool = False,
        ) -> Any:
            calls.append(
                {
                    "task": task,
                    "provider_key": provider_key,
                    "parameters": parameters,
                }
            )
            return Namespace(
                task=task,
                status="planned",
                raw_record_id=None,
                item_count=0,
                error_message=None,
                payload={},
            )

    monkeypatch.setattr(
        collect_base_data,
        "batch_crypto_derivative_symbols",
        lambda session, market, limit, fallback_symbol: [
            "BTCUSDT",
            "ETHUSDT",
            "SOLUSDT",
            "BNBUSDT",
        ],
        raising=False,
    )

    args = collect_base_data.default_collection_args(
        group=["crypto"],
        sync_task_type="derivative_refresh",
        symbol_source="market_assets",
        crypto_market_type="future",
        limit=2,
        batch_size=2,
    )

    results = collect_base_data.run_crypto(object(), args, RecordingRuntime())

    derivative_calls = [item for item in calls if item["task"] == "crypto_derivative_snapshot"]
    assert [item["parameters"]["symbol"] for item in derivative_calls] == [
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "BNBUSDT",
    ]
    assert [item["provider_key"] for item in derivative_calls] == [
        "binance_derivative_snapshot:future:BTCUSDT",
        "binance_derivative_snapshot:future:ETHUSDT",
        "binance_derivative_snapshot:future:SOLUSDT",
        "binance_derivative_snapshot:future:BNBUSDT",
    ]
    assert [
        item.payload.get("batch_index")
        for item in results
        if item.task == "crypto_derivative_snapshot"
    ] == [
        1,
        1,
        2,
        2,
    ]
