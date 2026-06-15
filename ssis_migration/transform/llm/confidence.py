"""
Confidence scoring for LLM-generated artefacts.

Final score = weighted sum of:
  - review_passed         (0.40) — ReviewAgent self-consistency pass/fail
  - static_analysis_ok    (0.30) — Python syntax parses cleanly
  - source_complexity     (0.15) — inverse of complexity (simpler = higher confidence)
  - no_risky_patterns     (0.15) — absence of COM interop, dynamic SQL exec, etc.

RAG similarity was removed: we have no embedding index yet, so carrying a 0.30
dead-weight term permanently capped every score at 0.40 (below the 0.50
accept threshold), making every item land in HUMAN_REVIEW regardless of quality.

Thresholds:
  >= 0.80  → auto-accept
  >= 0.50  → flag for optional review, proceed to validation
  < 0.50   → mandatory human review before proceeding

Score reference (simple package, no risky patterns, valid Python):
  review pass   → 0.40+0.30+0.15+0.15 = 1.00  (auto-accept)
  review fail   → 0.00+0.30+0.15+0.15 = 0.60  (optional-review — still proceeds)
  syntax error  → 0.40+0.00+0.15+0.15 = 0.70  (optional-review)
  both fail     → 0.00+0.00+0.15+0.15 = 0.30  (mandatory human review)
"""

from __future__ import annotations

import ast
import re

from ssis_migration.cir.models import ComplexityLevel

_RISKY_PATTERNS = [
    re.compile(r'\bComObject\b', re.I),
    re.compile(r'\bMarshal\b', re.I),
    re.compile(r'\bsp_executesql\b', re.I),
    re.compile(r'exec\s*\(', re.I),
    re.compile(r'\bsubprocess\b', re.I),
    re.compile(r'__import__', re.I),
]


def compute_confidence(
    generated_code: str,
    review_passed: bool,
    rag_similarity: float = 0.0,   # kept for API compat, ignored until RAG is built
    complexity: ComplexityLevel = ComplexityLevel.MEDIUM,
) -> float:
    """Return a confidence score in [0.0, 1.0]."""

    # Static analysis: does the code parse as valid Python?
    static_ok = _parses_as_python(generated_code)

    # Risky pattern check
    no_risky = not any(p.search(generated_code) for p in _RISKY_PATTERNS)

    # Inverse complexity score
    complexity_score = {
        ComplexityLevel.SIMPLE: 1.0,
        ComplexityLevel.MEDIUM: 0.75,
        ComplexityLevel.HIGH: 0.5,
        ComplexityLevel.VERY_HIGH: 0.25,
    }.get(complexity, 0.5)

    score = (
        0.40 * (1.0 if review_passed else 0.0)
        + 0.30 * (1.0 if static_ok else 0.0)
        + 0.15 * complexity_score
        + 0.15 * (1.0 if no_risky else 0.0)
    )
    return round(min(1.0, max(0.0, score)), 4)


def confidence_action(score: float) -> str:
    """Return the action to take based on confidence score."""
    if score >= 0.80:
        return "auto_accept"
    if score >= 0.50:
        return "optional_review"
    return "mandatory_review"


def _parses_as_python(code: str) -> bool:
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False
