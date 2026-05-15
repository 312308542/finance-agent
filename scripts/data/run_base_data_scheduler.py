"""基础数据调度器命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def main() -> None:
    """解析参数并运行基础数据调度器。"""

    from collect_base_data import collect_base_data, default_collection_args

    from finance_agent.scheduler import (
        BaseDataScheduler,
        default_scheduler_payload,
        load_scheduler_config,
    )

    args = parse_args()
    if args.dump_default_config:
        print(json.dumps(default_scheduler_payload(), ensure_ascii=False, indent=2))
        return

    config = load_scheduler_config(args.config)
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=collect_base_data,
        default_collection_args_func=default_collection_args,
    )

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
        "--dump-default-config",
        action="store_true",
        help="输出内置默认 JSON 配置模板后退出",
    )
    args = parser.parse_args()
    selected_modes = sum(bool(value) for value in (args.print_plan, args.run_once, args.loop))
    if selected_modes > 1:
        parser.error("--print-plan、--run-once、--loop 只能选择一个")
    if args.max_cycles is not None and args.max_cycles <= 0:
        parser.error("--max-cycles 必须大于 0")
    if args.max_cycles is not None and not args.loop:
        parser.error("--max-cycles 只在 --loop 模式下可用")
    return args


if __name__ == "__main__":
    main()
