from datetime import UTC, datetime

from finance_agent.monitoring.models import PositionAction
from finance_agent.monitoring.repository import PositionMonitoringRepository

NOW = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)


def _action(action: str, reasons=()):
    return PositionAction(
        position_id="position:owner:600519",
        action=action,
        severity="low",
        reason_codes=tuple(reasons),
        evaluated_at=NOW,
        quote_snapshot_id="quote:1",
        payload={
            "owner_id": "owner-a",
            "asset_id": "ashare:600519",
            "total_quantity": "1000",
            "sellable_quantity": "1000",
        },
    )


def test_repository_updates_current_state_and_appends_only_changed_events() -> None:
    repository = PositionMonitoringRepository()
    repository.save(_action("hold", ()))
    repository.save(_action("hold", ()))
    repository.save(_action("reduce", ("sector_cooling",)))

    current = repository.get_state("position:owner:600519")
    events = repository.list_events("position:owner:600519")
    assert current.current_action == "reduce"
    assert current.previous_valid_action == "reduce"
    assert [event["action"] for event in events] == ["hold", "reduce"]


def test_repository_keeps_previous_valid_action_for_unexecutable() -> None:
    repository = PositionMonitoringRepository()
    repository.save(_action("hold"))
    repository.save(_action("unexecutable", ("t1_not_sellable",)))
    current = repository.get_state("position:owner:600519")
    assert current.current_action == "unexecutable"
    assert current.previous_valid_action == "hold"
