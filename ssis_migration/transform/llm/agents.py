"""
LLM agent implementations.

Design: every agent uses a Generate → Review → Regenerate loop.
When the reviewer finds issues it returns them as a list; the GENERATOR is then
called again with those issues appended to its user prompt so it can reason
about and fix its own output.  The reviewer never patches code — it only judges.

Agents:
  ScriptTaskAgent       — C#/VB.NET Script Tasks → Python function
  ComplexSQLAgent       — procedural T-SQL → Spark SQL / PySpark
  ExpressionAgent       — SSIS expression language → PySpark column expression
  FunctionalValidatorAgent — full-package LLM equivalence check vs CIR
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
    FUNCTIONAL_CONTEXT_SUFFIX,
    FUNCTIONAL_VALIDATOR_SYSTEM,
    FUNCTIONAL_VALIDATOR_USER,
    REGEN_SUFFIX,
    REVIEW_SYSTEM,
    REVIEW_USER,
    SCRIPT_TASK_SYSTEM,
    SCRIPT_TASK_USER,
)

logger = logging.getLogger(__name__)


@dataclass
class AgentResult:
    success: bool
    code: str | None
    confidence: float           # 0.0–1.0
    notes: str = ""
    review_passed: bool = False
    iterations: int = 1


@dataclass
class FunctionalValidationResult:
    passed: bool
    equivalence_score: float    # 0.0–1.0
    critical_issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ── ReviewAgent ───────────────────────────────────────────────────────────────

class ReviewAgent:
    """
    Strict code reviewer.  Returns issues as a list for the generator to fix.
    Never patches code itself — corrected_code is always set to null in prompts.
    Uses a separate (stronger) model than the generator.
    """

    def __init__(self, client: CopilotClient, reviewer_model: str | None = None) -> None:
        self._client = client
        self._reviewer_model = reviewer_model  # None → client default

    def review(
        self,
        generated_code: str,
        component_type: str,
        input_columns: list[str],
        output_columns: list[str],
        source_context: str = "",
        spark_version: str = "3.3",
    ) -> tuple[bool, list[str]]:
        """Returns (passed, issues).  Never returns corrected code."""
        system = REVIEW_SYSTEM.format(spark_version=spark_version)
        user_msg = REVIEW_USER.format(
            spark_version=spark_version,
            component_type=component_type,
            input_columns=", ".join(input_columns) if input_columns else "none",
            output_columns=", ".join(output_columns) if output_columns else "none",
            source_context=source_context or "not available",
            generated_code=generated_code,
        )

        # Use reviewer model if specified, otherwise fall back to client default
        if self._reviewer_model:
            raw = self._client.simple_complete(system, user_msg, model=self._reviewer_model)
        else:
            raw = self._client.simple_complete(system, user_msg)

        # Strip any accidental markdown fences around the JSON
        raw = re.sub(r'^```(?:json)?\s*', '', raw.strip(), flags=re.MULTILINE)
        raw = re.sub(r'\s*```$', '', raw.strip(), flags=re.MULTILINE)

        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not json_match:
            logger.warning("ReviewAgent returned non-JSON: %.200s", raw)
            return True, []  # Assume pass if unparseable

        try:
            result = json.loads(json_match.group(0))
        except json.JSONDecodeError:
            logger.warning("ReviewAgent JSON parse failed: %.200s", raw)
            return True, []

        passed = result.get("passed", True)
        issues = result.get("issues", [])
        if issues:
            logger.debug("ReviewAgent issues (%s): %s", component_type, issues)
        return passed, issues


# ── ScriptTaskAgent ───────────────────────────────────────────────────────────

class ScriptTaskAgent:
    """Converts C# or VB.NET Script Task code to a Python function."""

    def __init__(
        self,
        client: CopilotClient,
        review_agent: ReviewAgent,
        max_iterations: int = 4,
        spark_version: str = "3.3",
    ) -> None:
        self._client = client
        self._reviewer = review_agent
        self._max_iterations = max_iterations
        self._spark_version = spark_version

    def convert(
        self,
        code: str,
        language: str = "csharp",
        referenced_assemblies: list[str] | None = None,
        read_vars: list[str] | None = None,
        write_vars: list[str] | None = None,
        functional_context: list[str] | None = None,
    ) -> AgentResult:
        system = SCRIPT_TASK_SYSTEM.format(spark_version=self._spark_version)
        base_user = SCRIPT_TASK_USER.format(
            spark_version=self._spark_version,
            language=language,
            assemblies=", ".join(referenced_assemblies or []) or "none",
            read_vars=", ".join(read_vars or []) or "none",
            write_vars=", ".join(write_vars or []) or "none",
            code=code,
        )
        if functional_context:
            base_user += FUNCTIONAL_CONTEXT_SUFFIX.format(
                issues="\n".join(f"- {i}" for i in functional_context)
            )

        user_msg = base_user
        for iteration in range(1, self._max_iterations + 1):
            generated = _strip_markdown_fences(
                self._client.simple_complete(system, user_msg)
            )

            passed, issues = self._reviewer.review(
                generated,
                component_type="script_task",
                input_columns=read_vars or [],
                output_columns=write_vars or [],
                source_context=f"[{language}]\n{code}",
                spark_version=self._spark_version,
            )
            if passed:
                return AgentResult(
                    success=True, code=generated,
                    confidence=_base_confidence(iteration),
                    review_passed=True, iterations=iteration,
                )

            logger.debug(
                "ScriptTaskAgent iteration %d/%d failed review: %s",
                iteration, self._max_iterations, issues,
            )
            if iteration < self._max_iterations:
                # Feed issues back to generator — regenerate, do not patch
                user_msg = base_user + REGEN_SUFFIX.format(
                    issues="\n".join(f"- {i}" for i in issues)
                )

        return AgentResult(
            success=False, code=generated, confidence=0.3,
            notes=f"Review-regen loop exhausted after {self._max_iterations} iterations",
            review_passed=False, iterations=self._max_iterations,
        )


