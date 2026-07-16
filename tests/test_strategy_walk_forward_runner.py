from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd

from finance_agent.research.strategy_walk_forward import (
    PointInTimeFactorSnapshot,
    WalkForwardOutcome,
)
from finance_agent.research.strategy_walk_forward_runner import (
    CandidateAsset,
    PointInTimeStrategyDataSource,
    SqlPointInTimeReader,
    StrategyWalkForwardRequest,
    StrategyWalkForwardRunner,
)
from finance_agent.storage.event_validation import STOCK_NEWS_SOURCE

SHORT = "strategy:ashare:short_swing"


class _CountingSession:
    """记录 SQL 读取次数，验证点时读取缓存不会改变结果。"""

    def __init__(self, *, scalar_rows: list[Any] | None = None) -> None:
        self.scalar_rows = scalar_rows or []
        self.scalar_calls = 0
        self.execute_rows: list[Any] = []
        self.execute_calls = 0

    def scalars(self, _statement: Any) -> list[Any]:
        self.scalar_calls += 1
        return list(self.scalar_rows)

    def execute(self, _statement: Any) -> Any:
        self.execute_calls += 1
        rows = list(self.execute_rows)

        class _Result:
            def __init__(self, values: list[Any]) -> None:
                self.values = values

            def __iter__(self):
                return iter(self.values)

        return _Result(rows)


def _bar_row(timestamp: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        timestamp=timestamp,
        open=10,
        high=11,
        low=9,
        close=10.5,
        volume=1000,
        amount=10500,
    )


def test_sql_reader_reuses_asset_bars_for_feature_and_forward_reads() -> None:
    """同一资产的特征和标签读取不得重复查询完整 K 线。"""

    signal_at = datetime(2025, 1, 2, tzinfo=UTC)
    session = _CountingSession(
        scalar_rows=[
            _bar_row(signal_at),
            _bar_row(signal_at + timedelta(days=1)),
            _bar_row(signal_at + timedelta(days=2)),
        ]
    )
    reader = SqlPointInTimeReader(session)

    bars = reader.list_bars(asset_id="ashare:000001", end_at=signal_at)
    forward = reader.list_forward_bars(
        asset_id="ashare:000001",
        signal_date=signal_at.date(),
        horizon_days=1,
    )

    assert len(bars) == 1
    assert len(forward) == 2
    assert session.scalar_calls == 1


def test_sql_reader_caches_static_point_in_time_records_per_asset() -> None:
    """基本面、资金流、事件、风险和题材记录应按资产复用。"""

    session = _CountingSession(scalar_rows=[SimpleNamespace(record_id="record:1")])
    reader = SqlPointInTimeReader(session)

    for method_name in (
        "list_fundamentals",
        "list_capital_flows",
        "list_events",
        "list_risks",
        "list_theme_memberships",
    ):
        method = getattr(reader, method_name)
        method(asset_id="ashare:000001")
        method(asset_id="ashare:000001")

    assert session.scalar_calls == 5


def test_sql_reader_caches_candidates_for_repeated_signal_date() -> None:
    """同一交易日重复请求候选池只执行一次日截面查询。"""

    session = _CountingSession()
    session.execute_rows = [
        ("ashare:000001", "000001"),
        ("ashare:000002", "000002"),
    ]
    reader = SqlPointInTimeReader(session)
    reader._warmup_timestamps = {
        "ashare:000001": datetime(2024, 1, 1, tzinfo=UTC),
    }

    first = reader.list_candidates(signal_date=date(2025, 1, 2))
    second = reader.list_candidates(signal_date=date(2025, 1, 2))

    assert first == [CandidateAsset(asset_id="ashare:000001", symbol="000001")]
    assert second == first
    assert session.execute_calls == 1


def _bars(*, end_at: datetime, count: int = 130) -> pd.DataFrame:
    timestamps = pd.date_range(end=end_at, periods=count, freq="B", tz="UTC")
    trend = np.arange(count, dtype=float)
    close = 20.0 + trend * 0.05 + np.sin(trend / 5.0)
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": close - 0.02,
            "high": close + 0.10,
            "low": close - 0.10,
            "close": close,
            "volume": 1_000_000.0 + trend * 1_000.0,
            "amount": (1_000_000.0 + trend * 1_000.0) * close,
        }
    )


