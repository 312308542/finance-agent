"""基础数据轻量调度器。

调度器只负责任务编排，不复制采集逻辑；实际采集仍复用
`scripts.data.collect_base_data` 中的分组入口。
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from finance_agent.data.sync_config import (
    DataSyncConfig,
    build_preset_config,
    export_scheduler_payload,
    load_data_sync_config,
    preview_data_sync_config,
    save_data_sync_config,
    validate_data_sync_config,
)

JsonDict = dict[str, Any]

COLLECTION_GROUPS = {"all", "ashare-p0", "ashare-p1", "ashare-p2", "ashare-risk", "crypto"}


@dataclass(frozen=True)
class BaseDataSchedulerJob:
    """单个基础数据采集任务计划。"""

    name: str
    group: str | tuple[str, ...]
    interval_seconds: int
    enabled: bool = True
    limit: int | None = None
    market: str | None = None
    params: JsonDict = field(default_factory=dict)


@dataclass(frozen=True)
class BaseDataSchedulerConfig:
    """基础数据调度器配置。"""

    enabled: bool = True
    cache_backend: str = "auto"
    lock_ttl_seconds: int = 600
    circuit_failure_threshold: int = 3
    circuit_cooldown_seconds: int = 900
    force_provider: bool = False
    loop_idle_seconds: int = 5
    max_job_retries: int = 2
    retry_backoff_seconds: int = 30
    health_stale_seconds: int = 300
    jobs: tuple[BaseDataSchedulerJob, ...] = ()


@dataclass
class ScheduledJobState:
    """运行期任务状态。"""

    job: BaseDataSchedulerJob
    next_run_at: datetime
    last_run_at: datetime | None = None
    last_summary: JsonDict | None = None


def default_scheduler_payload() -> JsonDict:
    """返回新一代全面数据同步默认调度计划。"""

    return export_scheduler_payload(build_preset_config())


def legacy_scheduler_payload() -> JsonDict:
    """返回旧版样例调度配置。

    该配置保留给本地样例采集和回归排查。正式数据同步默认使用
    `default_scheduler_payload()` 生成的全面计划。
    """

    return {
        "enabled": True,
        "cache_backend": "auto",
        "lock_ttl_seconds": 600,
        "circuit_failure_threshold": 3,
        "circuit_cooldown_seconds": 900,
        "force_provider": False,
        "loop_idle_seconds": 5,
        "max_job_retries": 2,
        "retry_backoff_seconds": 30,
        "health_stale_seconds": 300,
        "jobs": [
            {
                "name": "ashare-p0-assets-and-bars",
                "group": "ashare-p0",
                "enabled": True,
                "interval_seconds": 6 * 60 * 60,
                "limit": 20,
                "market": "ashare",
                "params": {
                    "ashare_symbol": "000001",
                    "ashare_name": "平安银行",
                    "ashare_start": "20260501",
                    "ashare_end": "20260514",
                    "ashare_timeframe": "1d",
                    "ashare_adjust": "qfq",
                },
            },
            {
                "name": "ashare-p1-sector-flow-news",
                "group": "ashare-p1",
                "enabled": True,
                "interval_seconds": 30 * 60,
                "limit": 10,
                "market": "ashare",
                "params": {
                    "industry": "银行",
                    "concept": "融资融券",
                    "flow_window": "5日",
                    "ashare_symbol": "000001",
                    "ashare_name": "平安银行",
                },
            },
            {
                "name": "ashare-p2-fundamental",
                "group": "ashare-p2",
                "enabled": True,
                "interval_seconds": 12 * 60 * 60,
                "limit": 10,
                "market": "ashare",
                "params": {
                    "ashare_symbol": "000001",
                    "ashare_name": "平安银行",
                    "report_date": "20250331",
                },
            },
            {
                "name": "ashare-risk-sentiment",
                "group": "ashare-risk",
                "enabled": True,
                "interval_seconds": 15 * 60,
                "limit": 10,
                "market": "ashare",
                "params": {
                    "risk_start": "20260501",
                    "risk_end": "20260514",
                    "risk_block_symbol": "A股",
                },
            },
            {
                "name": "crypto-spot-core",
                "group": "crypto",
                "enabled": True,
                "interval_seconds": 5 * 60,
                "limit": 20,
                "market": "crypto_spot",
                "params": {
                    "crypto_symbol": "BTCUSDT",
                    "crypto_timeframe": "1h",
                    "crypto_market_type": "spot",
                },
            },
        ],
    }


def default_data_sync_config_payload() -> JsonDict:
    """返回新的数据同步配置默认模板。"""

    return build_preset_config().to_dict()


def load_scheduler_config(path: str | Path | None = None) -> BaseDataSchedulerConfig:
    """从 JSON 文件读取调度器配置；未传路径时使用全面数据同步默认配置。"""

    if path is None:
        return parse_scheduler_config(default_scheduler_payload())

    config_path = Path(path)
    payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if is_data_sync_config_payload(payload):
        payload = export_scheduler_payload(load_data_sync_config(config_path))
    return parse_scheduler_config(payload)


def load_data_sync_scheduler_payload(path: str | Path | None = None) -> JsonDict:
    """加载新一代数据同步配置并导出底层调度器计划。"""

    config = load_data_sync_config(path)
    return export_scheduler_payload(config)


def is_data_sync_config_payload(payload: Any) -> bool:
    """判断 JSON 是否为新一代数据同步配置，而非已导出的调度计划。"""

    return (
        isinstance(payload, Mapping)
        and "markets" in payload
        and payload.get("schema_version") != "data-sync-scheduler-v1"
    )


def read_scheduler_health(
    status_file: str | Path,
    *,
    max_age_seconds: int | None = None,
) -> JsonDict:
    """读取调度器状态文件并返回健康检查摘要。"""

    path = Path(status_file)
    if not path.exists():
        return {
            "healthy": False,
            "status": "missing",
            "status_file": str(path),
            "message": "调度器状态文件不存在。",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        return {
            "healthy": False,
            "status": "invalid",
            "status_file": str(path),
            "message": f"调度器状态文件不是合法 JSON：{exc}",
        }

    updated_at = parse_datetime_or_none(payload.get("updated_at"))
    if updated_at is None:
        return {
            "healthy": False,
            "status": "invalid",
            "status_file": str(path),
            "payload": payload,
            "message": "调度器状态文件缺少 updated_at。",
        }

    age_seconds = max(0.0, (datetime.now(tz=UTC) - updated_at).total_seconds())
    stale_after = (
        max_age_seconds
        if max_age_seconds is not None
        else int(payload.get("health_stale_seconds") or 300)
    )
    state = str(payload.get("state") or "unknown")
    last_job_status = payload.get("last_job_status")
    healthy = age_seconds <= stale_after and state not in {"failed", "stale"}
    if last_job_status == "failed":
        healthy = False
    status = "healthy" if healthy else "unhealthy"
    if age_seconds > stale_after:
        status = "stale"
    return {
        "healthy": healthy,
        "status": status,
        "status_file": str(path),
        "age_seconds": round(age_seconds, 3),
        "stale_after_seconds": stale_after,
        "state": state,
        "last_job": payload.get("last_job"),
        "last_job_status": last_job_status,
        "last_error": payload.get("last_error"),
        "payload": payload,
    }


def parse_scheduler_config(payload: Mapping[str, Any]) -> BaseDataSchedulerConfig:
    """解析并校验调度器配置。"""

    jobs_payload = payload.get("jobs")
    if not isinstance(jobs_payload, list):
        raise ValueError("调度配置必须包含 jobs 数组")

    jobs = tuple(parse_scheduler_job(item, index=index) for index, item in enumerate(jobs_payload))
    return BaseDataSchedulerConfig(
        enabled=as_bool(payload.get("enabled", True), field_name="enabled"),
        cache_backend=as_choice(
            payload.get("cache_backend", "auto"),
            choices={"auto", "redis", "null"},
            field_name="cache_backend",
        ),
        lock_ttl_seconds=as_positive_int(
            payload.get("lock_ttl_seconds", 600),
            field_name="lock_ttl_seconds",
        ),
        circuit_failure_threshold=as_positive_int(
            payload.get("circuit_failure_threshold", 3),
            field_name="circuit_failure_threshold",
        ),
        circuit_cooldown_seconds=as_positive_int(
            payload.get("circuit_cooldown_seconds", 900),
            field_name="circuit_cooldown_seconds",
        ),
        force_provider=as_bool(payload.get("force_provider", False), field_name="force_provider"),
        loop_idle_seconds=as_positive_int(
            payload.get("loop_idle_seconds", 5),
            field_name="loop_idle_seconds",
        ),
        max_job_retries=as_non_negative_int(
            payload.get("max_job_retries", 2),
            field_name="max_job_retries",
        ),
        retry_backoff_seconds=as_positive_int(
            payload.get("retry_backoff_seconds", 30),
            field_name="retry_backoff_seconds",
        ),
        health_stale_seconds=as_positive_int(
            payload.get("health_stale_seconds", 300),
            field_name="health_stale_seconds",
        ),
        jobs=jobs,
    )


def parse_scheduler_job(payload: Any, *, index: int) -> BaseDataSchedulerJob:
    """解析单个任务配置。"""

    if not isinstance(payload, Mapping):
        raise ValueError(f"jobs[{index}] 必须是对象")

    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError(f"jobs[{index}].name 不能为空")

    group = as_group_choice(payload.get("group"), field_name=f"{name}.group")
    interval_seconds = as_positive_int(
        payload.get("interval_seconds"),
        field_name=f"{name}.interval_seconds",
    )
    limit = payload.get("limit")
    if limit is not None:
        limit = as_positive_int(limit, field_name=f"{name}.limit")

    market = payload.get("market")
    params = dict(payload.get("params", {}) or {})
    if params is None:
        params = {}
    if not isinstance(params, Mapping):
        raise ValueError(f"{name}.params 必须是对象")
    effective_group = as_group_choice(
        params.pop("group", group),
        field_name=f"{name}.params.group",
    )

    return BaseDataSchedulerJob(
        name=name,
        group=effective_group,
        interval_seconds=interval_seconds,
        enabled=as_bool(payload.get("enabled", True), field_name=f"{name}.enabled"),
        limit=limit,
        market=str(market) if market is not None else None,
        params=dict(params),
    )


class BaseDataScheduler:
    """按配置运行基础数据采集任务。"""

    def __init__(
        self,
        config: BaseDataSchedulerConfig,
        *,
        collect_base_data_func: Callable[[Any], JsonDict] | None = None,
        default_collection_args_func: Callable[..., Any] | None = None,
        sleep_func: Callable[[float], None] = time.sleep,
        status_file: str | Path | None = None,
        event_log_file: str | Path | None = None,
        service_name: str = "base_data_scheduler",
    ) -> None:
        self.config = config
        self._collect_base_data = collect_base_data_func
        self._default_collection_args = default_collection_args_func
        self._sleep = sleep_func
        self.status_file = Path(status_file) if status_file else None
        self.event_log_file = Path(event_log_file) if event_log_file else None
        self.service_name = service_name
        self.started_at: datetime | None = None

    def plan(self) -> JsonDict:
        """生成当前调度计划，不触发采集。"""

        enabled_jobs = [job for job in self.config.jobs if job.enabled]
        now = datetime.now(tz=UTC)
        return {
            "generated_at": now.isoformat(),
            "enabled": self.config.enabled,
            "cache_backend": self.config.cache_backend,
            "loop_idle_seconds": self.config.loop_idle_seconds,
            "jobs": [
                {
                    "name": job.name,
                    "group": job.group,
                    "enabled": job.enabled,
                    "interval_seconds": job.interval_seconds,
                    "limit": job.limit,
                    "market": job.market,
                    "params": job.params,
                    "will_run": self.config.enabled and job.enabled,
                }
                for job in self.config.jobs
            ],
            "enabled_job_count": len(enabled_jobs) if self.config.enabled else 0,
        }

    def run_once(self, *, dry_run: bool = False) -> JsonDict:
        """按配置执行一轮启用任务后退出。"""

        started_at = datetime.now(tz=UTC)
        self.started_at = started_at
        self.emit_event("scheduler_start", mode="run_once", dry_run=dry_run)
        self.write_status(
            state="running",
            mode="run_once",
            dry_run=dry_run,
            started_at=started_at,
        )
        if not self.config.enabled:
            result = {
                "mode": "run_once",
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(tz=UTC).isoformat(),
                "enabled": False,
                "dry_run": dry_run,
                "jobs": [],
            }
            self.stop_scheduler(result=result, state="disabled")
            return result

        job_summaries = [self.run_job(job, dry_run=dry_run) for job in self.enabled_jobs()]
        result = {
            "mode": "run_once",
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(tz=UTC).isoformat(),
            "enabled": True,
            "dry_run": dry_run,
            "jobs": job_summaries,
        }
        self.stop_scheduler(result=result, state="completed")
        return result

    def run_loop(self, *, dry_run: bool = False, max_cycles: int | None = None) -> JsonDict:
        """进入轻量循环模式，按 interval_seconds 调度任务。"""

        started_at = datetime.now(tz=UTC)
        self.started_at = started_at
        self.emit_event("scheduler_start", mode="loop", dry_run=dry_run)
        self.write_status(
            state="running",
            mode="loop",
            dry_run=dry_run,
            started_at=started_at,
        )
        if not self.config.enabled:
            result = {
                "mode": "loop",
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(tz=UTC).isoformat(),
                "enabled": False,
                "dry_run": dry_run,
                "cycles": 0,
                "jobs": [],
            }
            self.stop_scheduler(result=result, state="disabled")
            return result

        states = [
            ScheduledJobState(job=job, next_run_at=started_at)
            for job in self.enabled_jobs()
        ]
        cycles = 0
        executed: list[JsonDict] = []
        try:
            while states:
                now = datetime.now(tz=UTC)
                due_states = [state for state in states if state.next_run_at <= now]
                if not due_states:
                    self._sleep(seconds_until_next_run(states, self.config.loop_idle_seconds))
                    continue

                cycles += 1
                self.write_status(
                    state="running",
                    mode="loop",
                    dry_run=dry_run,
                    started_at=started_at,
                    cycles=cycles,
                )
                for state in due_states:
                    summary = self.run_job(state.job, dry_run=dry_run)
                    state.last_run_at = datetime.now(tz=UTC)
                    state.last_summary = summary
                    state.next_run_at = state.last_run_at + timedelta(
                        seconds=state.job.interval_seconds,
                    )
                    executed.append(
                        summary
                        | {
                            "last_run_at": state.last_run_at.isoformat(),
                            "next_run_at": state.next_run_at.isoformat(),
                        }
                    )

                if max_cycles is not None and cycles >= max_cycles:
                    break
        except KeyboardInterrupt:
            result = {
                "mode": "loop",
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(tz=UTC).isoformat(),
                "enabled": True,
                "dry_run": dry_run,
                "interrupted": True,
                "cycles": cycles,
                "jobs": executed,
            }
            self.stop_scheduler(result=result, state="interrupted")
            return result

        result = {
            "mode": "loop",
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(tz=UTC).isoformat(),
            "enabled": True,
            "dry_run": dry_run,
            "interrupted": False,
            "cycles": cycles,
            "jobs": executed,
        }
        self.stop_scheduler(result=result, state="completed")
        return result

    def run_job(self, job: BaseDataSchedulerJob, *, dry_run: bool = False) -> JsonDict:
        """执行单个启用任务。"""

        started_at = datetime.now(tz=UTC)
        args = self.build_collection_args(job)
        planned = {
            "job": job.name,
            "group": job.group,
            "market": job.market,
            "interval_seconds": job.interval_seconds,
            "collection_args": vars(args),
            "started_at": started_at.isoformat(),
        }
        if dry_run:
            summary = planned | {"dry_run": True, "status": "planned", "attempt_count": 0}
            self.emit_event("job_planned", job=job.name, market=job.market)
            self.write_status(
                state="running",
                last_job=job.name,
                last_job_status="planned",
                last_job_at=datetime.now(tz=UTC),
            )
            return summary

        max_attempts = self.config.max_job_retries + 1
        self.emit_event("job_start", job=job.name, market=job.market, max_attempts=max_attempts)
        last_error: str | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                summary = self.collect_base_data(args)
            except Exception as exc:
                last_error = str(exc)
                self.emit_event(
                    "job_error",
                    job=job.name,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    error_message=last_error,
                )
                if attempt < max_attempts:
                    self.emit_event(
                        "job_retry",
                        job=job.name,
                        attempt=attempt,
                        next_attempt=attempt + 1,
                        retry_backoff_seconds=self.config.retry_backoff_seconds,
                    )
                    self._sleep(float(self.config.retry_backoff_seconds))
                    continue
                failed = planned | {
                    "dry_run": False,
                    "status": "failed",
                    "attempt_count": attempt,
                    "error_message": last_error,
                    "finished_at": datetime.now(tz=UTC).isoformat(),
                }
                self.write_status(
                    state="running",
                    last_job=job.name,
                    last_job_status="failed",
                    last_job_at=datetime.now(tz=UTC),
                    last_error=last_error,
                )
                return failed

            executed = planned | {
                "dry_run": False,
                "status": "executed",
                "attempt_count": attempt,
                "summary": summary,
                "finished_at": datetime.now(tz=UTC).isoformat(),
            }
            self.emit_event(
                "job_success",
                job=job.name,
                attempt=attempt,
                summary=compact_collection_summary(summary),
            )
            self.write_status(
                state="running",
                last_job=job.name,
                last_job_status="executed",
                last_job_at=datetime.now(tz=UTC),
                last_error=None,
            )
            return executed

        raise RuntimeError("任务重试循环出现不可达状态")

    def enabled_jobs(self) -> tuple[BaseDataSchedulerJob, ...]:
        """返回全局和任务开关都启用的任务。"""

        if not self.config.enabled:
            return ()
        return tuple(job for job in self.config.jobs if job.enabled)

    def build_collection_args(self, job: BaseDataSchedulerJob) -> Any:
        """把任务配置转换为 collect_base_data 可接受的参数对象。"""

        if isinstance(job.group, tuple):
            group_value: list[str] = list(job.group)
        else:
            group_value = [job.group]
        overrides = {
            "group": group_value,
            "cache_backend": self.config.cache_backend,
            "lock_ttl_seconds": self.config.lock_ttl_seconds,
            "circuit_failure_threshold": self.config.circuit_failure_threshold,
            "circuit_cooldown_seconds": self.config.circuit_cooldown_seconds,
            "force_provider": self.config.force_provider,
        }
        if job.limit is not None:
            overrides["limit"] = job.limit
        overrides.update(job.params)
        return self.default_collection_args(**overrides)

    def collect_base_data(self, args: Any) -> JsonDict:
        """延迟导入采集入口，避免模块加载阶段触碰脚本依赖。"""

        if self._collect_base_data is None:
            module = import_collection_module()
            self._collect_base_data = module.collect_base_data
        return self._collect_base_data(args)

    def default_collection_args(self, **overrides: Any) -> Any:
        """延迟导入采集参数工厂。"""

        if self._default_collection_args is None:
            module = import_collection_module()
            self._default_collection_args = module.default_collection_args
        return self._default_collection_args(**overrides)

    def stop_scheduler(self, *, result: JsonDict, state: str) -> None:
        """写入停止状态和停止事件。"""

        finished_at = datetime.now(tz=UTC)
        last_job = None
        last_job_status = None
        last_error = None
        if result.get("jobs"):
            last = result["jobs"][-1]
            last_job = last.get("job")
            last_job_status = last.get("status")
            last_error = last.get("error_message")
        self.write_status(
            state=state,
            mode=result.get("mode"),
            dry_run=result.get("dry_run"),
            started_at=parse_datetime_or_none(result.get("started_at")) or self.started_at,
            finished_at=finished_at,
            cycles=result.get("cycles"),
            last_job=last_job,
            last_job_status=last_job_status,
            last_error=last_error,
        )
        self.emit_event(
            "scheduler_stop",
            state=state,
            mode=result.get("mode"),
            dry_run=result.get("dry_run"),
            cycles=result.get("cycles"),
            job_count=len(result.get("jobs", [])),
        )

    def write_status(self, **payload: Any) -> None:
        """写入调度器健康状态文件。"""

        if self.status_file is None:
            return
        now = datetime.now(tz=UTC)
        status = {
            "service": self.service_name,
            "pid": os.getpid(),
            "updated_at": now.isoformat(),
            "state": payload.get("state", "running"),
            "enabled": self.config.enabled,
            "job_count": len(self.config.jobs),
            "enabled_job_count": len(self.enabled_jobs()),
            "health_stale_seconds": self.config.health_stale_seconds,
        }
        for key, value in payload.items():
            status[key] = serialize_scheduler_value(value)
        self.status_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file = self.status_file.with_suffix(self.status_file.suffix + ".tmp")
        temp_file.write_text(
            json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        temp_file.replace(self.status_file)

    def emit_event(self, event: str, **payload: Any) -> None:
        """写入结构化 JSONL 事件日志。"""

        if self.event_log_file is None:
            return
        record = {
            "service": self.service_name,
            "event": event,
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "pid": os.getpid(),
        }
        for key, value in payload.items():
            record[key] = serialize_scheduler_value(value)
        self.event_log_file.parent.mkdir(parents=True, exist_ok=True)
        with self.event_log_file.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str))
            file.write("\n")


def import_collection_module() -> Any:
    """兼容脚本目录和包路径两种运行方式导入采集入口。"""

    try:
        return importlib.import_module("scripts.data.collect_base_data")
    except ModuleNotFoundError as exc:
        if not str(exc.name).startswith("scripts"):
            raise
        try:
            return importlib.import_module("collect_base_data")
        except ModuleNotFoundError:
            script_path = Path(__file__).resolve().parents[3] / "scripts" / "data" / "collect_base_data.py"
            spec = importlib.util.spec_from_file_location(
                "finance_agent_collect_base_data",
                script_path,
            )
            if spec is None or spec.loader is None:
                raise ModuleNotFoundError("无法定位 scripts/data/collect_base_data.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module


def seconds_until_next_run(states: list[ScheduledJobState], idle_seconds: int) -> float:
    """计算下一次循环休眠秒数。"""

    now = datetime.now(tz=UTC)
    next_run_at = min(state.next_run_at for state in states)
    wait_seconds = max(0.0, (next_run_at - now).total_seconds())
    return min(float(idle_seconds), wait_seconds) if wait_seconds else 0.0


def as_bool(value: Any, *, field_name: str) -> bool:
    """解析布尔配置值。"""

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on"}:
            return True
        if normalized in {"false", "0", "no", "n", "off"}:
            return False
    raise ValueError(f"{field_name} 必须是布尔值")


def as_positive_int(value: Any, *, field_name: str) -> int:
    """解析正整数配置值。"""

    if isinstance(value, bool):
        raise ValueError(f"{field_name} 必须是正整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是正整数") from exc
    if parsed <= 0:
        raise ValueError(f"{field_name} 必须大于 0")
    return parsed


def as_non_negative_int(value: Any, *, field_name: str) -> int:
    """解析非负整数配置值。"""

    if isinstance(value, bool):
        raise ValueError(f"{field_name} 必须是非负整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是非负整数") from exc
    if parsed < 0:
        raise ValueError(f"{field_name} 必须大于等于 0")
    return parsed


def as_choice(value: Any, *, choices: set[str], field_name: str) -> str:
    """解析枚举配置值。"""

    parsed = str(value or "").strip()
    if parsed not in choices:
        allowed = ", ".join(sorted(choices))
        raise ValueError(f"{field_name} 必须是以下取值之一: {allowed}")
    return parsed


def as_group_choice(value: Any, *, field_name: str) -> str | tuple[str, ...]:
    """解析采集分组，支持单分组和多分组。"""

    if isinstance(value, list | tuple):
        groups = tuple(str(item).strip() for item in value if str(item).strip())
        if not groups:
            raise ValueError(f"{field_name} 不能为空")
        for group in groups:
            as_choice(group, choices=COLLECTION_GROUPS, field_name=field_name)
        return groups
    return as_choice(value, choices=COLLECTION_GROUPS, field_name=field_name)


def parse_datetime_or_none(value: Any) -> datetime | None:
    """解析 ISO 时间字符串，无法解析时返回空。"""

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def serialize_scheduler_value(value: Any) -> Any:
    """把调度器状态中的值转换为 JSON 友好格式。"""

    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, dict):
        return {str(key): serialize_scheduler_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [serialize_scheduler_value(item) for item in value]
    return value


def compact_collection_summary(summary: JsonDict) -> JsonDict:
    """压缩采集摘要，避免事件日志写入过大的 Provider 响应。"""

    keys = (
        "status",
        "started_at",
        "finished_at",
        "total_tasks",
        "available",
        "failed",
        "skipped",
        "groups",
        "sync_task_type",
        "mode",
    )
    return {key: summary.get(key) for key in keys if key in summary}
