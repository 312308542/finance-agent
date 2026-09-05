from __future__ import annotations

import subprocess
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import JSON, Column, MetaData, Table, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session as DatabaseSession

import scripts.runtime.run_position_monitor as runner_module
from finance_agent.monitoring.position_engine import PositionMonitoringEngine
from finance_agent.monitoring.repository import PositionMonitoringRepository
from finance_agent.monitoring.service import PositionMonitoringService
from finance_agent.storage.orm import AssistantTriggerEventORM, PositionMonitoringStateORM


def test_runner同一quote_snapshot仍逐轮调用监控服务(tmp_path: Path, monkeypatch) -> None:
    """同一行情快照仍需重新评估持仓变化和时效。"""

    rows = [SimpleNamespace(data_snapshot_id="snapshot-1")]
    calls: list[str] = []

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def commit(self):
            return None

        def rollback(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(runner_module, "session_scope", lambda _factory: _SessionScope(FakeSession()))
    monkeypatch.setattr(
        runner_module,
        "PortfolioRepository",
        lambda _session: SimpleNamespace(
            list_active_positions_by_owner=lambda **_: [SimpleNamespace(asset_id="asset-1")]
        ),
    )
    monkeypatch.setattr(
        runner_module,
        "AssetRepository",
        lambda _session: SimpleNamespace(
            list_intraday_quote_latest=lambda **_: rows
        ),
    )

    class FakeService:
        def __init__(self, _session):
            pass

        def evaluate_owner(self, owner_id: str, *, as_of: datetime):
            calls.append(owner_id)
            return SimpleNamespace(
                owner_id=owner_id, evaluated_at=as_of, actions=(), error_count=0, changed_actions=()
            )

    runner = runner_module.PositionMonitorRunner(
        session_factory=lambda: FakeSession(),
        owner_id="owner-1",
        service_factory=FakeService,
        now_factory=lambda: datetime(2026, 9, 5, tzinfo=UTC),
    )
    first = runner.run_once(status_file=tmp_path / "status.json")
    second = runner.run_once(status_file=tmp_path / "status.json")

    assert first["status"] == "completed"
    assert second["status"] == "completed"
    assert calls == ["owner-1", "owner-1"]


def test_runner失败写健康文件并记录退避(tmp_path: Path, monkeypatch) -> None:
    """监控失败时进程不应静默退出，健康文件要包含错误和下一次重试时间。"""

    class BrokenSession:
        def __enter__(self):
            raise RuntimeError("数据库不可用")

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(runner_module, "session_scope", lambda _factory: BrokenSession())
    runner = runner_module.PositionMonitorRunner(
        session_factory=lambda: BrokenSession(),
        owner_id="owner-1",
        now_factory=lambda: datetime(2026, 9, 5, tzinfo=UTC),
    )

    result = runner.run_once(status_file=tmp_path / "status.json")

    assert result["status"] == "error"
    assert result["backoff_seconds"] >= 1
    assert "数据库不可用" in result["error"]
    assert '"status": "error"' in (tmp_path / "status.json").read_text(encoding="utf-8")


def test_runner将动作交给触发器唤醒_workflow(monkeypatch) -> None:
    """持仓监控动作变化后应进入统一触发事件链路。"""

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(runner_module, "session_scope", lambda _factory: _SessionScope(FakeSession()))
    monkeypatch.setattr(
        runner_module,
        "PortfolioRepository",
        lambda _session: SimpleNamespace(
            list_active_positions_by_owner=lambda **_: [SimpleNamespace(asset_id="asset-1")]
        ),
    )
    monkeypatch.setattr(
        runner_module,
        "AssetRepository",
        lambda _session: SimpleNamespace(
            list_intraday_quote_latest=lambda **_: [SimpleNamespace(data_snapshot_id="snapshot-2")]
        ),
    )
    actions = (SimpleNamespace(position_id="position-1"),)
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class FakeService:
        def __init__(self, _session):
            pass

        def evaluate_owner(self, owner_id: str, *, as_of: datetime):
            return SimpleNamespace(actions=actions, error_count=0, changed_actions=actions)

    class FakeTrigger:
        def __init__(self, _session):
            pass

        def persist_position_actions(self, received, *, as_of, cooldown_minutes):
            calls.append((tuple(received), {"as_of": as_of, "cooldown_minutes": cooldown_minutes}))

    runner = runner_module.PositionMonitorRunner(
        session_factory=lambda: FakeSession(),
        owner_id="owner-1",
        service_factory=FakeService,
        trigger_factory=FakeTrigger,
        now_factory=lambda: datetime(2026, 9, 5, tzinfo=UTC),
    )

    runner.run_once()

    assert calls == [(actions, {"as_of": datetime(2026, 9, 5, tzinfo=UTC), "cooldown_minutes": 0})]


class _SessionScope:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *_):
        return False


