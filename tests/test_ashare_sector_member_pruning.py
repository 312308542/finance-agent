from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from finance_agent.data.collectors import AshareP1Collector
from finance_agent.data.models import UniverseSeedData, UniverseSeedsResult


@pytest.mark.parametrize(
    ("method_name", "name_argument", "source_type"),
    [
        ("collect_industry_members", "industry_name", "industry"),
        ("collect_concept_members", "concept_name", "concept"),
    ],
)
def test_complete_sector_snapshot_prunes_stale_members(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    name_argument: str,
    source_type: str,
) -> None:
    """行业和概念完整快照写入后，应显式排除本轮缺席的旧成员。"""

    collected_at = datetime(2026, 7, 16, tzinfo=UTC)
    result = _seed_result(
        source_type=source_type,
        collected_at=collected_at,
        source_coverage="full",
    )
    universes = _RecordingUniverseRepository()
    collector = _collector(monkeypatch, result=result, universes=universes)

    getattr(collector, method_name)(
        **{
            name_argument: "测试板块",
            "universe_id": f"universe:test:{source_type}",
            "universe_name": "测试板块池",
            "strategy_context": "base_data_collect",
            "limit": None,
        }
    )

    assert universes.prune_calls == [
        {
            "universe_id": f"universe:test:{source_type}",
            "current_asset_ids": ["ashare:600000"],
            "as_of": collected_at,
            "removed_reason": "not_in_latest_sector_snapshot",
        }
    ]


@pytest.mark.parametrize(
    ("source_coverage", "limit"),
    [("first_page", None), ("limited", 10), ("full", 10)],
)
def test_partial_sector_snapshot_never_prunes_members(
    monkeypatch: pytest.MonkeyPatch,
    source_coverage: str,
    limit: int | None,
) -> None:
    """首屏降级或人工限量结果不得把未返回成员误标为退出板块。"""

    result = _seed_result(
        source_type="concept",
        collected_at=datetime(2026, 7, 16, tzinfo=UTC),
        source_coverage=source_coverage,
    )
    universes = _RecordingUniverseRepository()
    collector = _collector(monkeypatch, result=result, universes=universes)

    collector.collect_concept_members(
        concept_name="测试概念",
        universe_id="universe:test:concept",
        universe_name="测试概念池",
        strategy_context="base_data_collect",
        limit=limit,
    )

    assert universes.prune_calls == []


def _collector(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: UniverseSeedsResult,
    universes: "_RecordingUniverseRepository",
) -> AshareP1Collector:
    monkeypatch.setattr(
        "finance_agent.data.collectors.archive_provider_result",
        lambda *args, **kwargs: "raw:test:sector",
    )
    monkeypatch.setattr(
        "finance_agent.data.collectors._persist_seed_identity_rows",
        lambda *args, **kwargs: None,
    )
    collector = AshareP1Collector.__new__(AshareP1Collector)
    collector.raw_records = object()
    collector.assets = object()
    collector.universes = universes
    collector.sector_provider = SimpleNamespace(
        fetch_industry_members=lambda **kwargs: result,
        fetch_concept_members=lambda **kwargs: result,
    )
    return collector


def _seed_result(
    *,
    source_type: str,
    collected_at: datetime,
    source_coverage: str,
) -> UniverseSeedsResult:
    return UniverseSeedsResult(
        provider_name="test",
        status="available",
        collected_at=collected_at,
        seeds=[
            UniverseSeedData(
                seed_id=f"seed:{source_type}:600000",
                source_name="测试板块",
                source_type=source_type,
                symbol="600000",
                name="浦发银行",
                market="ashare",
                asset_id="ashare:600000",
                as_of=collected_at,
            )
        ],
        payload={"source_coverage": source_coverage},
    )


class _RecordingUniverseRepository:
    def __init__(self) -> None:
        self.prune_calls: list[dict[str, object]] = []

    def upsert_universe(self, **kwargs: object) -> None:
        return None

    def replace_members(self, **kwargs: object) -> list[object]:
        return []

    def prune_missing_members(self, **kwargs: object) -> int:
        self.prune_calls.append(dict(kwargs))
        return 0
