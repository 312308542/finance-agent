from datetime import UTC, datetime
import logging

from finance_agent.cache.null_cache import NullCacheClient
from finance_agent.data.collection_runtime import CollectionRuntime
from finance_agent.data.collectors import ArchivedProviderResult
from finance_agent.data.models import ProviderResult


def test_collection_runtime_logs_console_progress(caplog) -> None:
    """采集任务应通过标准 logging 输出开始和写库摘要，便于控制台观察。"""

    runtime = CollectionRuntime(cache=NullCacheClient(), locks=NullCacheClient())

    def collect() -> ArchivedProviderResult:
        return ArchivedProviderResult(
            result=ProviderResult(
                provider_name="unit-provider",
                status="available",
                collected_at=datetime.now(tz=UTC),
                payload={"actual_source": "unit-source"},
            ),
            raw_record_id="raw:unit",
        )

    caplog.set_level(logging.INFO, logger="finance_agent.data.collection_runtime")

    result = runtime.run_task(
        task="unit_task",
        provider_key="unit_provider",
        parameters={"symbol": "000001"},
        collect=collect,
    )

    messages = [record.getMessage() for record in caplog.records]
    assert result.status == "available"
    assert any("采集任务开始" in message and "task=unit_task" in message for message in messages)
    assert any(
        "采集任务完成" in message
        and "task=unit_task" in message
        and "status=available" in message
        and "raw_record_id=raw:unit" in message
        for message in messages
    )
