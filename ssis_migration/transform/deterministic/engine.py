"""
Deterministic Engine — orchestrates all rule-based transformations on a CIR.

After this pass, CIR nodes have either:
  - conversion_status = DETERMINISTIC (pyspark_snippet populated)
  - conversion_status = LLM_REQUIRED (flagged for the LLM pipeline)
"""

from __future__ import annotations

import logging

from ssis_migration.cir.models import CIR, ConversionStatus
from ssis_migration.transform.deterministic.component_mapper import ComponentMapper
from ssis_migration.transform.deterministic.sql_transpiler import transpile_sql

logger = logging.getLogger(__name__)


class DeterministicEngine:
    """
    Runs all deterministic transformation passes on a CIR and returns
    the annotated CIR (modified in-place).
    """

    def __init__(self) -> None:
        self._mapper = ComponentMapper()

    def process(self, cir: CIR) -> CIR:
        logger.info("Running deterministic engine on %s", cir.metadata.source_file)

        # Pass 1: Transpile all SQL in control flow executables
        for exe in cir.control_flow.execution_tree:
            self._transpile_executable(exe, cir)

        # Pass 2: Map data flow components
        self._mapper.process(cir)

        # Pass 3: Update top-level conversion status
        if not cir.conversion_metadata.llm_required_items:
            cir.conversion_metadata.conversion_status = ConversionStatus.DETERMINISTIC
        else:
            cir.conversion_metadata.conversion_status = ConversionStatus.LLM_REQUIRED

        logger.info(
            "Deterministic pass complete: coverage=%.0f%%, llm_items=%d",
            cir.conversion_metadata.deterministic_coverage * 100,
            len(cir.conversion_metadata.llm_required_items),
        )
        return cir

    def _transpile_executable(self, exe, cir: CIR) -> None:
        from ssis_migration.cir.models import TranspilationStatus

        if exe.type == "data_flow":
            # Data flow executables are handled by ComponentMapper — mark deterministic
            exe.conversion_status = ConversionStatus.DETERMINISTIC

        elif exe.sql:
            transpile_sql(exe.sql)
            if exe.sql.transpilation_status == TranspilationStatus.LLM_REQUIRED:
                cir.flag_for_llm(exe.id)
                exe.conversion_status = ConversionStatus.LLM_REQUIRED
            else:
                exe.conversion_status = ConversionStatus.DETERMINISTIC

        elif exe.type == "script_task":
            exe.conversion_status = ConversionStatus.LLM_REQUIRED
            cir.flag_for_llm(exe.id)

        elif exe.type in ("sequence", "for_loop", "foreach_loop"):
            # Containers are deterministic — children handled recursively
            exe.conversion_status = ConversionStatus.DETERMINISTIC

        elif exe.type in ("file_system", "ftp", "send_mail", "execute_process"):
            exe.conversion_status = ConversionStatus.DETERMINISTIC

        for child in exe.children:
            self._transpile_executable(child, cir)
