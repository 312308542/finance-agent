from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import scripts.runtime.run_position_monitor as runner_module


def test_runner只在quote_snapshot变化时调用监控服务(tmp_path: Path, monkeypatch) -> None:
    """同一行情快照重复轮询时不得重复计算持仓动作。"""

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
            return SimpleNamespace(owner_id=owner_id, evaluated_at=as_of, actions=(), error_count=0)

    runner = runner_module.PositionMonitorRunner(
        session_factory=lambda: FakeSession(),
        owner_id="owner-1",
        service_factory=FakeService,
        now_factory=lambda: datetime(2026, 9, 5, tzinfo=UTC),
    )
    first = runner.run_once(status_file=tmp_path / "status.json")
    second = runner.run_once(status_file=tmp_path / "status.json")

    assert first["status"] == "completed"
    assert second["status"] == "skipped"
    assert calls == ["owner-1"]


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
            return SimpleNamespace(actions=actions, error_count=0)

    class FakeTrigger:
        def __init__(self, _session):
            pass

        def persist_position_actions(self, received, *, as_of):
            calls.append((tuple(received), {"as_of": as_of}))

    runner = runner_module.PositionMonitorRunner(
        session_factory=lambda: FakeSession(),
        owner_id="owner-1",
        service_factory=FakeService,
        trigger_factory=FakeTrigger,
        now_factory=lambda: datetime(2026, 9, 5, tzinfo=UTC),
    )

    runner.run_once()

    assert calls == [(actions, {"as_of": datetime(2026, 9, 5, tzinfo=UTC)})]


class _SessionScope:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self.session

    def __exit__(self, *_):
        return False
