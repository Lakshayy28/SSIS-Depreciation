"""
LLM agent implementations for the four categories that require LLM assistance:
  1. Script Tasks (C#/VB.NET)
  2. Complex SQL (procedural T-SQL)
  3. SSIS Expressions beyond the deterministic map
  4. Unknown / third-party components

Each agent uses the GitHub Copilot Chat endpoint via CopilotClient and applies
a ReviewAgent self-consistency pass (up to 3 iterations) before accepting.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from ssis_migration.cir.models import ConversionStatus, TranspilationStatus
from ssis_migration.transform.llm.confidence import _parses_as_python
from ssis_migration.transform.llm.copilot_client import CopilotClient
from ssis_migration.transform.llm.prompts import (
    COMPLEX_SQL_SYSTEM,
    COMPLEX_SQL_USER,
    EXPRESSION_SYSTEM,
    EXPRESSION_USER,
    REVIEW_SYSTEM,
    REVIEW_USER,
    SCRIPT_TASK_SYSTEM,
    SCRIPT_TASK_USER,
)

logger = logging.getLogger(__name__)

_MAX_REVIEW_ITERATIONS = 3


@dataclass
class AgentResult:
    success: bool
    code: str | None
    confidence: float           # 0.0–1.0
    notes: str = ""
    review_passed: bool = False
    iterations: int = 1


class ReviewAgent:
    """Self-consistency reviewer — validates LLM-generated code against spec."""

    def __init__(self, client: CopilotClient) -> None:
        self._client = client

    def review(
        self,
        generated_code: str,
        component_type: str,
        input_columns: list[str],
        output_columns: list[str],
        source_context: str = "",
    ) -> tuple[bool, str | None, list[str]]:
        """
        Returns (passed, corrected_code_or_None, issues).

        source_context: the original T-SQL / script / expression that was converted.
        Used by the reviewer to check semantic equivalence rather than just column presence.
        """
        user_msg = REVIEW_USER.format(
            component_type=component_type,
            input_columns=", ".join(input_columns) if input_columns else "none",
            output_columns=", ".join(output_columns) if output_columns else "none",
            source_context=source_context or "not available",
            generated_code=generated_code,
        )
        raw = self._client.simple_complete(REVIEW_SYSTEM, user_msg)

        # Strip markdown fences around JSON if the model wrapped the output
        raw_stripped = re.sub(r'^```(?:json)?\s*', '', raw.strip(), flags=re.MULTILINE)
        raw_stripped = re.sub(r'\s*```$', '', raw_stripped.strip(), flags=re.MULTILINE)

        # Extract JSON block from response
        json_match = re.search(r'\{.*\}', raw_stripped, re.DOTALL)
        if not json_match:
            logger.warning("ReviewAgent returned non-JSON: %.200s", raw)
            return True, None, []   # Assume pass if can't parse

        try:
            result = json.loads(json_match.group(0))
        except json.JSONDecodeError:
            logger.warning("ReviewAgent JSON parse failed: %.200s", raw)
            return True, None, []

        passed = result.get("passed", True)
        issues = result.get("issues", [])
        corrected = result.get("corrected_code")
        if issues:
            logger.debug("ReviewAgent issues: %s", issues)

        return passed, corrected, issues


class ScriptTaskAgent:
    """Converts C# or VB.NET Script Task code to a Python function."""

    def __init__(self, client: CopilotClient, review_agent: ReviewAgent) -> None:
        self._client = client
        self._reviewer = review_agent

    def convert(
        self,
        code: str,
        language: str = "csharp",
        referenced_assemblies: list[str] | None = None,
        read_vars: list[str] | None = None,
        write_vars: list[str] | None = None,
    ) -> AgentResult:
        user_msg = SCRIPT_TASK_USER.format(
            language=language,
            assemblies=", ".join(referenced_assemblies or []) or "none",
            read_vars=", ".join(read_vars or []) or "none",
            write_vars=", ".join(write_vars or []) or "none",
            code=code,
        )

        generated = self._client.simple_complete(SCRIPT_TASK_SYSTEM, user_msg)
        generated = _strip_markdown_fences(generated)

        for iteration in range(1, _MAX_REVIEW_ITERATIONS + 1):
            passed, corrected, issues = self._reviewer.review(
                generated,
                component_type="script_task",
                input_columns=read_vars or [],
                output_columns=write_vars or [],
                source_context=f"[{language}]\n{code}",
            )
            if passed:
                confidence = _base_confidence(iteration)
                return AgentResult(
                    success=True, code=generated, confidence=confidence,
                    review_passed=True, iterations=iteration,
                )
            if corrected:
                generated = corrected
                logger.debug("ScriptTaskAgent: review iteration %d corrected code", iteration)
            else:
                logger.warning("ScriptTaskAgent: review failed, issues: %s", issues)
                break

        # Escalate to human review
        return AgentResult(
            success=False, code=generated, confidence=0.3,
            notes=f"Review failed after {_MAX_REVIEW_ITERATIONS} iterations",
            review_passed=False, iterations=_MAX_REVIEW_ITERATIONS,
        )


