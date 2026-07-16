from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from finance_agent.research.strategy_observation_service import (
    StrategyObservationService,
)

SHORT = "strategy:ashare:short_swing"
THEME = "strategy:ashare:theme_momentum"
MIXED = "strategy:ashare:short_theme_mixed_v1"
STRATEGIES = (SHORT, THEME, MIXED)
UNIVERSE = "universe:merged:ashare:recommendation"
DAY_1 = date(2026, 7, 16)
DAY_2 = date(2026, 7, 17)


class _ObservationRepository:
    def __init__(self) -> None:
        self.runs: dict[tuple[date, str], Any] = {}
        self.positions: dict[str, dict[str, Any]] = {}
        self.outcomes: dict[str, dict[str, Any]] = {}
        self.states: dict[str, Any] = {}

    def upsert_run(self, **kwargs: Any) -> Any:
        key = (kwargs["trade_date"], kwargs["universe_id"])
        self.runs.setdefault(key, dict(kwargs))
        return SimpleNamespace(**self.runs[key])

    def upsert_positions(self, positions: list[dict[str, Any]]) -> int:
        for item in positions:
            self.positions[item["position_id"]] = dict(item)
        return len(positions)

    def ensure_outcomes(self, outcomes: list[dict[str, Any]]) -> int:
        inserted = 0
        for item in outcomes:
            if item["outcome_id"] not in self.outcomes:
                inserted += 1
                self.outcomes[item["outcome_id"]] = dict(item)
        return inserted

    def list_due_outcomes(self, *, as_of: date, limit: int = 500) -> list[Any]:
        due = [
            SimpleNamespace(**item)
            for item in self.outcomes.values()
            if item["status"] == "pending"
            and item.get("due_trade_date") is not None
            and item["due_trade_date"] <= as_of
        ]
        return due[:limit]

    def list_pending_without_due(self, *, limit: int = 500) -> list[Any]:
        return [
            SimpleNamespace(**item)
            for item in self.outcomes.values()
            if item["status"] == "pending" and item.get("due_trade_date") is None
        ][:limit]

    def update_outcome_due_dates(self, updates: list[dict[str, Any]]) -> int:
        for item in updates:
            target = self.outcomes[item["outcome_id"]]
            target["due_trade_date"] = item["due_trade_date"]
            target["reason"] = item.get("reason")
        return len(updates)

    def update_position_entries(self, entries: list[dict[str, Any]]) -> int:
        for item in entries:
            target = self.positions[item["position_id"]]
            target.update({key: value for key, value in item.items() if key != "position_id"})
        return len(entries)

    def mature_outcomes(self, outcomes: list[dict[str, Any]]) -> int:
        for item in outcomes:
            target = self.outcomes[item["outcome_id"]]
            target.update({key: value for key, value in item.items() if key != "outcome_id"})
        return len(outcomes)

    def get_trial_state(self, strategy_id: str) -> Any | None:
        return self.states.get(strategy_id)

    def upsert_trial_state(self, **kwargs: Any) -> Any:
        existing = self.states.get(kwargs["strategy_id"])
        values = dict(vars(existing)) if existing is not None else {}
        values.update(kwargs)
        state = SimpleNamespace(**values)
        self.states[kwargs["strategy_id"]] = state
        return state

    def list_recent_matured_outcomes(
        self,
        *,
        strategy_id: str,
        horizon_days: int | None = None,
        limit: int = 500,
    ) -> list[Any]:
        position_ids = {
            position_id
            for position_id, item in self.positions.items()
            if item["strategy_id"] == strategy_id
        }
        rows = [
            SimpleNamespace(**item)
            for item in self.outcomes.values()
            if item["position_id"] in position_ids
            and item["status"] == "matured"
            and (horizon_days is None or item["horizon_days"] == horizon_days)
        ]
        return rows[:limit]

    @property
    def run_count(self) -> int:
        return len(self.runs)

    @property
    def position_count(self) -> int:
        return len(self.positions)


