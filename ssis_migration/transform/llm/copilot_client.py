"""
GitHub Copilot Chat completions client.

Uses the GitHub Copilot Chat API endpoint with a GitHub token for
authentication. This is the only LLM provider used in this framework.

Credentials are loaded from (in priority order):
  1. Constructor arguments
  2. GITHUB_TOKEN / COPILOT_MODEL env vars
  3. .env file in the repo root (loaded via python-dotenv in ssis_migration.config)

The endpoint is the GitHub Copilot chat completions API:
  POST https://api.githubcopilot.com/chat/completions
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from ssis_migration.resilience import (
    CircuitBreaker,
    CircuitBreakerError,
    TokenBucket,
    backoff_delay,
)

logger = logging.getLogger(__name__)

_COPILOT_BASE_URL = "https://api.githubcopilot.com"
_CHAT_ENDPOINT = f"{_COPILOT_BASE_URL}/chat/completions"

# Process-wide circuit breaker name for the Copilot endpoint. Every CopilotClient
# instance (the pipeline builds several per package) shares this breaker, so a
# dead endpoint trips once and fast-fails the whole run instead of per-instance.
_BREAKER_NAME = "copilot_chat"
# Lazily built from cfg on first client construction (shared across instances).
_RATE_LIMITER: TokenBucket | None = None


class CopilotUnavailableError(RuntimeError):
    """Raised when the Copilot endpoint is unreachable or the breaker is open."""

# One JSON-L file per process inside the project so logs are easy to inspect.
# The directory is git-ignored — logs are never committed.
_LOG_DIR = Path(__file__).resolve().parents[3] / "copilot_chat_completions"
_LOG_PATH: Path = _LOG_DIR / f"copilot_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.log"


def _mask_sensitive(obj: Any) -> Any:
    """Recursively mask bearer tokens and secrets in dicts/lists/strings."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k.lower() in ("authorization", "github_token", "token"):
                out[k] = "Bearer ***MASKED***" if str(v).startswith("Bearer ") else "***MASKED***"
            else:
                out[k] = _mask_sensitive(v)
        return out
    if isinstance(obj, list):
        return [_mask_sensitive(i) for i in obj]
    if isinstance(obj, str):
        # Mask ghp_* / gho_* / github_pat_* tokens that leaked into strings
        return re.sub(r"gh[pos]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+", "***MASKED***", obj)
    return obj


def _write_log(entry: dict[str, Any]) -> None:
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # Never let logging break the pipeline


@dataclass
class Message:
    role: str       # "system" | "user" | "assistant"
    content: str


@dataclass
class CompletionRequest:
    messages: list[Message]
    model: str = ""                # Empty = read from cfg at call time
    temperature: float = -1.0      # -1 = read from cfg at call time
    max_tokens: int = -1           # -1 = read from cfg at call time
    top_p: float = 0.95
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, default_model: str = "gpt-4o-mini",
               default_temperature: float = 0.1,
               default_max_tokens: int = 4096) -> dict[str, Any]:
        return {
            "model": self.model or default_model,
            "messages": [{"role": m.role, "content": m.content} for m in self.messages],
            "temperature": self.temperature if self.temperature >= 0 else default_temperature,
            "max_tokens": self.max_tokens if self.max_tokens > 0 else default_max_tokens,
            "top_p": self.top_p,
            **self.extra,
        }


@dataclass
class CompletionResponse:
    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str


