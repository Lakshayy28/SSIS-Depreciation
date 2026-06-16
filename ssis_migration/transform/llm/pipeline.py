"""
LLM Pipeline — routes LLM_REQUIRED items from a CIR through the appropriate agent.

Each component goes through a Generate → Review → Regenerate loop until either:
  - The reviewer passes the code (→ LLM_COMPLETE), or
  - Max iterations are exhausted (→ HUMAN_REVIEW)

The reviewer always uses a separate, stronger model (COPILOT_REVIEWER_MODEL).
"""

from __future__ import annotations

import logging

from ssis_migration.cir.models import (
    CIR,
    ConversionStatus,
    TranspilationStatus,
)
from ssis_migration.transform.llm.agents import (
    ComplexSQLAgent,
    ExpressionAgent,
    FunctionalValidatorAgent,
    ReviewAgent,
    ScriptTaskAgent,
)
from ssis_migration.transform.llm.confidence import compute_confidence, confidence_action
from ssis_migration.transform.llm.copilot_client import CopilotClient

logger = logging.getLogger(__name__)


class LLMPipeline:
    """
    Processes all LLM_REQUIRED items in a CIR.

    Args:
        github_token:      overrides GITHUB_TOKEN env var
        spark_version:     target PySpark version (e.g. "3.3"); threaded into
                           every generation and review prompt
        functional_context: list of critical issues from a previous
                           FunctionalValidatorAgent pass; prepended to every
                           agent's user prompt so generators know what was wrong
                           at the package level in the last iteration
    """

    def __init__(
        self,
        github_token: str | None = None,
        spark_version: str = "3.3",
        reviewer_model: str | None = None,
    ) -> None:
        from ssis_migration.config import cfg

        self._spark_version = spark_version
        self._client = CopilotClient(token=github_token)
        _reviewer_model = reviewer_model or cfg.copilot_reviewer_model
        _max_iter = cfg.copilot_max_review_iterations

        reviewer = ReviewAgent(self._client, reviewer_model=_reviewer_model)
        self._script_agent = ScriptTaskAgent(
            self._client, reviewer,
            max_iterations=_max_iter,
            spark_version=spark_version,
        )
        self._sql_agent = ComplexSQLAgent(
            self._client, reviewer,
            max_iterations=_max_iter,
            spark_version=spark_version,
        )
        self._expr_agent = ExpressionAgent(self._client, spark_version=spark_version)
        self._func_validator = FunctionalValidatorAgent(self._client, spark_version=spark_version)

    def process(self, cir: CIR, functional_context: list[str] | None = None) -> CIR:
        logger.info(
            "LLM pipeline: %d items to process (spark=%s)",
            len(cir.conversion_metadata.llm_required_items),
            self._spark_version,
        )
        if functional_context:
            logger.info("Carrying %d functional-validation issues from previous pass", len(functional_context))

        for exe in cir.control_flow.execution_tree:
            self._process_executable(exe, cir, functional_context)

        for df in cir.data_flows:
            for comp in df.components:
                if comp.conversion_status == ConversionStatus.LLM_REQUIRED:
                    self._process_component(comp, cir, functional_context)

        remaining_llm = [
            item for item in cir.conversion_metadata.llm_required_items
            if not self._is_resolved(item, cir)
        ]
        cir.conversion_metadata.conversion_status = (
            ConversionStatus.LLM_COMPLETE if not remaining_llm else ConversionStatus.HUMAN_REVIEW
        )
        return cir

    def functional_validate(self, pyspark_code: str, cir: CIR):
        """Run package-level functional equivalence check. Returns FunctionalValidationResult."""
        from ssis_migration.transform.llm.agents import FunctionalValidationResult
        cir_summary = _build_cir_summary(cir)
        return self._func_validator.validate(pyspark_code, cir_summary)

    # ── private ───────────────────────────────────────────────────────────────

    def _process_executable(self, exe, cir: CIR, functional_context) -> None:
        if exe.conversion_status != ConversionStatus.LLM_REQUIRED:
            for child in exe.children:
                self._process_executable(child, cir, functional_context)
            return

        _STRUCTURAL = ("sequence", "for_loop", "foreach_loop", "data_flow")
        _OPERATIONAL = ("file_system", "ftp", "send_mail", "execute_process", "execute_sql")

        if exe.type in _STRUCTURAL:
            exe.conversion_status = ConversionStatus.DETERMINISTIC

        elif exe.type == "script_task" and exe.script_code:
            result = self._script_agent.convert(
                code=exe.script_code,
                language=exe.script_language or "csharp",
                referenced_assemblies=exe.referenced_assemblies,
                functional_context=functional_context,
            )
            confidence = compute_confidence(
                result.code or "",
                result.review_passed,
                complexity=cir.metadata.complexity_score,
            )
            if result.success and confidence_action(confidence) in ("auto_accept", "optional_review"):
                exe.pyspark_snippet = result.code
                exe.conversion_status = ConversionStatus.LLM_COMPLETE
                exe.conversion_notes = f"confidence={confidence:.2f} iterations={result.iterations}"
            else:
                exe.conversion_status = ConversionStatus.HUMAN_REVIEW
                exe.pyspark_snippet = result.code
                cir.flag_for_human_review(exe.id)
                logger.warning("Script task %s escalated to human review (confidence=%.2f)", exe.id, confidence)

        elif exe.sql and exe.sql.transpilation_status == TranspilationStatus.LLM_REQUIRED:
            result = self._sql_agent.convert(
                sql=exe.sql.original_text,
                partial_transpilation=exe.sql.transpiled_text or "",
                functional_context=functional_context,
            )
            if result.success:
                exe.sql.transpiled_text = result.code
                exe.sql.transpilation_status = TranspilationStatus.COMPLETE
                exe.sql.transpilation_notes = f"LLM iterations={result.iterations}"
                exe.conversion_status = ConversionStatus.LLM_COMPLETE
            else:
                exe.conversion_status = ConversionStatus.HUMAN_REVIEW
                cir.flag_for_human_review(exe.id)

        elif exe.type in _OPERATIONAL:
            exe.conversion_status = ConversionStatus.DETERMINISTIC

        else:
            exe.conversion_status = ConversionStatus.HUMAN_REVIEW
            cir.flag_for_human_review(exe.id)
            logger.warning("No LLM handler for executable %s (type=%s)", exe.id, exe.type)

        for child in exe.children:
            self._process_executable(child, cir, functional_context)

    def _process_component(self, comp, cir: CIR, functional_context) -> None:
        if comp.subtype in ("script_component",) and comp.script_code:
            result = self._script_agent.convert(
                code=comp.script_code,
                language=comp.script_language or "csharp",
                referenced_assemblies=comp.referenced_assemblies,
                functional_context=functional_context,
            )
            confidence = compute_confidence(
                result.code or "", result.review_passed,
                complexity=cir.metadata.complexity_score,
            )
            if result.success and confidence >= 0.50:
                comp.pyspark_snippet = result.code
                comp.conversion_status = ConversionStatus.LLM_COMPLETE
                comp.conversion_notes = f"confidence={confidence:.2f}"
            else:
                comp.conversion_status = ConversionStatus.HUMAN_REVIEW
                comp.pyspark_snippet = result.code
                cir.flag_for_human_review(comp.id)

        elif comp.expressions:
            all_resolved = True
            for expr_node in comp.expressions:
                if expr_node.translation_status != TranspilationStatus.LLM_REQUIRED:
                    continue
                input_cols = [c.name for c in comp.output_columns]
                result = self._expr_agent.convert(
                    ssis_expression=expr_node.ssis_expression,
                    output_column=expr_node.output_column,
                    input_columns=input_cols,
                )
                if result.success:
                    expr_node.pyspark_expression = result.code
                    expr_node.translation_status = TranspilationStatus.COMPLETE
                    expr_node.translation_notes = f"LLM confidence={result.confidence:.2f}"
                else:
                    all_resolved = False
                    expr_node.translation_status = TranspilationStatus.LLM_REQUIRED
                    cir.flag_for_human_review(f"{comp.id}::expr::{expr_node.output_column}")

            comp.conversion_status = (
                ConversionStatus.LLM_COMPLETE if all_resolved else ConversionStatus.HUMAN_REVIEW
            )

        else:
            comp.conversion_status = ConversionStatus.HUMAN_REVIEW
            cir.flag_for_human_review(comp.id)

    def _is_resolved(self, item_id: str, cir: CIR) -> bool:
        for exe in cir.control_flow.execution_tree:
            if exe.id == item_id:
                return exe.conversion_status in (
                    ConversionStatus.LLM_COMPLETE, ConversionStatus.DETERMINISTIC
                )
        for df in cir.data_flows:
            for comp in df.components:
                if comp.id == item_id:
                    return comp.conversion_status in (
                        ConversionStatus.LLM_COMPLETE, ConversionStatus.DETERMINISTIC
                    )
        return False


