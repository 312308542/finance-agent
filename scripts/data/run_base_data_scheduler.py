"""基础数据调度器命令行入口。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import sys
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def configure_logging(*, log_level: str, process_log_file: str | None) -> None:
    """配置调度器进程的控制台日志，并按需同步写入进程日志文件。"""

    level = getattr(logging, log_level.upper(), logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [pid=%(process)d thread=%(threadName)s] "
        "%(name)s - %(message)s"
    )
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(level)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)
    root_logger.addHandler(console_handler)

    if process_log_file:
        path = Path(process_log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        root_logger.addHandler(file_handler)

    # 采集任务会在超时保护子进程里执行，使用环境变量让子进程复用同一套日志出口。
    import os

    os.environ["FINANCE_AGENT_LOG_LEVEL"] = logging.getLevelName(level)
    if process_log_file:
        os.environ["FINANCE_AGENT_PROCESS_LOG_FILE"] = str(Path(process_log_file))


@contextmanager
def persistent_task_queue_scope() -> Iterator[object]:
    """为常驻调度器提供 PostgreSQL 任务队列事务。"""

    from finance_agent.scheduler import PersistentTaskQueue
    from finance_agent.storage.db import create_session_factory, session_scope
    from finance_agent.storage.repositories import OutboxEventRepository

    session_factory = create_session_factory()
    with session_scope(session_factory) as session:
        yield PersistentTaskQueue(
            session,
            outbox_repository=OutboxEventRepository(session),
        )


def main() -> None:
    """解析参数并运行基础数据调度器。"""

    from collect_base_data import default_collection_args

    from finance_agent.scheduler import (
        BaseDataScheduler,
        default_data_sync_config_payload,
        default_scheduler_payload,
        legacy_scheduler_payload,
        load_scheduler_config,
        read_scheduler_health,
    )

    args = parse_args()
    configure_logging(log_level=args.log_level, process_log_file=args.process_log_file)
    if args.health_check:
        health = read_scheduler_health(
            args.status_file,
            max_age_seconds=args.health_max_age_seconds,
        )
        print(json.dumps(health, ensure_ascii=False, indent=2, default=str))
        raise SystemExit(0 if health["healthy"] else 2)

    if args.dump_default_config:
        print(json.dumps(default_scheduler_payload(), ensure_ascii=False, indent=2))
        return
    if args.dump_default_legacy_config:
        print(json.dumps(legacy_scheduler_payload(), ensure_ascii=False, indent=2))
        return
    if args.dump_default_data_sync_config:
        print(json.dumps(default_data_sync_config_payload(), ensure_ascii=False, indent=2))
        return

    config = load_scheduler_config(args.config)
    if args.only:
        selected_names = set(args.only)
        available_names = {job.name for job in config.jobs}
        missing_names = sorted(selected_names - available_names)
        if missing_names:
            raise SystemExit(f"--only 未匹配任何任务：{', '.join(missing_names)}")
        selected_jobs = tuple(job for job in config.jobs if job.name in selected_names)
        if args.run_once:
            # 手动任务在常驻计划中保持禁用；显式 run-once 选择时仅为本次进程临时启用。
            selected_jobs = tuple(replace(job, enabled=True) for job in selected_jobs)
        config = replace(
            config,
            jobs=selected_jobs,
        )
    scheduler = BaseDataScheduler(
        config,
        default_collection_args_func=default_collection_args,
        status_file=args.status_file,
        event_log_file=args.event_log_file,
        scheduler_config_file=Path(args.config) if args.config else None,
        persistent_task_queue_scope=persistent_task_queue_scope,
    )

    with build_gotdx_gateway_context(args):
        if args.run_once:
            result = scheduler.run_once(dry_run=args.dry_run)
        elif args.loop:
            result = scheduler.run_loop(dry_run=args.dry_run, max_cycles=args.max_cycles)
        else:
            result = scheduler.plan()

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def parse_args() -> argparse.Namespace:
    """解析调度器命令行参数。"""

    parser = argparse.ArgumentParser(description="按配置调度 A 股和数字货币基础数据采集")
    parser.add_argument(
        "--config",
        help="调度器 JSON 配置路径；未提供时使用内置默认计划",
    )
    parser.add_argument(
        "--print-plan",
        action="store_true",
        help="只打印调度计划；这是未指定运行模式时的默认行为",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="演练 run-once 或 loop，只输出将要执行的采集参数，不触发采集",
    )
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="按配置执行所有启用任务后退出",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="只运行指定任务名；可重复传入，适合单独验收某个调度链路",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="按 interval_seconds 常驻循环运行",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="loop 模式最多执行多少个调度周期；主要用于验证",
    )
    parser.add_argument(
        "--status-file",
        default="runtime/base_data_scheduler/status.json",
        help="调度器健康状态 JSON 文件路径",
    )
    parser.add_argument(
        "--event-log-file",
        default="runtime/base_data_scheduler/events.jsonl",
        help="调度器结构化事件日志 JSONL 文件路径",
    )
    parser.add_argument(
        "--process-log-file",
        default=None,
        help="同步写入调度器普通文本日志文件；不影响控制台输出",
    )
    parser.add_argument(
        "--manage-gotdx-gateway",
        action="store_true",
        help="启动并监管本机的 gotdx Go 网关",
    )
    parser.add_argument(
        "--gotdx-gateway-command",
        default=None,
        help="gotdx 网关启动命令；未提供时从环境变量或默认目录发现",
    )
    parser.add_argument(
        "--gotdx-gateway-url",
        default=None,
        help="gotdx 网关健康检查地址",
    )
    parser.add_argument(
        "--gotdx-gateway-working-dir",
        default=None,
        help="gotdx 网关工作目录，默认 prototypes/gotdx-gateway",
    )
    parser.add_argument(
        "--gotdx-gateway-log-file",
        default="runtime/gotdx_gateway/gateway.log",
        help="gotdx 网关 stdout/stderr 日志文件",
    )
    parser.add_argument(
        "--gotdx-gateway-startup-timeout-seconds",
        type=float,
        default=None,
        help="gotdx 网关启动健康检查超时时间",
    )
    parser.add_argument(
        "--gotdx-gateway-monitor-interval-seconds",
        type=float,
        default=None,
        help="gotdx 网关后台探活周期",
    )
    parser.add_argument(
        "--gotdx-gateway-max-restarts",
        type=int,
        default=None,
        help="单次监管周期允许的 gotdx 网关最大重启次数",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="控制台日志级别",
    )
    parser.add_argument(
        "--health-check",
        action="store_true",
        help="读取 --status-file 并执行健康检查，不启动调度器",
    )
    parser.add_argument(
        "--health-max-age-seconds",
        type=int,
        default=None,
        help="健康检查允许的最大心跳年龄；未提供时读取状态文件中的阈值",
    )
    parser.add_argument(
        "--dump-default-config",
        action="store_true",
        help="输出新一代全面数据同步调度计划后退出",
    )
    parser.add_argument(
        "--dump-default-data-sync-config",
        action="store_true",
        help="输出与 data config init 对齐的数据同步配置模板后退出",
    )
    parser.add_argument(
        "--dump-default-legacy-config",
        action="store_true",
        help="输出旧版样例调度配置后退出",
    )
    args = parser.parse_args()
    selected_modes = sum(bool(value) for value in (args.print_plan, args.run_once, args.loop))
    if selected_modes > 1:
        parser.error("--print-plan、--run-once、--loop 只能选择一个")
    template_modes = sum(
        bool(value)
        for value in (
            args.dump_default_config,
            args.dump_default_data_sync_config,
            args.dump_default_legacy_config,
        )
    )
    if template_modes > 1:
        parser.error(
            "--dump-default-config、--dump-default-data-sync-config、"
            "--dump-default-legacy-config 只能选择一个"
        )
    if args.max_cycles is not None and args.max_cycles <= 0:
        parser.error("--max-cycles 必须大于 0")
    if args.max_cycles is not None and not args.loop:
        parser.error("--max-cycles 只在 --loop 模式下可用")
    if args.health_check and not args.status_file:
        parser.error("--health-check 需要 --status-file")
    if args.health_max_age_seconds is not None and args.health_max_age_seconds <= 0:
        parser.error("--health-max-age-seconds 必须大于 0")
    if args.manage_gotdx_gateway and not (args.run_once or args.loop):
        parser.error("--manage-gotdx-gateway 只能与 --run-once 或 --loop 一起使用")
    if args.loop and not args.config:
        parser.error("--loop 常驻模式必须显式提供 --config")
    if args.gotdx_gateway_startup_timeout_seconds is not None and args.gotdx_gateway_startup_timeout_seconds <= 0:
        parser.error("--gotdx-gateway-startup-timeout-seconds 必须大于 0")
    if args.gotdx_gateway_monitor_interval_seconds is not None and args.gotdx_gateway_monitor_interval_seconds <= 0:
        parser.error("--gotdx-gateway-monitor-interval-seconds 必须大于 0")
    if args.gotdx_gateway_max_restarts is not None and args.gotdx_gateway_max_restarts <= 0:
        parser.error("--gotdx-gateway-max-restarts 必须大于 0")
    return args


def build_gotdx_gateway_context(args: argparse.Namespace):
    """按 CLI 参数构建 gotdx 网关生命周期上下文。"""

    if not args.manage_gotdx_gateway:
        return nullcontext()

    from finance_agent.runtime import GotdxGatewayConfig, GotdxGatewaySupervisor

    command = None
    if args.gotdx_gateway_command:
        command = tuple(shlex.split(args.gotdx_gateway_command, posix=os.name != "nt"))
    working_dir = _resolve_project_path(args.gotdx_gateway_working_dir)
    if working_dir is None:
        working_dir = ROOT_DIR / "prototypes" / "gotdx-gateway"
    log_file = _resolve_project_path(args.gotdx_gateway_log_file)
    config = GotdxGatewayConfig.from_environment(
        root_dir=ROOT_DIR,
        command=command,
        base_url=args.gotdx_gateway_url,
        working_dir=working_dir,
        log_file=log_file,
        startup_timeout_seconds=args.gotdx_gateway_startup_timeout_seconds,
        monitor_interval_seconds=args.gotdx_gateway_monitor_interval_seconds,
        max_restart_attempts=args.gotdx_gateway_max_restarts,
    )
    return GotdxGatewaySupervisor(config)


def _resolve_project_path(value: str | None) -> Path | None:
    if value is None or not value.strip():
        return None
    path = Path(value)
    return path if path.is_absolute() else ROOT_DIR / path


if __name__ == "__main__":
    main()
