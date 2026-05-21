"""验证 A 股 Universe 刷新会按目录展开。

这个 smoke 不访问真实数据库和网络，只把 `AshareP1Collector` 替换成轻量对象，
确认 `run_ashare_p1(..., sync_task_type="universe_refresh")` 会先读取指数、行业、
概念目录，再批量登记成员采集任务。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT_DIR / "src"
DATA_SCRIPT_DIR = ROOT_DIR / "scripts" / "data"
for path in (SRC_DIR, DATA_SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import collect_base_data as collector_module  # noqa: E402
from finance_agent.data.models import ProviderResult  # noqa: E402


class FakeSectorProvider:
    """返回固定目录，用于验证目录驱动展开。"""

    def fetch_index_catalog(self, *, limit: int | None = None) -> ProviderResult:
        return ProviderResult(
            provider_name="fake",
            status="available",
            collected_at=datetime.now(tz=UTC),
            payload={
                "indexes": [
                    {"code": "000300", "name": "沪深300"},
                    {"code": "000905", "name": "中证500"},
                ]
            },
        )

    def fetch_industry_names(self, *, limit: int | None = None) -> ProviderResult:
        return ProviderResult(
            provider_name="fake",
            status="available",
            collected_at=datetime.now(tz=UTC),
            payload={"names": ["银行", "半导体"]},
        )

    def fetch_concept_names(self, *, limit: int | None = None) -> ProviderResult:
        return ProviderResult(
            provider_name="fake",
            status="available",
            collected_at=datetime.now(tz=UTC),
            payload={"names": ["融资融券", "人工智能"]},
        )


class FakeAshareP1Collector:
    """只暴露 run_ashare_p1 需要的接口，不触发数据库写入。"""

    def __init__(self, session: Any) -> None:
        self.sector_provider = FakeSectorProvider()

    def collect_index_members(self, **kwargs: Any) -> Any:
        return {"kind": "index", **kwargs}

    def collect_industry_members(self, **kwargs: Any) -> Any:
        return {"kind": "industry", **kwargs}

    def collect_concept_members(self, **kwargs: Any) -> Any:
        return {"kind": "concept", **kwargs}

    def collect_flow_rank(self, **kwargs: Any) -> Any:
        return {"kind": "flow", **kwargs}

    def collect_stock_news(self, **kwargs: Any) -> Any:
        return {"kind": "news", **kwargs}

    def collect_notice_reports(self, **kwargs: Any) -> Any:
        return {"kind": "notice", **kwargs}


class RecordingRuntime:
    """记录任务，不执行真实采集。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run_task(
        self,
        *,
        task: str,
        provider_key: str,
        parameters: dict[str, Any],
        collect: Any,
        force: bool = False,
    ) -> Any:
        self.calls.append(
            {
                "task": task,
                "provider_key": provider_key,
                "parameters": parameters,
                "force": force,
            }
        )
        return type(
            "RecordedTask",
            (),
            {
                "task": task,
                "status": "planned",
                "raw_record_id": None,
                "item_count": 0,
                "error_message": None,
                "payload": {"provider_key": provider_key},
            },
        )()


def main() -> None:
    """执行目录展开 smoke。"""

    original_collector = collector_module.AshareP1Collector
    collector_module.AshareP1Collector = FakeAshareP1Collector
    try:
        runtime = RecordingRuntime()
        args = collector_module.default_collection_args(
            group=["ashare-p1"],
            sync_task_type="universe_refresh",
            limit=2,
            catalog_member_limit=7,
        )
        collector_module.run_ashare_p1(object(), args, runtime)
    finally:
        collector_module.AshareP1Collector = original_collector

    task_names = [item["task"] for item in runtime.calls]
    required_tasks = {
        "ashare_p1_index_members:000300",
        "ashare_p1_index_members:000905",
        "ashare_p1_industry_members:银行",
        "ashare_p1_industry_members:半导体",
        "ashare_p1_concept_members:融资融券",
        "ashare_p1_concept_members:人工智能",
        "ashare_p1_flow_rank",
    }
    missing = required_tasks - set(task_names)
    if missing:
        raise AssertionError(f"A 股 Universe 目录展开缺失任务：{sorted(missing)}")

    member_tasks = [
        item for item in runtime.calls if item["task"].startswith("ashare_p1_index_members:")
    ]
    if not all(item["parameters"]["limit"] == 7 for item in member_tasks):
        raise AssertionError("目录成员采集应使用 catalog_member_limit，而不是目录数量 limit。")

    print({"status": "ok", "task_count": len(task_names), "tasks": task_names})


if __name__ == "__main__":
    main()