@pytest.fixture
def monitor(monkeypatch):
    """监控状态使用内存适配，真实触发仓储在隔离的 SQLite 中验证写入。"""

    database = create_engine("sqlite://")
    original = AssistantTriggerEventORM.__table__
    # 仅替换 SQLite 不支持的 JSONB 建表类型，不改业务查询和去重逻辑。
    trigger_table = Table(
        original.name,
        MetaData(),
        *(Column(
            column.name,
            JSON() if isinstance(column.type, JSONB) else column.type,
            primary_key=column.primary_key,
            nullable=column.nullable,
        ) for column in original.columns),
    )
    trigger_table.create(database)
    now = datetime(2026, 9, 7, 2, 0, tzinfo=UTC)
    facts = SimpleNamespace(
        now=now,
        positions=[_position("position-1")],
        quotes=[SimpleNamespace(
            asset_id="asset-1",
            data_snapshot_id="snapshot-1",
            quote_snapshot_id="snapshot-1",
            source="gotdx",
            as_of=now,
            last_price=Decimal("10"),
            quality_status="available",
            payload={},
        )],
        states=PositionMonitoringRepository(),
        fail_commits=0,
        broken_positions=set(),
        rollbacks=0,
        trigger_events=[],
    )
    portfolios = SimpleNamespace(list_active_positions_by_owner=lambda **_: facts.positions)
    assets = SimpleNamespace(list_intraday_quote_latest=lambda **_: facts.quotes)
    monkeypatch.setattr(runner_module, "PortfolioRepository", lambda _session: portfolios)
    monkeypatch.setattr(runner_module, "AssetRepository", lambda _session: assets)

    class Session:
        def __init__(self):
            self.states = deepcopy(facts.states)
            self.database = DatabaseSession(database)

        def __getattr__(self, name):
            return getattr(self.database, name)

        def get(self, model, identity):
            if model is PositionMonitoringStateORM:
                return self.states.get_state(identity.removeprefix("monitoring:"))
            return self.database.get(model, identity)

        def commit(self):
            if facts.fail_commits:
                facts.fail_commits -= 1
                raise RuntimeError("提交失败")
            events = list(self.database.scalars(select(AssistantTriggerEventORM.trigger_event_id)))
            self.database.commit()
            facts.states = self.states
            facts.trigger_events = events

        def rollback(self):
            facts.rollbacks += 1
            self.database.rollback()

        def close(self):
            self.database.close()

    class Engine(PositionMonitoringEngine):
        def evaluate(self, state, snapshot):
            if state.position_id in facts.broken_positions:
                raise RuntimeError("持仓事实暂时不可用")
            return super().evaluate(state, snapshot)

    facts.runner = runner_module.PositionMonitorRunner(
        session_factory=Session,
        owner_id="owner-1",
        service_factory=lambda session: PositionMonitoringService(
            portfolio_repository=portfolios,
            asset_repository=assets,
            state_repository=session.states,
            engine=Engine(),
        ),
        now_factory=lambda: facts.now,
    )
    yield facts
    database.dispose()


