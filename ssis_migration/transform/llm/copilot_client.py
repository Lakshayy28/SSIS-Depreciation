"""
GitHub Copilot Chat completions client.

Uses the GitHub Copilot Chat API endpoint with a GitHub token for
authentication. This is the only LLM provider used in this framework.

Environment variable: GITHUB_TOKEN (required for LLM phases)

The endpoint is the GitHub Copilot chat completions API:
  POST https://api.githubcopilot.com/chat/completions
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_COPILOT_BASE_URL = "https://api.githubcopilot.com"
_CHAT_ENDPOINT = f"{_COPILOT_BASE_URL}/chat/completions"
_DEFAULT_MODEL = "gpt-4o"
_MAX_RETRIES = 3
_RETRY_BACKOFF = [1.0, 2.0, 4.0]


@dataclass
class Message:
    role: str       # "system" | "user" | "assistant"
    content: str


@dataclass
class CompletionRequest:
    messages: list[Message]
    model: str = _DEFAULT_MODEL
    temperature: float = 0.1       # Low temperature for deterministic code generation
    max_tokens: int = 4096
    top_p: float = 0.95
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in self.messages],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
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

    Authentication: Bearer token via GITHUB_TOKEN env var.
    Handles retries with exponential backoff for rate-limit and server errors.
    """

    def __init__(self, token: str | None = None, model: str = _DEFAULT_MODEL) -> None:
        self._token = token or os.environ.get("GITHUB_TOKEN", "")
        self._model = model
        if not self._token:
            logger.warning(
                "GITHUB_TOKEN not set — LLM calls will fail. "
                "Set GITHUB_TOKEN before running LLM-augmented phases."
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

        payload = request.to_dict()

        last_exc: Exception | None = None
        for attempt, backoff in enumerate(_RETRY_BACKOFF):
            try:
                with httpx.Client(timeout=120.0) as client:
                    resp = client.post(_CHAT_ENDPOINT, headers=headers, json=payload)

                if resp.status_code == 200:
                    return self._parse_response(resp.json())

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
            model=self._model,
            messages=[
                Message(role="system", content=system_prompt),
                Message(role="user", content=user_message),
            ],
        )
        resp = self.complete(req)
        return resp.content