class CopilotClient:
    """
    Thin HTTP client for the GitHub Copilot Chat completions endpoint.

    Authentication: Bearer token from (priority order):
      1. constructor `token` arg
      2. GITHUB_TOKEN env var / .env file

    Model and tuning params come from (priority order):
      1. constructor `model` arg
      2. COPILOT_MODEL / COPILOT_TEMPERATURE / COPILOT_MAX_TOKENS env vars / .env
    """

    def __init__(self, token: str | None = None, model: str | None = None) -> None:
        global _RATE_LIMITER
        from ssis_migration.config import cfg
        self._token = token or cfg.github_token
        self._model = model or cfg.copilot_model
        self._temperature = cfg.copilot_temperature
        self._max_tokens = cfg.copilot_max_tokens

        # ── NFR knobs ──────────────────────────────────────────────────────────
        self._timeout = cfg.copilot_request_timeout
        self._max_retries = max(1, cfg.copilot_max_retries)
        self._breaker = CircuitBreaker.get(
            _BREAKER_NAME,
            failure_threshold=cfg.circuit_breaker_threshold,
            recovery_timeout=cfg.circuit_breaker_cooldown,
        )
        if _RATE_LIMITER is None and cfg.copilot_rate_limit_per_min > 0:
            # Convert requests/minute → tokens/second; allow a small burst.
            rate = cfg.copilot_rate_limit_per_min / 60.0
            _RATE_LIMITER = TokenBucket(rate=rate, capacity=max(1.0, rate * 5))
        self._rate_limiter = _RATE_LIMITER

        logger.info("Copilot request/response log: %s", _LOG_PATH)
        if not self._token:
            logger.warning(
                "GITHUB_TOKEN not set — LLM calls will fail. "
                "Add it to .env or export GITHUB_TOKEN=<token> before running LLM phases."
            )

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Send a chat completion request and return the response."""
        if not self._token:
            raise RuntimeError(
                "GITHUB_TOKEN environment variable is required for LLM phases. "
                "Export GITHUB_TOKEN=<your-github-token> and retry."
            )

        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Editor-Version": "vscode/1.90.0",          # Required by Copilot API
            "Editor-Plugin-Version": "copilot/1.0.0",
            "Openai-Intent": "conversation-edits",
        }

        payload = request.to_dict(
            default_model=self._model,
            default_temperature=self._temperature,
            default_max_tokens=self._max_tokens,
        )

        _write_log({
            "event": "request",
            "ts": datetime.now(timezone.utc).isoformat(),
            "endpoint": _CHAT_ENDPOINT,
            "headers": _mask_sensitive(dict(headers)),
            "payload": _mask_sensitive(payload),
        })

        # NFR 1 — client-side rate limiting (no-op unless COPILOT_RATE_LIMIT_PER_MIN > 0)
        if self._rate_limiter is not None:
            self._rate_limiter.acquire()

        # NFR 2 — circuit breaker: fast-fail if the endpoint is presumed dead.
        try:
            self._breaker.before_call()
        except CircuitBreakerError as exc:
            _write_log({
                "event": "circuit_open",
                "ts": datetime.now(timezone.utc).isoformat(),
                "detail": str(exc),
            })
            raise CopilotUnavailableError(str(exc)) from exc

        # NFR 3 — bounded retry with jittered exponential backoff.
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            delay = backoff_delay(attempt, base_delay=1.0, max_delay=30.0)
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    resp = client.post(_CHAT_ENDPOINT, headers=headers, json=payload)

                if resp.status_code == 200:
                    resp_data = resp.json()
                    _write_log({
                        "event": "response",
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "status": resp.status_code,
                        "body": _mask_sensitive(resp_data),
                    })
                    self._breaker.record_success()
                    return self._parse_response(resp_data)

                _write_log({
                    "event": "response_error",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "status": resp.status_code,
                    "attempt": attempt,
                    "body": resp.text[:2000],
                })

                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("retry-after", delay))
                    logger.warning("Rate limited; waiting %.1fs before retry %d", retry_after, attempt)
                    self._breaker.record_failure()
                    if attempt < self._max_retries:
                        time.sleep(retry_after)
                        continue
                    raise CopilotUnavailableError("Copilot API rate limit exhausted")

                if resp.status_code >= 500:
                    logger.warning("Server error %d on attempt %d", resp.status_code, attempt)
                    self._breaker.record_failure()
                    if attempt < self._max_retries:
                        time.sleep(delay)
                        continue
                    raise CopilotUnavailableError(
                        f"Copilot API server error {resp.status_code} after {attempt} attempts"
                    )

                # 4xx other than 429 are caller/config errors — not retryable and
                # NOT a breaker failure (a bad model id shouldn't trip the breaker).
                raise RuntimeError(
                    f"GitHub Copilot API error: {resp.status_code} {resp.text[:500]}"
                )

            except httpx.TimeoutException as exc:
                logger.warning("Timeout on attempt %d: %s", attempt, exc)
                _write_log({"event": "timeout", "ts": datetime.now(timezone.utc).isoformat(), "attempt": attempt})
                self._breaker.record_failure()
                last_exc = exc
                if attempt < self._max_retries:
                    time.sleep(delay)
                    continue
            except httpx.TransportError as exc:
                logger.warning("Transport error on attempt %d: %s", attempt, exc)
                _write_log({"event": "transport_error", "ts": datetime.now(timezone.utc).isoformat(),
                            "attempt": attempt, "detail": str(exc)})
                self._breaker.record_failure()
                last_exc = exc
                if attempt < self._max_retries:
                    time.sleep(delay)
                    continue

        raise CopilotUnavailableError(
            f"GitHub Copilot API unreachable after {self._max_retries} attempts"
        ) from last_exc

    def _parse_response(self, data: dict[str, Any]) -> CompletionResponse:
        choices = data.get("choices", [])
        if not choices:
            raise ValueError("Empty choices in Copilot response")
        message = choices[0].get("message", {})
        usage = data.get("usage", {})
        return CompletionResponse(
            content=message.get("content", ""),
            model=data.get("model", self._model),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            finish_reason=choices[0].get("finish_reason", "stop"),
        )

    def simple_complete(self, system_prompt: str, user_message: str, model: str | None = None) -> str:
        """Convenience wrapper returning just the completion text.

        model: override the instance model for this single call (used so the
               ReviewAgent can use a stronger model than the generator).
        """
        req = CompletionRequest(
            messages=[
                Message(role="system", content=system_prompt),
                Message(role="user", content=user_message),
            ],
            model=model or "",  # empty string → use instance default in to_dict()
        )
        resp = self.complete(req)
        return resp.content
