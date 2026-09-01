"""补跑计划构建器。

把 GapDetector 的检测结果组装为带冻结范围、步骤依赖、目标区间、
预计请求量和计划哈希的 `RecoveryPlan`（规格 4.2 职责 2 / 7.1 步骤表）。
计划本身不执行任何采集，也不写入业务事实表。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, time, timedelta

from finance_agent.data_recovery.gap_detector import (
    ASHARE_TIMEZONE,
    CutoffResolution,
    DomainGapQueries,
)
from finance_agent.data_recovery.models import (
    RECOVERY_STRATEGY_VERSION,
    DataDomain,
    PlanStep,
    RecoveryPlan,
    UniverseSnapshot,
)
from finance_agent.data_recovery.repository import plan_fingerprint


class PlanBuilder:
    """生成 P0～P8 步骤依赖的补跑计划草稿。"""

    def __init__(self, queries: DomainGapQueries) -> None:
        self.queries = queries

    def build_plan(
        self,
        *,
        market: str,
        cutoff: CutoffResolution,
        universe: UniverseSnapshot,
        trading_dates: Sequence,
        now: datetime,
        max_retry_rounds: int = 3,
    ) -> RecoveryPlan:
        """按规格 7.1 的阶段表生成完整步骤列表。"""

        cutoff_date = cutoff.cutoff_date
        assert cutoff_date is not None
        # 复审 MEDIUM：显式绑定上海时区，冻结截止时刻不随宿主机本地时区漂移，
        # 保证跨入口运行的 plan_hash 与估值阈值确定性（规格 6.1）。
        cutoff_at = datetime.combine(cutoff_date, time.max, tzinfo=ASHARE_TIMEZONE)
        asset_ids = list(universe.asset_ids)

        bar_targets = self.queries.detect_bar_gap_targets(
            asset_ids=asset_ids, trading_dates=trading_dates
        )
        fundamental_targets = self.queries.detect_fundamental_gap_targets(
            asset_ids=asset_ids, cutoff_at=cutoff_at
        )
        valuation_targets = self.queries.detect_valuation_gap_targets(
            asset_ids=asset_ids,
            required_as_of=cutoff_at - timedelta(days=1),
            cutoff_at=cutoff_at,
        )
        capital_flow_targets = self.queries.detect_capital_flow_gap_targets(
            asset_ids=asset_ids, trading_dates=trading_dates
        )
        window_days = _window_days(trading_dates, default=3)
        event_target = self.queries.detect_window_gap_target(
            data_domain=DataDomain.EVENTS,
            gap_start_at=cutoff_at - timedelta(days=window_days),
            cutoff_at=cutoff_at,
            watermark_latest=self.queries.domain_watermark_latest(
                data_domain=DataDomain.EVENTS
            ),
            stale_tolerance=timedelta(hours=48),
        )
        risk_target = self.queries.detect_window_gap_target(
            data_domain=DataDomain.RISK_SENTIMENT,
            gap_start_at=cutoff_at - timedelta(days=window_days),
            cutoff_at=cutoff_at,
            watermark_latest=self.queries.domain_watermark_latest(
                data_domain=DataDomain.RISK_SENTIMENT
            ),
            stale_tolerance=timedelta(days=1),
        )

        fact_domains = [
            (DataDomain.FUNDAMENTALS, fundamental_targets),
            (DataDomain.VALUATION, valuation_targets),
            (DataDomain.CAPITAL_FLOW, capital_flow_targets),
            (DataDomain.EVENTS, [event_target] if event_target else []),
            (DataDomain.RISK_SENTIMENT, [risk_target] if risk_target else []),
        ]

        all_targets = [
            *bar_targets,
            *[target for _, targets in fact_domains for target in targets],
        ]
        gap_start_date = min(
            (target.gap_start_at.date() for target in all_targets),
            default=None,
        )

        steps = [
            PlanStep(phase="P0", data_domain=DataDomain.ORCHESTRATION),
            PlanStep(
                phase="P1",
                data_domain=DataDomain.MARKET_CALENDAR,
                depends_on_phases=("P0",),
                task_params={"persist_universe": True},
            ),
            PlanStep(
                phase="P2",
                data_domain=DataDomain.ORCHESTRATION,
                depends_on_phases=("P1",),
                task_params={"materialize_targets": True},
            ),
            PlanStep(
                phase="P3",
                data_domain=DataDomain.MARKET_BARS,
                depends_on_phases=("P2",),
                targets=tuple(bar_targets),
                estimated_requests=sum(max(1, t.expected_count) for t in bar_targets),
                parallelizable=True,
            ),
        ]
        for domain, targets in fact_domains:
            steps.append(
                PlanStep(
                    phase="P4",
                    data_domain=domain,
                    depends_on_phases=("P2", "P3"),
                    targets=tuple(targets),
                    estimated_requests=sum(
                        max(1, t.expected_count) if t.asset_id else 1 for t in targets
                    ),
                    parallelizable=True,
                )
            )
        steps.extend(
            [
                PlanStep(
                    phase="P5",
                    data_domain=DataDomain.ORCHESTRATION,
                    depends_on_phases=("P3", "P4"),
                    task_params={
                        "residual_recheck": True,
                        "max_retry_rounds": int(max_retry_rounds),
                    },
                ),
                PlanStep(
                    phase="P6",
                    data_domain=DataDomain.ORCHESTRATION,
                    depends_on_phases=("P5",),
                    task_params={"quality_acceptance": True},
                ),
                PlanStep(
                    phase="P7",
                    data_domain=DataDomain.ORCHESTRATION,
                    depends_on_phases=("P6",),
                    task_params={
                        "derived_refresh_for": cutoff_date.isoformat(),
                        "pipeline": [
                            "data_quality_refresh",
                            "technical_screening_refresh",
                            "universe_merge",
                            "recommendation_pipeline",
                        ],
                    },
                ),
                PlanStep(
                    phase="P8",
                    data_domain=DataDomain.ORCHESTRATION,
                    depends_on_phases=("P7",),
                    task_params={"final_acceptance": True, "release_gate": True},
                ),
            ]
        )

        plan_hash = plan_fingerprint(
            market=market,
            universe_snapshot_hash=universe.snapshot_hash,
            cutoff_date=cutoff_date,
            gap_scope_keys=[target.scope_key() for target in all_targets],
            strategy_version=RECOVERY_STRATEGY_VERSION,
        )
        blockers = []
        warnings = []
        if not cutoff.executable:
            blockers.append("calendar_not_executable")
        if universe.source in {"provider_unavailable", "stale_without_provider"}:
            warnings.append("universe_snapshot_stale")
        return RecoveryPlan(
            market=market,
            cutoff_date=cutoff_date,
            gap_start_date=gap_start_date,
            universe=universe,
            plan_hash=plan_hash,
            steps=tuple(steps),
            created_at=now,
            executable=not blockers,
            blockers=tuple(blockers),
            warnings=tuple(warnings),
            calendar_source=cutoff.source,
            total_targets=len(all_targets),
            total_estimated_requests=sum(step.estimated_requests for step in steps),
        )


def _window_days(trading_dates, *, default):
    """事件/风险窗口默认取最近若干个交易日；无日历时退回固定天数。"""

    if not trading_dates:
        return default
    return max(default, min(len(trading_dates), 10))
