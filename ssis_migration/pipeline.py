"""
End-to-end migration pipeline for a single .dtsx file.

Orchestrates: Parse → Deterministic → [LLM] → Generate → Validate → Report
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ssis_migration.cir.models import CIR, ConversionStatus
from ssis_migration.codegen.generator import CodeGenerator
from ssis_migration.parser import DTSXParser
from ssis_migration.transform.deterministic import DeterministicEngine
from ssis_migration.validation.report import ValidationReport
from ssis_migration.validation.semantic import SemanticValidator
from ssis_migration.validation.static import StaticValidator

logger = logging.getLogger(__name__)


@dataclass
class PipelineConfig:
    output_dir: Path = field(default_factory=lambda: Path("output"))
    cir_dir: Path | None = None          # Where to save CIR JSON (default: output_dir/cir)
    enable_llm: bool = True              # Run LLM pipeline for LLM_REQUIRED items
    github_token: str | None = None      # Override GITHUB_TOKEN env var
    spark_version: str = "3.3"          # Target PySpark version
    save_cir: bool = True               # Persist annotated CIR JSON to disk


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

    def run(self, dtsx_path: Path) -> PipelineResult:
        result = PipelineResult(source_file=str(dtsx_path))
        logger.info("=== Pipeline start: %s ===", dtsx_path.name)

        # Phase 1: Parse
        try:
            cir = self._parser.parse(dtsx_path)
            result.cir = cir
            logger.info("[1/5] Parse complete: complexity=%s", cir.metadata.complexity_score.value)
        except Exception as exc:
            result.error = f"Parse failed: {exc}"
            logger.error("Parse failed for %s: %s", dtsx_path, exc)
            return result

        # Phase 2: Deterministic transformation
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

        # Phase 3: LLM augmentation (optional)
        if self._config.enable_llm and cir.conversion_metadata.llm_required_items:
            try:
                from ssis_migration.transform.llm import LLMPipeline
                llm_pipeline = LLMPipeline(github_token=self._config.github_token)
                cir = llm_pipeline.process(cir)
                logger.info(
                    "[3/5] LLM complete: human_review=%d",
                    len(cir.conversion_metadata.human_review_required),
                )
                if self._config.save_cir:
                    self._save_cir(cir, dtsx_path, stage="resolved")
            except Exception as exc:
                logger.warning("LLM pipeline failed (continuing without): %s", exc)
        else:
            logger.info("[3/5] LLM skipped (no LLM_REQUIRED items or disabled)")

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

    def _save_cir(self, cir: CIR, dtsx_path: Path, stage: str) -> None:
        cir_dir = self._config.cir_dir or (self._config.output_dir / "cir")
        cir_dir.mkdir(parents=True, exist_ok=True)
        cir_path = cir_dir / f"{dtsx_path.stem}_cir_{stage}.json"
        cir.save(cir_path)
        logger.debug("Saved CIR: %s", cir_path)
