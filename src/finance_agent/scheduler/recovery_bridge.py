"""BaseDataScheduler 的停跑恢复补跑桥接层。

规格 5.3：调度器只负责执行补跑分区任务和检查 RecoveryGate，不计算缺口。
本 Mixin 以最小侵入方式挂入 BaseDataScheduler：领取持久队列中的
recovery.* 分区任务、执行采集或派生链路，并在任务结束后触发逐目标
事实验证回调。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from finance_agent.scheduler.persistent_task_queue import TaskClaim

logger = logging.getLogger(__name__)

JsonDict = dict[str, Any]

#: 补跑任务领取扫描的最小间隔，避免常驻循环空转打查询。
RECOVERY_SWEEP_MIN_INTERVAL_SECONDS = 5.0


class RecoverySchedulerMixin:
    """补跑执行与门控检查能力；由 BaseDataScheduler 继承使用。"""

    _RECOVERY_JOB_NAMES: tuple[str, ...] = (
        "recovery.ashare.bars",
        "recovery.ashare.fundamentals",
        "recovery.ashare.valuation",
        "recovery.ashare.capital_flow",
        "recovery.ashare.events",
        "recovery.ashare.risk_sentiment",
        "recovery.ashare.derived",
    )

    _RECOVERY_DOMAIN_TASK_TYPES: dict[str, tuple[str, str]] = {
        "market_bars": ("market_bars_backfill", "ashare-p0"),
        "fundamentals": ("fundamental_refresh", "ashare-p2"),
        "valuation": ("valuation_backfill", "ashare-p2"),
        "capital_flow": ("capital_flow_backfill", "ashare-p1"),
        "events": ("event_refresh", "ashare-p1"),
        "risk_sentiment": ("risk_sentiment_refresh", "ashare-risk"),
    }

    def _recovery_inflight(self) -> int:
        """当前在飞的补跑分区任务数。"""

        return int(getattr(self, "_recovery_inflight_count", 0))

    def _claim_recovery_tasks(self, executor: Any, *, free_slots: int) -> None:
        """从持久队列领取补跑分区任务并提交线程池；幂等键防重复。"""

        scope = getattr(self, "_persistent_task_queue_scope", None)
        if scope is None or free_slots <= 0:
            return
        now_monotonic = time.monotonic()
        last_sweep = float(getattr(self, "_recovery_last_sweep", 0.0) or 0.0)
        if (
            last_sweep > 0
            and (now_monotonic - last_sweep) < RECOVERY_SWEEP_MIN_INTERVAL_SECONDS
        ):
            return
        self._recovery_last_sweep = now_monotonic
        claimed = 0
        worker_id = getattr(self, "_worker_id", "recovery-worker")
        lease_seconds = max(600, int(self.config.job_timeout_seconds) + 60)
        # 轮转起点：避免固定顺序下大队列域（如估值）长期霸占全部并发槽，
        # 导致排在后面的域（如资金流）饥饿（实机联调发现）。
        names = self._RECOVERY_JOB_NAMES
        cursor = int(getattr(self, "_recovery_job_cursor", 0) or 0) % len(names)
        for offset in range(len(names)):
            if claimed >= free_slots:
                break
            idx = (cursor + offset) % len(names)
            job_name = names[idx]
            cursor = (idx + 1) % len(names)
            while claimed < free_slots:
                try:
                    with scope() as queue:
                        claim = queue.claim(
                            worker_id=worker_id,
                            lease_seconds=lease_seconds,
                            job_name=job_name,
                        )
                except Exception:
                    logger.exception("领取补跑任务失败 job=%s", job_name)
                    self._recovery_job_cursor = cursor
                    return
                if claim is None:
                    break
                lock = getattr(self, "_recovery_lock", None)
                if lock is not None:
                    with lock:
                        self._recovery_inflight_count = (
                            self._recovery_inflight_count + 1
                        )
                executor.submit(self._run_recovery_claim, claim)
                claimed += 1
        self._recovery_job_cursor = cursor



    def _run_recovery_claim(self, claim: TaskClaim) -> JsonDict:
        """执行补跑分区：完成租约并触发事实验证回调；不消耗普通重试预算。"""

        try:
            try:
                summary = self._execute_recovery_task(claim)
            except Exception as exc:
                logger.exception("补跑任务执行异常 task=%s", claim.task_id)
                summary = {"status": "failed", "error_message": str(exc)}
            succeeded = summary.get("status") == "executed"
            self._finish_persistent_task(
                claim,
                succeeded=succeeded,
                error_message=str(summary.get("error_message") or ""),
            )
            self._notify_recovery_module(claim, succeeded=succeeded)
            return dict(summary) | {"recovery_task_id": claim.task_id}
        finally:
            lock = getattr(self, "_recovery_lock", None)
            if lock is not None:
                with lock:
                    self._recovery_inflight_count = max(
                        0, self._recovery_inflight_count - 1
                    )

    @staticmethod
    def _collection_summary_ok(summary: dict) -> bool:
        """collect_base_data 顶层通常没有 status 字段，按聚合计数判定。

        旧实现直接读 summary["status"] 恒为空，导致采集成功也被判失败
        （实机验证发现）。
        """

        status = str(summary.get("status") or "")
        if status in {"executed", "ok", "success"}:
            return True
        if not status:
            return (
                int(summary.get("error") or 0) == 0
                and int(summary.get("unavailable") or 0) == 0
            )
        return False

    def _execute_recovery_task(self, claim: TaskClaim) -> JsonDict:
        """按 payload 分派补跑采集或派生链路。"""

        payload = dict(claim.payload or {})
        if claim.job_name == "recovery.ashare.derived":
            return self._execute_recovery_derived(payload)
        data_domain = str(payload.get("data_domain") or "")
        targets = list(payload.get("targets") or [])
        summaries: list[JsonDict] = []
        failures = 0
        for item in targets:
            args = self._build_recovery_collect_args(data_domain, item)
            if args is None:
                failures += 1
                continue
            summary = self.collect_base_data(args, job_name=claim.job_name)
            if not self._collection_summary_ok(summary):
                failures += 1
            summaries.append(summary)
        if failures:
            return {
                "status": "failed",
                "error_message": f"recovery_targets_failed={failures}",
                "recent_summaries": summaries[-3:],
            }
        return {
            "status": "executed",
            "target_count": len(targets),
            "recent_summaries": summaries[-3:],
        }

    def _build_recovery_collect_args(self, data_domain: str, target_item: dict) -> Any:
        """把目标区间转换为 collect_base_data 的手动单标的参数。"""

        mapping = self._RECOVERY_DOMAIN_TASK_TYPES.get(data_domain)
        default_args = getattr(self, "_default_collection_args", None)
        if mapping is None or default_args is None:
            return None
        sync_task_type, group = mapping
        asset_id = target_item.get("asset_id")
        overrides: dict[str, Any] = {
            "group": group,
            "sync_task_type": sync_task_type,
            "symbol_source": "manual" if asset_id else "market_assets",
        }
        from datetime import date as _date
        from datetime import timedelta as _timedelta

        raw_start = target_item.get("gap_start_at")
        raw_end = target_item.get("gap_end_at")
        start_d = _date.fromisoformat(str(raw_start)[:10]) if raw_start else None
        end_d = _date.fromisoformat(str(raw_end)[:10]) if raw_end else None
        if asset_id and start_d is not None and end_d is not None:
            overrides["ashare_symbol"] = str(asset_id).split(":")[-1]
            # collect_base_data 期望 %Y%m%d 紧凑日期。仅 K 线向两侧扩窗，
            # 估值/资金流保留目标窗口，由历史 Provider 精确过滤。
            if data_domain == "market_bars":
                start_d -= _timedelta(days=7)
                end_d += _timedelta(days=7)
            today = _date.today()
            if end_d > today:
                end_d = today
            overrides["ashare_start"] = start_d.strftime("%Y%m%d")
            overrides["ashare_end"] = end_d.strftime("%Y%m%d")
            overrides["limit"] = max(30, (end_d - start_d).days + 1)
            overrides["is_closed"] = True
        elif data_domain == "events" and start_d is not None and end_d is not None:
            overrides["risk_start"] = start_d.strftime("%Y%m%d")
            overrides["risk_end"] = end_d.strftime("%Y%m%d")
        try:
            return default_args(**overrides)
        except Exception:
            logger.exception("构造补跑采集参数失败 domain=%s", data_domain)
            return None



    def _execute_recovery_derived(self, payload: dict) -> JsonDict:
        """P7：按现有依赖图对冻结截止日重建一次最新派生截面（规格 7.2）。"""

        params = dict((payload or {}).get("task_params") or {})
        pipeline = list(
            params.get(
                "pipeline",
                [
                    "data_quality_refresh",
                    "technical_screening_refresh",
                    "universe_merge",
                    "recommendation_pipeline",
                ],
            )
        )
        # 派生阶段 runner 必须引用 BaseDataScheduler 的公开执行方法，
        # 而不是注入回调字段（默认 None），否则重跑派生前会报
        # missing_runner:stage（实测 P7 发现）。
        stage_map = {
            "data_quality_refresh": (
                "quality.",
                getattr(self, "build_data_quality_refresh_kwargs", None),
                getattr(self, "run_data_quality_refresh", None),
            ),
            "technical_screening_refresh": (
                "analytics.technical_screening",
                getattr(self, "build_technical_screening_refresh_kwargs", None),
                getattr(self, "run_technical_screening_refresh", None),
            ),
            "universe_merge": (
                "analytics.universe.merge",
                getattr(self, "build_universe_merge_kwargs", None),
                getattr(self, "run_universe_merge", None),
            ),
            "recommendation_pipeline": (
                "analytics.recommendations",
                getattr(self, "build_recommendation_pipeline_kwargs", None),
                getattr(self, "run_recommendation_pipeline", None),
            ),
        }
        executed: list[str] = []
        skipped: list[str] = []
        for stage in pipeline:
            entry = stage_map.get(stage)
            if entry is None:
                skipped.append(stage)
                continue
            prefix, build_kwargs, runner = entry
            job = next(
                (
                    candidate
                    for candidate in self.config.jobs
                    if candidate.name.startswith(prefix)
                    and candidate.market == "ashare"
                ),
                None,
            )
            if job is None or build_kwargs is None or runner is None:
                # 规格 15.5：必需阶段缺能力必须失败，不得静默跳过。
                return {
                    "status": "failed",
                    "error_message": f"missing_runner:{stage}",
                    "pipeline": executed,
                    "cutoff": params.get("derived_refresh_for"),
                }
            result = runner(**build_kwargs(job)) or {}
            if str(result.get("status") or "") in {"failed", "error"}:
                return {
                    "status": "failed",
                    "error_message": f"derived_stage_failed={stage}",
                    "pipeline": executed,
                }
            executed.append(stage)
        return {
            "status": "executed",
            "pipeline": executed,
            "skipped": skipped,
            "cutoff": params.get("derived_refresh_for"),
        }



    def _notify_recovery_module(self, claim: TaskClaim, *, succeeded: bool) -> None:
        """任务结束后的逐目标事实验证回调（规格 11.3）；失败不抛出。"""

        payload = dict(claim.payload or {})
        if not payload.get("recovery_run_id"):
            return
        scope = getattr(self, "_persistent_task_queue_scope", None)
        if scope is None:
            return
        try:

            with scope() as queue:
                from finance_agent.data_recovery.assembly import (
                    build_default_recovery_module,
                )

                module = build_default_recovery_module(queue.repository.session)
                module.on_task_finished(payload, success=succeeded)
        except Exception:
            logger.exception(
                "补跑事实验证回调失败 run=%s step=%s",
                payload.get("recovery_run_id"),
                payload.get("recovery_step_id"),
            )

    def _filter_by_recovery_gate(self, due_states):
        """RecoveryGate 只按任务显式依赖的数据域过滤。"""

        if not due_states:
            return due_states, []
        scope = getattr(self, "_persistent_task_queue_scope", None)
        if scope is None:
            return due_states, []
        try:
            from finance_agent.data_recovery.gate import RecoveryGate
            from finance_agent.data_recovery.repository import RecoveryRepository

            with scope() as queue:
                gate = RecoveryGate(RecoveryRepository(queue.repository.session))
                runnable, blocked = gate.filter_due_states(due_states)
        except Exception:
            logger.exception("读取补跑门控失败，本轮按放行处理")
            return due_states, []
        return runnable, blocked

    def _run_recovery_startup_scan(self) -> None:
        """规格 13.1：租约恢复后只读扫描缺口并创建/复用草稿；不阻塞启动。"""

        scope = getattr(self, "_persistent_task_queue_scope", None)
        if scope is None:
            return
        try:

            with scope() as queue:
                from finance_agent.data_recovery.assembly import (
                    build_default_recovery_module,
                )

                module = build_default_recovery_module(queue.repository.session)
                scan_result = module.startup_scan()
            logger.info("停跑恢复启动扫描完成 result=%s", scan_result)
        except Exception:
            logger.exception("停跑恢复启动扫描失败（不阻塞调度器启动）")
