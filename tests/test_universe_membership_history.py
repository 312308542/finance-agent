from finance_agent.storage.repositories import UniverseMembershipHistoryRepository
from scripts.data.backfill_universe_membership_history import build_report


def test_backfill_rejects_unverifiable_current_records() -> None:
    report = build_report(
        [
            {"provider": "akshare", "content_hash": "h", "collected_at": "2026-01-01"},
            {
                "provider": "akshare",
                "content_hash": "h2",
                "collected_at": "2026-01-01",
                "as_of": "2026-01-01",
            },
        ],
        minimum_snapshots=1,
    )
    assert report.accepted_records == 1
    assert report.rejected_records == 1


def test_backfill_reports_insufficient_independent_snapshots() -> None:
    report = build_report([], minimum_snapshots=120)
    assert report.status == "insufficient_data"
    assert report.reason == "independent_snapshots_below_minimum"


def test_membership_repository_exposes_point_in_time_query() -> None:
    assert hasattr(UniverseMembershipHistoryRepository, "list_members_as_of")