def _build_cir_summary(cir: CIR) -> str:
    """Produce a compact human-readable summary of the CIR for the functional validator."""
    import json as _json

    lines = [
        f"Package: {cir.metadata.package_name}",
        f"Complexity: {cir.metadata.complexity_score.value}",
        f"Parameters: {[p.name for p in cir.parameters]}",
        f"Connections: {[c.name for c in cir.connections]}",
        "",
        "Control flow executables:",
    ]
    for exe in cir.control_flow.execution_tree:
        sql_text = exe.sql.original_text[:120] if exe.sql else None
        lines.append(
            f"  [{exe.id}] type={exe.type} name={exe.name!r}"
            + (f" sql={sql_text!r}" if sql_text else "")
            + (f" status={exe.conversion_status.value}" if exe.conversion_status else "")
            + (f"\n    pyspark_snippet={exe.pyspark_snippet[:200]!r}" if exe.pyspark_snippet else "")
        )

    if cir.data_flows:
        lines.append("\nData flows:")
        for df in cir.data_flows:
            lines.append(f"  DataFlow id={df.id}")
            for comp in df.components:
                lines.append(
                    f"    [{comp.id}] subtype={comp.subtype} name={comp.name!r}"
                    + (f"\n      pyspark_snippet={comp.pyspark_snippet[:200]!r}" if comp.pyspark_snippet else "")
                )

    return "\n".join(lines)