# ── ComplexSQLAgent ───────────────────────────────────────────────────────────

class ComplexSQLAgent:
    """Converts procedural T-SQL to Spark SQL / PySpark DataFrame operations."""

    def __init__(
        self,
        client: CopilotClient,
        review_agent: ReviewAgent,
        max_iterations: int = 4,
        spark_version: str = "3.3",
    ) -> None:
        self._client = client
        self._reviewer = review_agent
        self._max_iterations = max_iterations
        self._spark_version = spark_version

    def convert(
        self,
        sql: str,
        partial_transpilation: str = "",
        connection_type: str = "oledb",
        jdbc_url_template: str = "jdbc:sqlserver://{host}:{port};databaseName={database}",
        connection_name: str = "sqlserver_conn",
        functional_context: list[str] | None = None,
    ) -> AgentResult:
        system = COMPLEX_SQL_SYSTEM.format(
            spark_version=self._spark_version,
            connection_name=connection_name,
        )
        base_user = COMPLEX_SQL_USER.format(
            spark_version=self._spark_version,
            sql=sql,
            partial_transpilation=partial_transpilation or "none",
            connection_type=connection_type,
            jdbc_url_template=jdbc_url_template,
            connection_name=connection_name,
        )
        if functional_context:
            base_user += FUNCTIONAL_CONTEXT_SUFFIX.format(
                issues="\n".join(f"- {i}" for i in functional_context)
            )

        user_msg = base_user
        generated = ""
        for iteration in range(1, self._max_iterations + 1):
            generated = _strip_markdown_fences(
                self._client.simple_complete(system, user_msg)
            )

            passed, issues = self._reviewer.review(
                generated,
                component_type="complex_sql",
                input_columns=[],
                output_columns=[],
                source_context=f"[T-SQL]\n{sql}",
                spark_version=self._spark_version,
            )
            if passed:
                return AgentResult(
                    success=True, code=generated,
                    confidence=_base_confidence(iteration),
                    review_passed=True, iterations=iteration,
                )

            logger.debug(
                "ComplexSQLAgent iteration %d/%d failed review: %s",
                iteration, self._max_iterations, issues,
            )
            if iteration < self._max_iterations:
                user_msg = base_user + REGEN_SUFFIX.format(
                    issues="\n".join(f"- {i}" for i in issues)
                )

        return AgentResult(
            success=False, code=generated, confidence=0.3,
            notes=f"Review-regen loop exhausted after {self._max_iterations} iterations",
            review_passed=False, iterations=self._max_iterations,
        )


