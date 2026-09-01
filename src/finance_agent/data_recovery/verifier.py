"""补跑事实验收器。

规格 11.3 / 12.2：任务成功不等于目标完成。采集任务返回成功、空结果或
已处理数量都不能替代事实覆盖验证；本模块在任务结束后重新读取事实表，
逐目标判定完成、例外或继续重试。放行依据复用现有健康检查与
recommendation_readiness 结论，不另造质量标准。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

from finance_agent.data.freshness import (
    ashare_daily_snapshot_at,
    expected_ashare_report_period,
)
from finance_agent.data_recovery.gap_detector import (
    FUNDAMENTAL_REPORT_SOURCES,
    DomainGapQueries,
    report_period_to_date,
)
from finance_agent.data_recovery.models import (
    DataDomain,
)

JsonDict = dict[str, Any]

#: 报告期缺失时的最小占位日期。
_DATE_MIN = date.min


class RecoveryVerifier:
    """按数据域语义对目标区间做事实验证。"""

    def __init__(self, session, *, lifecycle_fn=None, market: str = "ashare") -> None:
        self.session = session
        self.queries = DomainGapQueries(session)
        # 规格 8：注入资产生命周期窗口（停牌/未上市/退市日不构成缺口）。
        self.lifecycle_fn = lifecycle_fn
        self.market = market

    def _calendar_trading_dates(self, start_date, end_date) -> list:
        """读取市场日历内的真实交易日（剔除周末/休市）。"""

        from sqlalchemy import select

        from finance_agent.storage.orm import MarketCalendarORM

        rows = self.session.execute(
            select(MarketCalendarORM).where(
                MarketCalendarORM.market == self.market,
                MarketCalendarORM.trade_date >= start_date,
                MarketCalendarORM.trade_date <= end_date,
            )
        ).scalars()
        return [
            row.trade_date
            for row in rows
            if row.is_trading_day and str(row.status or "") != "invalid"
        ]

    def verify_target(self, target_row) -> tuple[str, str | None, JsonDict]:
        """重新读取事实表判定单个目标。

        返回 (status, exception_code|None, evidence)；status ∈
        {completed, failed_transient, exception_allowed, exception_blocking}。
        空事实不能直接解释为停牌或无事件（规格 8）：K 线空缺默认继续重试，
        只有调用方注入的生命周期/第二 Provider 证据才能转为例外。
        """

        domain = str(target_row.data_domain)
        start = target_row.gap_start_at
        end = target_row.gap_end_at
        asset_id = target_row.asset_id
        if domain == DataDomain.MARKET_BARS:
            covered = self.queries.bar_coverage_by_asset(
                asset_ids=[asset_id],
                start_at=start,
                end_at=end + timedelta(days=1),
                timeframe=str(target_row.granularity or "1d"),
            ).get(asset_id, set())
            required = _dates_between(start.date(), end.date())
            if self.lifecycle_fn is not None:
                windows = (
                    self.lifecycle_fn(
                        asset_ids=[str(asset_id)],
                        window_start=start.date(),
                        window_end=end.date(),
                    )
                    or {}
                )
                window = windows.get(str(asset_id))
                if window is not None:
                    # 停牌、上市前、退市后的日期不构成 K 线缺口。
                    # required_dates(trading_dates)：以日历交易日为基做集合过滤。
                    required = tuple(
                        window.required_dates(
                            self._calendar_trading_dates(start.date(), end.date())
                        )
                    )
            missing = sorted(set(required) - covered)
            if not missing:
                return "completed", None, {"verified_days": len(required)}
            evidence = {
                "missing_dates": [d.isoformat() for d in missing[:31]],
                "missing_count": len(missing),
            }
            return "failed_transient", None, evidence
        if domain == DataDomain.FUNDAMENTALS:
            # H2：目标必须携带资产标识，否则 IN (NULL) 永远无法核验（规格 11.3）。
            if not asset_id:
                return (
                    "failed_transient",
                    None,
                    {"error": "fundamental_target_without_asset_scope"},
                )
            expected_period = report_period_to_date(
                (target_row.exception_evidence or {}).get("expected_report_period")
            )
            if expected_period is None and end is not None:
                # 证据缺失时按窗口终点重算依法应有的报告期。
                expected_period = expected_ashare_report_period(end)
            latest = self.session.execute(
                _latest_report_period_statement([asset_id])
            ).scalar_one_or_none()
            have = report_period_to_date(latest)
            if expected_period is not None and (have or _DATE_MIN) >= expected_period:
                return "completed", None, {"verified_report_period": str(have)}
            return (
                "failed_transient",
                None,
                {
                    "asset_id": str(asset_id),
                    "expected_report_period": (
                        expected_period.isoformat() if expected_period else None
                    ),
                    "latest_report_period": str(have),
                },
            )
        if domain == DataDomain.VALUATION:
            # H3：门槛用计划要求的窗口起点，不用目标行携带的旧记录时间；
            # 缺记录资产同样能被 detect_valuation_gap_targets 判为未补齐。
            if not asset_id:
                return (
                    "failed_transient",
                    None,
                    {"error": "valuation_target_without_asset_scope"},
                )
            required = _required_valuation_threshold(target_row, end)
            latest = self.queries.detect_valuation_gap_targets(
                asset_ids=[asset_id],
                required_as_of=required,
                cutoff_at=end,
            )
            if not latest:
                return "completed", None, {"required_as_of": required.isoformat()}
            return "failed_transient", None, {"valuation_stale": True, "required_as_of": required.isoformat()}
        if domain == DataDomain.CAPITAL_FLOW:
            targets = self.queries.detect_capital_flow_gap_targets(
                asset_ids=[asset_id], trading_dates=_dates_between(start.date(), end.date())
            )
            if not targets:
                return "completed", None, {}
            return (
                "failed_transient",
                None,
                {"missing_days": sum(t.expected_count for t in targets)},
            )
        # 事件/风险等窗口域：水位达到窗口终点即视为覆盖。
        watermark = self.queries.domain_watermark_latest(data_domain=domain)
        if watermark is not None and watermark.astimezone() >= end - timedelta(minutes=5):
            return "completed", None, {"watermark_latest": watermark.isoformat()}
        return "failed_transient", None, {"watermark_latest": watermark.isoformat() if watermark else None}



    def final_gate_check(
        self,
        *,
        run_id: str,
        cutoff_date: Any,
        readiness_fn: Callable[[], JsonDict] | None = None,
    ) -> JsonDict:
        """最终验收（规格 15）：核心目标全部解决且 readiness 可执行。

        readiness_fn 由调用方注入（默认实现读取最新推荐运行并复用
        evaluate_recommendation_readiness），本模块不复制质量规则。
        """

        from sqlalchemy import func, select

        from finance_agent.data_recovery.models import BLOCKING_EXCEPTION_CODES
        from finance_agent.storage.orm import DataRecoveryTargetORM

        counts = self.queries and _target_counts(self.session, run_id)
        blocking_exceptions = int(
            self.session.execute(
                select(func.count(DataRecoveryTargetORM.target_id)).where(
                    DataRecoveryTargetORM.run_id == run_id,
                    DataRecoveryTargetORM.status == "exception",
                    DataRecoveryTargetORM.exception_code.in_(BLOCKING_EXCEPTION_CODES),
                )
            ).scalar_one()
        )
        unresolved = int(counts.get("pending", 0)) + int(counts.get("failed", 0))
        reasons: list[str] = []
        if unresolved:
            reasons.append("unresolved_core_targets")
        if blocking_exceptions:
            reasons.append("blocking_exception_targets")
        readiness: JsonDict = {"executable": False, "reasons": ["not_evaluated"]}
        if not reasons and readiness_fn is not None:
            readiness = readiness_fn() or {}
            if not readiness.get("executable"):
                reasons.append("recommendation_not_executable")
        return {
            "executable": not reasons,
            "reasons": reasons,
            "target_counts": dict(counts),
            "readiness": readiness,
            "checked_at": datetime.now().astimezone().isoformat(),
        }

    def final_data_gate_check(
        self,
        *,
        run_id: str,
    ) -> JsonDict:
        """P6 数据层验收：只检查核心目标是否全部解决，不要求推荐链路已就绪。

        推荐就绪度应在 P7 派生重建之后由 final_gate_check 复核，否则本批次
        会在尚未执行派生刷新时因 empty_recommendations 错误停在 P6。
        """

        from sqlalchemy import func, select

        from finance_agent.data_recovery.models import BLOCKING_EXCEPTION_CODES
        from finance_agent.storage.orm import DataRecoveryTargetORM

        counts = self.queries and _target_counts(self.session, run_id)
        blocking_exceptions = int(
            self.session.execute(
                select(func.count(DataRecoveryTargetORM.target_id)).where(
                    DataRecoveryTargetORM.run_id == run_id,
                    DataRecoveryTargetORM.status == "exception",
                    DataRecoveryTargetORM.exception_code.in_(BLOCKING_EXCEPTION_CODES),
                )
            ).scalar_one()
        )
        unresolved = int(counts.get("pending", 0)) + int(counts.get("failed", 0))
        reasons: list[str] = []
        if unresolved:
            reasons.append("unresolved_core_targets")
        if blocking_exceptions:
            reasons.append("blocking_exception_targets")
        return {
            "executable": not reasons,
            "reasons": reasons,
            "target_counts": dict(counts),
            "readiness": {"executable": True, "reasons": []},
            "checked_at": datetime.now().astimezone().isoformat(),
        }


def _required_valuation_threshold(target_row, window_end):
    """估值补齐门槛：计划要求的窗口起点，而非目标携带的旧记录时间（H3）。"""

    raw = (getattr(target_row, 'exception_evidence', None) or {}).get(
        "required_as_of"
    )
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    # 兼容无证据的历史目标：窗口终点（cutoff）前一个自然日。
    end = window_end or datetime.now().astimezone()
    return ashare_daily_snapshot_at(end) - timedelta(days=1)


def _dates_between(start_date, end_date):
    from datetime import timedelta as td

    days = (end_date - start_date).days
    return [start_date + td(days=offset) for offset in range(max(0, days + 1))]


def _latest_report_period_statement(asset_ids):
    from sqlalchemy import func, select

    from finance_agent.storage.orm import FundamentalSnapshotORM

    # 复审 MEDIUM：与检测同口径，只认权威财务指标来源的报告期，
    # 防止业绩报表等来源提前让目标完成。
    return (
        select(func.max(FundamentalSnapshotORM.report_period))
        .where(
            FundamentalSnapshotORM.asset_id.in_(list(asset_ids)),
            FundamentalSnapshotORM.report_period.is_not(None),
            FundamentalSnapshotORM.source.in_(FUNDAMENTAL_REPORT_SOURCES),
        )
    )


def _target_counts(session, run_id):
    from sqlalchemy import func, select

    from finance_agent.storage.orm import DataRecoveryTargetORM

    rows = session.execute(
        select(DataRecoveryTargetORM.status, func.count(DataRecoveryTargetORM.target_id))
        .where(DataRecoveryTargetORM.run_id == run_id)
        .group_by(DataRecoveryTargetORM.status)
    ).all()
    return {status: int(count) for status, count in rows}
