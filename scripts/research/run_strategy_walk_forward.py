"""运行严格点时的策略 walk-forward 历史研究。"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime

from finance_agent.research.strategy_walk_forward_runner import (
    DEFAULT_UNIVERSE_ID,
    FIXED_ROUND_TRIP_COST,
    run_strategy_walk_forward,
)
from finance_agent.storage.db import create_session_factory, session_scope


def parse_datetime(value: str) -> datetime:
    """解析命令行 ISO 日期或时间，并统一为 UTC。"""

    normalized = value.strip()
    if len(normalized) == 10:
        normalized = f"{normalized}T00:00:00+00:00"
    parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def current_commit() -> str | None:
    """读取当前代码提交，失败时保持空值而不是伪造版本。"""

    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def build_parser() -> argparse.ArgumentParser:
    """构建独立历史研究命令参数。"""

    parser = argparse.ArgumentParser(description="运行严格无前视策略 walk-forward 研究")
    parser.add_argument("--strategy-id", required=True, help="带版本的评分策略 ID")
    parser.add_argument("--start-at", required=True, type=parse_datetime, help="研究开始日期/时间")
    parser.add_argument("--end-at", required=True, type=parse_datetime, help="研究结束日期/时间")
    parser.add_argument("--universe-id", default=DEFAULT_UNIVERSE_ID, help="审计候选池 ID")
    parser.add_argument("--topn", type=int, default=20, help="每个截面持有的前 N 个标的")
    parser.add_argument(
        "--cost",
        type=float,
        default=FIXED_ROUND_TRIP_COST,
        help="双边成本与滑点，规格固定为 0.003",
    )
    parser.add_argument("--dry-run", action="store_true", help="只计算，不写 backtest_results")
    parser.add_argument(
        "--asset-limit",
        type=int,
        default=None,
        help="仅用于诊断；正式验收禁止设置",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    with session_scope(create_session_factory()) as session:
        result = run_strategy_walk_forward(
            session,
            strategy_id=args.strategy_id,
            start_at=args.start_at,
            end_at=args.end_at,
            universe_id=args.universe_id,
            topn=args.topn,
            round_trip_cost=args.cost,
            dry_run=args.dry_run,
            asset_limit=args.asset_limit,
            code_commit=current_commit(),
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
