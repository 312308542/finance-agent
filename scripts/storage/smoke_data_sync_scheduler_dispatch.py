"""验证数据同步配置能落到任务类型分发。

这个 smoke 不访问数据库和真实数据源，只检查调度器 dry-run 输出，以及
`collect_base_data.py` 中各分组函数是否会按 `sync_task_type` 收敛到对应任务。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT_DIR / "src"
DATA_SCRIPT_DIR = ROOT_DIR / "scripts" / "data"
for path in (SRC_DIR, DATA_SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from collect_base_data import (  # noqa: E402
    default_collection_args,
    run_ashare_p0,
    run_ashare_p1,
    run_ashare_p2,
    run_ashare_risk,
    run_crypto,
)
from finance_agent.scheduler import BaseDataScheduler, load_scheduler_config  # noqa: E402


class RecordingRuntime:
    """只记录任务，不执行 Provider。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run_task(
        self,
        *,
        task: str,
        provider_key: str,
        parameters: dict[str, Any],
        collect: Any,
        force: bool = False,
    ) -> Any:
        """记录运行计划并返回轻量摘要对象。"""

        self.calls.append(
            {
                "task": task,
                "provider_key": provider_key,
                "parameters": parameters,
                "force": force,
            }
        )
        return type(
            "RecordedTask",
            (),
            {
                "task": task,
                "status": "planned",
                "raw_record_id": None,
                "item_count": 0,
                "error_message": None,
                "payload": {"provider_key": provider_key},
            },
        )()


def main() -> None:
    """执行调度和任务分发 smoke。"""

    scheduler = BaseDataScheduler(load_scheduler_config())
    dry_run = scheduler.run_once(dry_run=True)
    planned_args = {
        item["job"]: item["collection_args"]
        for item in dry_run["jobs"]
    }
    assert planned_args["ashare.universe.all"]["sync_task_type"] == "universe_refresh"
    assert planned_args["ashare.universe.all"]["group"] == ["ashare-p0", "ashare-p1", "ashare-risk"]
    assert planned_args["ashare.universe.all"]["index_catalog_limit"] == 200
    assert planned_args["ashare.universe.all"]["catalog_member_limit"] == 0
    assert planned_args["ashare.bars.1d"]["sync_task_type"] == "market_bars_backfill"
    assert planned_args["crypto_future.derivatives"]["crypto_market_type"] == "future"

    assert_tasks(
        run_ashare_p0,
        default_collection_args(group=["ashare-p0"], sync_task_type="universe_refresh"),
        ["ashare_p0_assets"],
    )
    assert_tasks(
        run_ashare_p0,
        default_collection_args(group=["ashare-p0"], sync_task_type="market_bars_backfill"),
        ["ashare_p0_ohlcv"],
    )
    assert_tasks(
        run_ashare_p1,
        default_collection_args(group=["ashare-p1"], sync_task_type="capital_flow_refresh"),
        ["ashare_p1_flow_rank"],
    )
    assert_tasks(
        run_ashare_p1,
        default_collection_args(group=["ashare-p1"], sync_task_type="event_refresh"),
        ["ashare_p1_stock_news", "ashare_p1_notice_reports"],
    )
    assert_tasks(
        run_ashare_p2,
        default_collection_args(group=["ashare-p2"], sync_task_type="fundamental_refresh"),
        [
            "ashare_p2_financial_indicators",
            "ashare_p2_valuation",
            "ashare_p2_performance_report",
            "ashare_p2_dividend_yield",
        ],
    )
    assert_tasks(
        run_ashare_risk,
        default_collection_args(group=["ashare-risk"], sync_task_type="risk_sentiment_refresh"),
        [
            "ashare_risk_stop_list",
            "ashare_sentiment_hot_rank",
            "ashare_sentiment_zt_pool",
            "ashare_risk_lhb_detail",
            "ashare_risk_block_trades",
            "ashare_risk_margin_sse",
            "ashare_risk_margin_szse",
        ],
    )
    assert_tasks(
        run_crypto,
        default_collection_args(group=["crypto"], sync_task_type="universe_refresh"),
        ["crypto_markets"],
    )
    assert_tasks(
        run_crypto,
        default_collection_args(group=["crypto"], sync_task_type="market_bars_backfill"),
        ["crypto_ohlcv"],
    )
    assert_tasks(
        run_crypto,
        default_collection_args(group=["crypto"], sync_task_type="derivative_refresh"),
        ["crypto_derivative_snapshot"],
    )

    print({"status": "ok", "checked": len(planned_args)})


def assert_tasks(
    runner: Any,
    args: Any,
    expected_tasks: list[str],
) -> None:
    """断言某个分组函数只登记预期任务。"""

    runtime = RecordingRuntime()
    runner(object(), args, runtime)
    actual_tasks = [item["task"] for item in runtime.calls]
    if actual_tasks != expected_tasks:
        raise AssertionError(f"任务分发不符合预期：expected={expected_tasks}, actual={actual_tasks}")


if __name__ == "__main__":
    main()
