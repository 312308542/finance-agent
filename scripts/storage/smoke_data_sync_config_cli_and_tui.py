"""验证数据同步配置 CLI/TUI 能生成、校验和预览全面采集配置。"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    """执行数据同步配置 CLI/TUI 冒烟验证。"""

    with tempfile.TemporaryDirectory() as temp_dir:
        config_file = Path(temp_dir) / "data_sync.json"

        init_payload = run_json(
            [
                "data",
                "config",
                "init",
                "--preset",
                "personal-comprehensive",
                "--markets",
                "ashare,crypto_spot,crypto_future",
                "--output",
                str(config_file),
            ]
        )
        if init_payload["data"]["preset"] != "personal-comprehensive":
            raise AssertionError("初始化必须使用私人助手全面模式。")
        if not config_file.exists():
            raise AssertionError("初始化命令必须写出数据同步配置文件。")

        validate_payload = run_json(["data", "config", "validate", "--config-file", str(config_file)])
        if not validate_payload["data"]["valid"]:
            raise AssertionError(f"全面采集配置必须通过校验：{validate_payload}")

        preview_payload = run_json(["data", "config", "preview", "--config-file", str(config_file)])
        task_keys = {task["task_key"] for task in preview_payload["data"]["tasks"]}
        required_tasks = {
            "ashare.universe.all",
            "ashare.bars.1d",
            "ashare.fundamentals",
            "ashare.capital_flow",
            "ashare.events",
            "ashare.risk_sentiment",
            "crypto_spot.universe.binance",
            "crypto_spot.bars.1h",
            "crypto_future.universe.binance",
            "crypto_future.derivatives",
        }
        missing = required_tasks - task_keys
        if missing:
            raise AssertionError(f"预览必须包含全面采集任务，缺失：{sorted(missing)}")
        if preview_payload["data"]["manual_symbol_required"]:
            raise AssertionError("全面配置不应该要求用户手填股票代码。")

        export_payload = run_json(["data", "config", "export", "--config-file", str(config_file)])
        scheduler_jobs = export_payload["data"]["scheduler_payload"]["jobs"]
        if not scheduler_jobs:
            raise AssertionError("导出必须生成底层调度器 jobs。")
        if any("ashare_symbol" in job.get("params", {}) for job in scheduler_jobs):
            raise AssertionError("新配置导出的全量任务不应依赖样例股票代码。")
        ashare_universe_jobs = [
            job for job in scheduler_jobs if job["name"] == "ashare.universe.all"
        ]
        if not ashare_universe_jobs:
            raise AssertionError("导出必须包含 A 股 Universe 全量刷新任务。")
        ashare_universe_params = ashare_universe_jobs[0]["params"]
        required_catalog_params = {
            "index_catalog_limit",
            "industry_catalog_limit",
            "concept_catalog_limit",
            "catalog_member_limit",
        }
        missing_catalog_params = required_catalog_params - set(ashare_universe_params)
        if missing_catalog_params:
            raise AssertionError(f"A 股 Universe 任务缺少目录展开参数：{sorted(missing_catalog_params)}")
        if ashare_universe_params["catalog_member_limit"] != 0:
            raise AssertionError("默认全面模式不应截断每个目录下的成分成员。")

        tui_process = subprocess.run(
            [
                sys.executable,
                "-m",
                "finance_agent.cli",
                "data",
                "config",
                "tui",
                "--config-file",
                str(config_file),
                "--scripted",
                "preview",
            ],
            check=True,
            capture_output=True,
            encoding="utf-8",
            text=True,
        )
        if "数据同步配置 TUI" not in tui_process.stdout or "私人助手全面模式" not in tui_process.stdout:
            raise AssertionError("脚本化 TUI 必须展示数据同步配置和全面模式摘要。")

    print(
        {
            "status": "ok",
            "task_count": len(task_keys),
            "required_tasks": sorted(required_tasks),
        }
    )


def run_json(args: list[str]) -> dict[str, object]:
    """运行 finance-agent CLI 并解析 JSON。"""

    process = subprocess.run(
        [sys.executable, "-m", "finance_agent.cli", *args],
        check=True,
        capture_output=True,
        encoding="utf-8",
        text=True,
    )
    payload = json.loads(process.stdout)
    if payload["status"] != "ok":
        raise AssertionError(f"CLI 返回非 ok：{payload}")
    return payload


if __name__ == "__main__":
    main()