class _ObservationSource:
    def __init__(self) -> None:
        self.settlements: dict[str, dict[str, Any]] = {}
        self.future_dates_override: list[date] | None = None
        self.future_date_calls = 0

    def list_top_scores(
        self,
        *,
        screening_id: str,
        strategy_id: str,
        limit: int,
    ) -> list[Any]:
        return [
            SimpleNamespace(
                score_id=f"score:{screening_id}:{strategy_id}:{index}",
                asset_id=f"ashare:{index:06d}",
                symbol=f"{index:06d}",
                universe_id=UNIVERSE,
                rank=index,
                total_score=Decimal(str(100 - index)),
                factor_frame_id=f"factor:{screening_id}:{index}",
                as_of=datetime.combine(DAY_1, datetime.min.time(), tzinfo=UTC),
                payload={"strategy_id": strategy_id},
            )
            for index in range(1, limit + 1)
        ]

    def future_trade_dates(self, *, trade_date: date, count: int) -> list[date]:
        self.future_date_calls += 1
        if self.future_dates_override is not None:
            return self.future_dates_override[:count]
        return [trade_date + timedelta(days=index) for index in range(1, count + 1)]

    def data_versions(self, *, screening_id: str) -> dict[str, Any]:
        return {"screening_id": screening_id, "code_commit": "abc123"}

    def resolve_outcome(
        self,
        outcome: Any,
        *,
        round_trip_cost: float,
    ) -> dict[str, Any]:
        return dict(
            self.settlements.get(
                outcome.outcome_id,
                {
                    "status": "pending",
                    "reason": "missing_price",
                    "payload": {"round_trip_cost": round_trip_cost},
                },
            )
        )


def _service() -> tuple[StrategyObservationService, _ObservationRepository, _ObservationSource]:
    repository = _ObservationRepository()
    source = _ObservationSource()
    return (
        StrategyObservationService(repository=repository, source=source),
        repository,
        source,
    )


def test_capture_same_day_is_idempotent_and_next_day_appends() -> None:
    """每日观察按交易日追加，同日重试不得复制仓位。"""

    service, repository, _source = _service()

    service.capture(screening_id="screen:1", trade_date=DAY_1, strategy_ids=STRATEGIES)
    service.capture(screening_id="screen:1", trade_date=DAY_1, strategy_ids=STRATEGIES)
    service.capture(screening_id="screen:2", trade_date=DAY_2, strategy_ids=STRATEGIES)

    assert repository.run_count == 2
    assert repository.position_count == 120
    assert len(repository.outcomes) == 360
    due_dates = {
        item["horizon_days"]: item["due_trade_date"]
        for item in repository.outcomes.values()
        if item["position_id"] in repository.positions
    }
    assert due_dates == {
        5: DAY_2 + timedelta(days=5),
        10: DAY_2 + timedelta(days=10),
        20: DAY_2 + timedelta(days=20),
    }


def test_settle_due_matures_complete_price_and_keeps_missing_price_pending() -> None:
    """到期价格完整才成熟；缺价格继续 pending 并记录原因。"""

    service, repository, source = _service()
    service.capture(screening_id="screen:1", trade_date=DAY_1, strategy_ids=(MIXED,))
    due = list(repository.outcomes.values())[:2]
    for item in due:
        item["due_trade_date"] = DAY_1
    source.settlements[due[0]["outcome_id"]] = {
        "status": "matured",
        "entry_date": DAY_1 + timedelta(days=1),
        "entry_price": Decimal("10.00000000"),
        "benchmark_entry_price": None,
        "exit_date": DAY_1 + timedelta(days=5),
        "exit_price": Decimal("11.00000000"),
        "gross_return": Decimal("0.10000000"),
        "net_return": Decimal("0.09700000"),
        "benchmark_return": Decimal("0.03000000"),
        "excess_return": Decimal("0.06700000"),
        "reason": None,
        "payload": {"round_trip_cost": 0.003},
    }

    result = service.settle_due(as_of=DAY_1)

    assert result["matured_count"] == 1
    assert result["pending_count"] == 1
    assert repository.outcomes[due[0]["outcome_id"]]["status"] == "matured"
    assert repository.outcomes[due[1]["outcome_id"]]["status"] == "pending"
    assert repository.outcomes[due[1]["outcome_id"]]["reason"] == "missing_price"
    position = repository.positions[due[0]["position_id"]]
    assert position["entry_price"] == Decimal("10.00000000")
    assert position["status"] == "entered"


