"""基础数据轻量调度器。

调度器只负责任务编排，不复制采集逻辑；实际采集仍复用
`scripts.data.collect_base_data` 中的分组入口。
"""

from __future__ import annotations

import importlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

JsonDict = dict[str, Any]

COLLECTION_GROUPS = {"all", "ashare-p0", "ashare-p1", "ashare-p2", "ashare-risk", "crypto"}


@dataclass(frozen=True)
class BaseDataSchedulerJob:
    """单个基础数据采集任务计划。"""

    name: str
    group: str
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
    jobs: tuple[BaseDataSchedulerJob, ...] = ()


@dataclass
class ScheduledJobState:
    """运行期任务状态。"""

    job: BaseDataSchedulerJob
    next_run_at: datetime
    last_run_at: datetime | None = None
    last_summary: JsonDict | None = None


def default_scheduler_payload() -> JsonDict:
    """返回适合本项目基础数据层的默认调度配置。"""

    return {
        "enabled": True,
        "cache_backend": "auto",
        "lock_ttl_seconds": 600,
        "circuit_failure_threshold": 3,
        "circuit_cooldown_seconds": 900,
        "force_provider": False,
        "loop_idle_seconds": 5,
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


def load_scheduler_config(path: str | Path | None = None) -> BaseDataSchedulerConfig:
    """从 JSON 文件读取调度器配置；未传路径时使用内置默认配置。"""

    if path is None:
        return parse_scheduler_config(default_scheduler_payload())

    config_path = Path(path)
    payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
    return parse_scheduler_config(payload)


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
        jobs=jobs,
    )


def parse_scheduler_job(payload: Any, *, index: int) -> BaseDataSchedulerJob:
    """解析单个任务配置。"""

    if not isinstance(payload, Mapping):
        raise ValueError(f"jobs[{index}] 必须是对象")

    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError(f"jobs[{index}].name 不能为空")

    group = as_choice(payload.get("group"), choices=COLLECTION_GROUPS, field_name=f"{name}.group")
    interval_seconds = as_positive_int(
        payload.get("interval_seconds"),
        field_name=f"{name}.interval_seconds",
    )
    limit = payload.get("limit")
    if limit is not None:
        limit = as_positive_int(limit, field_name=f"{name}.limit")

    market = payload.get("market")
    params = payload.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, Mapping):
        raise ValueError(f"{name}.params 必须是对象")

    return BaseDataSchedulerJob(
        name=name,
        group=group,
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
    ) -> None:
        self.config = config
        self._collect_base_data = collect_base_data_func
        self._default_collection_args = default_collection_args_func
        self._sleep = sleep_func

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
        if not self.config.enabled:
            return {
                "mode": "run_once",
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(tz=UTC).isoformat(),
                "enabled": False,
                "dry_run": dry_run,
                "jobs": [],
            }

        job_summaries = [self.run_job(job, dry_run=dry_run) for job in self.enabled_jobs()]
        return {
            "mode": "run_once",
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(tz=UTC).isoformat(),
            "enabled": True,
            "dry_run": dry_run,
            "jobs": job_summaries,
        }

    def run_loop(self, *, dry_run: bool = False, max_cycles: int | None = None) -> JsonDict:
        """进入轻量循环模式，按 interval_seconds 调度任务。"""

        started_at = datetime.now(tz=UTC)
        if not self.config.enabled:
            return {
                "mode": "loop",
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(tz=UTC).isoformat(),
                "enabled": False,
                "dry_run": dry_run,
                "cycles": 0,
                "jobs": [],
            }

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
            return {
                "mode": "loop",
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(tz=UTC).isoformat(),
                "enabled": True,
                "dry_run": dry_run,
                "interrupted": True,
                "cycles": cycles,
                "jobs": executed,
            }

        return {
            "mode": "loop",
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(tz=UTC).isoformat(),
            "enabled": True,
            "dry_run": dry_run,
            "interrupted": False,
            "cycles": cycles,
            "jobs": executed,
        }

    def run_job(self, job: BaseDataSchedulerJob, *, dry_run: bool = False) -> JsonDict:
        """执行单个启用任务。"""

        args = self.build_collection_args(job)
        planned = {
            "job": job.name,
            "group": job.group,
            "market": job.market,
            "interval_seconds": job.interval_seconds,
            "collection_args": vars(args),
        }
        if dry_run:
            return planned | {"dry_run": True, "status": "planned"}

        summary = self.collect_base_data(args)
        return planned | {"dry_run": False, "status": "executed", "summary": summary}

    def enabled_jobs(self) -> tuple[BaseDataSchedulerJob, ...]:
        """返回全局和任务开关都启用的任务。"""

        if not self.config.enabled:
            return ()
        return tuple(job for job in self.config.jobs if job.enabled)

    def build_collection_args(self, job: BaseDataSchedulerJob) -> Any:
        """把任务配置转换为 collect_base_data 可接受的参数对象。"""

        overrides = {
            "group": [job.group],
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
            module = importlib.import_module("scripts.data.collect_base_data")
            self._collect_base_data = module.collect_base_data
        return self._collect_base_data(args)

    def default_collection_args(self, **overrides: Any) -> Any:
        """延迟导入采集参数工厂。"""

        if self._default_collection_args is None:
            module = importlib.import_module("scripts.data.collect_base_data")
            self._default_collection_args = module.default_collection_args
        return self._default_collection_args(**overrides)


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


def as_choice(value: Any, *, choices: set[str], field_name: str) -> str:
    """解析枚举配置值。"""

    parsed = str(value or "").strip()
    if parsed not in choices:
        allowed = ", ".join(sorted(choices))
        raise ValueError(f"{field_name} 必须是以下取值之一: {allowed}")
    return parsed
