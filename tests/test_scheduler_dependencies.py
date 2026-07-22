"""统一任务依赖表达测试。"""

from __future__ import annotations

from datetime import UTC, datetime

from finance_agent.scheduler.base_data_scheduler import (
    BaseDataSchedulerJob,
    ScheduledJobState,
    dependency_is_satisfied,
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