def test_settle_due_backfills_due_date_from_later_realized_trading_days() -> None:
    """捕获时未知的未来交易日，应在后续行情出现后回填并结算。"""

    service, repository, source = _service()
    source.future_dates_override = []
    service.capture(screening_id="screen:1", trade_date=DAY_1, strategy_ids=(MIXED,))
    horizon_five = next(
        item for item in repository.outcomes.values() if item["horizon_days"] == 5
    )
    assert horizon_five["due_trade_date"] is None

    source.future_dates_override = [DAY_1 + timedelta(days=index) for index in range(1, 21)]
    source.settlements[horizon_five["outcome_id"]] = {
        "status": "matured",
        "entry_date": DAY_1 + timedelta(days=1),
        "entry_price": Decimal("10.00000000"),
        "benchmark_entry_price": None,
        "exit_date": DAY_1 + timedelta(days=5),
        "exit_price": Decimal("11.00000000"),
        "gross_return": Decimal("0.10000000"),
        "net_return": Decimal("0.09700000"),
        "benchmark_return": Decimal("0.03000000"),
        "excess_return": Decimal("0.06700000"),
        "reason": None,
        "payload": {"round_trip_cost": 0.003},
    }

    result = service.settle_due(as_of=DAY_1 + timedelta(days=5), limit=60)

    assert result["due_date_backfilled_count"] == 60
    assert result["matured_count"] == 1
    assert horizon_five["due_trade_date"] == DAY_1 + timedelta(days=5)
    assert source.future_date_calls == 2


def test_historical_pass_moves_new_strategy_to_trial() -> None:
    """只有真实 available 且 gate_passed 的历史结果允许进入 trial。"""

    service, repository, _source = _service()

    state = service.apply_historical_result(
        strategy_id=MIXED,
        result={
            "status": "available",
            "backtest_id": "bt:wf:passed",
            "metrics": {"gate_passed": True},
            "data_versions": {"code_commit": "abc123"},
        },
    )

    assert state.state == "trial"
    assert state.historical_evidence_id == "bt:wf:passed"
    assert repository.states[MIXED].consecutive_failure_count == 0


def test_historical_failure_or_shortage_keeps_strategy_in_research() -> None:
    """历史失败和数据不足都不能进入试运行。"""

    service, _repository, _source = _service()

    failed = service.apply_historical_result(
        strategy_id=THEME,
        result={"status": "failed", "backtest_id": "bt:failed", "metrics": {}},
    )
    insufficient = service.apply_historical_result(
        strategy_id=MIXED,
        result={
            "status": "insufficient_data",
            "backtest_id": "bt:shortage",
            "metrics": {},
        },
    )

    assert failed.state == "research"
    assert insufficient.state == "research"


def _trial_state(strategy_id: str = MIXED) -> Any:
    return SimpleNamespace(
        strategy_id=strategy_id,
        strategy_version="v1",
        state="trial",
        historical_evidence_id="bt:wf:passed",
        forward_metrics={},
        consecutive_failure_count=0,
        disabled_reason=None,
        last_evaluated_at=None,
        payload={},
    )


def _failing_metrics() -> dict[str, Any]:
    return {
        "sample_counts": {"5": 30, "10": 30, "20": 20},
        "median_excess_returns": {"5": -0.01, "10": 0.0, "20": -0.02},
        "drawdown_gap": 0.05,
        "data_integrity_violations": [],
        "gate_passed": False,
    }


def test_two_consecutive_forward_failures_disable_trial() -> None:
    """两个不同周的失败才自动关闭，重复执行同一周不重复计数。"""

    service, repository, _source = _service()
    repository.states[MIXED] = _trial_state()
    week_1 = datetime(2026, 8, 3, tzinfo=UTC)
    week_2 = datetime(2026, 8, 10, tzinfo=UTC)

    service.evaluate_weekly(strategy_id=MIXED, as_of=week_1, metrics=_failing_metrics())
    service.evaluate_weekly(strategy_id=MIXED, as_of=week_1, metrics=_failing_metrics())
    assert repository.states[MIXED].state == "trial"
    assert repository.states[MIXED].consecutive_failure_count == 1

    service.evaluate_weekly(strategy_id=MIXED, as_of=week_2, metrics=_failing_metrics())
    assert repository.states[MIXED].state == "disabled"
    assert repository.states[MIXED].consecutive_failure_count == 2


