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
    AUTO = "auto"        # deterministic + risk-aware router decides per item


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
    routing_plan: object | None = None     # RoutingPlan (AUTO mode only)
    scorecard: object | None = None        # MigrationScorecard (LLM modes)
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

        # Retain the raw DTSX XML so the equivalence reviewer + parsing-fidelity
        # auditor can compare DTSX ↔ CIR ↔ PySpark directly (ground truth).
        try:
            dtsx_xml = dtsx_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            dtsx_xml = ""

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

            # AUTO mode: risk-aware router decides, per item, what the
            # deterministic engine produced can be trusted vs. what must be
            # escalated to the LLM (or to a human) for faithfulness.
            if mode == ConversionMode.AUTO:
                from ssis_migration.transform.routing import Router
                router = Router()
                plan = router.plan(cir)
                result.routing_plan = plan
                logger.info("[2.5/5] AUTO routing: %s", plan.counts())
                self._save_routing_report(plan, dtsx_path)

            if self._config.save_cir:
                self._save_cir(cir, dtsx_path, stage="annotated")
        else:
            logger.info("[2/5] Deterministic skipped (mode=llm)")

        # Phase 3+4: LLM augmentation → codegen → functional validation loop
        # (Phases 3 and 4 are wrapped in an outer loop so a failing functional
        # validation can re-trigger LLM conversion with the validator's feedback.)
        from ssis_migration.config import cfg as _cfg

        func_result = None      # last FunctionalValidationResult, for the scorecard
        llm_pipeline = None     # ONE instance per package: agent memory and the
                                # hybrid manifest accumulate across outer passes

        if mode == ConversionMode.DETERMINISTIC:
            logger.info("[3/5] LLM skipped (mode=deterministic)")
            paths = self._codegen(cir, result, dtsx_path)
            if paths is None:
                return result

        else:
            if self._config.github_token:
                try:
                    from ssis_migration.transform.llm import LLMPipeline
                    llm_pipeline = LLMPipeline(
                        github_token=self._config.github_token,
                        spark_version=self._config.spark_version,
                    )
                except Exception as exc:
                    logger.warning("LLM pipeline unavailable (%s) — deterministic output only", exc)
            # HYBRID / LLM / AUTO: review→regen→re-validate loop. Each pass:
            #   convert LLM items → generate code → review equivalence vs DTSX →
            #   if it fails, feed the critical issues back and re-convert.
            # The final artifact is ALWAYS validated (even with 0 LLM items, so
            # deterministic-only output is still equivalence-checked).
            max_func_iters = max(1, _cfg.functional_validation_max_iterations)
            functional_feedback: list[str] | None = None
            can_validate = bool(self._config.github_token)

            for func_iter in range(1, max_func_iters + 1):
                logger.info("[3/5] conversion pass %d/%d", func_iter, max_func_iters)

                if mode == ConversionMode.LLM:
                    self._flag_all_for_llm(cir)

                has_llm_items = bool(cir.conversion_metadata.llm_required_items)
                if has_llm_items and llm_pipeline is not None:
                    self._run_llm(llm_pipeline, cir, dtsx_path, functional_feedback=functional_feedback)
                else:
                    logger.info("[3/5] LLM skipped (%s)",
                                "0 LLM_REQUIRED items" if not has_llm_items else "no LLM pipeline")

                # Phase 4: generate code (with whole-file compile gate)
                paths = self._codegen(cir, result, dtsx_path, llm_pipeline)
                if paths is None:
                    return result

                if not can_validate or llm_pipeline is None:
                    break

                # Equivalence review of the freshly generated artifact.
                func_result = self._run_functional_validation(
                    llm_pipeline, result.module_path, cir, dtsx_path, dtsx_xml,
                )

                regen_possible = has_llm_items and func_iter < max_func_iters
                if func_result is not None and not func_result.passed and regen_possible:
                    logger.warning(
                        "[3/5] Equivalence review FAILED (score=%.2f) — re-converting "
                        "with %d critical issue(s) as feedback (iter %d/%d)",
                        func_result.equivalence_score, len(func_result.critical_issues),
                        func_iter, max_func_iters,
                    )
                    functional_feedback = func_result.critical_issues
                    self._reset_llm_items(cir)
                    continue

                logger.info(
                    "[3/5] Equivalence review %s on iter %d",
                    "PASSED" if (func_result is None or func_result.passed) else
                    "still failing (iterations exhausted)", func_iter,
                )
                break

        # Phase 5: Validation
        try:
            static_report = self._static_validator.validate(result.module_path, cir)
            semantic_report = self._semantic_validator.validate(result.module_path, cir)

            merged = static_report
            merged.findings.extend(semantic_report.findings)
            merged.acceptable_divergences.extend(semantic_report.acceptable_divergences)

            result.validation_report = merged
            logger.info("[5/5] Validation: %s", merged.summary())

            report_path = self._config.output_dir / f"validation_report_{dtsx_path.stem}.json"
            merged.save(report_path)
        except Exception as exc:
            logger.error("Validation failed: %s", exc)
            result.error = f"Validation failed: {exc}"

        # Scorecard: dual-axis (parsing × functional) migration fidelity score.
        if mode != ConversionMode.DETERMINISTIC and result.module_path is not None:
            try:
                result.scorecard = self._build_scorecard(
                    llm_pipeline, cir, dtsx_path, dtsx_xml, result.module_path, func_result,
                )
                logger.info("[score] %s", result.scorecard.summary())
            except Exception as exc:
                logger.warning("Scorecard build failed (skipping): %s", exc)

        logger.info("=== Pipeline end: %s — %s ===", dtsx_path.name,
                    "SUCCESS" if result.success else "ISSUES FOUND")
        return result

    def _codegen(
        self, cir: CIR, result: PipelineResult, dtsx_path: Path, llm_pipeline=None,
    ) -> dict | None:
        """Run code generation, populate result.module_path/test_path. Returns paths or None on error."""
        try:
            paths = self._generator.generate(cir)
            result.module_path = paths["module"]
            result.test_path = paths["test"]
            logger.info("[4/5] Code generation complete: %s", paths["module"])
        except Exception as exc:
            result.error = f"Code generation failed: {exc}"
            logger.error("Code generation failed: %s", exc)
            return None

        # WHOLE-FILE COMPILE GATE — the final module must be importable Python.
        # Deterministic repair always runs; the LLM syntax editor joins in when
        # a pipeline (and therefore a token) is available.
        try:
            from ssis_migration.config import cfg
            from ssis_migration.transform.llm.repair import ensure_compilable, syntax_error

            source = result.module_path.read_text(encoding="utf-8")
            if syntax_error(source) is not None:
                repair = ensure_compilable(
                    source,
                    fixer=getattr(llm_pipeline, "fixer", None),
                    max_llm_fixes=cfg.syntax_fix_max_iterations,
                    label=result.module_path.name,
                )
                if repair.ok and repair.code.strip():
                    result.module_path.write_text(repair.code, encoding="utf-8")
                    logger.info(
                        "[4/5] Whole-file syntax repair succeeded (%s)",
                        " → ".join(repair.stages) or "normalization",
                    )
                else:
                    logger.error(
                        "[4/5] Generated module STILL does not compile: %s", repair.error,
                    )
        except Exception as exc:
            logger.warning("Whole-file compile gate errored (module left as-is): %s", exc)

        return paths

    def _run_llm(
        self,
        llm_pipeline,
        cir: CIR,
        dtsx_path: Path,
        functional_feedback: list[str] | None = None,
    ) -> None:
        try:
            llm_pipeline.process(cir, functional_context=functional_feedback)
            logger.info(
                "[3/5] LLM complete: human_review=%d",
                len(cir.conversion_metadata.human_review_required),
            )
            # Persist the hybrid stage (chunk-by-chunk assembly record).
            try:
                manifest_path = self._config.output_dir / f"hybrid_{dtsx_path.stem}.json"
                self._config.output_dir.mkdir(parents=True, exist_ok=True)
                llm_pipeline.manifest.save(manifest_path)
                logger.info("[3/5] Hybrid assembly manifest: %s (%s)",
                            manifest_path.name, llm_pipeline.manifest.summary())
            except Exception as exc:
                logger.debug("Manifest save skipped: %s", exc)
            if self._config.save_cir:
                self._save_cir(cir, dtsx_path, stage="resolved")
        except Exception as exc:
            logger.warning("LLM pipeline failed (continuing without): %s", exc)

    def _run_functional_validation(
        self, llm_pipeline, module_path: Path, cir: CIR, dtsx_path: Path, dtsx_xml: str = "",
    ):
        """Run functional equivalence review. Returns FunctionalValidationResult or None."""
        try:
            pyspark_code = module_path.read_text(encoding="utf-8")
            func_result = llm_pipeline.functional_validate(pyspark_code, cir, dtsx_xml=dtsx_xml)
            logger.info(
                "Equivalence review: passed=%s score=%.2f critical=%d warnings=%d version=%d",
                func_result.passed,
                func_result.equivalence_score,
                len(func_result.critical_issues),
                len(func_result.warnings),
                len(func_result.version_issues),
            )
            return func_result
        except Exception as exc:
            logger.warning("Functional validation failed (skipping): %s", exc)
            return None

    def _build_scorecard(self, llm_pipeline, cir, dtsx_path, dtsx_xml, module_path, func_result):
        """Assemble the dual-axis (parsing × functional) scorecard for this package."""
        from ssis_migration import scoring

        # ── Parsing axis: DTSX → CIR ──────────────────────────────────────────
        dtsx_counts = scoring.count_dtsx_elements(dtsx_path)
        cir_counts = scoring.count_cir_elements(cir)
        coverage, detail = scoring.structural_coverage(dtsx_counts, cir_counts)
        unmapped = scoring._unmapped_items(cir)

        llm_fidelity = None
        parse_issues: list[str] = []
        if llm_pipeline is not None:
            try:
                pf = llm_pipeline.parsing_fidelity(cir, dtsx_xml)
                llm_fidelity = pf.fidelity_score
                parse_issues = list(pf.missing_elements) + list(pf.misrepresentations)
            except Exception as exc:
                logger.warning("Parsing-fidelity audit failed (using coverage only): %s", exc)

        parsing = scoring.compute_parsing_score(
            coverage, detail, unmapped, llm_fidelity, parse_issues,
        )

        # ── Functional axis: CIR/DTSX → PySpark ──────────────────────────────
        code = module_path.read_text(encoding="utf-8") if module_path.exists() else ""
        det_version_ok, det_version_issues = scoring.check_pyspark_version(
            code, self._config.spark_version,
        )
        judged = func_result is not None
        if func_result is not None:
            equivalence = func_result.equivalence_score
            critical = func_result.critical_issues
            warnings = func_result.warnings
            llm_version_issues = func_result.version_issues
        else:
            # Judge never ran — placeholders only; build_scorecard will not PASS.
            equivalence, critical, llm_version_issues = 1.0, [], []
            warnings = ["functional equivalence was NOT judged (LLM unavailable)"]

        version_issues = det_version_issues + list(llm_version_issues)
        version_ok = det_version_ok and not llm_version_issues
        functional = scoring.compute_functional_score(
            equivalence, critical, warnings, version_ok, version_issues,
            judged=judged,
        )

        from ssis_migration.config import cfg
        card = scoring.build_scorecard(
            self._config.spark_version, parsing, functional,
            threshold=cfg.migration_pass_threshold,
            human_review_items=len(cir.conversion_metadata.human_review_required),
        )

        self._config.output_dir.mkdir(parents=True, exist_ok=True)
        card.save(self._config.output_dir / f"scorecard_{dtsx_path.stem}.json")
        return card

    def _reset_llm_items(self, cir: CIR) -> None:
        """Reset LLM_COMPLETE items back to LLM_REQUIRED so they can be re-converted."""
        for exe in cir.control_flow.execution_tree:
            if exe.conversion_status == ConversionStatus.LLM_COMPLETE:
                exe.conversion_status = ConversionStatus.LLM_REQUIRED
                exe.pyspark_snippet = None
                cir.flag_for_llm(exe.id)
            if exe.sql and exe.sql.transpilation_status.value == "complete":
                from ssis_migration.cir.models import TranspilationStatus
                exe.sql.transpilation_status = TranspilationStatus.LLM_REQUIRED
        for df in cir.data_flows:
            for comp in df.components:
                if comp.conversion_status == ConversionStatus.LLM_COMPLETE:
                    comp.conversion_status = ConversionStatus.LLM_REQUIRED
                    comp.pyspark_snippet = None
                    cir.flag_for_llm(comp.id)
        # Clear human-review flags too so they get another chance
        cir.conversion_metadata.human_review_required.clear()

    def _flag_all_for_llm(self, cir: CIR) -> None:
        """In pure LLM mode: flag convertible items for LLM; resolve structural ones immediately."""
        from ssis_migration.cir.models import ConversionStatus, TranspilationStatus

        _STRUCTURAL = ("sequence", "for_loop", "foreach_loop")

        for exe in cir.control_flow.execution_tree:
            if exe.sql:
                exe.sql.transpilation_status = TranspilationStatus.LLM_REQUIRED

            if exe.type in _STRUCTURAL:
                # Containers have no code body — resolve immediately so they
                # don't surface as UNCONVERTED in validation.
                exe.conversion_status = ConversionStatus.DETERMINISTIC
            elif exe.type == "data_flow":
                # Data flow executables are resolved via their components below.
                exe.conversion_status = ConversionStatus.DETERMINISTIC
            else:
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

    def _save_routing_report(self, plan, dtsx_path: Path) -> None:
        import json
        self._config.output_dir.mkdir(parents=True, exist_ok=True)
        path = self._config.output_dir / f"routing_report_{dtsx_path.stem}.json"
        path.write_text(json.dumps(plan.to_report(), indent=2), encoding="utf-8")
        logger.debug("Saved routing report: %s", path)
