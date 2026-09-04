"""统一任务依赖表达测试。"""

from __future__ import annotations

from datetime import UTC, datetime

from finance_agent.data.sync_config import build_preset_config, export_scheduler_payload
from finance_agent.scheduler.base_data_scheduler import (
    BaseDataSchedulerJob,
    ScheduledJobState,
    dependency_is_satisfied,
    filter_due_states_by_dependencies,
    parse_scheduler_job,
    trigger_after_success_dependents,
)

NOW = datetime(2026, 7, 20, 9, 40, tzinfo=UTC)


def _state(name: str, generation: int | None) -> ScheduledJobState:
    return ScheduledJobState(
        job=BaseDataSchedulerJob(name=name, group="analytics", interval_seconds=0),
        next_run_at=None,
        last_success_generation=generation,
    )


def test_all_of_requires_every_dependency_to_have_succeeded() -> None:
    job = BaseDataSchedulerJob(
        name="consumer",
        group="analytics",
        interval_seconds=0,
        schedule_type="after_success",
        depends_on=("source-a", "source-b"),
        dependency_mode="all_of",
    )
    states = [_state("source-a", 1), _state("source-b", None), ScheduledJobState(job, None)]

    assert dependency_is_satisfied(job, states, generation=1) is False
    states[1].last_success_generation = 2
    assert dependency_is_satisfied(job, states, generation=1) is True


def test_any_of_wakes_after_one_dependency() -> None:
    job = BaseDataSchedulerJob(
        name="consumer",
        group="analytics",
        interval_seconds=0,
        schedule_type="after_success",
        depends_on=("source-a", "source-b"),
        dependency_mode="any_of",
    )
    states = [_state("source-a", 3), _state("source-b", None), ScheduledJobState(job, None)]

    assert dependency_is_satisfied(job, states, generation=3) is True


def test_barrier_requires_all_dependencies_in_same_generation() -> None:
    job = BaseDataSchedulerJob(
        name="consumer",
        group="analytics",
        interval_seconds=0,
        schedule_type="after_success",
        depends_on=("source-a", "source-b"),
        dependency_mode="barrier",
    )
    states = [_state("source-a", 4), _state("source-b", 3), ScheduledJobState(job, None)]

    assert dependency_is_satisfied(job, states, generation=4) is False
    states[1].last_success_generation = 4
    assert dependency_is_satisfied(job, states, generation=4) is True


def test_after_success_only_wakes_once_all_dependencies_are_satisfied() -> None:
    source_a = _state("source-a", 5)
    source_b = _state("source-b", None)
    consumer_job = BaseDataSchedulerJob(
        name="consumer",
        group="analytics",
        interval_seconds=0,
        schedule_type="after_success",
        depends_on=("source-a", "source-b"),
        dependency_mode="all_of",
    )
    consumer = ScheduledJobState(consumer_job, None)
    states = [source_a, source_b, consumer]

    trigger_after_success_dependents(
        states,
        completed_job_name="source-a",
        triggered_at=NOW,
        completion_generation=5,
    )
    assert consumer.next_run_at is None

    source_b.last_success_generation = 5
    trigger_after_success_dependents(
        states,
        completed_job_name="source-b",
        triggered_at=NOW,
        completion_generation=5,
    )
    assert consumer.next_run_at == NOW


def test_barrier_due_filter_uses_current_generation_instead_of_blocking_forever() -> None:
    source_a = _state("source-a", 7)
    source_b = _state("source-b", 7)
    consumer_job = BaseDataSchedulerJob(
        name="consumer",
        group="analytics",
        interval_seconds=0,
        schedule_type="after_success",
        depends_on=("source-a", "source-b"),
        dependency_mode="barrier",
    )
    consumer = ScheduledJobState(consumer_job, NOW)

    runnable, blocked = filter_due_states_by_dependencies(
        [consumer],
        all_states=[source_a, source_b, consumer],
        generation=7,
    )

    assert runnable == [consumer]
    assert blocked == []


