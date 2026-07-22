"""gotdx 网关进程监管器。

这个模块只负责启动、探活、重启和退出 Go 网关进程，不参与行情解析。
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import requests


class _ProcessLike(Protocol):
    pid: int | None

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


class GotdxGatewayStartupError(RuntimeError):
    """网关启动或健康检查失败。"""


@dataclass(frozen=True)
class GotdxGatewayConfig:
    """gotdx 网关监管配置。"""

    command: tuple[str, ...]
    base_url: str = "http://127.0.0.1:8790"
    health_path: str = "/healthz"
    working_dir: Path | None = None
    log_file: Path | None = None
    startup_timeout_seconds: float = 10.0
    health_interval_seconds: float = 0.25
    monitor_interval_seconds: float = 5.0
    restart_backoff_seconds: float = 1.0
    stop_timeout_seconds: float = 5.0
    max_restart_attempts: int = 3
    environment: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_environment(
        cls,
        *,
        root_dir: Path,
        command: Sequence[str] | None = None,
        base_url: str | None = None,
        health_path: str | None = None,
        working_dir: Path | None = None,
        log_file: Path | None = None,
        startup_timeout_seconds: float | None = None,
        health_interval_seconds: float | None = None,
        monitor_interval_seconds: float | None = None,
        restart_backoff_seconds: float | None = None,
        stop_timeout_seconds: float | None = None,
        max_restart_attempts: int | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> GotdxGatewayConfig:
        """从环境和可选参数构建网关配置。"""

        resolved_command = tuple(command or _discover_gateway_command(root_dir))
        return cls(
            command=resolved_command,
            base_url=(base_url or os.getenv("FINANCE_AGENT_GOTDX_GATEWAY_URL") or "http://127.0.0.1:8790").rstrip("/"),
            health_path=health_path or os.getenv("FINANCE_AGENT_GOTDX_GATEWAY_HEALTH_PATH") or "/healthz",
            working_dir=working_dir,
            log_file=log_file,
            startup_timeout_seconds=_float_env_or_default(
                startup_timeout_seconds,
                "FINANCE_AGENT_GOTDX_GATEWAY_STARTUP_TIMEOUT_SECONDS",
                10.0,
            ),
            health_interval_seconds=_float_env_or_default(
                health_interval_seconds,
                "FINANCE_AGENT_GOTDX_GATEWAY_HEALTH_INTERVAL_SECONDS",
                0.25,
            ),
            monitor_interval_seconds=_float_env_or_default(
                monitor_interval_seconds,
                "FINANCE_AGENT_GOTDX_GATEWAY_MONITOR_INTERVAL_SECONDS",
                5.0,
            ),
            restart_backoff_seconds=_float_env_or_default(
                restart_backoff_seconds,
                "FINANCE_AGENT_GOTDX_GATEWAY_RESTART_BACKOFF_SECONDS",
                1.0,
            ),
            stop_timeout_seconds=_float_env_or_default(
                stop_timeout_seconds,
                "FINANCE_AGENT_GOTDX_GATEWAY_STOP_TIMEOUT_SECONDS",
                5.0,
            ),
            max_restart_attempts=_int_env_or_default(
                max_restart_attempts,
                "FINANCE_AGENT_GOTDX_GATEWAY_MAX_RESTARTS",
                3,
            ),
            environment=dict(environment or {}),
        )


@dataclass(frozen=True)
class GotdxGatewayStartResult:
    """启动结果。"""

    started_process: bool
    reused_external: bool
    pid: int | None = None


class GotdxGatewaySupervisor(AbstractContextManager["GotdxGatewaySupervisor"]):
    """监管 gotdx 网关生命周期。"""

    def __init__(
        self,
        config: GotdxGatewayConfig,
        *,
        healthcheck: Callable[[], bool] | None = None,
        popen_factory: Callable[..., _ProcessLike] | None = None,
        sleep_func: Callable[[float], None] = time.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        if not config.command:
            raise ValueError("gotdx 网关命令不能为空")
        self.config = config
        self._healthcheck = healthcheck or self._default_healthcheck
        self._popen_factory = popen_factory or subprocess.Popen
        self._sleep = sleep_func
        self._logger = logger or logging.getLogger(__name__)
        self._lock = threading.RLock()
        self._process: _ProcessLike | None = None
        self._log_handle: Any = None
        self._stop_event = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._owns_process = False

    @property
    def process(self) -> _ProcessLike | None:
        return self._process

    def __enter__(self) -> GotdxGatewaySupervisor:
        self.start()
        self.start_monitor()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        del exc_type, exc, tb
        self.stop()
        return False

    def start(self) -> GotdxGatewayStartResult:
        """如果没有可用网关则启动；若已有健康网关则直接复用。"""

        with self._lock:
            if self._is_process_alive():
                if self._is_healthy():
                    self._owns_process = False
                    return GotdxGatewayStartResult(False, True, self._process.pid if self._process else None)
                self._terminate_owned_process_locked()
            if self._is_healthy():
                self._owns_process = False
                return GotdxGatewayStartResult(False, True, None)
            return self._spawn_until_healthy_locked()

    def ensure_running(self) -> bool:
        """检查网关是否可用；不可用时尝试重启。"""

        with self._lock:
            if self._is_healthy():
                return False
            if self._is_process_alive() or self._process is not None:
                self._terminate_owned_process_locked()
            self._spawn_until_healthy_locked()
            return True

    def start_monitor(self) -> None:
        """启动守护线程，周期性补活网关。"""

        with self._lock:
            if self._monitor_thread and self._monitor_thread.is_alive():
                return
            self._stop_event.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                name="gotdx-gateway-monitor",
                daemon=True,
            )
            self._monitor_thread.start()

    def stop_monitor(self) -> None:
        """停止守护线程。"""

        self._stop_event.set()
        thread = self._monitor_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=self.config.stop_timeout_seconds)
        self._monitor_thread = None

    def stop(self) -> None:
        """优雅停止受管网关并关闭日志。"""

        self.stop_monitor()
        with self._lock:
            self._terminate_owned_process_locked()
            self._close_log_handle_locked()

    def _monitor_loop(self) -> None:
        while not self._stop_event.wait(self.config.monitor_interval_seconds):
            try:
                self.ensure_running()
            except Exception:  # noqa: BLE001 - 监管线程必须吞掉异常并记录
                self._logger.exception("gotdx 网关监管循环失败")

    def _spawn_until_healthy_locked(self) -> GotdxGatewayStartResult:
        attempts = max(1, self.config.max_restart_attempts)
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                process = self._spawn_process_locked()
            except Exception as exc:  # noqa: BLE001 - 将 Popen/日志错误统一纳入启动失败
                last_error = exc if isinstance(exc, Exception) else RuntimeError(str(exc))
                if attempt + 1 < attempts and self.config.restart_backoff_seconds > 0:
                    self._sleep(self.config.restart_backoff_seconds * (2**attempt))
                continue
            self._process = process
            self._owns_process = True
            try:
                self._wait_until_healthy_locked()
                return GotdxGatewayStartResult(True, False, getattr(process, "pid", None))
            except Exception as exc:  # noqa: BLE001 - 启动阶段统一转成可读错误
                last_error = exc if isinstance(exc, Exception) else RuntimeError(str(exc))
                self._terminate_owned_process_locked()
                if attempt + 1 < attempts and self.config.restart_backoff_seconds > 0:
                    self._sleep(self.config.restart_backoff_seconds * (2**attempt))
        if last_error is None:
            last_error = GotdxGatewayStartupError("gotdx 网关启动失败")
        raise GotdxGatewayStartupError("健康检查失败，gotdx 网关未能在超时时间内就绪") from last_error

    def _spawn_process_locked(self) -> _ProcessLike:
        self._ensure_log_handle_locked()
        env = os.environ.copy()
        env.update(self.config.environment)
        kwargs: dict[str, Any] = {
            "cwd": str(self.config.working_dir) if self.config.working_dir else None,
            "env": env,
            "stdout": self._log_handle,
            "stderr": self._log_handle,
        }
        kwargs = {key: value for key, value in kwargs.items() if value is not None}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        self._logger.info("启动 gotdx 网关: %s", " ".join(self.config.command))
        return self._popen_factory(tuple(self.config.command), **kwargs)

    def _wait_until_healthy_locked(self) -> None:
        deadline = time.monotonic() + self.config.startup_timeout_seconds
        while time.monotonic() <= deadline:
            if self._is_healthy():
                return
            if self._process is not None and self._process.poll() is not None:
                raise GotdxGatewayStartupError(f"gotdx 网关提前退出，exitcode={self._process.poll()}")
            if self.config.health_interval_seconds > 0:
                self._sleep(self.config.health_interval_seconds)
        raise GotdxGatewayStartupError("gotdx 网关健康检查超时")

    def _terminate_owned_process_locked(self) -> None:
        if self._process is None or not self._owns_process:
            self._process = None
            self._owns_process = False
            return
        process = self._process
        self._process = None
        self._owns_process = False
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=self.config.stop_timeout_seconds)
                except Exception:  # noqa: BLE001 - 终止失败后强制 kill
                    process.kill()
                    process.wait(timeout=self.config.stop_timeout_seconds)
        finally:
            self._close_log_handle_locked()

    def _close_log_handle_locked(self) -> None:
        handle = self._log_handle
        self._log_handle = None
        if handle is not None:
            try:
                handle.close()
            except Exception:  # noqa: BLE001 - 关闭日志失败不应阻塞退出
                self._logger.exception("关闭 gotdx 网关日志文件失败")

    def _ensure_log_handle_locked(self) -> None:
        if self._log_handle is not None:
            return
        if self.config.log_file is None:
            return
        config_log_file = self.config.log_file
        config_log_file.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = config_log_file.open("a", encoding="utf-8")

    def _is_process_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _default_healthcheck(self) -> bool:
        url = f"{self.config.base_url.rstrip('/')}{self.config.health_path}"
        try:
            response = requests.get(url, timeout=max(0.5, self.config.health_interval_seconds or 0.5))
        except requests.RequestException:
            return False
        return 200 <= int(getattr(response, "status_code", 0) or 0) < 300

    def _is_healthy(self) -> bool:
        try:
            return bool(self._healthcheck())
        except Exception:  # noqa: BLE001 - 探活异常等价于当前不可用
            self._logger.debug("gotdx 网关健康检查异常", exc_info=True)
            return False


def _discover_gateway_command(root_dir: Path) -> tuple[str, ...]:
    env_command = os.getenv("FINANCE_AGENT_GOTDX_GATEWAY_COMMAND")
    if env_command:
        return tuple(token for token in shlex.split(env_command, posix=os.name != "nt") if token)
    candidate_names = (
        ["gotdx-gateway.exe", "gotdx-gateway"]
        if os.name == "nt"
        else ["gotdx-gateway", "gotdx-gateway.exe"]
    )
    candidate_dirs = [root_dir / "prototypes" / "gotdx-gateway", root_dir / "build", root_dir]
    for directory in candidate_dirs:
        for name in candidate_names:
            candidate = directory / name
            if candidate.exists():
                return (str(candidate),)
    raise GotdxGatewayStartupError(
        "未找到 gotdx 网关可执行文件，请先构建 prototypes/gotdx-gateway 或显式传入 --gotdx-gateway-command"
    )


def _float_env_or_default(value: float | None, name: str, fallback: float) -> float:
    if value is not None:
        return float(value)
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return fallback
    try:
        return float(raw)
    except ValueError:
        return fallback


def _int_env_or_default(value: int | None, name: str, fallback: int) -> int:
    if value is not None:
        return int(value)
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return fallback
    try:
        parsed = int(raw)
    except ValueError:
        return fallback
    return parsed if parsed > 0 else fallback
