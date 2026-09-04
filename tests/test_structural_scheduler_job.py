from __future__ import annotations

from argparse import Namespace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from finance_agent.data.sync_config import build_preset_config, export_scheduler_payload
from finance_agent.scheduler.base_data_scheduler import (
    BaseDataScheduler,
    BaseDataSchedulerConfig,
    BaseDataSchedulerJob,
    parse_scheduler_config,
)


def test_scheduler_payload_registers_structural_methodology_job() -> None:
    """结构方法论应作为技术初筛后的 analytics 任务导出。"""

    payload = export_scheduler_payload(build_preset_config("personal-comprehensive"))
    jobs = {job["name"]: job for job in payload["jobs"]}

    job = jobs["analytics.structural.ashare.daily"]

    assert job["job_type"] == "structural_methodology_refresh"
    assert job["group"] == "analytics"
    assert job["market"] == "ashare"
    assert job["resource_pool"] == "analytics"
    assert job["priority"] == 540
    assert job["schedule_type"] == "after_success"
    assert job["depends_on"] == ["ashare.bars.1d.close_final"]
    assert job["params"] == {
        "sync_task_type": "analytics.structural_methodology",
        "market": "ashare",
        "timeframe": "1d",
        "engines": ["swings", "smc", "harmonic", "elliott", "ichimoku"],
        "universe_ids": ["universe:merged:ashare:recommendation"],
        "lookback_bars": 250,
        "swing_window": 10,
        "harmonic_max_bars_since_d": 10,
        "fvg_min_atr_ratio": 0.3,
        "elliott_confidence_threshold": 0.6,
    }


def test_scheduler_parses_and_dry_runs_structural_methodology_job() -> None:
    """调度器应能解析结构任务，并在 dry-run 中展示结构计算参数。"""

    config = parse_scheduler_config(
        {
            "enabled": True,
            "jobs": [
                {
                    "name": "analytics.structural.ashare.daily",
                    "job_type": "structural_methodology_refresh",
                    "group": "analytics",
                    "enabled": True,
                    "schedule_type": "after_success",
                    "depends_on": ["ashare.bars.1d.close_final"],
                    "market": "ashare",
                    "limit": 50,
                    "params": {
                        "market": "ashare",
                        "timeframe": "1d",
                        "engines": ["swings", "smc"],
                        "lookback_bars": 120,
                    },
                }
            ],
        }
    )

    scheduler = BaseDataScheduler(config)
    planned = scheduler.run_job(config.jobs[0], dry_run=True)

    assert config.jobs[0].job_type == "structural_methodology_refresh"
    assert planned["status"] == "planned"
    assert planned["structural_methodology_args"] == {
        "market": "ashare",
        "timeframe": "1d",
        "engines": ["swings", "smc"],
        "lookback_bars": 120,
        "limit": 50,
    }


def test_scheduler_runs_structural_methodology_without_collection() -> None:
    """结构任务应调用 analytics 执行器，不应误走基础采集入口。"""

    calls: list[dict[str, Any]] = []

    def run_structural_methodology_refresh(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "status": "available",
            "asset_count": 1,
            "written_count": 4,
            "engine_counts": {"swings": 1, "smc": 1, "harmonic": 1, "elliott": 1},
        }

    def collect_base_data(_: Any) -> dict[str, Any]:
        raise AssertionError("结构方法论任务不应调用基础采集入口")

    config = BaseDataSchedulerConfig(
        job_timeout_seconds=0,
        jobs=(
            BaseDataSchedulerJob(
                name="analytics.structural.ashare.daily",
                job_type="structural_methodology_refresh",
                group="analytics",
                interval_seconds=0,
                schedule_type="after_success",
                depends_on=("ashare.bars.1d.close_final",),
                limit=50,
                market="ashare",
                params={
                    "market": "ashare",
                    "timeframe": "1d",
                    "engines": ["swings", "smc"],
                    "lookback_bars": 120,
                },
            ),
        ),
    )
    scheduler = BaseDataScheduler(
        config,
        collect_base_data_func=collect_base_data,
        default_collection_args_func=lambda **kwargs: Namespace(**kwargs),
        run_structural_methodology_refresh_func=run_structural_methodology_refresh,
    )

    result = scheduler.run_once()

    assert result["jobs"][0]["status"] == "executed"
    assert result["jobs"][0]["summary"]["written_count"] == 4
    assert calls == [
        {
            "market": "ashare",
            "timeframe": "1d",
            "engines": ["swings", "smc"],
            "lookback_bars": 120,
            "limit": 50,
        }
    ]


