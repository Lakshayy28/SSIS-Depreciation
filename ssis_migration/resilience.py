"""
Non-functional reliability primitives for the LLM-backed pipeline.

The migration pipeline makes many remote calls to the GitHub Copilot Chat API
(one or more per component, plus reviewer and functional-validator passes, across
potentially hundreds of packages in a batch run).  A single bad token, a regional
outage, or an aggressive rate limit must not turn into hundreds of slow, doomed
retries.  This module provides three composable primitives:

  CircuitBreaker  — after N consecutive failures, "open" and fast-fail every
                    subsequent call for a cooldown window instead of hammering a
                    dead endpoint.  After cooldown it goes "half-open" and lets a
                    single probe through; success closes it, failure re-opens it.

  retry_call      — bounded exponential backoff with full jitter, over a
                    caller-supplied set of retryable exceptions.

  TokenBucket     — simple client-side rate limiter so we stay under the API's
                    requests-per-minute budget without relying on 429s.

All three are thread-safe.  Breakers are registered by name so every component
of the pipeline that talks to the same endpoint shares one breaker, regardless
of how many client objects get constructed.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable, Iterable
from enum import Enum
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ─── Circuit breaker ──────────────────────────────────────────────────────────

class CircuitState(str, Enum):
    CLOSED = "closed"        # normal operation
    OPEN = "open"            # failing fast, endpoint presumed unhealthy
    HALF_OPEN = "half_open"  # cooldown elapsed, allowing a single probe


class CircuitBreakerError(RuntimeError):
    """Raised when a call is rejected because the breaker is open."""


class CircuitBreaker:
    """
    Trips after ``failure_threshold`` consecutive failures, then rejects calls
    for ``recovery_timeout`` seconds before allowing a single half-open probe.

    Usage::

        breaker = CircuitBreaker.get("copilot")
        breaker.before_call()          # raises CircuitBreakerError if open
        try:
            result = do_work()
        except Exception:
            breaker.record_failure()
            raise
        else:
            breaker.record_success()
    """

    _registry: dict[str, "CircuitBreaker"] = {}
    _registry_lock = threading.Lock()

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
    ) -> None:
        self.name = name
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_timeout = max(0.0, recovery_timeout)

        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._lock = threading.Lock()

    # -- registry ---------------------------------------------------------------

    @classmethod
    def get(
        cls,
        name: str = "default",
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
    ) -> "CircuitBreaker":
        """Return the process-wide breaker for ``name``, creating it on first use."""
        with cls._registry_lock:
            breaker = cls._registry.get(name)
            if breaker is None:
                breaker = cls(name, failure_threshold, recovery_timeout)
                cls._registry[name] = breaker
            return breaker

    @classmethod
    def reset_all(cls) -> None:
        """Reset every registered breaker (used by tests)."""
        with cls._registry_lock:
            for b in cls._registry.values():
                b.reset()

    # -- state ------------------------------------------------------------------

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._current_state_locked()

    def _current_state_locked(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if (time.monotonic() - self._opened_at) >= self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                logger.info("Circuit '%s' → HALF_OPEN (cooldown elapsed)", self.name)
        return self._state

    def before_call(self) -> None:
        """Raise ``CircuitBreakerError`` if the breaker is currently open."""
        with self._lock:
            state = self._current_state_locked()
            if state == CircuitState.OPEN:
                remaining = self.recovery_timeout - (time.monotonic() - self._opened_at)
                raise CircuitBreakerError(
                    f"Circuit '{self.name}' is OPEN after {self._consecutive_failures} "
                    f"consecutive failures; retry in {max(0.0, remaining):.0f}s"
                )

    def record_success(self) -> None:
        with self._lock:
            if self._state != CircuitState.CLOSED:
                logger.info("Circuit '%s' → CLOSED (probe succeeded)", self.name)
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if (
                self._state == CircuitState.HALF_OPEN
                or self._consecutive_failures >= self.failure_threshold
            ):
                if self._state != CircuitState.OPEN:
                    logger.warning(
                        "Circuit '%s' → OPEN (%d consecutive failures)",
                        self.name, self._consecutive_failures,
                    )
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()

    def reset(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._opened_at = 0.0

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "name": self.name,
                "state": self._current_state_locked().value,
                "consecutive_failures": self._consecutive_failures,
                "failure_threshold": self.failure_threshold,
            }


# ─── Backoff + retry ──────────────────────────────────────────────────────────

def backoff_delay(
    attempt: int,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: bool = True,
) -> float:
    """
    Exponential backoff with optional full jitter.

    attempt is 1-based: attempt 1 → ~base_delay, attempt 2 → ~2·base_delay, …
    Full jitter (random between 0 and the computed cap) spreads retries so a
    fleet of workers doesn't synchronise into a thundering herd.
    """
    cap = min(max_delay, base_delay * (2 ** max(0, attempt - 1)))
    if jitter:
        return random.uniform(0.0, cap)
    return cap


def retry_call(
    fn: Callable[[], T],
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: bool = True,
    retryable: Iterable[type[BaseException]] = (Exception,),
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """
    Call ``fn`` up to ``max_attempts`` times, backing off between attempts.

    Only exceptions in ``retryable`` trigger a retry; anything else propagates
    immediately.  The last exception is re-raised if all attempts fail.
    ``on_retry(attempt, exc, sleep_seconds)`` is invoked before each sleep.
    """
    retryable = tuple(retryable)
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except retryable as exc:  # type: ignore[misc]
            last_exc = exc
            if attempt >= max_attempts:
                break
            delay = backoff_delay(attempt, base_delay, max_delay, jitter)
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            else:
                logger.warning(
                    "retry_call: attempt %d/%d failed (%s); sleeping %.1fs",
                    attempt, max_attempts, exc, delay,
                )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


# ─── Rate limiter ─────────────────────────────────────────────────────────────

class TokenBucket:
    """
    Thread-safe token-bucket rate limiter.

    Refills at ``rate`` tokens/second up to ``capacity``.  ``acquire`` blocks
    until a token is available (or returns False if ``block=False``).
    A ``rate`` of 0 disables limiting entirely.
    """

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        self.rate = max(0.0, rate)
        self.capacity = capacity if capacity is not None else max(1.0, rate)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0, block: bool = True) -> bool:
        if self.rate <= 0:
            return True
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._last) * self.rate
                )
                self._last = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return True
                if not block:
                    return False
                deficit = tokens - self._tokens
                wait = deficit / self.rate
            time.sleep(min(wait, 1.0))
