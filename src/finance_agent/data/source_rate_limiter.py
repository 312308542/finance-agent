"""按数据源隔离的轻量限流器。

采集层的并发是按标的展开的，但上游真实限制通常按 host 或接口维度生效。
这里提供一个进程内限流器，避免同一数据源在短时间内被多个 worker 同时打满。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SourceRatePolicy:
    """单个数据源的限流策略。"""

    max_concurrency: int = 1
    min_interval_seconds: float = 0.0
    timeout_seconds: float | None = None


@dataclass
class AdaptiveSourceRateState:
    """单个数据源的运行期退避状态。"""

    base_policy: SourceRatePolicy
    failure_rate_threshold: float = 0.1
    cooldown_seconds: float = 900.0
    max_interval_seconds: float = 10.0
    success_count: int = 0
    failure_count: int = 0
    timeout_count: int = 0
    disconnect_count: int = 0
    rate_limited_count: int = 0
    next_recover_at: float | None = None

    def record_success(self, *, now: float) -> None:
        """记录一次成功请求。"""

        self.success_count += 1
        if self.next_recover_at is not None and now >= self.next_recover_at:
            self.next_recover_at = None

    def record_failure(self, *, now: float, error_message: str | None = None) -> None:
        """记录一次失败请求，并在失败率过高时进入退避。"""

        self.failure_count += 1
        message = (error_message or "").lower()
        if "timeout" in message or "curl: (28)" in message:
            self.timeout_count += 1
        if "connection" in message or "curl: (56)" in message:
            self.disconnect_count += 1
        if "429" in message or "rate limit" in message or "too many" in message or "403" in message:
            self.rate_limited_count += 1
        if self.failure_rate() >= self.failure_rate_threshold:
            self.next_recover_at = now + self.cooldown_seconds

    def failure_rate(self) -> float:
        """计算当前窗口失败率。"""

        total = self.success_count + self.failure_count
        if total <= 0:
            return 0.0
        return self.failure_count / total

    def effective_policy(self, *, now: float) -> SourceRatePolicy:
        """返回考虑退避后的有效策略。"""

        if self.next_recover_at is None or now >= self.next_recover_at:
            return self.base_policy
        backoff_interval = max(
            self.base_policy.min_interval_seconds,
            self.max_interval_seconds,
        )
        return SourceRatePolicy(
            max_concurrency=max(1, self.base_policy.max_concurrency // 2),
            min_interval_seconds=backoff_interval,
            timeout_seconds=self.base_policy.timeout_seconds,
        )

    def snapshot(self, *, now: float) -> dict[str, Any]:
        """输出可写入 Redis 或进度日志的运行期状态。"""

        effective = self.effective_policy(now=now)
        return {
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "timeout_count": self.timeout_count,
            "disconnect_count": self.disconnect_count,
            "rate_limited_count": self.rate_limited_count,
            "failure_rate": self.failure_rate(),
            "effective_max_concurrency": effective.max_concurrency,
            "effective_min_interval_seconds": effective.min_interval_seconds,
            "next_recover_at": self.next_recover_at,
        }


class SourceRateLimiter:
    """按 source_key 维护独立信号量和最小请求间隔。"""

    def __init__(
        self,
        *,
        policies: Mapping[str, SourceRatePolicy] | None = None,
        adaptive_states: Mapping[str, AdaptiveSourceRateState] | None = None,
        default_policy: SourceRatePolicy | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.policies = dict(policies or {})
        self.adaptive_states = dict(adaptive_states or {})
        self.default_policy = default_policy or SourceRatePolicy()
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._next_allowed_at: dict[str, float] = {}
        self._active_counts: dict[str, int] = {}

    @contextmanager
    def acquire(self, source_key: str) -> Iterator[None]:
        """获取某个数据源的请求许可。"""

        key = source_key or "default"
        self._acquire_slot(key)
        try:
            policy = self._policy_for(key)
            self._wait_for_interval(key, policy)
            yield
        finally:
            self._release_slot(key)

    def _policy_for(self, source_key: str) -> SourceRatePolicy:
        adaptive_state = self.adaptive_states.get(source_key)
        if adaptive_state is not None:
            return adaptive_state.effective_policy(now=self.monotonic())
        return self.policies.get(source_key, self.default_policy)

    def update_policies(self, payload: Mapping[str, Any] | None) -> None:
        """热更新数据源限频策略，并保留已有自适应统计窗口。"""

        policies, adaptive_states, default_policy = parse_source_rate_policies(payload)
        with self._condition:
            merged_adaptive_states: dict[str, AdaptiveSourceRateState] = {}
            for source_key, new_state in adaptive_states.items():
                existing_state = self.adaptive_states.get(source_key)
                if existing_state is None:
                    merged_adaptive_states[source_key] = new_state
                    continue
                existing_state.base_policy = new_state.base_policy
                existing_state.failure_rate_threshold = new_state.failure_rate_threshold
                existing_state.cooldown_seconds = new_state.cooldown_seconds
                existing_state.max_interval_seconds = new_state.max_interval_seconds
                merged_adaptive_states[source_key] = existing_state
            self.policies = policies
            self.adaptive_states = merged_adaptive_states
            self.default_policy = default_policy
            self._condition.notify_all()

    def record_success(self, source_key: str) -> None:
        """记录数据源成功请求，用于自适应退避恢复。"""

        with self._condition:
            adaptive_state = self.adaptive_states.get(source_key)
            if adaptive_state is not None:
                adaptive_state.record_success(now=self.monotonic())
            self._condition.notify_all()

    def record_failure(self, source_key: str, error_message: str | None = None) -> None:
        """记录数据源失败请求，用于自适应退避。"""

        with self._condition:
            adaptive_state = self.adaptive_states.get(source_key)
            if adaptive_state is not None:
                adaptive_state.record_failure(now=self.monotonic(), error_message=error_message)
            self._condition.notify_all()

    def adaptive_snapshot(self, source_key: str) -> dict[str, Any] | None:
        """读取某个数据源的自适应退避快照。"""

        with self._condition:
            if source_key not in self.adaptive_states:
                return None
            return self.source_snapshot(source_key)

    def source_snapshot(self, source_key: str) -> dict[str, Any]:
        """读取某个数据源的配置策略、有效策略和运行期统计。"""

        key = source_key or "default"
        with self._condition:
            now = self.monotonic()
            adaptive_state = self.adaptive_states.get(key)
            if adaptive_state is not None:
                base_policy = adaptive_state.base_policy
                snapshot = adaptive_state.snapshot(now=now)
            else:
                base_policy = self.policies.get(key, self.default_policy)
                snapshot = {
                    "success_count": 0,
                    "failure_count": 0,
                    "timeout_count": 0,
                    "disconnect_count": 0,
                    "rate_limited_count": 0,
                    "failure_rate": 0.0,
                    "effective_max_concurrency": base_policy.max_concurrency,
                    "effective_min_interval_seconds": base_policy.min_interval_seconds,
                    "next_recover_at": None,
                }
            return {
                "source_key": key,
                "configured_max_concurrency": base_policy.max_concurrency,
                "configured_min_interval_seconds": base_policy.min_interval_seconds,
                "timeout_seconds": base_policy.timeout_seconds,
                "active_count": self._active_counts.get(key, 0),
                **snapshot,
            }

    def _acquire_slot(self, source_key: str) -> None:
        """根据当前有效策略进入源级并发门。"""

        with self._condition:
            while True:
                policy = self._policy_for(source_key)
                max_concurrency = max(int(policy.max_concurrency), 1)
                active_count = self._active_counts.get(source_key, 0)
                if active_count < max_concurrency:
                    self._active_counts[source_key] = active_count + 1
                    return
                self._condition.wait(timeout=0.1)

    def _release_slot(self, source_key: str) -> None:
        """释放源级并发门，并唤醒等待中的请求。"""

        with self._condition:
            active_count = max(self._active_counts.get(source_key, 0) - 1, 0)
            if active_count:
                self._active_counts[source_key] = active_count
            else:
                self._active_counts.pop(source_key, None)
            self._condition.notify_all()

    def _wait_for_interval(self, source_key: str, policy: SourceRatePolicy) -> None:
        interval = max(float(policy.min_interval_seconds), 0.0)
        if interval <= 0:
            return
        with self._lock:
            now = self.monotonic()
            wait_seconds = max(self._next_allowed_at.get(source_key, now) - now, 0.0)
            self._next_allowed_at[source_key] = now + wait_seconds + interval
        if wait_seconds > 0:
            self.sleep(wait_seconds)


def default_source_rate_limiter() -> SourceRateLimiter:
    """创建基础数据采集默认限流器。"""

    return build_source_rate_limiter(
        {
            "eastmoney_kline": {
                "max_concurrency": 1,
                "min_interval_seconds": 1.0,
                "backoff": {
                    "failure_rate_threshold": 0.1,
                    "cooldown_seconds": 900,
                    "max_interval_seconds": 12,
                },
            },
            "tencent_kline": {
                "max_concurrency": 2,
                "min_interval_seconds": 0.5,
                "backoff": {
                    "failure_rate_threshold": 0.1,
                    "cooldown_seconds": 900,
                    "max_interval_seconds": 10,
                },
            },
            "stock_zh_a_hist": {
                "max_concurrency": 1,
                "min_interval_seconds": 1.0,
                "backoff": {
                    "failure_rate_threshold": 0.1,
                    "cooldown_seconds": 900,
                    "max_interval_seconds": 12,
                },
            },
            "stock_zh_a_hist_tx": {
                "max_concurrency": 2,
                "min_interval_seconds": 0.5,
                "backoff": {"failure_rate_threshold": 0.1, "cooldown_seconds": 900},
            },
            "stock_news_em": {
                "max_concurrency": 2,
                "min_interval_seconds": 2.0,
                "backoff": {"failure_rate_threshold": 0.1, "cooldown_seconds": 900},
            },
            "stock_notice_report": {"max_concurrency": 1, "min_interval_seconds": 3.0},
            "ccxt_binance_fetch_ohlcv": {
                "max_concurrency": 3,
                "min_interval_seconds": 0.05,
            },
            "default": {"max_concurrency": 4, "min_interval_seconds": 0.0},
        },
    )


def build_source_rate_limiter(
    payload: Mapping[str, Any] | None,
    *,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> SourceRateLimiter:
    """根据配置字典创建限频器。"""

    policies, adaptive_states, default_policy = parse_source_rate_policies(payload)
    return SourceRateLimiter(
        policies=policies,
        adaptive_states=adaptive_states,
        default_policy=default_policy,
        monotonic=monotonic,
        sleep=sleep,
    )


def parse_source_rate_policies(
    payload: Mapping[str, Any] | None,
) -> tuple[dict[str, SourceRatePolicy], dict[str, AdaptiveSourceRateState], SourceRatePolicy]:
    """把原始限频配置解析为固定策略和自适应状态模板。"""

    policies: dict[str, SourceRatePolicy] = {}
    adaptive_states: dict[str, AdaptiveSourceRateState] = {}
    default_policy = SourceRatePolicy(max_concurrency=4, min_interval_seconds=0.0)
    for source_key, raw_policy in (payload or {}).items():
        if not isinstance(raw_policy, Mapping):
            continue
        policy = SourceRatePolicy(
            max_concurrency=max(int(raw_policy.get("max_concurrency", 1)), 1),
            min_interval_seconds=max(float(raw_policy.get("min_interval_seconds", 0.0)), 0.0),
            timeout_seconds=(
                float(raw_policy["timeout_seconds"])
                if raw_policy.get("timeout_seconds") is not None
                else None
            ),
        )
        if source_key == "default":
            default_policy = policy
            continue
        policies[source_key] = policy
        backoff = raw_policy.get("backoff")
        if isinstance(backoff, Mapping):
            adaptive_states[source_key] = AdaptiveSourceRateState(
                base_policy=policy,
                failure_rate_threshold=float(backoff.get("failure_rate_threshold", 0.1)),
                cooldown_seconds=float(backoff.get("cooldown_seconds", 900.0)),
                max_interval_seconds=float(backoff.get("max_interval_seconds", 10.0)),
            )
    return policies, adaptive_states, default_policy
