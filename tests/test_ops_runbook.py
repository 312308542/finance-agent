from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "docs" / "运维手册.md"


def test_ops_runbook_documents_scheduler_health_and_recovery() -> None:
    """运维手册应覆盖调度器健康检查、故障处理和补采流程。"""

    content = RUNBOOK.read_text(encoding="utf-8")

    required_phrases = [
        "# 运维手册",
        "runtime/base_data_scheduler/status.json",
        "runtime/base_data_scheduler/events.jsonl",
        "--health-check",
        "Windows 任务计划",
        "Docker",
        "Redis 断连",
        "源熔断",
        "Cookie 过期",
        "refresh_eastmoney_cookie_file",
        "check_base_data_health.py",
        "backfill_jobs",
        "finance-agent data production backfill-plan",
        "不得直接暴露",
    ]

    for phrase in required_phrases:
        assert phrase in content