def _position(position_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        position_id=position_id,
        portfolio_id="portfolio-1",
        asset_id="asset-1",
        symbol="600000",
        market="ashare",
        quantity=Decimal("100"),
        payload={"owner_id": "owner-1", "sellable_quantity": "100"},
    )


def test_runner_retries_same_snapshot_after_commit_failure(monitor) -> None:
    monitor.fail_commits = 2

    first = monitor.runner.run_once()
    assert first["status"] == "error"
    assert first["backoff_seconds"] == 1
    assert monitor.states.get_state("position-1") is None
    assert monitor.trigger_events == []

    second = monitor.runner.run_once()
    assert second["status"] == "error"
    assert second["backoff_seconds"] == 2

    third = monitor.runner.run_once()

    assert third["status"] == "completed"
    assert monitor.rollbacks == 2
    assert monitor.states.get_state("position-1").current_action == "hold"
    assert len(monitor.states.list_events("position-1")) == 1
    assert len(monitor.trigger_events) == 1


def test_runner_retries_partial_failures_without_duplicate_success_events(monitor, tmp_path) -> None:
    monitor.positions.append(_position("position-2"))
    monitor.broken_positions.add("position-2")

    first = monitor.runner.run_once(status_file=tmp_path / "status.json")

    assert first["status"] == "error"
    assert first["error_count"] == 1
    assert first["action_count"] == 2
    assert first["backoff_seconds"] == 1
    assert monitor.states.get_state("position-1").current_action == "hold"
    assert monitor.states.get_state("position-2").current_action == "unexecutable"
    assert '"status": "error"' in (tmp_path / "status.json").read_text(encoding="utf-8")

    second = monitor.runner.run_once()
    assert second["status"] == "error"
    assert second["backoff_seconds"] == 2
    assert len(monitor.trigger_events) == 2

    monitor.broken_positions.clear()
    third = monitor.runner.run_once()
    fourth = monitor.runner.run_once()

    assert third["status"] == fourth["status"] == "completed"
    assert len(monitor.states.list_events("position-1")) == 1
    assert len(monitor.states.list_events("position-2")) == 2
    assert monitor.states.get_state("position-2").current_action == "hold"
    assert len(monitor.trigger_events) == 3
    monitor.broken_positions.add("position-2")
    assert monitor.runner.run_once()["backoff_seconds"] == 1


@pytest.mark.parametrize("has_quotes", [True, False])
def test_runner_evaluates_new_position_with_unchanged_quotes(monitor, has_quotes) -> None:
    if not has_quotes:
        monitor.quotes.clear()
    monitor.runner.run_once()
    monitor.positions.append(_position("position-2"))

    result = monitor.runner.run_once()

    assert result["status"] == "completed"
    assert monitor.states.get_state("position-2") is not None
    assert len(monitor.states.list_events("position-1")) == 1
    assert len(monitor.states.list_events("position-2")) == 1


def test_runner_rechecks_sellable_quantity_without_executing_trades(monitor) -> None:
    monitor.positions[0].payload["sellable_quantity"] = "0"
    monitor.quotes[0].payload["structure_invalidated"] = True
    monitor.runner.run_once()
    assert monitor.states.get_state("position-1").payload["reason_codes"] == ["t1_not_sellable"]

    monitor.positions[0].payload["sellable_quantity"] = "100"
    before = deepcopy(monitor.positions)
    monitor.runner.run_once()

    assert monitor.states.get_state("position-1").current_action == "exit"
    assert monitor.positions == before


def test_runner_rechecks_quote_expiry_with_unchanged_snapshot(monitor) -> None:
    monitor.runner.run_once()
    assert monitor.states.get_state("position-1").current_action == "hold"
    monitor.now += timedelta(seconds=4)

    result = monitor.runner.run_once()

    assert result["status"] == "completed"
    state = monitor.states.get_state("position-1")
    assert state.current_action == "unexecutable"
    assert state.payload["reason_codes"] == ["quote_stale"]
    assert len(monitor.states.list_events("position-1")) == 2