def test_scheduler_defaults_structural_methodology_to_ichimoku() -> None:
    """未显式指定引擎时，调度器默认参数也应包含 Ichimoku。"""

    config = parse_scheduler_config(
        {
            "enabled": True,
            "jobs": [
                {
                    "name": "analytics.structural.ashare.daily",
                    "job_type": "structural_methodology_refresh",
                    "group": "analytics",
                    "enabled": True,
                    "interval_seconds": 60,
                    "market": "ashare",
                    "params": {"market": "ashare"},
                }
            ],
        }
    )

    scheduler = BaseDataScheduler(config)

    assert scheduler.build_structural_methodology_refresh_kwargs(config.jobs[0])["engines"] == [
        "swings",
        "smc",
        "harmonic",
        "elliott",
        "ichimoku",
    ]


def test_structural_service_writes_ichimoku_v1_frame() -> None:
    """K 线充足时应把确定性 Ichimoku 结果写成指标帧。"""

    result, upsert_calls = _run_single_asset_ichimoku_refresh(bar_count=60)

    assert result["status"] == "available"
    assert result["written_count"] == 1
    assert result["engine_counts"] == {"ichimoku": 1}
    assert result["error_count"] == 0
    assert len(upsert_calls) == 1
    saved = upsert_calls[0]
    assert saved["horizon"] == "ichimoku_v1"
    assert saved["payload"]["status"] == "available"
    assert saved["payload"]["lines"]
    assert saved["payload"]["evidence_id"].startswith("ichimoku:")


def test_structural_service_writes_insufficient_ichimoku_frame() -> None:
    """预热不足时也应写可审计空帧，不能把资产计入错误。"""

    result, upsert_calls = _run_single_asset_ichimoku_refresh(bar_count=3)

    assert result["written_count"] == 1
    assert result["engine_counts"] == {"ichimoku": 1}
    assert result["status_counts"] == {"insufficient_data": 1}
    assert result["error_count"] == 0
    saved = upsert_calls[0]
    assert saved["horizon"] == "ichimoku_v1"
    assert saved["status"] == "insufficient_data"
    assert saved["payload"]["status"] == "insufficient_data"
    assert saved["payload"]["evidence_id"].startswith("ichimoku_v1:")
    assert "至少需要 52 根" in saved["payload"]["caveats"][0]
    assert "不得用模型自行补算" in saved["payload"]["red_lines"][1]


def test_structural_service_writes_insufficient_outputs_with_stable_upsert_keys() -> None:
    """K 线不足也要写入结构证据快照，并保持同输入幂等键稳定。"""

    from finance_agent.application.structural_methodology_service import (
        StructuralMethodologyRefreshService,
    )
    from finance_agent.indicators.structural_methodology_adapters import ENGINE_VERSION

    asset = SimpleNamespace(asset_id="ashare:600519", symbol="600519", market="ashare")
    bars = [_market_bar(index, asset=asset) for index in range(3)]
    saved_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    upsert_calls: list[dict[str, Any]] = []

    class FakeUniverseRepository:
        def __init__(self, _session: Any) -> None:
            pass

        def list_members(self, universe_id: str, *, included_only: bool = True) -> list[Any]:
            assert universe_id == "universe:technical:ashare:main_board"
            assert included_only is True
            return [asset]

    class FakeMarketDataRepository:
        def __init__(self, _session: Any) -> None:
            pass

        def list_recent_bars(self, **kwargs: Any) -> list[Any]:
            assert kwargs == {
                "asset_id": "ashare:600519",
                "timeframe": "1d",
                "limit": 250,
                "source": None,
            }
            return bars

    class FakeIndicatorFrameRepository:
        def __init__(self, _session: Any) -> None:
            pass

        def upsert_indicator_frame(self, **kwargs: Any) -> Any:
            upsert_calls.append(kwargs)
            key = (
                kwargs["asset_id"],
                kwargs["timeframe"],
                kwargs["horizon"],
                kwargs["library"],
                kwargs["input_end_at"],
            )
            saved_by_key[key] = kwargs
            return SimpleNamespace(**kwargs)

    service = StructuralMethodologyRefreshService(
        object(),
        universe_repository=FakeUniverseRepository(object()),
        market_data=FakeMarketDataRepository(object()),
        indicators=FakeIndicatorFrameRepository(object()),
    )

    first = service.refresh(
        market="ashare",
        timeframe="1d",
        engines=["swings", "smc", "harmonic", "elliott"],
        universe_ids=["universe:technical:ashare:main_board"],
        lookback_bars=250,
    )
    second = service.refresh(
        market="ashare",
        timeframe="1d",
        engines=["swings", "smc", "harmonic", "elliott"],
        universe_ids=["universe:technical:ashare:main_board"],
        lookback_bars=250,
    )

    assert first["status"] == "available"
    assert first["asset_count"] == 1
    assert first["written_count"] == 4
    assert second["written_count"] == 4
    assert len(saved_by_key) == 4
    assert len(upsert_calls) == 8
    assert {call["horizon"] for call in upsert_calls} == {
        "structural_swings_v2",
        "smc_lite_v2",
        "harmonic_lite_v2",
        "elliott_lite_v2",
    }
    for call in upsert_calls:
        assert call["library"] == "structural-lite"
        assert call["library_version"] == ENGINE_VERSION
        assert call["status"] == "insufficient_data"
        assert call["payload"]["status"] == "insufficient_data"
        assert call["payload"]["schema_version"] == call["horizon"]
        assert call["payload"]["red_lines"]


