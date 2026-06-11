from __future__ import annotations

from finance_agent.scheduler.task_queue_model import (
    ProviderChain,
    ProviderFetchResult,
    ProviderSource,
    TaskItem,
    TaskRun,
)


def test_provider_chain_falls_back_and_records_attempts() -> None:
    """ProviderChain 应按顺序降级，并完整记录每个源的尝试结果。"""

    chain = ProviderChain(
        [
            ProviderSource(name="eastmoney:direct:kline", rate_key="eastmoney_kline"),
            ProviderSource(name="tencent:direct:kline", rate_key="tencent_kline"),
        ]
    )

    def fetch(source: ProviderSource) -> ProviderFetchResult:
        if source.name == "eastmoney:direct:kline":
            raise RuntimeError("curl(56) connection closed")
        return ProviderFetchResult.ok(payload={"rows": [1, 2, 3]}, row_count=3)

    result = chain.run(fetch)

    assert result.status == "ok"
    assert result.source == "tencent:direct:kline"
    assert result.row_count == 3
    assert [attempt.source for attempt in result.attempts] == [
        "eastmoney:direct:kline",
        "tencent:direct:kline",
    ]
    assert [attempt.status for attempt in result.attempts] == ["error", "ok"]
    assert result.attempts[0].error_message == "curl(56) connection closed"


def test_provider_chain_returns_last_error_when_all_sources_fail() -> None:
    """所有源失败时应返回结构化 error，而不是抛出到任务批次外层。"""

    chain = ProviderChain(
        [
            ProviderSource(name="source:a"),
            ProviderSource(name="source:b"),
        ]
    )

    def fetch(source: ProviderSource) -> ProviderFetchResult:
        return ProviderFetchResult.error(f"{source.name} unavailable")

    result = chain.run(fetch)

    assert result.status == "error"
    assert result.source is None
    assert result.error_message == "source:b unavailable"
    assert [attempt.status for attempt in result.attempts] == ["error", "error"]


def test_task_run_snapshot_counts_item_statuses() -> None:
    """TaskRun 应能汇总 item 状态，供 Redis 进度快照和前端任务详情复用。"""

    run = TaskRun(job_name="ashare.fundamentals", run_id="run-001")
    completed = TaskItem(
        item_id="ashare:000001:fundamentals",
        data_domain="fundamentals",
        market="ashare",
        asset_id="ashare:000001",
        symbol="000001",
    )
    failed = TaskItem(
        item_id="ashare:000002:fundamentals",
        data_domain="fundamentals",
        market="ashare",
        asset_id="ashare:000002",
        symbol="000002",
    )
    skipped = TaskItem(
        item_id="ashare:000003:fundamentals",
        data_domain="fundamentals",
        market="ashare",
        asset_id="ashare:000003",
        symbol="000003",
    )

    run.add_item(completed.mark_completed(item_count=2))
    run.add_item(failed.mark_failed("network timeout"))
    run.add_item(skipped.mark_skipped("watermark is fresh"))

    snapshot = run.snapshot()

    assert snapshot["job_name"] == "ashare.fundamentals"
    assert snapshot["run_id"] == "run-001"
    assert snapshot["total_items"] == 3
    assert snapshot["completed_items"] == 1
    assert snapshot["failed_items"] == 1
    assert snapshot["skipped_items"] == 1
    assert snapshot["remaining_items"] == 0
    assert snapshot["items"][0]["status"] == "completed"