class _PointInTimeReader:
    def __init__(self, *, as_of: datetime) -> None:
        self.as_of = as_of

    def list_bars(self, *, asset_id: str, end_at: datetime) -> pd.DataFrame:
        assert asset_id == "ashare:000001"
        assert end_at == self.as_of
        return _bars(end_at=end_at)

    def list_fundamentals(self, *, asset_id: str) -> list[Any]:
        return [
            SimpleNamespace(
                snapshot_id="fundamental:no-disclosure",
                asset_id=asset_id,
                symbol="000001",
                report_period="2024-12-31",
                as_of=self.as_of - timedelta(days=30),
                status="available",
                payload={},
            )
        ]

    def list_capital_flows(self, *, asset_id: str) -> list[Any]:
        return [
            SimpleNamespace(
                snapshot_id="flow:future",
                asset_id=asset_id,
                as_of=self.as_of + timedelta(days=1),
                status="available",
            )
        ]

    def list_events(self, *, asset_id: str) -> list[Any]:
        return [
            SimpleNamespace(
                event_id="event:ambiguous",
                asset_id=asset_id,
                source=STOCK_NEWS_SOURCE,
                published_at=self.as_of - timedelta(hours=2),
                collected_at=self.as_of - timedelta(hours=1),
                sentiment="positive",
                payload={"entity_validation": {"status": "ambiguous"}},
            )
        ]

    def list_risks(self, *, asset_id: str) -> list[Any]:
        return []

    def list_theme_memberships(self, *, asset_id: str) -> list[Any]:
        return [
            SimpleNamespace(
                id="member:current",
                asset_id=asset_id,
                as_of=self.as_of + timedelta(days=100),
                included=True,
                payload={
                    "theme_context": {
                        "factor_groups": [
                            {"group": "sector_strength", "status": "available", "score": 99},
                            {"group": "leadership", "status": "available", "score": 98},
                        ]
                    }
                },
            )
        ]


def test_point_in_time_source_rejects_future_and_unprovable_inputs() -> None:
    """点时数据源必须拒绝未来值和无法证明当时可见的数据。"""

    as_of = datetime(2025, 6, 30, 15, tzinfo=UTC)
    source = PointInTimeStrategyDataSource(reader=_PointInTimeReader(as_of=as_of))

    snapshot = source.build_factor_snapshot(
        asset_id="ashare:000001",
        symbol="000001",
        as_of=as_of,
        strategy_id=SHORT,
    )

    assert snapshot.groups["technical"]["status"] == "available"
    assert snapshot.groups["fundamental"]["status"] == "unavailable"
    assert snapshot.groups["valuation"]["status"] == "unavailable"
    assert snapshot.groups["capital_flow"]["status"] == "unavailable"
    assert snapshot.groups["event"]["status"] == "unavailable"
    assert snapshot.groups["sector_strength"]["status"] == "unavailable"
    assert snapshot.groups["leadership"]["status"] == "unavailable"
    assert "fundamental:no-disclosure" not in snapshot.source_ids
    assert "flow:future" not in snapshot.source_ids
    assert "event:ambiguous" not in snapshot.source_ids
    assert "member:current" not in snapshot.source_ids


class _EqualDisclosureReader(_PointInTimeReader):
    def list_fundamentals(self, *, asset_id: str) -> list[Any]:
        common = {
            "asset_id": asset_id,
            "symbol": "000001",
            "report_period": "2024-12-31",
            "pe_ttm": Decimal("10"),
            "pb": Decimal("1"),
            "roe": Decimal("0.12"),
            "revenue_growth_yoy": Decimal("0.08"),
            "net_profit_growth_yoy": Decimal("0.06"),
            "debt_to_asset": Decimal("0.30"),
            "operating_cashflow": Decimal("1000000"),
            "as_of": self.as_of - timedelta(days=30),
            "status": "available",
            "missing_fields": [],
            "payload": {"disclosed_at": (self.as_of - timedelta(days=10)).isoformat()},
        }
        return [
            SimpleNamespace(snapshot_id="fundamental:1", **common),
            SimpleNamespace(snapshot_id="fundamental:2", **common),
        ]


def test_point_in_time_source_handles_equal_disclosure_timestamps() -> None:
    """同一披露时间的多来源记录不得触发 ORM 对象比较。"""

    as_of = datetime(2025, 6, 30, 15, tzinfo=UTC)
    source = PointInTimeStrategyDataSource(reader=_EqualDisclosureReader(as_of=as_of))

    snapshot = source.build_factor_snapshot(
        asset_id="ashare:000001",
        symbol="000001",
        as_of=as_of,
        strategy_id=SHORT,
    )

    assert snapshot.groups["fundamental"]["status"] == "available"
    assert any(source_id.startswith("fundamental:") for source_id in snapshot.source_ids)


