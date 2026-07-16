from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_action_loop_closed_cycle_smoke_script_outputs_audit_summary() -> None:
    """08-T7 冒烟脚本应输出建议到复盘的完整闭环摘要。"""

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "storage" / "smoke_action_loop_closed_cycle.py"),
            "--dry-run",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)

    assert summary["closed_loop_status"] == "completed"
    assert summary["decision_status"] == "accepted"
    assert summary["order_draft_status"] == "drafted"
    assert summary["execution_source"] == "user_reported"
    assert summary["position_status"] == "active"
    assert summary["review_outcome"] == "confirmed"
    assert summary["memory_recall_count"] >= 1
    assert summary["audit_chain"] == [
        "recommendation",
        "confirmation",
        "order_draft",
        "execution",
        "review",
        "memory",
    ]
