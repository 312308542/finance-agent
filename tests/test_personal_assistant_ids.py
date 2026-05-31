from finance_agent.agents.personal_assistant import (
    build_alert_id,
    build_decision_id,
    build_memory_id,
    build_review_task_id,
)


def test_monitoring_workflow_ids_fit_database_columns() -> None:
    """长 Workflow ID 生成的关联主键不应超过数据库 String(160) 限制。"""

    run_id = (
        "workflow:watchlist:default-owner:ashare:recommendations:"
        "recommendation_intake:run:balanced_swing_v1:swing:"
        "20260531T045951Z:a4d3722df766:20260531045951"
    )
    asset_id = "ashare:688635"

    ids = [
        build_alert_id(run_id=run_id, asset_id=asset_id),
        build_decision_id(
            run_id=run_id,
            asset_id=asset_id,
            decision_type="recommendation_intake",
        ),
        build_memory_id(run_id=run_id, asset_id=asset_id),
        build_review_task_id(run_id=run_id, asset_id=asset_id),
    ]

    assert all(len(value) <= 160 for value in ids)
    assert all(asset_id in value for value in ids)
