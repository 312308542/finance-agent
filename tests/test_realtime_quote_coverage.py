"""A 股实时行情 priority/market sweep 覆盖语义测试。"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from finance_agent.data.models import AssetData, AssetListResult
from finance_agent.scheduler.base_data_scheduler import import_collection_module

NOW = datetime(2026, 8, 31, 2, 0, tzinfo=UTC)


def test_collection_cli_accepts_full_market_snapshot_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = import_collection_module()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "collect_base_data.py",
            "--scope",
            "market_sweep",
            "--source-mode",
            "akshare_full_market",
            "--write-chunk-size",
            "500",
        ],
    )

    args = module.parse_args()

    assert args.scope == "market_sweep"
    assert args.source_mode == "akshare_full_market"
    assert args.write_chunk_size == 500


def test_realtime_task_uses_declared_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """执行器必须使用统一 limit，不能回退不存在的 realtime_quote_limit。"""

    module = import_collection_module()
    args = module.default_collection_args(limit=200)
    captured: dict[str, object] = {}

    def resolve(_session, *, max_symbols: int):
        captured["max_symbols"] = max_symbols
        return ["600519.SH"]

    class _Runtime:
        def run_task(self, **kwargs):
            captured.update(kwargs["parameters"])
            return SimpleNamespace(payload={})

    monkeypatch.setattr(module, "resolve_realtime_quote_symbols", resolve)

    module.build_ashare_parallel_realtime_task(object(), args, _Runtime())

    assert captured["max_symbols"] == 200


def test_market_sweep_cursor_resumes_from_persistent_partition() -> None:
    """market sweep 必须按稳定分区从持久 cursor 继续，而非重回首批。"""

    module = import_collection_module()
    partition = module.build_realtime_quote_partition(
        universe=[f"{index:06d}.SZ" for index in range(1_000)],
        scope="market_sweep",
        limit=None,
        batch_size=100,
        partition_cursor=2,
    )

    assert partition.symbols[0] == "000200.SZ"
    assert partition.symbols[-1] == "000299.SZ"
    assert partition.metrics["partition_cursor"] == 2
    assert partition.metrics["partition_count"] == 10
    assert partition.metrics["target_count"] == 1_000
    assert partition.next_partition_payload == {
        "partition_cursor": 3,
        "partition_count": 10,
    }


def test_full_market_sweep_fetches_akshare_once_and_writes_in_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全市场任务必须只抓一次整表，再按固定行数写库。"""

    module = import_collection_module()
    fetch_count = 0
    write_calls: list[tuple[str, int, int]] = []

    class _Akshare:
        def fetch_assets(self, *, limit: int | None = None) -> AssetListResult:
            nonlocal fetch_count
            assert limit is None
            fetch_count += 1
            collected_at = datetime(2026, 9, 7, 2, 0, tzinfo=UTC)
            return AssetListResult(
                provider_name="akshare",
                status="available",
                collected_at=collected_at,
                assets=[
                    AssetData(
                        asset_id=f"ashare:{600000 + index:06d}",
                        symbol=f"{600000 + index:06d}",
                        name=f"测试股票{index}",
                        market="ashare",
                        asset_type="stock",
                        payload={"最新价": "10.00", "成交量": str(index)},
                    )
                    for index in range(1001)
                ],
            )

    class _Repo:
        history_rows: list[dict[str, Any]] = []
        latest_rows: list[dict[str, Any]] = []

        def upsert_realtime_quote_snapshots(
            self,
            rows: Any,
            *,
            chunk_size: int,
        ) -> int:
            self.history_rows = list(rows)
            write_calls.append(("history", len(self.history_rows), chunk_size))
            return len(self.history_rows)

        def upsert_intraday_quote_latest(
            self,
            rows: Any,
            *,
            chunk_size: int,
        ) -> int:
            self.latest_rows = list(rows)
            write_calls.append(("latest", len(self.latest_rows), chunk_size))
            return len(self.latest_rows)

    class _Snapshots:
        def insert_snapshot(self, snapshot: Any) -> Any:
            return snapshot

    class _Runtime:
        def run_task(self, *, collect: Any, **_: Any) -> Any:
            return collect()

    repository = _Repo()
    monkeypatch.setattr(module, "AkshareProvider", _Akshare)
    monkeypatch.setattr(module, "AssetRepository", lambda _session: repository)
    monkeypatch.setattr(module, "DataSnapshotRepository", lambda _session: _Snapshots())
    args = module.default_collection_args(
        scope="market_sweep",
        mode="full_market_snapshot",
        source_mode="akshare_full_market",
        write_chunk_size=500,
    )

    result = module.build_ashare_full_market_realtime_task(object(), args, _Runtime())

    assert fetch_count == 1
    assert write_calls == [("history", 1001, 500), ("latest", 1001, 500)]
    assert result.result.status == "available"
    assert result.result.payload["rows_written"] == 1001
    assert result.result.payload["source_mode"] == "akshare_full_market"


def test_realtime_coverage_counts_unique_fresh_assets_and_max_lag() -> None:
    """双源重复行不能虚增覆盖率，陈旧行必须进入最大滞后。"""

    module = import_collection_module()
    metrics = module.build_realtime_quote_coverage_metrics(
        target_symbols=("600519.SH", "000001.SZ", "000002.SZ"),
        requested_symbols=("600519.SH", "000001.SZ"),
        rows=(
            {"asset_id": "ashare:600519", "as_of": NOW - timedelta(seconds=30)},
            {"asset_id": "ashare:600519", "as_of": NOW - timedelta(seconds=35)},
            {"asset_id": "ashare:000001", "as_of": NOW - timedelta(seconds=700)},
        ),
        written_count=3,
        captured_at=NOW,
        freshness_seconds=120,
        source_statuses={"gotdx:tdx_main": "available", "akshare:stock_zh_a_spot": "partial"},
    )

    assert metrics["target_count"] == 3
    assert metrics["requested_count"] == 2
    assert metrics["written_count"] == 3
    assert metrics["fresh_count"] == 1
    assert metrics["coverage_ratio"] == pytest.approx(1 / 3)
    assert metrics["max_lag_seconds"] == 700
    assert metrics["source_statuses"]["akshare:stock_zh_a_spot"] == "partial"


def test_realtime_coverage_accepts_repository_orm_rows() -> None:
    """仓储返回 ORM 对象时也必须读取资产和时间字段。"""

    module = import_collection_module()
    metrics = module.build_realtime_quote_coverage_metrics(
        target_symbols=("600519.SH",),
        requested_symbols=("600519.SH",),
        rows=(
            SimpleNamespace(
                asset_id="ashare:600519",
                as_of=NOW - timedelta(seconds=30),
            ),
        ),
        written_count=1,
        captured_at=NOW,
        freshness_seconds=120,
        source_statuses={"gotdx:tdx_main": "available"},
    )

    assert metrics["fresh_count"] == 1
    assert metrics["coverage_ratio"] == 1
    assert metrics["max_lag_seconds"] == 30
