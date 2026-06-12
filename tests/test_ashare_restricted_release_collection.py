from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from finance_agent.data import normalizers
from finance_agent.data.models import RiskFindingsResult
from finance_agent.data.collectors import AshareRiskSentimentCollector


class _RawRecords:
    def insert_raw_record(self, **_: Any) -> Any:
        return type("RawRecord", (), {"raw_record_id": "raw:restricted_release"})()


def test_normalize_ashare_restricted_release_outputs_event_and_near_term_risk() -> None:
    """限售解禁详情应落事件，并对临近高占比解禁生成风险。"""

    collected_at = datetime(2026, 6, 12, tzinfo=UTC)
    df = pd.DataFrame(
        [
            {
                "股票代码": "600519",
                "股票简称": "贵州茅台",
                "解禁时间": "2026-06-20",
                "限售股类型": "首发原股东限售股份",
                "实际解禁数量": "1000000",
                "实际解禁市值": "1200000000",
                "占解禁前流通市值比例": "0.08",
            },
            {
                "股票代码": "300750",
                "股票简称": "宁德时代",
                "解禁时间": "2026-06-20",
                "限售股类型": "首发原股东限售股份",
                "实际解禁数量": "1000000",
                "实际解禁市值": "1200000000",
                "占解禁前流通市值比例": "0.08",
            },
        ]
    )

    assert hasattr(normalizers, "normalize_ashare_restricted_release_detail")
    risks, events = normalizers.normalize_ashare_restricted_release_detail(
        df,
        source="akshare:stock_restricted_release_detail_em",
        collected_at=collected_at,
        risk_window_days=30,
        risk_ratio_threshold=Decimal("0.05"),
    )

    assert len(events) == 1
    assert events[0].event_type == "restricted_release"
    assert events[0].asset_id == "ashare:600519"
    assert events[0].sentiment == "negative"
    assert events[0].importance == "high"
    assert events[0].payload["release_ratio"] == "0.08"
    assert len(risks) == 1
    assert risks[0].risk_type == "restricted_release"
    assert risks[0].severity == "high"
    assert risks[0].score == Decimal("0.8")
    assert risks[0].evidence_ids == [events[0].event_id]


def test_ashare_risk_collector_collects_restricted_release_in_batch() -> None:
    """限售解禁采集应批量写入事件和风险，不走逐条 upsert。"""

    calls: dict[str, list[list[dict[str, Any]]]] = {"events": [], "risks": []}

    class EventRepo:
        def upsert_events(self, rows: list[dict[str, Any]]) -> int:
            calls["events"].append(rows)
            return len(rows)

        def upsert_event(self, **_: Any) -> None:
            raise AssertionError("限售解禁事件不应逐条写入")

        def upsert_evidence_items(self, rows: list[dict[str, Any]]) -> int:
            return len(rows)

    class RiskRepo:
        def upsert_risk_findings(self, rows: list[dict[str, Any]]) -> int:
            calls["risks"].append(rows)
            return len(rows)

        def upsert_risk_finding(self, **_: Any) -> None:
            raise AssertionError("限售解禁风险不应逐条写入")

    class Provider:
        def fetch_restricted_release(
            self,
            *,
            start_date: str,
            end_date: str,
            limit: int | None = None,
            risk_window_days: int = 30,
            risk_ratio_threshold: Decimal = Decimal("0.05"),
        ) -> RiskFindingsResult:
            as_of = datetime(2026, 6, 12, tzinfo=UTC)
            event = normalizers.EventRecordData(
                event_id="event:restricted:600519",
                asset_id="ashare:600519",
                symbol="600519",
                market="ashare",
                event_type="restricted_release",
                title="贵州茅台(600519) 限售股解禁",
                summary="测试",
                sentiment="negative",
                importance="high",
                source="akshare:stock_restricted_release_detail_em",
                published_at=as_of,
                collected_at=as_of,
                payload={},
            )
            risk = normalizers.RiskFindingData(
                risk_id="risk:restricted:600519",
                asset_id="ashare:600519",
                scope="asset",
                risk_type="restricted_release",
                severity="high",
                score=Decimal("0.8"),
                title="贵州茅台(600519) 临近限售股解禁",
                description="测试",
                as_of=as_of,
                evidence_ids=[event.event_id],
                payload={},
            )
            return RiskFindingsResult(
                provider_name="akshare",
                status="available",
                collected_at=as_of,
                risks=[risk],
                events=[event],
                payload={"endpoint": "stock_restricted_release_detail_em"},
            )

    collector = AshareRiskSentimentCollector.__new__(AshareRiskSentimentCollector)
    collector.events = EventRepo()
    collector.risks = RiskRepo()
    collector.raw_records = _RawRecords()
    collector.risk_provider = Provider()

    result = collector.collect_restricted_release(
        start_date="20260601",
        end_date="20260630",
        limit=5,
        risk_window_days=30,
        risk_ratio_threshold=Decimal("0.05"),
    )

    assert result.raw_record_id == "raw:restricted_release"
    assert calls["events"][0][0]["event_id"] == "event:restricted:600519"
    assert calls["risks"][0][0]["risk_type"] == "restricted_release"
