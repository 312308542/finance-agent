from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent.cli.data_sync import dispatch_data
from finance_agent.data.sync_config import build_preset_config, export_scheduler_payload


REQUIRED_BATCH5_PREREQUISITE_JOBS = {
    "analytics.triggers.evaluate.daily",
    "analytics.triggers.evaluate.intraday",
    "agent.loop.consume.after_trigger",
    "agent.loop.consume.sweep",
    "analytics.high_risk_reviews.after_agent",
    "analytics.high_risk_reviews.sweep",
    "analytics.reviews.due",
    "analytics.backtest.weekly",
    "analytics.technical_screening.ashare.main_board",
    "analytics.universe.merge.ashare.recommendation",
    "analytics.universe.rebuild_avoid_pool.ashare",
}


def job_names(payload: dict) -> set[str]:
    return {str(job["name"]) for job in payload["jobs"]}


def test_personal_comprehensive_preset_regenerates_batch5_prerequisite_jobs() -> None:
    """默认预设应从零导出方案 10 前置所需的全部调度任务。"""

    payload = export_scheduler_payload(build_preset_config("personal-comprehensive"))

    assert REQUIRED_BATCH5_PREREQUISITE_JOBS.issubset(job_names(payload))

    jobs = {str(job["name"]): job for job in payload["jobs"]}
    assert jobs["analytics.triggers.evaluate.daily"]["schedule_type"] == "after_success"
    assert jobs["agent.loop.consume.after_trigger"]["depends_on"] == [
        "analytics.triggers.evaluate.daily",
        "analytics.triggers.evaluate.intraday",
    ]
    assert jobs["analytics.high_risk_reviews.after_agent"]["depends_on"] == [
        "agent.loop.consume.after_trigger"
    ]
    assert jobs["analytics.backtest.weekly"]["depends_on"] == [
        "analytics.recommendations.ashare.all_a"
    ]
    assert jobs["analytics.universe.merge.ashare.recommendation"]["depends_on"] == [
        "analytics.technical_screening.ashare.main_board"
    ]


def test_data_config_export_cli_path_writes_regenerable_scheduler_payload(
    tmp_path: Path,
) -> None:
    """CLI 导出路径也应写出同一组任务，避免只在本地 runtime 中存在。"""

    output = tmp_path / "scheduler.json"
    result = dispatch_data(
        argparse.Namespace(
            command="config",
            subcommand="export",
            config_file=None,
            output=str(output),
        )
    )

    assert result["status"] == "ok"
    assert output.exists()
    written_payload = json.loads(output.read_text(encoding="utf-8"))

    assert REQUIRED_BATCH5_PREREQUISITE_JOBS.issubset(job_names(written_payload))
    assert job_names(written_payload) == job_names(result["data"]["scheduler_payload"])