class _ResearchSource:
    def __init__(self, *, available_weight: float = 0.90) -> None:
        self.available_weight = available_weight
        self.days = [date(2025, 1, 1) + timedelta(days=index) for index in range(120)]
        self.assets = [
            CandidateAsset(asset_id="ashare:000001", symbol="000001"),
            CandidateAsset(asset_id="ashare:600519", symbol="600519"),
        ]

    def list_signal_dates(self, *, start_at: datetime, end_at: datetime) -> list[date]:
        return self.days

    def list_candidates(self, *, signal_date: date) -> list[CandidateAsset]:
        assert signal_date in self.days
        return self.assets

    def build_factor_snapshot(
        self,
        *,
        asset_id: str,
        symbol: str,
        as_of: datetime,
        strategy_id: str,
    ) -> PointInTimeFactorSnapshot:
        assert strategy_id == SHORT
        return PointInTimeFactorSnapshot(
            asset_id=asset_id,
            symbol=symbol,
            as_of=as_of,
            groups={},
            available_weight=self.available_weight,
            source_ids=(f"source:{asset_id}",),
        )

    def score_snapshot(
        self,
        snapshot: PointInTimeFactorSnapshot,
        *,
        strategy_id: str,
    ) -> float:
        return 90.0 if snapshot.asset_id.endswith("000001") else 80.0

    def label_asset(
        self,
        *,
        asset_id: str,
        signal_date: date,
        horizon_days: int,
        round_trip_cost: float,
    ) -> WalkForwardOutcome:
        gross_return = 0.02 if asset_id.endswith("000001") else 0.0
        return WalkForwardOutcome(
            signal_date=signal_date,
            entry_date=signal_date + timedelta(days=1),
            exit_date=signal_date + timedelta(days=horizon_days),
            horizon_days=horizon_days,
            gross_return=gross_return,
            net_return=gross_return - round_trip_cost,
            benchmark_return=0.0,
            excess_return=gross_return - round_trip_cost,
        )

    def data_versions(self) -> dict[str, Any]:
        return {"bars_max_at": "2026-07-16T00:00:00+00:00", "code_commit": "abc123"}


class _CoverageShortCircuitSource(_ResearchSource):
    """构造覆盖必然不足的截面，验证 runner 不做无意义的评分和标签。"""

    def __init__(self) -> None:
        super().__init__(available_weight=0.70)
        self.assets = [
            CandidateAsset(asset_id=f"ashare:{index:06d}", symbol=f"{index:06d}")
            for index in range(20)
        ]
        self.snapshot_calls = 0
        self.label_calls = 0

    def build_factor_snapshot(self, **kwargs: Any) -> PointInTimeFactorSnapshot:
        self.snapshot_calls += 1
        return super().build_factor_snapshot(**kwargs)

    def label_asset(self, **kwargs: Any) -> WalkForwardOutcome:
        self.label_calls += 1
        return super().label_asset(**kwargs)


class _BacktestRepository:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def upsert_result(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(backtest_id=kwargs["backtest_id"])


def _request(*, dry_run: bool = False) -> StrategyWalkForwardRequest:
    return StrategyWalkForwardRequest(
        strategy_id=SHORT,
        universe_id="universe:merged:ashare:recommendation",
        start_at=datetime(2025, 1, 1, tzinfo=UTC),
        end_at=datetime(2026, 1, 1, tzinfo=UTC),
        topn=1,
        round_trip_cost=0.003,
        dry_run=dry_run,
    )


def test_runner_builds_candidate_benchmark_and_persists_reproducible_result() -> None:
    """独立 runner 应计算候选池基准，并保存参数与数据版本。"""

    repository = _BacktestRepository()
    result = StrategyWalkForwardRunner(
        source=_ResearchSource(),
        repository=repository,
    ).run(_request())

    assert result["status"] == "available"
    assert result["metrics"]["gate_passed"] is True
    assert result["metrics"]["horizon_mean_excess_returns"]["10"] > 0
    assert len(repository.calls) == 1
    saved = repository.calls[0]
    assert saved["status"] == "available"
    assert saved["data_versions"]["code_commit"] == "abc123"
    assert saved["payload"]["parameters"] == {
        "topn": 1,
        "round_trip_cost": 0.003,
        "entry": "next_trading_day_open",
        "horizons": [5, 10, 20],
    }
    assert saved["backtest_id"] == result["backtest_id"]


def test_runner_dry_run_does_not_persist_result() -> None:
    """dry-run 只返回研究摘要，不写 backtest_results。"""

    repository = _BacktestRepository()

    result = StrategyWalkForwardRunner(
        source=_ResearchSource(),
        repository=repository,
    ).run(_request(dry_run=True))

    assert result["status"] == "available"
    assert result["persisted"] is False
    assert repository.calls == []


def test_runner_keeps_data_shortage_separate_from_strategy_failure() -> None:
    """覆盖率不足必须持久化为 insufficient_data。"""

    repository = _BacktestRepository()

    result = StrategyWalkForwardRunner(
        source=_ResearchSource(available_weight=0.70),
        repository=repository,
    ).run(_request())

    assert result["status"] == "insufficient_data"
    assert repository.calls[0]["status"] == "insufficient_data"


def test_runner_short_circuits_when_cross_section_coverage_cannot_pass() -> None:
    """覆盖失败数超过 10% 后，应停止该截面的评分和未来标签。"""

    source = _CoverageShortCircuitSource()

    result = StrategyWalkForwardRunner(
        source=source,
        repository=_BacktestRepository(),
    ).run(_request(dry_run=True))

    assert result["status"] == "insufficient_data"
    assert result["metrics"]["total_cross_sections"] == len(source.days)
    assert source.snapshot_calls == len(source.days) * 3
    assert source.label_calls == 0