def test_runner_evaluates_next_trading_day_with_no_new_quote(monitor) -> None:
    monitor.quotes.clear()
    monitor.runner.run_once()
    monitor.now += timedelta(days=1)

    monitor.runner.run_once()

    assert monitor.states.get_state("position-1").last_evaluated_at == monitor.now
    assert len(monitor.states.list_events("position-1")) == 1


def test_runner_repeated_poll_does_not_duplicate_business_events(monitor) -> None:
    for _ in range(3):
        result = monitor.runner.run_once()
        assert result["status"] == "completed"

    assert len(monitor.states.list_active_states("owner-1")) == 1
    assert len(monitor.states.list_events("position-1")) == 1
    assert len(monitor.trigger_events) == 1


@pytest.mark.parametrize("monitor_error", [False, True])
def test_runner_unchanged_risk_does_not_wake_again_after_cooldown(monitor, monitor_error) -> None:
    monitor.quotes.clear()
    if monitor_error:
        monitor.broken_positions.add("position-1")
        monitor.quotes.append(SimpleNamespace(asset_id="asset-1", as_of=monitor.now, payload={}))
    monitor.runner.run_once()
    first_events = tuple(monitor.trigger_events)
    monitor.now += timedelta(minutes=16)

    monitor.runner.run_once()

    assert len(monitor.states.list_events("position-1")) == 1
    assert tuple(monitor.trigger_events) == first_events


def test_runner_after_cooldown_only_new_position_and_new_action_wake(monitor) -> None:
    monitor.quotes.clear()
    monitor.runner.run_once()
    monitor.now += timedelta(minutes=16)
    monitor.positions.append(_position("position-2"))

    monitor.runner.run_once()

    assert len(monitor.trigger_events) == 2
    monitor.quotes.append(SimpleNamespace(
        asset_id="asset-1", as_of=monitor.now, last_price=Decimal("10"), payload={}
    ))
    monitor.runner.run_once()

    assert len(monitor.trigger_events) == 4
    assert len(monitor.states.list_events("position-1")) == 2
    assert len(monitor.states.list_events("position-2")) == 2


@pytest.mark.parametrize("commit_failure", [False, True])
def test_runner_reappearing_risk_wakes_inside_cooldown_and_survives_rollback(monitor, commit_failure) -> None:
    monitor.quotes.clear()
    monitor.runner.run_once()
    monitor.now += timedelta(seconds=1)
    monitor.quotes.append(SimpleNamespace(
        asset_id="asset-1", as_of=monitor.now, last_price=Decimal("10"), payload={}
    ))
    monitor.runner.run_once()
    assert len(monitor.trigger_events) == 2

    monitor.now += timedelta(seconds=1)
    monitor.quotes.clear()
    if commit_failure:
        monitor.fail_commits = 1
        failed = monitor.runner.run_once()
        assert failed["status"] == "error"
        assert len(monitor.states.list_events("position-1")) == 2
        assert len(monitor.trigger_events) == 2

    result = monitor.runner.run_once()

    assert result["status"] == "completed"
    assert len(monitor.states.list_events("position-1")) == 3
    assert len(monitor.trigger_events) == 3
    monitor.now += timedelta(minutes=16)
    monitor.runner.run_once()
    assert len(monitor.trigger_events) == 3


def test_runner_source_is_publishable_and_runtime_data_stays_ignored() -> None:
    source = "scripts/runtime/run_position_monitor.py"
    generated = (
        "runtime/position_monitor/status.json",
        "scripts/runtime/status.json",
        "scripts/runtime/__pycache__/run_position_monitor.pyc",
    )
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin", "-z"],
        cwd=Path(runner_module.__file__).resolve().parents[2],
        input="\0".join((source, *generated)).encode() + b"\0",
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert set(result.stdout.decode().rstrip("\0").split("\0")) == set(generated)