def _run_single_asset_ichimoku_refresh(
    *, bar_count: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """用单资产测试替身运行 Ichimoku 刷新。"""

    from finance_agent.application.structural_methodology_service import (
        StructuralMethodologyRefreshService,
    )

    asset = SimpleNamespace(asset_id="ashare:600519", symbol="600519", market="ashare")
    bars = [_market_bar(index, asset=asset) for index in range(bar_count)]
    upsert_calls: list[dict[str, Any]] = []

    class FakeUniverseRepository:
        def list_members(self, universe_id: str, *, included_only: bool = True) -> list[Any]:
            assert universe_id == "universe:technical:ashare:main_board"
            assert included_only is True
            return [asset]

    class FakeMarketDataRepository:
        def list_recent_bars(self, **kwargs: Any) -> list[Any]:
            assert kwargs["asset_id"] == asset.asset_id
            assert kwargs["timeframe"] == "1d"
            assert kwargs["limit"] == 250
            return bars

    class FakeIndicatorFrameRepository:
        def upsert_indicator_frame(self, **kwargs: Any) -> Any:
            upsert_calls.append(kwargs)
            return SimpleNamespace(**kwargs)

    service = StructuralMethodologyRefreshService(
        object(),
        universe_repository=FakeUniverseRepository(),
        market_data=FakeMarketDataRepository(),
        indicators=FakeIndicatorFrameRepository(),
    )
    result = service.refresh(
        market="ashare",
        timeframe="1d",
        engines=["ichimoku"],
        universe_ids=["universe:technical:ashare:main_board"],
        lookback_bars=250,
    )
    return result, upsert_calls


def _market_bar(index: int, *, asset: Any) -> Any:
    timestamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)
    close = Decimal("100") + Decimal(index)
    return SimpleNamespace(
        asset_id=asset.asset_id,
        symbol=asset.symbol,
        market=asset.market,
        timeframe="1d",
        timestamp=timestamp,
        open=close,
        high=close + Decimal("1"),
        low=close - Decimal("1"),
        close=close,
        volume=Decimal("1000000"),
        amount=Decimal("100000000"),
        source="fixture",
        adjustment="qfq",
        is_closed=True,
        status="available",
    )


def test_structural_candidates_are_prioritized_before_limit() -> None:
    from finance_agent.application.structural_methodology_service import (
        StructuralMethodologyRefreshService,
    )

    members = [
        SimpleNamespace(asset_id="ashare:000005", market="ashare", payload={}),
        SimpleNamespace(
            asset_id="ashare:000004",
            market="ashare",
            payload={"previous_recommendation_rank": 10},
        ),
        SimpleNamespace(
            asset_id="ashare:000003",
            market="ashare",
            payload={"sector_role": "leader"},
        ),
        SimpleNamespace(
            asset_id="ashare:000002",
            market="ashare",
            payload={"recommendation_state": "active"},
        ),
        SimpleNamespace(
            asset_id="ashare:000001",
            market="ashare",
            payload={"held": True},
        ),
    ]

    class _Universes:
        def list_members(self, _universe_id: str, *, included_only: bool) -> list[Any]:
            assert included_only is True
            return members

    service = StructuralMethodologyRefreshService(
        None,
        universe_repository=_Universes(),
    )

    selected = service.list_candidate_assets(
        market="ashare",
        universe_ids=["universe:merged:ashare:recommendation"],
        limit=4,
    )

    assert [item.asset_id for item in selected] == [
        "ashare:000001",
        "ashare:000002",
        "ashare:000003",
        "ashare:000004",
    ]
