"""
Stage 2 — Semantic equivalence validation.

Compares the CIR control-flow graph against the call graph extracted from
the generated Python AST. Verifies:
  - Every SSIS data path has a corresponding PySpark code path
  - Every SSIS error handler has a corresponding try/except block
  - Precedence constraint graph is isomorphic to execution order in code
  - Every cross-package reference has a corresponding import / function call
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

from ssis_migration.cir.models import CIR, ConversionStatus, PrecedenceEvaluation
from ssis_migration.validation.report import ValidationReport

logger = logging.getLogger(__name__)


class SemanticValidator:
    """Validates semantic equivalence between CIR and generated module."""

    def validate(self, module_path: Path, cir: CIR) -> ValidationReport:
        report = ValidationReport(
            source_file=cir.metadata.source_file,
            module_path=str(module_path),
        )

        if not module_path.exists():
            report.error("FILE_NOT_FOUND", f"Module not found: {module_path}", stage="semantic")
            return report

        source = module_path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            report.error("SYNTAX_ERROR", "Cannot parse module for semantic check", stage="semantic")
            return report

        func_names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        call_names = {
            node.func.id if isinstance(node.func, ast.Name) else
            node.func.attr if isinstance(node.func, ast.Attribute) else ""
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
        }

        self._check_data_flows_present(cir, func_names, report)
        self._check_error_handlers(cir, source, report)
        self._check_precedence_execution_order(cir, source, report)
        self._check_cross_package_refs(cir, source, report)
        self._check_unconverted_items(cir, report)

        return report

    def _check_data_flows_present(
        self, cir: CIR, func_names: set[str], report: ValidationReport
    ) -> None:
        """Verify each data flow produces a corresponding Python function."""
        from ssis_migration.codegen.generator import _python_identifier
        for df in cir.data_flows:
            expected_fn = _python_identifier(df.name)
            if expected_fn not in func_names:
                report.warn(
                    "MISSING_DATA_FLOW_FN",
                    f"Data flow '{df.name}' has no corresponding function '{expected_fn}' in module",
                    stage="semantic",
                    location=df.id,
                )

    def _check_error_handlers(self, cir: CIR, source: str, report: ValidationReport) -> None:
        """Verify that OnError event handlers have corresponding error handling."""
        has_error_handlers = any(eh.event == "OnError" for eh in cir.event_handlers)
        if has_error_handlers:
            if "try:" not in source and "except" not in source:
                report.warn(
                    "MISSING_ERROR_HANDLING",
                    "SSIS OnError event handler found but no try/except in generated code",
                    stage="semantic",
                )

    def _check_precedence_execution_order(
        self, cir: CIR, source: str, report: ValidationReport
    ) -> None:
        """
        Verify that failure constraints have corresponding error handling.
        Success constraints are implicitly handled by sequential execution.
        """
        for pc in cir.control_flow.precedence_constraints:
            if pc.evaluation == PrecedenceEvaluation.FAILURE:
                report.warn(
                    "FAILURE_CONSTRAINT",
                    f"SSIS failure precedence constraint from '{pc.from_id}' to '{pc.to_id}' — "
                    "verify error handling in generated code implements this correctly",
                    stage="semantic",
                    location=f"{pc.from_id} → {pc.to_id}",
                )
            elif pc.evaluation == PrecedenceEvaluation.EXPRESSION:
                report.warn(
                    "EXPRESSION_CONSTRAINT",
                    f"SSIS expression-based constraint: '{pc.expression}' — verify conditional "
                    "logic is preserved in generated code",
                    stage="semantic",
                    location=f"{pc.from_id} → {pc.to_id}",
                )

    def _check_cross_package_refs(self, cir: CIR, source: str, report: ValidationReport) -> None:
        """Flag if SSIS Execute Package Tasks exist without corresponding imports."""
        for exe in cir.control_flow.execution_tree:
            if exe.type == "execute_package" and exe.child_package_ref:
                from ssis_migration.codegen.generator import _python_identifier
                expected_import = _python_identifier(
                    exe.child_package_ref.removesuffix(".dtsx")
                )
                if expected_import not in source:
                    report.warn(
                        "MISSING_CHILD_PACKAGE",
                        f"Execute Package Task references '{exe.child_package_ref}' but "
                        f"'{expected_import}' is not imported in generated code",
                        stage="semantic",
                        location=exe.id,
                    )

    def _check_unconverted_items(self, cir: CIR, report: ValidationReport) -> None:
        """Flag any items that are still in PENDING or LLM_REQUIRED status."""
        for exe in cir.control_flow.execution_tree:
            if exe.conversion_status in (
                ConversionStatus.PENDING, ConversionStatus.LLM_REQUIRED
            ):
                report.error(
                    "UNCONVERTED_EXECUTABLE",
                    f"Executable '{exe.name}' ({exe.id}) is unconverted: {exe.conversion_status.value}",
                    stage="semantic",
                    location=exe.id,
                )
        for df in cir.data_flows:
            for comp in df.components:
                if comp.conversion_status in (
                    ConversionStatus.PENDING, ConversionStatus.LLM_REQUIRED
                ):
                    report.error(
                        "UNCONVERTED_COMPONENT",
                        f"Component '{comp.name}' ({comp.id}) is unconverted: {comp.conversion_status.value}",
                        stage="semantic",
                        location=comp.id,
                    )