def test_integrity_violation_or_large_drawdown_disables_immediately() -> None:
    """数据完整性问题和超额回撤不等待样本或连续周数。"""

    service, repository, _source = _service()
    repository.states[THEME] = _trial_state(THEME)

    service.evaluate_weekly(
        strategy_id=THEME,
        as_of=datetime(2026, 8, 3, tzinfo=UTC),
        metrics={
            "sample_counts": {"5": 0, "10": 0, "20": 0},
            "median_excess_returns": {"5": None, "10": None, "20": None},
            "drawdown_gap": 0.0,
            "data_integrity_violations": ["future_data_detected"],
            "gate_passed": False,
        },
    )
    assert repository.states[THEME].state == "disabled"
    assert "future_data_detected" in repository.states[THEME].disabled_reason

    repository.states[MIXED] = _trial_state()
    metrics = _failing_metrics()
    metrics["sample_counts"] = {"5": 0, "10": 0, "20": 0}
    metrics["drawdown_gap"] = 0.101
    service.evaluate_weekly(
        strategy_id=MIXED,
        as_of=datetime(2026, 8, 3, tzinfo=UTC),
        metrics=metrics,
    )
    assert repository.states[MIXED].state == "disabled"
    assert repository.states[MIXED].disabled_reason == "drawdown_gap_above_10pct"


def test_sixty_t20_samples_and_revalidated_gate_promote_trial() -> None:
    """至少 60 个 T+20 截面且复核门槛通过后才晋级。"""

    service, repository, _source = _service()
    repository.states[MIXED] = _trial_state()

    state = service.evaluate_weekly(
        strategy_id=MIXED,
        as_of=datetime(2026, 9, 7, tzinfo=UTC),
        metrics={
            "sample_counts": {"5": 80, "10": 70, "20": 60},
            "median_excess_returns": {"5": 0.01, "10": 0.02, "20": 0.03},
            "drawdown_gap": 0.01,
            "data_integrity_violations": [],
            "gate_passed": True,
        },
    )

    assert state.state == "validated"
    assert state.disabled_reason is None


def test_short_baseline_failure_only_records_alert() -> None:
    """没有替代基线时，短线策略失败只能告警，不能自动中断。"""

    service, repository, _source = _service()
    repository.states[SHORT] = _trial_state(SHORT)
    metrics = _failing_metrics()
    metrics["drawdown_gap"] = 0.20

    state = service.evaluate_weekly(
        strategy_id=SHORT,
        as_of=datetime(2026, 8, 3, tzinfo=UTC),
        metrics=metrics,
    )

    assert state.state == "trial"
    assert state.payload["high_risk_alerts"][-1]["reason"] == "drawdown_gap_above_10pct"


def test_disabled_strategy_cannot_be_reenabled_by_routine_evaluation() -> None:
    """disabled 是当前策略版本终态，普通历史/周度任务不得自动重开。"""

    service, repository, _source = _service()
    disabled = _trial_state()
    disabled.state = "disabled"
    disabled.disabled_reason = "two_consecutive_forward_failures"
    repository.states[MIXED] = disabled

    historical = service.apply_historical_result(
        strategy_id=MIXED,
        result={
            "status": "available",
            "backtest_id": "bt:wf:new-pass",
            "metrics": {"gate_passed": True},
        },
    )
    weekly = service.evaluate_weekly(
        strategy_id=MIXED,
        as_of=datetime(2026, 9, 14, tzinfo=UTC),
        metrics={
            "sample_counts": {"5": 80, "10": 70, "20": 60},
            "median_excess_returns": {"5": 0.01, "10": 0.02, "20": 0.03},
            "drawdown_gap": 0.01,
            "data_integrity_violations": [],
            "gate_passed": True,
        },
    )

    assert historical.state == "disabled"
    assert weekly.state == "disabled"
    assert weekly.disabled_reason == "two_consecutive_forward_failures"


def test_research_strategy_cannot_skip_historical_gate_during_weekly_evaluation() -> None:
    """research 不能借周度前向指标绕过历史门槛直接晋级。"""

    service, repository, _source = _service()
    research = _trial_state()
    research.state = "research"
    research.historical_evidence_id = None
    repository.states[MIXED] = research

    state = service.evaluate_weekly(
        strategy_id=MIXED,
        as_of=datetime(2026, 9, 14, tzinfo=UTC),
        metrics={
            "sample_counts": {"5": 80, "10": 70, "20": 60},
            "median_excess_returns": {"5": 0.01, "10": 0.02, "20": 0.03},
            "drawdown_gap": 0.01,
            "data_integrity_violations": [],
            "gate_passed": True,
        },
    )

    assert state.state == "research"
    assert state.historical_evidence_id is None
