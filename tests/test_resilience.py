"""Unit tests for the resilience primitives (circuit breaker, retry, rate limit)."""

from __future__ import annotations

import time

import pytest

from ssis_migration.resilience import (
    CircuitBreaker,
    CircuitBreakerError,
    CircuitState,
    TokenBucket,
    backoff_delay,
    retry_call,
)


# ─── CircuitBreaker ───────────────────────────────────────────────────────────

def test_breaker_opens_after_threshold():
    cb = CircuitBreaker("t1", failure_threshold=3, recovery_timeout=10)
    cb.before_call()  # closed → allowed
    for _ in range(3):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN
    with pytest.raises(CircuitBreakerError):
        cb.before_call()


def test_breaker_success_resets_failures():
    cb = CircuitBreaker("t2", failure_threshold=3, recovery_timeout=10)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    assert cb.state == CircuitState.CLOSED  # counter was reset by the success


def test_breaker_half_open_then_close():
    cb = CircuitBreaker("t3", failure_threshold=2, recovery_timeout=0.05)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    time.sleep(0.06)
    assert cb.state == CircuitState.HALF_OPEN  # cooldown elapsed
    cb.before_call()  # probe allowed
    cb.record_success()
    assert cb.state == CircuitState.CLOSED


def test_breaker_half_open_failure_reopens():
    cb = CircuitBreaker("t4", failure_threshold=2, recovery_timeout=0.05)
    cb.record_failure()
    cb.record_failure()
    time.sleep(0.06)
    assert cb.state == CircuitState.HALF_OPEN
    cb.record_failure()  # probe failed
    assert cb.state == CircuitState.OPEN


def test_breaker_registry_shared():
    a = CircuitBreaker.get("shared", failure_threshold=2)
    b = CircuitBreaker.get("shared")
    assert a is b


# ─── retry_call ───────────────────────────────────────────────────────────────

def test_retry_succeeds_after_failures():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("transient")
        return "ok"

    out = retry_call(flaky, max_attempts=5, base_delay=0.0, jitter=False)
    assert out == "ok"
    assert calls["n"] == 3


def test_retry_exhausts_and_raises_last():
    def always_fail():
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError, match="nope"):
        retry_call(always_fail, max_attempts=3, base_delay=0.0, jitter=False)


def test_retry_non_retryable_propagates_immediately():
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise KeyError("fatal")

    with pytest.raises(KeyError):
        retry_call(boom, max_attempts=5, base_delay=0.0, retryable=(ValueError,))
    assert calls["n"] == 1  # not retried


# ─── backoff ──────────────────────────────────────────────────────────────────

def test_backoff_respects_cap():
    for attempt in range(1, 12):
        assert backoff_delay(attempt, base_delay=1.0, max_delay=5.0, jitter=False) <= 5.0


def test_backoff_jitter_within_bounds():
    for _ in range(50):
        d = backoff_delay(3, base_delay=1.0, max_delay=8.0, jitter=True)
        assert 0.0 <= d <= 8.0


# ─── TokenBucket ──────────────────────────────────────────────────────────────

def test_token_bucket_disabled_when_rate_zero():
    tb = TokenBucket(rate=0)
    assert tb.acquire() is True


def test_token_bucket_nonblocking_exhausts():
    tb = TokenBucket(rate=1, capacity=2)
    assert tb.acquire(block=False) is True
    assert tb.acquire(block=False) is True
    assert tb.acquire(block=False) is False  # bucket empty
