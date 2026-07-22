from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from finance_agent.runtime.gotdx_gateway_supervisor import (
    GotdxGatewayConfig,
    GotdxGatewayStartupError,
    GotdxGatewaySupervisor,
)


class _FakeProcess:
    def __init__(self, pid: int = 4321) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _config(tmp_path: Path) -> GotdxGatewayConfig:
    return GotdxGatewayConfig(
        command=("gotdx-gateway",),
        base_url="http://127.0.0.1:8790",
        log_file=tmp_path / "gateway.log",
        startup_timeout_seconds=0.05,
        health_interval_seconds=0,
        restart_backoff_seconds=0,
    )


def test_start_reuses_healthy_external_gateway(tmp_path: Path) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []
    supervisor = GotdxGatewaySupervisor(
        _config(tmp_path),
        healthcheck=lambda: True,
        popen_factory=lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    result = supervisor.start()

    assert result.reused_external is True
    assert result.started_process is False
    assert calls == []
    supervisor.stop()


def test_start_spawns_gateway_and_waits_for_health(tmp_path: Path) -> None:
    health_states = iter((False, False, True))
    process = _FakeProcess()
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def popen_factory(*args: Any, **kwargs: Any) -> _FakeProcess:
        calls.append((args, kwargs))
        return process

    supervisor = GotdxGatewaySupervisor(
        _config(tmp_path),
        healthcheck=lambda: next(health_states),
        popen_factory=popen_factory,
    )

    result = supervisor.start()

    assert result.started_process is True
    assert result.reused_external is False
    assert result.pid == 4321
    assert calls[0][0] == (("gotdx-gateway",),)
    assert (tmp_path / "gateway.log").exists()
    supervisor.stop()
    assert process.terminated is True


def test_start_terminates_process_when_health_never_recovers(tmp_path: Path) -> None:
    process = _FakeProcess()
    supervisor = GotdxGatewaySupervisor(
        _config(tmp_path),
        healthcheck=lambda: False,
        popen_factory=lambda *_args, **_kwargs: process,
    )

    with pytest.raises(GotdxGatewayStartupError, match="健康检查失败"):
        supervisor.start()

    assert process.terminated is True
    assert supervisor.process is None


def test_ensure_running_restarts_unhealthy_owned_gateway(tmp_path: Path) -> None:
    health_states = iter((False, True, False, True))
    processes = [_FakeProcess(1001), _FakeProcess(1002)]
    calls = 0

    def popen_factory(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        nonlocal calls
        process = processes[calls]
        calls += 1
        return process

    supervisor = GotdxGatewaySupervisor(
        _config(tmp_path),
        healthcheck=lambda: next(health_states),
        popen_factory=popen_factory,
    )
    supervisor.start()

    restarted = supervisor.ensure_running()

    assert restarted is True
    assert calls == 2
    assert processes[0].terminated is True
    assert supervisor.process is processes[1]
    supervisor.stop()


def test_stop_does_not_terminate_external_gateway(tmp_path: Path) -> None:
    supervisor = GotdxGatewaySupervisor(
        _config(tmp_path),
        healthcheck=lambda: True,
    )

    supervisor.start()
    supervisor.stop()

    assert supervisor.process is None


def test_healthcheck_exception_is_treated_as_unhealthy(tmp_path: Path) -> None:
    process = _FakeProcess()
    supervisor = GotdxGatewaySupervisor(
        _config(tmp_path),
        healthcheck=lambda: (_ for _ in ()).throw(RuntimeError("connection reset")),
        popen_factory=lambda *_args, **_kwargs: process,
    )

    with pytest.raises(GotdxGatewayStartupError, match="健康检查失败"):
        supervisor.start()

    assert process.terminated is True
