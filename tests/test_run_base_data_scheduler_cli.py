"""基础数据调度器 CLI 的回归测试。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


def test_run_base_data_scheduler_only_runs_named_job(tmp_path: Path) -> None:
    """`--only` 应显式启用并只执行指定的手动任务。"""

    config_path = tmp_path / "scheduler.json"
    config_path.write_text(
        json.dumps(
            {
                "enabled": True,
                "cache_backend": "null",
                "jobs": [
                    {
                        "name": "ashare.universe.all",
                        "job_type": "collection",
                        "group": "ashare-p0",
                        "interval_seconds": 3600,
                        "market": "ashare",
                        "limit": 1,
                        "params": {"group": ["ashare-p0"]},
                    },
                    {
                        "name": "analytics.structural.ashare.daily",
                        "enabled": False,
                        "job_type": "structural_methodology_refresh",
                        "group": "analytics",
                        "interval_seconds": 3600,
                        "market": "ashare",
                        "schedule_type": "manual",
                        "params": {"scope": "technical_screening_pool"},
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT_DIR / "scripts" / "data" / "run_base_data_scheduler.py"),
            "--config",
            str(config_path),
            "--run-once",
            "--dry-run",
            "--only",
            "analytics.structural.ashare.daily",
            "--status-file",
            str(tmp_path / "status.json"),
            "--event-log-file",
            str(tmp_path / "events.jsonl"),
            "--log-level",
            "CRITICAL",
        ],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(result.stdout)
    assert [job["job"] for job in payload["jobs"]] == ["analytics.structural.ashare.daily"]
    assert payload["jobs"][0]["status"] == "planned"
