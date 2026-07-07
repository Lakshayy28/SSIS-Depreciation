"""
Syntax repair — the EDITING validator for generated code.

Philosophy: this framework has two kinds of "reviewer" and they must never be
confused.

  Semantic reviewer  (agents.ReviewAgent)   — judges correctness, NEVER edits;
                                              its issues go back to the
                                              generator for regeneration.
  Syntax validator   (this module)          — makes sure code COMPILES and has
                                              EDIT AUTHORITY, because compile
                                              failures are mechanical damage
                                              (fences, whitespace, truncation),
                                              not design choices.

Repair runs in escalating stages, cheapest first:

  stage 1  extract_code       — pull the code out of fences / surrounding prose
  stage 2  normalize_code     — unicode quotes/dashes, BOM, tabs, dedent, EOLs
  stage 3  LLM syntax fixer   — bounded edit loop with the exact compiler error

Every artifact passes through ``ensure_compilable`` at three altitudes:
per-chunk, per-item (assembled snippet), and whole-file (final module).
"""

from __future__ import annotations

import ast
import logging
import re
import textwrap
from dataclasses import dataclass, field

from ssis_migration.transform.llm.prompts import (
    SYNTAX_FIXER_SYSTEM,
    SYNTAX_FIXER_USER,
    python_compat_note,
)

logger = logging.getLogger(__name__)

# Characters LLMs substitute that break the tokenizer
_UNICODE_FIXES = {
    "“": '"', "”": '"',       # smart double quotes
    "‘": "'", "’": "'",       # smart single quotes
    "–": "-", "—": "-",       # en/em dashes
    " ": " ",                        # non-breaking space
    "​": "", "‌": "", "‍": "",  # zero-width
    "﻿": "",                         # BOM
}

_FENCE_RE = re.compile(
    r"```(?:python|py|pyspark|sql)?[ \t]*\n(.*?)(?:\n)?```",
    re.DOTALL | re.IGNORECASE,
)

# Prose lines models prepend despite instructions
_PROSE_LINE_RE = re.compile(
    r"^(here'?s?\b|sure[,!]|certainly|below is|the following|this (code|snippet)|"
    r"i('| ha)ve\b|note:).{0,200}$",
    re.IGNORECASE,
)

_CODE_LINE_RE = re.compile(
    r"[=(\[{]|^\s*(def|class|import|from|return|if|for|while|with|try|raise|@|#)\b"
)


# ─── Stage 1: extraction ──────────────────────────────────────────────────────

def extract_code(text: str) -> str:
    """
    Pull the actual code out of an LLM response.

    If fenced blocks exist, the LARGEST block is taken (models sometimes emit a
    short "usage example" fence next to the real one). Otherwise leading prose
    lines are stripped until the content looks like code.
    """
    if not text:
        return ""
    blocks = _FENCE_RE.findall(text)
    if blocks:
        return max(blocks, key=len)

    # No complete fence — drop a dangling opening/closing fence line if present.
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("```")]

    # Strip leading prose — but NEVER a line that looks like code.
    start = 0
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if not stripped:
            start = i + 1
            continue
        if _PROSE_LINE_RE.match(stripped) and not _CODE_LINE_RE.search(stripped):
            start = i + 1
            continue
        break
    return "\n".join(lines[start:])


# ─── Stage 2: normalization ───────────────────────────────────────────────────

def normalize_code(code: str) -> str:
    """Mechanical whitespace/character cleanup that never changes semantics."""
    if not code:
        return ""
    for bad, good in _UNICODE_FIXES.items():
        code = code.replace(bad, good)
    code = code.replace("\r\n", "\n").replace("\r", "\n")
    code = code.expandtabs(4)
    # Remove a uniform leading indent (models sometimes indent the whole block)
    code = textwrap.dedent(code)
    # Drop trailing whitespace-damage per line (breaks implicit continuations)
    code = "\n".join(ln.rstrip() for ln in code.splitlines())
    return code.strip("\n") + "\n" if code.strip() else ""


# ─── Compile gate ─────────────────────────────────────────────────────────────

