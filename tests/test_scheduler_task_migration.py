"""持久调度状态迁移的结构回归测试。"""

from __future__ import annotations

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT_DIR
    / "src"
    / "finance_agent"
    / "storage"
    / "migrations"
    / "versions"
    / "20260831_0027_expand_scheduler_task_runs.py"
)


def test_scheduler_task_migration_is_additive_and_replaces_status_constraint() -> None:
    content = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "20260831_0027"' in content
    assert 'down_revision = "20260823_0026"' in content
    assert 'drop_constraint("ck_scheduler_task_runs_status"' in content
    assert "scheduled" in content
    assert "blocked" in content
    upgrade_content = content.split("def downgrade()", 1)[0]
    assert upgrade_content.count("op.add_column(") == 13
    for column in (
        "schedule_type",
        "scheduled_for",
        "priority",
        "resource_pool",
        "mutex_key",
        "dependency_generation",
        "required_data_domains",
        "blocked_reason",
        "blocked_detail",
        "blocked_until",
        "config_digest",
        "coalesced_count",
        "cancel_requested_at",
    ):
        assert f'"{column}"' in upgrade_content


def test_scheduler_task_migration_downgrade_removes_only_new_columns() -> None:
    content = MIGRATION.read_text(encoding="utf-8")

    assert "def downgrade()" in content
    assert 'drop_table("scheduler_task_runs")' not in content
    assert content.count('drop_column("scheduler_task_runs"') >= 13