# ── ExpressionAgent ───────────────────────────────────────────────────────────

class ExpressionAgent:
    """Converts SSIS expressions that exceeded the deterministic map."""

    def __init__(self, client: CopilotClient, spark_version: str = "3.3") -> None:
        self._client = client
        self._spark_version = spark_version

    def convert(
        self,
        ssis_expression: str,
        output_column: str,
        input_columns: list[str] | None = None,
    ) -> AgentResult:
        system = EXPRESSION_SYSTEM.format(spark_version=self._spark_version)
        user_msg = EXPRESSION_USER.format(
            spark_version=self._spark_version,
            ssis_expression=ssis_expression,
            output_column=output_column,
            input_columns=", ".join(input_columns or []) or "unknown",
        )
        generated = self._client.simple_complete(system, user_msg)
        generated = _strip_markdown_fences(generated).strip()

        if generated and (generated.startswith("F.") or generated.startswith("(")):
            return AgentResult(success=True, code=generated, confidence=0.75, review_passed=True)

        return AgentResult(
            success=False, code=generated, confidence=0.4,
            notes="Expression output did not start with F. or ( — may be wrong format",
        )


# ── FunctionalValidatorAgent ──────────────────────────────────────────────────

class FunctionalValidatorAgent:
    """
    Compares the generated PySpark module against the CIR (parsed SSIS logic)
    and returns a structured equivalence assessment.

    This is the package-level gate: if it fails, all LLM-converted items are
    re-generated with the critical issues as additional context.
    """

    def __init__(self, client: CopilotClient, spark_version: str = "3.3") -> None:
        self._client = client
        self._spark_version = spark_version

    def validate(
        self,
        pyspark_code: str,
        cir_summary: str,
    ) -> FunctionalValidationResult:
        system = FUNCTIONAL_VALIDATOR_SYSTEM.format(spark_version=self._spark_version)
        user_msg = FUNCTIONAL_VALIDATOR_USER.format(
            spark_version=self._spark_version,
            cir_summary=cir_summary,
            pyspark_code=pyspark_code,
        )
        raw = self._client.simple_complete(system, user_msg)

        raw = re.sub(r'^```(?:json)?\s*', '', raw.strip(), flags=re.MULTILINE)
        raw = re.sub(r'\s*```$', '', raw.strip(), flags=re.MULTILINE)

        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not json_match:
            logger.warning("FunctionalValidatorAgent returned non-JSON: %.200s", raw)
            return FunctionalValidationResult(passed=True, equivalence_score=1.0)

        try:
            result = json.loads(json_match.group(0))
        except json.JSONDecodeError:
            logger.warning("FunctionalValidatorAgent JSON parse failed")
            return FunctionalValidationResult(passed=True, equivalence_score=1.0)

        passed = result.get("passed", True)
        score = float(result.get("equivalence_score", 1.0))
        critical = result.get("critical_issues", [])
        warnings = result.get("warnings", [])

        if critical:
            logger.warning("Functional validation critical issues: %s", critical)
        if warnings:
            logger.info("Functional validation warnings: %s", warnings)

        return FunctionalValidationResult(
            passed=passed,
            equivalence_score=score,
            critical_issues=critical,
            warnings=warnings,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _strip_markdown_fences(text: str) -> str:
    text = re.sub(r'^```(?:python|sql|pyspark)?\s*\n', '', text.strip(), flags=re.MULTILINE)
    text = re.sub(r'\n```\s*$', '', text.strip(), flags=re.MULTILINE)
    return text.strip()


def _base_confidence(iteration: int) -> float:
    """Higher confidence when review passes on first attempt."""
    return max(0.5, 1.0 - (iteration - 1) * 0.15)