def test_barrier_due_filter_keeps_stale_mixed_generations_blocked() -> None:
    source_a = _state("source-a", 7)
    source_b = _state("source-b", 6)
    consumer_job = BaseDataSchedulerJob(
        name="consumer",
        group="analytics",
        interval_seconds=0,
        schedule_type="after_success",
        depends_on=("source-a", "source-b"),
        dependency_mode="barrier",
    )
    consumer = ScheduledJobState(consumer_job, NOW)

    runnable, blocked = filter_due_states_by_dependencies(
        [consumer],
        all_states=[source_a, source_b, consumer],
        generation=7,
    )

    assert runnable == []
    assert blocked == [consumer]


def test_chained_close_dag_propagates_one_generation_to_recommendation_barrier() -> None:
    close = _state("close", 9)
    market = ScheduledJobState(
        BaseDataSchedulerJob(
            name="market",
            group="analytics",
            interval_seconds=0,
            schedule_type="after_success",
            depends_on=("close",),
        ),
        None,
    )
    technical = ScheduledJobState(
        BaseDataSchedulerJob(
            name="technical",
            group="analytics",
            interval_seconds=0,
            schedule_type="after_success",
            depends_on=("close",),
        ),
        None,
    )
    structure = ScheduledJobState(
        BaseDataSchedulerJob(
            name="structure",
            group="analytics",
            interval_seconds=0,
            schedule_type="after_success",
            depends_on=("close",),
        ),
        None,
    )
    merge = ScheduledJobState(
        BaseDataSchedulerJob(
            name="merge",
            group="analytics",
            interval_seconds=0,
            schedule_type="after_success",
            depends_on=("technical",),
        ),
        None,
    )
    sector = ScheduledJobState(
        BaseDataSchedulerJob(
            name="sector",
            group="analytics",
            interval_seconds=0,
            schedule_type="after_success",
            depends_on=("merge",),
        ),
        None,
    )
    recommendation = ScheduledJobState(
        BaseDataSchedulerJob(
            name="recommendation",
            group="analytics",
            interval_seconds=0,
            schedule_type="after_success",
            depends_on=("market", "sector", "structure"),
            dependency_mode="barrier",
        ),
        None,
    )
    states = [close, market, technical, structure, merge, sector, recommendation]

    trigger_after_success_dependents(
        states,
        completed_job_name="close",
        triggered_at=NOW,
        completion_generation=9,
    )
    market.last_success_generation = market.pending_generation
    structure.last_success_generation = structure.pending_generation
    technical.last_success_generation = technical.pending_generation
    trigger_after_success_dependents(
        states,
        completed_job_name="technical",
        triggered_at=NOW,
        completion_generation=technical.last_success_generation,
    )
    merge.last_success_generation = merge.pending_generation
    trigger_after_success_dependents(
        states,
        completed_job_name="merge",
        triggered_at=NOW,
        completion_generation=merge.last_success_generation,
    )
    sector.last_success_generation = sector.pending_generation
    trigger_after_success_dependents(
        states,
        completed_job_name="sector",
        triggered_at=NOW,
        completion_generation=sector.last_success_generation,
    )

    runnable, blocked = filter_due_states_by_dependencies(
        [recommendation],
        all_states=states,
    )
    assert recommendation.pending_generation == 9
    assert runnable == [recommendation]
    assert blocked == []


def test_scheduler_job_parses_dependency_mode() -> None:
    job = parse_scheduler_job(
        {
            "name": "consumer",
            "group": "analytics",
            "job_type": "data_quality_refresh",
            "schedule_type": "after_success",
            "depends_on": ["source-a", "source-b"],
            "dependency_mode": "barrier",
        },
        index=0,
    )

    assert job.dependency_mode == "barrier"


def test_recommendation_job_depends_on_close_sector_and_structure() -> None:
    payload = export_scheduler_payload(build_preset_config("personal-comprehensive"))
    jobs = {job["name"]: job for job in payload["jobs"]}
    recommendation = jobs["analytics.recommendations.ashare.all_a"]

    assert recommendation["depends_on"] == [
        "analytics.snapshot.ashare.close",
        "analytics.sector.ashare.daily",
        "analytics.structural.ashare.daily",
        "analytics.strategy.validation_gate",
    ]
    assert recommendation["dependency_mode"] == "barrier"
    assert all(name in jobs for name in recommendation["depends_on"])
    sector = jobs["analytics.sector.ashare.daily"]
    assert sector["depends_on"] == [
        "analytics.universe.merge.ashare.recommendation",
        "analytics.snapshot.ashare.close",
    ]
    assert sector["dependency_mode"] == "barrier"
