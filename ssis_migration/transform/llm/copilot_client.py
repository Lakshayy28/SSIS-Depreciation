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

logger = logging.getLogger(__name__)

_COPILOT_BASE_URL = "https://api.githubcopilot.com"
_CHAT_ENDPOINT = f"{_COPILOT_BASE_URL}/chat/completions"
_MAX_RETRIES = 3
_RETRY_BACKOFF = [1.0, 2.0, 4.0]

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
        from ssis_migration.config import cfg
        self._token = token or cfg.github_token
        self._model = model or cfg.copilot_model
        self._temperature = cfg.copilot_temperature
        self._max_tokens = cfg.copilot_max_tokens
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

        last_exc: Exception | None = None
        for attempt, backoff in enumerate(_RETRY_BACKOFF):
            try:
                with httpx.Client(timeout=120.0) as client:
                    resp = client.post(_CHAT_ENDPOINT, headers=headers, json=payload)

                if resp.status_code == 200:
                    resp_data = resp.json()
                    _write_log({
                        "event": "response",
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "status": resp.status_code,
                        "body": _mask_sensitive(resp_data),
                    })
                    return self._parse_response(resp_data)

                _write_log({
                    "event": "response_error",
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "status": resp.status_code,
                    "body": resp.text[:2000],
                })

                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("retry-after", backoff))
                    logger.warning("Rate limited; waiting %.1fs before retry %d", retry_after, attempt + 1)
                    time.sleep(retry_after)
                    continue

                if resp.status_code >= 500:
                    logger.warning("Server error %d on attempt %d", resp.status_code, attempt + 1)
                    time.sleep(backoff)
                    continue

                # 4xx errors other than 429 are not retryable
                resp.raise_for_status()

            except httpx.TimeoutException as exc:
                logger.warning("Timeout on attempt %d: %s", attempt + 1, exc)
                _write_log({"event": "timeout", "ts": datetime.now(timezone.utc).isoformat(), "attempt": attempt + 1})
                last_exc = exc
                time.sleep(backoff)

            except httpx.HTTPStatusError as exc:
                raise RuntimeError(f"GitHub Copilot API error: {exc.response.status_code} {exc.response.text}") from exc

        raise RuntimeError(
            f"GitHub Copilot API unreachable after {_MAX_RETRIES} retries"
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

    def simple_complete(self, system_prompt: str, user_message: str) -> str:
        """Convenience wrapper returning just the completion text."""
        req = CompletionRequest(
            messages=[
                Message(role="system", content=system_prompt),
                Message(role="user", content=user_message),
            ],
        )
        resp = self.complete(req)
        return resp.content