def syntax_error(code: str) -> SyntaxError | None:
    """Return the SyntaxError compiling ``code``, or None if it compiles."""
    try:
        ast.parse(code)
        return None
    except SyntaxError as exc:
        return exc


def _error_context(code: str, lineno: int | None, radius: int = 2) -> str:
    if not lineno:
        return "(no line info)"
    lines = code.splitlines()
    lo = max(0, lineno - 1 - radius)
    hi = min(len(lines), lineno + radius)
    return "\n".join(
        f"{'>>' if i == lineno - 1 else '  '} {i + 1:4d} | {lines[i]}"
        for i in range(lo, hi)
    )


# ─── Result ───────────────────────────────────────────────────────────────────

@dataclass
class RepairResult:
    code: str
    ok: bool
    stages: list[str] = field(default_factory=list)   # what it took to fix
    error: str | None = None                          # last error if not ok

    @property
    def was_repaired(self) -> bool:
        return self.ok and bool(self.stages)


# ─── The editing validator ────────────────────────────────────────────────────

class SyntaxFixer:
    """LLM-backed syntax editor. Only invoked after deterministic repair fails."""

    def __init__(self, client, spark_version: str = "3.3", model: str | None = None) -> None:
        self._client = client
        self._model = model
        self._system = SYNTAX_FIXER_SYSTEM.format(
            python_compat=python_compat_note(spark_version)
        )

    def fix(self, code: str, error: SyntaxError) -> str:
        user = SYNTAX_FIXER_USER.format(
            error=f"{error.msg}",
            lineno=error.lineno or 0,
            error_context=_error_context(code, error.lineno),
            code=code,
        )
        raw = self._client.simple_complete(self._system, user, model=self._model)
        return normalize_code(extract_code(raw))


def ensure_compilable(
    text: str,
    fixer: SyntaxFixer | None = None,
    max_llm_fixes: int = 2,
    label: str = "",
) -> RepairResult:
    """
    Drive a candidate LLM response (or assembled file) to compilable Python.

    Stages: raw check → extract+normalize → up to ``max_llm_fixes`` LLM edits.
    Returns the best code seen with ``ok`` reflecting the final compile state —
    callers decide whether a still-broken artifact goes to human review.
    """
    stages: list[str] = []

    code = text or ""
    err = syntax_error(code)
    if err is None and code.strip():
        # Even valid code gets normalized so downstream diffs are stable.
        cleaned = normalize_code(extract_code(code))
        if syntax_error(cleaned) is None:
            return RepairResult(code=cleaned, ok=True, stages=stages)
        return RepairResult(code=code, ok=True, stages=stages)

    # Stage 1+2: deterministic extraction + normalization
    code = normalize_code(extract_code(text))
    if not code.strip():
        return RepairResult(code="", ok=False, stages=stages, error="empty after extraction")
    err = syntax_error(code)
    if err is None:
        stages.append("deterministic")
        logger.debug("Syntax repair (%s): fixed deterministically", label or "snippet")
        return RepairResult(code=code, ok=True, stages=stages)

    # Stage 3: bounded LLM edit loop
    if fixer is not None:
        for attempt in range(1, max_llm_fixes + 1):
            logger.info(
                "Syntax repair (%s): LLM edit %d/%d — %s at line %s",
                label or "snippet", attempt, max_llm_fixes, err.msg, err.lineno,
            )
            try:
                candidate = fixer.fix(code, err)
            except Exception as exc:
                logger.warning("Syntax fixer call failed (%s): %s", label, exc)
                break
            if not candidate.strip():
                continue
            new_err = syntax_error(candidate)
            if new_err is None:
                stages.append(f"llm_fix_{attempt}")
                return RepairResult(code=candidate, ok=True, stages=stages)
            # Keep the attempt only if it made progress (error moved later)
            if (new_err.lineno or 0) > (err.lineno or 0):
                code, err = candidate, new_err
            else:
                err = new_err if attempt == max_llm_fixes else err

    return RepairResult(
        code=code, ok=False, stages=stages,
        error=f"{err.msg} (line {err.lineno})",
    )
