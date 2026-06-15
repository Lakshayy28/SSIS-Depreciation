"""
LLM Pipeline — routes LLM_REQUIRED items from a CIR through the appropriate agent.

After this pass:
  - items with confidence >= 0.50 have pyspark_snippet set and status = LLM_COMPLETE
  - items with confidence < 0.50 have status = HUMAN_REVIEW
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
    ReviewAgent,
    ScriptTaskAgent,
)
from ssis_migration.transform.llm.confidence import compute_confidence, confidence_action
from ssis_migration.transform.llm.copilot_client import CopilotClient

logger = logging.getLogger(__name__)


class LLMPipeline:
    """
    Processes all LLM_REQUIRED items in a CIR using the GitHub Copilot Chat endpoint.

    Usage:
        pipeline = LLMPipeline()    # reads GITHUB_TOKEN from env
        cir = pipeline.process(cir)
    """

    def __init__(self, github_token: str | None = None) -> None:
        self._client = CopilotClient(token=github_token)
        reviewer = ReviewAgent(self._client)
        self._script_agent = ScriptTaskAgent(self._client, reviewer)
        self._sql_agent = ComplexSQLAgent(self._client, reviewer)
        self._expr_agent = ExpressionAgent(self._client)

    def process(self, cir: CIR) -> CIR:
        logger.info(
            "LLM pipeline: %d items to process",
            len(cir.conversion_metadata.llm_required_items),
        )

        # Process control flow executables
        for exe in cir.control_flow.execution_tree:
            self._process_executable(exe, cir)

        # Process data flow components
        for df in cir.data_flows:
            for comp in df.components:
                if comp.conversion_status == ConversionStatus.LLM_REQUIRED:
                    self._process_component(comp, cir)

        # Update top-level status
        remaining_llm = [
            item for item in cir.conversion_metadata.llm_required_items
            if not self._is_resolved(item, cir)
        ]
        if not remaining_llm:
            cir.conversion_metadata.conversion_status = ConversionStatus.LLM_COMPLETE
        else:
            cir.conversion_metadata.conversion_status = ConversionStatus.HUMAN_REVIEW

        return cir

    def _process_executable(self, exe, cir: CIR) -> None:
        if exe.conversion_status != ConversionStatus.LLM_REQUIRED:
            for child in exe.children:
                self._process_executable(child, cir)
            return

        if exe.type == "script_task" and exe.script_code:
            result = self._script_agent.convert(
                code=exe.script_code,
                language=exe.script_language or "csharp",
                referenced_assemblies=exe.referenced_assemblies,
            )
            confidence = compute_confidence(
                result.code or "",
                result.review_passed,
                complexity=cir.metadata.complexity_score,
            )
            action = confidence_action(confidence)
            if result.success and action in ("auto_accept", "optional_review"):
                exe.pyspark_snippet = result.code
                exe.conversion_status = ConversionStatus.LLM_COMPLETE
                exe.conversion_notes = f"confidence={confidence:.2f}"
            else:
                exe.conversion_status = ConversionStatus.HUMAN_REVIEW
                exe.pyspark_snippet = result.code   # Preserve for human reference
                cir.flag_for_human_review(exe.id)
                logger.warning("Script task %s (%s) escalated to human review", exe.id, exe.name)

        elif exe.sql and exe.sql.transpilation_status == TranspilationStatus.LLM_REQUIRED:
            result = self._sql_agent.convert(
                sql=exe.sql.original_text,
                partial_transpilation=exe.sql.transpiled_text or "",
            )
            if result.success:
                exe.sql.transpiled_text = result.code
                exe.sql.transpilation_status = TranspilationStatus.COMPLETE
                exe.sql.transpilation_notes = f"LLM: confidence={confidence_action(result.confidence)}"
                exe.conversion_status = ConversionStatus.LLM_COMPLETE
            else:
                exe.conversion_status = ConversionStatus.HUMAN_REVIEW
                cir.flag_for_human_review(exe.id)

        for child in exe.children:
            self._process_executable(child, cir)

    def _process_component(self, comp, cir: CIR) -> None:
        if comp.subtype in ("script_component",) and comp.script_code:
            result = self._script_agent.convert(
                code=comp.script_code,
                language=comp.script_language or "csharp",
                referenced_assemblies=comp.referenced_assemblies,
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
                    expr_node.translation_notes = f"LLM: confidence={result.confidence:.2f}"
                else:
                    all_resolved = False
                    expr_node.translation_status = TranspilationStatus.LLM_REQUIRED
                    cir.flag_for_human_review(f"{comp.id}::expr::{expr_node.output_column}")

            comp.conversion_status = (
                ConversionStatus.LLM_COMPLETE if all_resolved else ConversionStatus.HUMAN_REVIEW
            )

        else:
            # Unknown component or no resolvable content
            comp.conversion_status = ConversionStatus.HUMAN_REVIEW
            cir.flag_for_human_review(comp.id)

    def _is_resolved(self, item_id: str, cir: CIR) -> bool:
        # Check executables
        for exe in cir.control_flow.execution_tree:
            if exe.id == item_id:
                return exe.conversion_status in (
                    ConversionStatus.LLM_COMPLETE, ConversionStatus.DETERMINISTIC
                )
        # Check components
        for df in cir.data_flows:
            for comp in df.components:
                if comp.id == item_id:
                    return comp.conversion_status in (
                        ConversionStatus.LLM_COMPLETE, ConversionStatus.DETERMINISTIC
                    )
        return False
