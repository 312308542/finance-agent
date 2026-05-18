"""验证金融事实工具可以读取指标、因子、评分和证据上下文。

圆桌 Workflow 的角色不能直接调用 AKShare、TA-Lib 或外部网页，只能通过工具层
读取已经清洗入库的数据。本脚本先锁定这个工具协议。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from finance_agent.agents.tools import FinanceToolRuntime
from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.repositories import (
    AssetScoreRepository,
    EventRepository,
    FactorFrameRepository,
    IndicatorFrameRepository,
)


def main() -> None:
    """执行指标和因子上下文工具冒烟。"""

    session_factory = create_session_factory()
    as_of = datetime(2026, 5, 18, 14, 0, tzinfo=UTC)
    asset_id = "asset:smoke:roundtable:factor"
    symbol = "RT001"
    factor_frame_id = f"factor:{asset_id}:swing:roundtable"
    indicator_frame_id = f"indicator:{asset_id}:1d:swing:roundtable"
    score_id = f"score:{asset_id}:roundtable"
    evidence_id = f"evidence:{asset_id}:roundtable"

    with session_scope(session_factory) as session:
        indicators = IndicatorFrameRepository(session)
        factors = FactorFrameRepository(session)
        scores = AssetScoreRepository(session)
        events = EventRepository(session)

        indicators.upsert_indicator_frame(
            indicator_frame_id=indicator_frame_id,
            asset_id=asset_id,
            symbol=symbol,
            market="ashare",
            timeframe="1d",
            horizon="swing",
            library="TA-Lib",
            library_version="0.6.8",
            input_start_at=as_of - timedelta(days=60),
            input_end_at=as_of,
            bar_count=60,
            rsi_14=Decimal("66.500000"),
            macd=Decimal("1.280000"),
            macd_signal=Decimal("0.920000"),
            macd_hist=Decimal("0.360000"),
            atr_14=Decimal("2.100000"),
            bb_percent_b=Decimal("0.780000"),
            ma_20=Decimal("31.200000"),
            ma_60=Decimal("28.900000"),
            status="available",
            as_of=as_of,
            payload={"source": "smoke_finance_tool_factor_context"},
        )
        factors.upsert_factor_frame(
            factor_frame_id=factor_frame_id,
            asset_id=asset_id,
            symbol=symbol,
            market="ashare",
            horizon="swing",
            status="available",
            total_available_groups=5,
            missing_groups=["valuation"],
            source_ids=[indicator_frame_id, "akshare:fund_flow:roundtable"],
            indicator_frame_id=indicator_frame_id,
            as_of=as_of,
            payload={
                "source": "smoke_finance_tool_factor_context",
                "factor_groups": {
                    "technical": {"score": "82.0", "source": "TA-Lib"},
                    "flow": {"score": "76.0", "source": "AKShare"},
                    "fundamental": {"score": "68.0", "source": "AKShare"},
                },
            },
        )
        scores.upsert_asset_score(
            score_id=score_id,
            asset_id=asset_id,
            symbol=symbol,
            market="ashare",
            universe_id="universe:smoke:roundtable",
            screening_id="screening:smoke:roundtable",
            factor_frame_id=factor_frame_id,
            horizon="swing",
            total_score=Decimal("81.500000"),
            rank=1,
            confidence=Decimal("0.790000"),
            rule_version="smoke_roundtable",
            status="available",
            as_of=as_of,
            risk_penalty=Decimal("3.000000"),
            missing_penalty=Decimal("2.000000"),
            technical_score=Decimal("82.000000"),
            fundamental_score=Decimal("68.000000"),
            valuation_score=None,
            flow_score=Decimal("76.000000"),
            derivatives_score=None,
            event_score=Decimal("72.000000"),
            rank_in_universe=1,
            payload={"source": "smoke_finance_tool_factor_context"},
        )
        events.upsert_evidence(
            evidence_id=evidence_id,
            evidence_type="fund_flow",
            asset_id=asset_id,
            source="AKShare",
            title="主力资金连续流入",
            summary="AKShare 资金流数据提示近 5 日主力资金净流入。",
            data_ref="capital_flow_snapshots",
            reliability="medium",
            as_of=as_of,
            collected_at=as_of,
            payload={"source": "smoke_finance_tool_factor_context"},
        )

        runtime = FinanceToolRuntime(session)
        if "factor.get_asset_factor_context" not in set(runtime.list_tools()):
            raise AssertionError("缺少 factor.get_asset_factor_context 工具。")
        context = runtime.call(
            "factor.get_asset_factor_context",
            asset_id=asset_id,
            horizon="swing",
            timeframe="1d",
            evidence_limit=3,
        )

        if context["indicator_frame"]["indicator_frame_id"] != indicator_frame_id:
            raise AssertionError("工具必须返回最新 TA 指标快照。")
        if context["factor_frame"]["factor_frame_id"] != factor_frame_id:
            raise AssertionError("工具必须返回最新因子快照。")
        if context["score"]["score_id"] != score_id:
            raise AssertionError("工具必须返回最新评分。")
        if context["evidence"][0]["evidence_id"] != evidence_id:
            raise AssertionError("工具必须返回 AKShare 等数据源形成的证据。")

        print(
            {
                "asset_id": asset_id,
                "indicator": context["indicator_frame"]["indicator_frame_id"],
                "factor": context["factor_frame"]["factor_frame_id"],
                "score": context["score"]["score_id"],
                "evidence_count": len(context["evidence"]),
            }
        )


if __name__ == "__main__":
    main()