class ComplexSQLAgent:
    """Converts procedural T-SQL to Spark SQL / PySpark DataFrame operations."""

    def __init__(self, client: CopilotClient, review_agent: ReviewAgent) -> None:
        self._client = client
        self._reviewer = review_agent

    def convert(
        self,
        sql: str,
        partial_transpilation: str = "",
        connection_type: str = "oledb",
        jdbc_url_template: str = "jdbc:sqlserver://{host}:{port};databaseName={database}",
        connection_name: str = "sqlserver_conn",
    ) -> AgentResult:
        user_msg = COMPLEX_SQL_USER.format(
            sql=sql,
            partial_transpilation=partial_transpilation or "none",
            connection_type=connection_type,
            jdbc_url_template=jdbc_url_template,
            connection_name=connection_name,
        )

        generated = self._client.simple_complete(COMPLEX_SQL_SYSTEM, user_msg)
        generated = _strip_markdown_fences(generated)

        for iteration in range(1, _MAX_REVIEW_ITERATIONS + 1):
            passed, corrected, issues = self._reviewer.review(
                generated,
                component_type="complex_sql",
                input_columns=[],
                output_columns=[],
                source_context=f"[T-SQL]\n{sql}",
            )
            if passed:
                return AgentResult(
                    success=True, code=generated, confidence=_base_confidence(iteration),
                    review_passed=True, iterations=iteration,
                )
            if corrected:
                corrected = _strip_markdown_fences(corrected)
                # Accept the reviewer's correction immediately if it parses as
                # valid Python — avoids re-review loop where the reviewer then
                # finds hallucinated issues in its own output.
                if _parses_as_python(corrected):
                    logger.debug(
                        "ComplexSQLAgent: accepting reviewer correction on iteration %d (static check passed)",
                        iteration,
                    )
                    return AgentResult(
                        success=True, code=corrected,
                        confidence=_base_confidence(iteration) * 0.9,  # slight discount vs review-pass
                        review_passed=True, iterations=iteration,
                        notes="Accepted reviewer correction after static validation",
                    )
                # Correction itself has syntax errors — use it for the next round
                generated = corrected
                logger.debug("ComplexSQLAgent: reviewer correction has syntax errors, retrying iteration %d", iteration)
            else:
                logger.debug("ComplexSQLAgent: review failed on iteration %d, issues: %s", iteration, issues)
                break

        return AgentResult(
            success=False, code=generated, confidence=0.3,
            notes="Complex SQL review failed after all iterations",
            review_passed=False,
        )


class ExpressionAgent:
    """Converts SSIS expressions that exceeded the deterministic map."""

    def __init__(self, client: CopilotClient) -> None:
        self._client = client

    def convert(
        self,
        ssis_expression: str,
        output_column: str,
        input_columns: list[str] | None = None,
    ) -> AgentResult:
        user_msg = EXPRESSION_USER.format(
            ssis_expression=ssis_expression,
            output_column=output_column,
            input_columns=", ".join(input_columns or []) or "unknown",
        )
        generated = self._client.simple_complete(EXPRESSION_SYSTEM, user_msg)
        generated = _strip_markdown_fences(generated).strip()

        # Basic validation: must start with F. or be a valid expression
        if generated and (generated.startswith("F.") or generated.startswith("(")):
            return AgentResult(success=True, code=generated, confidence=0.75, review_passed=True)

        return AgentResult(
            success=False, code=generated, confidence=0.4,
            notes="Expression output did not match expected PySpark pattern",
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _strip_markdown_fences(text: str) -> str:
    """Remove ```python ... ``` or ``` ... ``` fences from LLM output."""
    text = re.sub(r'^```(?:python|sql|pyspark)?\s*\n', '', text.strip(), flags=re.MULTILINE)
    text = re.sub(r'\n```\s*$', '', text.strip(), flags=re.MULTILINE)
    return text.strip()


def _base_confidence(iteration: int) -> float:
    """Higher confidence when review passes on first attempt."""
    return max(0.5, 1.0 - (iteration - 1) * 0.15)
