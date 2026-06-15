"""
End-to-end migration pipeline for a single .dtsx file.

Orchestrates: Parse → Deterministic → [LLM] → Generate → Validate → Report

Three conversion modes
─────────────────────
  deterministic  Parse + deterministic engine only. No LLM calls. Fast.
                 Items that need LLM are left as TODO stubs in the output.

  hybrid         Parse + deterministic, then LLM only for LLM_REQUIRED items.
                 Default. Minimises token cost while maximising coverage.

  llm            Parse only, then LLM for every component and SQL statement,
                 skipping the deterministic engine entirely. Useful to compare
                 pure-LLM output against the deterministic+hybrid results.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ssis_migration.cir.models import CIR, ConversionStatus
from ssis_migration.codegen.generator import CodeGenerator
from ssis_migration.parser import DTSXParser
from ssis_migration.transform.deterministic import DeterministicEngine
from ssis_migration.validation.report import ValidationReport
from ssis_migration.validation.semantic import SemanticValidator
from ssis_migration.validation.static import StaticValidator

logger = logging.getLogger(__name__)


class ConversionMode(str, Enum):
    DETERMINISTIC = "deterministic"
    HYBRID = "hybrid"
    LLM = "llm"


@dataclass
class PipelineConfig:
    output_dir: Path = field(default_factory=lambda: Path("output"))
    cir_dir: Path | None = None
    mode: ConversionMode = ConversionMode.HYBRID
    github_token: str | None = None      # Overrides GITHUB_TOKEN / .env
    spark_version: str = "3.3"
    save_cir: bool = True

    # Back-compat alias so existing call sites using enable_llm=False still work
    @property
    def enable_llm(self) -> bool:
        return self.mode != ConversionMode.DETERMINISTIC

    @enable_llm.setter
    def enable_llm(self, value: bool) -> None:
        self.mode = ConversionMode.HYBRID if value else ConversionMode.DETERMINISTIC


@dataclass
class PipelineResult:
    source_file: str
    cir: CIR | None = None
    module_path: Path | None = None
    test_path: Path | None = None
    validation_report: ValidationReport | None = None
    error: str | None = None

    @property
    def success(self) -> bool:
        return (
            self.error is None
            and self.validation_report is not None
            and self.validation_report.passed
        )


class MigrationPipeline:
    """
    Runs the full SSIS-to-PySpark migration pipeline for a single package.

    Usage:
        pipeline = MigrationPipeline(config)
        result = pipeline.run(Path("CustomerLoad.dtsx"))
    """

    def __init__(self, config: PipelineConfig | None = None) -> None:
        self._config = config or PipelineConfig()
        self._parser = DTSXParser()
        self._deterministic = DeterministicEngine()
        self._generator = CodeGenerator(self._config.output_dir)
        self._static_validator = StaticValidator(self._config.spark_version)
        self._semantic_validator = SemanticValidator()

    def run(self, dtsx_path: Path, mode: ConversionMode | None = None) -> PipelineResult:
        mode = mode or self._config.mode
        result = PipelineResult(source_file=str(dtsx_path))
        logger.info("=== Pipeline start: %s  mode=%s ===", dtsx_path.name, mode.value)

        # Phase 1: Parse (always)
        try:
            cir = self._parser.parse(dtsx_path)
            result.cir = cir
            logger.info("[1/5] Parse complete: complexity=%s", cir.metadata.complexity_score.value)
        except Exception as exc:
            result.error = f"Parse failed: {exc}"
            logger.error("Parse failed for %s: %s", dtsx_path, exc)
            return result

        # Phase 2: Deterministic transformation (skipped in pure LLM mode)
        if mode != ConversionMode.LLM:
            try:
                cir = self._deterministic.process(cir)
                logger.info(
                    "[2/5] Deterministic complete: coverage=%.0f%%",
                    cir.conversion_metadata.deterministic_coverage * 100,
                )
            except Exception as exc:
                result.error = f"Deterministic engine failed: {exc}"
                logger.error("Deterministic engine failed: %s", exc)
                return result

            if self._config.save_cir:
                self._save_cir(cir, dtsx_path, stage="annotated")
        else:
            logger.info("[2/5] Deterministic skipped (mode=llm)")

        # Phase 3: LLM augmentation
        if mode == ConversionMode.DETERMINISTIC:
            logger.info("[3/5] LLM skipped (mode=deterministic)")

        elif mode == ConversionMode.HYBRID:
            # LLM only for items the deterministic engine flagged
            if cir.conversion_metadata.llm_required_items:
                self._run_llm(cir, dtsx_path)
            else:
                logger.info("[3/5] LLM skipped (0 LLM_REQUIRED items)")

        elif mode == ConversionMode.LLM:
            # Flag every component and SQL statement for LLM processing
            self._flag_all_for_llm(cir)
            self._run_llm(cir, dtsx_path)

        # Phase 4: Code generation
        try:
            paths = self._generator.generate(cir)
            result.module_path = paths["module"]
            result.test_path = paths["test"]
            logger.info("[4/5] Code generation complete: %s", paths["module"])
        except Exception as exc:
            result.error = f"Code generation failed: {exc}"
            logger.error("Code generation failed: %s", exc)
            return result

        # Phase 5: Validation
        try:
            static_report = self._static_validator.validate(result.module_path, cir)
            semantic_report = self._semantic_validator.validate(result.module_path, cir)

            # Merge findings
            merged = static_report
            merged.findings.extend(semantic_report.findings)
            merged.acceptable_divergences.extend(semantic_report.acceptable_divergences)

            result.validation_report = merged
            logger.info("[5/5] Validation: %s", merged.summary())

            # Save validation report
            report_path = self._config.output_dir / f"validation_report_{dtsx_path.stem}.json"
            merged.save(report_path)
        except Exception as exc:
            logger.error("Validation failed: %s", exc)
            result.error = f"Validation failed: {exc}"

        logger.info("=== Pipeline end: %s — %s ===", dtsx_path.name,
                    "SUCCESS" if result.success else "ISSUES FOUND")
        return result

    def _run_llm(self, cir: CIR, dtsx_path: Path) -> None:
        try:
            from ssis_migration.transform.llm import LLMPipeline
            llm_pipeline = LLMPipeline(github_token=self._config.github_token)
            llm_pipeline.process(cir)
            logger.info(
                "[3/5] LLM complete: human_review=%d",
                len(cir.conversion_metadata.human_review_required),
            )
            if self._config.save_cir:
                self._save_cir(cir, dtsx_path, stage="resolved")
        except Exception as exc:
            logger.warning("LLM pipeline failed (continuing without): %s", exc)

    def _flag_all_for_llm(self, cir: CIR) -> None:
        """In pure LLM mode: flag every component and SQL statement."""
        from ssis_migration.cir.models import ConversionStatus, TranspilationStatus
        for exe in cir.control_flow.execution_tree:
            if exe.sql:
                exe.sql.transpilation_status = TranspilationStatus.LLM_REQUIRED
            if exe.type not in ("data_flow",):
                exe.conversion_status = ConversionStatus.LLM_REQUIRED
                cir.flag_for_llm(exe.id)
        for df in cir.data_flows:
            for comp in df.components:
                comp.conversion_status = ConversionStatus.LLM_REQUIRED
                comp.pyspark_snippet = None
                cir.flag_for_llm(comp.id)

    def _save_cir(self, cir: CIR, dtsx_path: Path, stage: str) -> None:
        cir_dir = self._config.cir_dir or (self._config.output_dir / "cir")
        cir_dir.mkdir(parents=True, exist_ok=True)
        cir_path = cir_dir / f"{dtsx_path.stem}_cir_{stage}.json"
        cir.save(cir_path)
        logger.debug("Saved CIR: %s", cir_path)
