"""Unit tests for Copilot client hardening (pure parts — no network)."""

from __future__ import annotations

from ssis_migration.transform.llm.copilot_client import (
    CompletionRequest,
    Message,
    stitch_continuation,
)


# ─── stitch_continuation ──────────────────────────────────────────────────────

def test_stitch_no_overlap():
    assert stitch_continuation("abc\n", "def") == "abc\ndef"


def test_stitch_removes_repeated_tail():
    first = "df = spark.read.jdbc(\n    url=jdbc_url,\n    table='ord"
    second = "    table='orders',\n    properties=props,\n)"
    out = stitch_continuation(first, second)
    assert out.count("table='ord") == 1
    assert out.endswith(")")


def test_stitch_exact_line_repeat():
    first = "line1\nline2\n"
    second = "line2\nline3\n"
    assert stitch_continuation(first, second) == "line1\nline2\nline3\n"


def test_stitch_drops_leading_fence():
    first = "x = 1\ny = "
    second = "```python\ny = 2\n```"
    out = stitch_continuation(first, second)
    assert "```python" not in out
    assert out.startswith("x = 1")


def test_stitch_empty_continuation():
    assert stitch_continuation("abc", "") == "abc"


# ─── CompletionRequest budgets ────────────────────────────────────────────────

def test_request_uses_explicit_max_tokens():
    req = CompletionRequest(
        messages=[Message(role="user", content="hi")], max_tokens=9000,
    )
    assert req.to_dict(default_max_tokens=4096)["max_tokens"] == 9000


def test_request_falls_back_to_default():
    req = CompletionRequest(messages=[Message(role="user", content="hi")])
    assert req.to_dict(default_max_tokens=4096)["max_tokens"] == 4096


def test_request_model_fallback():
    req = CompletionRequest(messages=[Message(role="user", content="hi")], model="")
    assert req.to_dict(default_model="claude-haiku-4.5")["model"] == "claude-haiku-4.5"
