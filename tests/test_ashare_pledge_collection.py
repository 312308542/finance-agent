from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from finance_agent.application.data_production_service import AvoidPoolPolicy
from finance_agent.data import normalizers
from finance_agent.data.collectors import AshareRiskSentimentCollector
from finance_agent.data.models import RiskFindingsResult
from finance_agent.data.providers import akshare_risk_sentiment_provider


class _RawRecords:
    def insert_raw_record(self, **_: Any) -> Any:
        return type("RawRecord", (), {"raw_record_id": "raw:pledge"})()


def test_normalize_ashare_pledge_ratio_outputs_high_risk_for_main_board() -> None:
    """股权质押比例超过阈值时，应生成主板标的风险并过滤非可交易主板。"""

    collected_at = datetime(2026, 6, 12, tzinfo=UTC)
    df = pd.DataFrame(
        [
            {
                "股票代码": "600519",
                "股票简称": "贵州茅台",
                "质押比例": "35.2",
                "质押笔数": "12",
                "质押市值": "1234567890",
                "统计日期": "2026-06-11",
            },
            {
                "股票代码": "300750",
                "股票简称": "宁德时代",
                "质押比例": "41.0",
                "质押笔数": "8",
                "质押市值": "2234567890",
                "统计日期": "2026-06-11",
            },
            {
                "股票代码": "601398",
                "股票简称": "工商银行",
                "质押比例": "4.0",
                "质押笔数": "1",
                "质押市值": "1000000",
                "统计日期": "2026-06-11",
            },
        ]
    )

    assert hasattr(normalizers, "normalize_ashare_pledge_ratio")
    risks = normalizers.normalize_ashare_pledge_ratio(
        df,
        source="akshare:stock_gpzy_pledge_ratio_em",
        collected_at=collected_at,
        risk_ratio_threshold=Decimal("0.30"),
    )

    assert len(risks) == 1
    assert risks[0].asset_id == "ashare:600519"
    assert risks[0].risk_type == "pledge_ratio"
    assert risks[0].severity == "high"
    assert risks[0].score == Decimal("0.704")
    assert risks[0].payload["pledge_ratio"] == "0.352"
    assert risks[0].payload["pledge_count"] == "12"


def test_ashare_risk_collector_collects_pledge_ratio_in_batch() -> None:
    """股权质押采集应批量写入风险发现，不走逐条 upsert。"""

    calls: dict[str, list[list[dict[str, Any]]]] = {"risks": []}

    class EventRepo:
        def upsert_events(self, rows: list[dict[str, Any]]) -> int:
            return len(rows)

        def upsert_evidence_items(self, rows: list[dict[str, Any]]) -> int:
            return len(rows)

    class RiskRepo:
        def upsert_risk_findings(self, rows: list[dict[str, Any]]) -> int:
            calls["risks"].append(rows)
            return len(rows)

        def upsert_risk_finding(self, **_: Any) -> None:
            raise AssertionError("股权质押风险不应逐条写入")

    class Provider:
        def fetch_pledge_ratio(
            self,
            *,
            date: str | None = None,
            limit: int | None = None,
            risk_ratio_threshold: Decimal = Decimal("0.30"),
        ) -> RiskFindingsResult:
            as_of = datetime(2026, 6, 12, tzinfo=UTC)
            risk = normalizers.RiskFindingData(
                risk_id="risk:pledge:600519",
                asset_id="ashare:600519",
                scope="asset",
                risk_type="pledge_ratio",
                severity="high",
                score=Decimal("0.704"),
                title="贵州茅台(600519) 股权质押比例偏高",
                description="测试",
                as_of=as_of,
                payload={"pledge_ratio": "0.352"},
            )
            return RiskFindingsResult(
                provider_name="akshare",
                status="available",
                collected_at=as_of,
                risks=[risk],
                payload={"endpoint": "stock_gpzy_pledge_ratio_em"},
            )

    collector = AshareRiskSentimentCollector.__new__(AshareRiskSentimentCollector)
    collector.events = EventRepo()
    collector.risks = RiskRepo()
    collector.raw_records = _RawRecords()
    collector.risk_provider = Provider()

    result = collector.collect_pledge_ratio(limit=5, risk_ratio_threshold=Decimal("0.30"))

    assert result.raw_record_id == "raw:pledge"
    assert calls["risks"][0][0]["risk_type"] == "pledge_ratio"
    assert calls["risks"][0][0]["payload"]["pledge_ratio"] == "0.352"


def test_avoid_pool_policy_blocks_high_pledge_ratio() -> None:
    """高质押比例风险应进入回避池，供推荐入口剔除。"""

    as_of = datetime(2026, 6, 12, tzinfo=UTC)
    plans = AvoidPoolPolicy().build_avoid_members(
        universe_id="universe:avoid:ashare:system",
        market="ashare",
        assets=[
            {
                "asset_id": "ashare:600519",
                "symbol": "600519",
                "name": "贵州茅台",
                "status": "active",
                "tradable": True,
            }
        ],
        risks=[
            {
                "asset_id": "ashare:600519",
                "risk_type": "pledge_ratio",
                "severity": "medium",
                "title": "贵州茅台(600519) 股权质押比例偏高",
            }
        ],
        as_of=as_of,
    )

    assert len(plans) == 1
    assert plans[0].included is False
    assert "股权质押比例偏高" in (plans[0].removed_reason or "")


def test_pledge_ratio_provider_falls_back_to_latest_available_source_date(
    monkeypatch: Any,
) -> None:
    """指定日期无质押数据时，应降级到源端最近可用交易日。"""

    calls: list[str] = []

    def fake_pledge_ratio(date: str) -> pd.DataFrame:
        calls.append(date)
        if date == "20260612":
            raise TypeError("'NoneType' object is not subscriptable")
        return pd.DataFrame(
            [
                {
                    "股票代码": "600519",
                    "股票简称": "贵州茅台",
                    "交易日期": "2024-09-06",
                    "质押比例": "35.2",
                    "质押笔数": "12",
                    "质押市值": "1234567890",
                }
            ]
        )

    monkeypatch.setattr(
        akshare_risk_sentiment_provider.ak,
        "stock_gpzy_pledge_ratio_em",
        fake_pledge_ratio,
    )
    monkeypatch.setattr(
        akshare_risk_sentiment_provider.ak,
        "stock_gpzy_profile_em",
        lambda: pd.DataFrame(
            [
                {"交易日期": "2024-09-06", "A股质押总比例": 0.05},
                {"交易日期": "2024-08-30", "A股质押总比例": 0.04},
            ]
        ),
    )

    provider = akshare_risk_sentiment_provider.AshareRiskProvider()
    result = provider.fetch_pledge_ratio(
        date="20260612",
        limit=1,
        risk_ratio_threshold=Decimal("0.30"),
    )

    assert result.status == "available"
    assert calls == ["20260612", "20240906"]
    assert result.payload["requested_date"] == "20260612"
    assert result.payload["date"] == "20240906"
    assert result.payload["fallback_used"] is True
