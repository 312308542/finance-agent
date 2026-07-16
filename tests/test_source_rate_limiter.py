import threading
import time

import pytest

from finance_agent.data.source_rate_limiter import (
    AdaptiveSourceRateState,
    SourceRateLimiter,
    SourceRatePolicy,
    build_source_rate_limiter,
    default_source_rate_limiter,
)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_source_rate_limiter_enforces_min_interval_per_source() -> None:
    """同一数据源的连续请求应遵守最小间隔，避免瞬时打满上游接口。"""

    clock = _FakeClock()
    limiter = SourceRateLimiter(
        policies={
            "eastmoney": SourceRatePolicy(max_concurrency=1, min_interval_seconds=0.5),
        },
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    with limiter.acquire("eastmoney"):
        pass
    with limiter.acquire("eastmoney"):
        pass

    assert clock.sleeps == [0.5]


def test_source_rate_limiter_keeps_sources_independent() -> None:
    """不同数据源应独立限流，腾讯等待不应阻塞东方财富的时间窗口。"""

    clock = _FakeClock()
    limiter = SourceRateLimiter(
        policies={
            "tencent": SourceRatePolicy(max_concurrency=1, min_interval_seconds=0.5),
            "eastmoney": SourceRatePolicy(max_concurrency=1, min_interval_seconds=0.2),
        },
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    with limiter.acquire("tencent"):
        pass
    with limiter.acquire("eastmoney"):
        pass

    assert clock.sleeps == []


def test_build_source_rate_limiter_accepts_configured_policies() -> None:
    """限频策略应可由调度配置或运行配置覆盖，而不是只能写死在代码里。"""

    clock = _FakeClock()
    limiter = build_source_rate_limiter(
        {
            "stock_zh_a_hist_tx": {
                "max_concurrency": 1,
                "min_interval_seconds": 1.0,
            },
            "default": {
                "max_concurrency": 2,
                "min_interval_seconds": 0.25,
            },
        },
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    with limiter.acquire("stock_zh_a_hist_tx"):
        pass
    with limiter.acquire("stock_zh_a_hist_tx"):
        pass
    with limiter.acquire("unknown"):
        pass
    with limiter.acquire("unknown"):
        pass

    assert clock.sleeps == [1.0, 0.25]


def test_default_source_rate_limiter_splits_ashare_kline_sources() -> None:
    """默认限流器应把东财、腾讯和 AKShare 包装源拆成独立状态。"""

    limiter = default_source_rate_limiter()

    assert "eastmoney_kline" in limiter.policies
    assert "tencent_kline" in limiter.policies
    assert "stock_zh_a_hist" in limiter.policies
    assert limiter.policies["eastmoney_kline"].max_concurrency == 1
    assert limiter.policies["tencent_kline"].max_concurrency == 2


def test_adaptive_source_rate_state_backs_off_after_high_failure_rate() -> None:
    """短窗口失败率过高时，应降低有效并发并拉大请求间隔。"""

    state = AdaptiveSourceRateState(
        base_policy=SourceRatePolicy(max_concurrency=4, min_interval_seconds=0.5),
        failure_rate_threshold=0.25,
        cooldown_seconds=900,
        max_interval_seconds=8.0,
    )

    for _ in range(3):
        state.record_success(now=100.0)
    for _ in range(2):
        state.record_failure(now=100.0, error_message="curl: (56) Connection closed abruptly")

    effective = state.effective_policy(now=100.0)
    snapshot = state.snapshot(now=100.0)

    assert effective.max_concurrency == 2
    assert effective.min_interval_seconds == pytest.approx(8.0)
    assert snapshot["failure_count"] == 2
    assert snapshot["disconnect_count"] == 2
    assert snapshot["next_recover_at"] == pytest.approx(1000.0)


def test_source_rate_limiter_applies_hot_policy_concurrency_to_new_acquires() -> None:
    """运行中收紧源并发后，新的请求应等待已有请求释放，而不是沿用旧信号量容量。"""

    limiter = build_source_rate_limiter(
        {
            "eastmoney_kline": {
                "max_concurrency": 2,
                "min_interval_seconds": 0.0,
            }
        }
    )
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def hold_first_request() -> None:
        with limiter.acquire("eastmoney_kline"):
            first_entered.set()
            assert release_first.wait(timeout=2.0)

    first_thread = threading.Thread(target=hold_first_request)
    first_thread.start()
    assert first_entered.wait(timeout=2.0)

    second_thread: threading.Thread | None = None
    try:
        limiter.update_policies(
            {
                "eastmoney_kline": {
                    "max_concurrency": 1,
                    "min_interval_seconds": 0.0,
                }
            }
        )

        def enter_second_request() -> None:
            with limiter.acquire("eastmoney_kline"):
                second_entered.set()

        second_thread = threading.Thread(target=enter_second_request)
        second_thread.start()
        time.sleep(0.05)
        assert not second_entered.is_set()
    finally:
        release_first.set()
        first_thread.join(timeout=2.0)

    if second_thread is not None:
        second_thread.join(timeout=2.0)

    assert second_entered.is_set()


def test_source_rate_limiter_snapshot_reports_configured_and_effective_policy() -> None:
    """源状态快照应同时展示基础配置和退避后的有效策略，供任务监控页解释。"""

    clock = _FakeClock()
    limiter = build_source_rate_limiter(
        {
            "tencent_kline": {
                "max_concurrency": 4,
                "min_interval_seconds": 0.5,
                "timeout_seconds": 45,
                "backoff": {
                    "failure_rate_threshold": 0.1,
                    "cooldown_seconds": 900,
                    "max_interval_seconds": 8.0,
                },
            }
        },
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    limiter.record_failure("tencent_kline", "curl: (56) Connection closed abruptly")
    snapshot = limiter.source_snapshot("tencent_kline")

    assert snapshot["source_key"] == "tencent_kline"
    assert snapshot["configured_max_concurrency"] == 4
    assert snapshot["configured_min_interval_seconds"] == pytest.approx(0.5)
    assert snapshot["timeout_seconds"] == pytest.approx(45)
    assert snapshot["effective_max_concurrency"] == 2
    assert snapshot["effective_min_interval_seconds"] == pytest.approx(8.0)
    assert snapshot["failure_count"] == 1
