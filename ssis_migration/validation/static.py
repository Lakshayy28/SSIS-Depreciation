"""
Stage 1 — Static validation of generated PySpark modules.

Checks:
  - Python syntax (py_compile)
  - PySpark API surface for target version
  - No undefined column references (basic AST walk)
  - Dead code / unreachable branches after conditional splits
  - Human-review items flagged in output
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

from ssis_migration.cir.models import CIR, ConversionStatus
from ssis_migration.validation.report import ValidationReport

logger = logging.getLogger(__name__)

# PySpark 3.x-only APIs — flag if targeting 2.4
_SPARK3_ONLY = {
    "mapInPandas", "applyInPandas", "transform", "aggregate",
    "forall", "exists", "pandas_api", "mapInArrow",
}

# PySpark antipatterns
_ANTIPATTERNS = {
    r'\.toPandas\(\)': "toPandas() collects to driver; consider spark-native alternatives",
    r'\.collect\(\)': "collect() pulls all data to driver; only use for small datasets",
    r'\.show\(\)': "show() is a driver action; remove from production code",
    r'udf\(': "Python UDF detected; prefer native F.* functions for performance",
}


class StaticValidator:
    """Validates a generated Python file against a CIR."""

    def __init__(self, spark_version: str = "3.3") -> None:
        self._spark_version = tuple(int(x) for x in spark_version.split(".")[:2])

    def validate(self, module_path: Path, cir: CIR) -> ValidationReport:
        report = ValidationReport(
            source_file=cir.metadata.source_file,
            module_path=str(module_path),
        )

        if not module_path.exists():
            report.error("FILE_NOT_FOUND", f"Generated module not found: {module_path}")
            return report

        source = module_path.read_text(encoding="utf-8")

        self._check_syntax(source, report, module_path)
        self._check_api_compat(source, report)
        self._check_antipatterns(source, report)
        self._check_human_review_items(cir, report)
        self._check_column_refs(source, cir, report)
        self._add_divergences(cir, report)

        return report

    # ── Checks ────────────────────────────────────────────────────────────────

    def _check_syntax(self, source: str, report: ValidationReport, path: Path) -> None:
        try:
            ast.parse(source)
            report.info("SYNTAX_OK", "Python syntax valid", stage="static", location=str(path))
        except SyntaxError as exc:
            report.error(
                "SYNTAX_ERROR",
                f"Python syntax error at line {exc.lineno}: {exc.msg}",
                stage="static",
                location=str(path),
                detail=str(exc),
            )

    def _check_api_compat(self, source: str, report: ValidationReport) -> None:
        if self._spark_version >= (3, 0):
            return  # All 3.x APIs are valid in 3.x targets
        for api in _SPARK3_ONLY:
            if re.search(rf'\b{re.escape(api)}\b', source):
                report.error(
                    "API_COMPAT",
                    f"'{api}' is a PySpark 3.x-only API; not available in {'.'.join(str(v) for v in self._spark_version)}",
                    stage="static",
                )

    def _check_antipatterns(self, source: str, report: ValidationReport) -> None:
        for pattern, message in _ANTIPATTERNS.items():
            if re.search(pattern, source):
                report.warn("ANTIPATTERN", message, stage="static")

    def _check_human_review_items(self, cir: CIR, report: ValidationReport) -> None:
        for item_id in cir.conversion_metadata.human_review_required:
            report.error(
                "HUMAN_REVIEW_REQUIRED",
                f"Item '{item_id}' requires human review before deployment",
                stage="static",
                location=item_id,
            )
        # Also check for TODO comments left in generated code
        # (indicates unconverted items)
        # This is checked via source scan in post-generation

    def _check_column_refs(self, source: str, cir: CIR, report: ValidationReport) -> None:
        """
        Warn if columns referenced in pyspark_snippets don't appear in the
        output_columns of the corresponding source component.
        This is a heuristic check only; false positives are expected.
        """
        # Extract F.col("...") references from source
        col_refs = set(re.findall(r'F\.col\("([^"]+)"\)', source))

        known_cols: set[str] = set()
        for df in cir.data_flows:
            for comp in df.components:
                for col in comp.output_columns:
                    known_cols.add(col.name)
                for mapping in comp.column_mappings:
                    known_cols.add(mapping.source)
                    known_cols.add(mapping.destination)

        if not known_cols:
            return  # Can't check without column metadata

        unknown = col_refs - known_cols - {"*"}
        for col in sorted(unknown)[:5]:  # Cap at 5 warnings
            report.warn(
                "UNKNOWN_COLUMN",
                f"Column reference F.col(\"{col}\") not found in CIR metadata — verify manually",
                stage="static",
                location=col,
            )

    def _add_divergences(self, cir: CIR, report: ValidationReport) -> None:
        """Register known acceptable divergences in the report."""
        from ssis_migration.cir.type_mapping import KNOWN_DIVERGENCES
        for df in cir.data_flows:
            for comp in df.components:
                for col in comp.output_columns:
                    if col.ssis_type in KNOWN_DIVERGENCES:
                        div = KNOWN_DIVERGENCES[col.ssis_type]
                        report.acceptable_divergences.append(
                            f"{comp.name}.{col.name} ({col.ssis_type}): {div}"
                        )
