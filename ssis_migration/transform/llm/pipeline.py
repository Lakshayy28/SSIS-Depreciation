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
    ParsingFidelityAgent,
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
        from ssis_migration.transform.llm.assembly import AssemblyManifest
        from ssis_migration.transform.llm.chunking import AgentMemory
        from ssis_migration.transform.llm.repair import SyntaxFixer

        self._spark_version = spark_version
        self._client = CopilotClient(token=github_token)
        _reviewer_model = reviewer_model or cfg.copilot_reviewer_model
        _max_iter = cfg.copilot_max_review_iterations

        # Shared per-package state: one memory + one hybrid-stage manifest.
        # The pipeline instance is reused across functional-validation passes,
        # so memory (symbols, pitfalls) accumulates over the whole conversion.
        self.memory = AgentMemory(facts={"spark_version": spark_version})
        self.manifest = AssemblyManifest(package="", spark_version=spark_version)
        # Public: the main pipeline reuses this fixer for the whole-file gate.
        self.fixer = SyntaxFixer(self._client, spark_version=spark_version)
        self._fixer = self.fixer

        reviewer = ReviewAgent(self._client, reviewer_model=_reviewer_model)
        self._script_agent = ScriptTaskAgent(
            self._client, reviewer,
            max_iterations=_max_iter,
            spark_version=spark_version,
            fixer=self._fixer, memory=self.memory, manifest=self.manifest,
        )
        self._sql_agent = ComplexSQLAgent(
            self._client, reviewer,
            max_iterations=_max_iter,
            spark_version=spark_version,
            fixer=self._fixer, memory=self.memory, manifest=self.manifest,
        )
        self._expr_agent = ExpressionAgent(self._client, spark_version=spark_version)
        # Judges use the (stronger) reviewer model, like the component reviewer.
        self._func_validator = FunctionalValidatorAgent(
            self._client, spark_version=spark_version, reviewer_model=_reviewer_model,
        )
        self._parsing_agent = ParsingFidelityAgent(
            self._client, reviewer_model=_reviewer_model,
        )

    def process(self, cir: CIR, functional_context: list[str] | None = None) -> CIR:
        logger.info(
            "LLM pipeline: %d items to process (spark=%s)",
            len(cir.conversion_metadata.llm_required_items),
            self._spark_version,
        )
        if functional_context:
            logger.info("Carrying %d functional-validation issues from previous pass", len(functional_context))
            self.memory.record_issues(functional_context)

        self._seed_memory(cir)

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

    def functional_validate(self, pyspark_code: str, cir: CIR, dtsx_xml: str | None = None):
        """
        Package-level functional equivalence review comparing the raw DTSX, the
        CIR, and the generated PySpark. Returns FunctionalValidationResult.
        """
        cir_summary = _build_cir_summary(cir)
        excerpt = _excerpt_dtsx(dtsx_xml) if dtsx_xml else "not available"
        return self._func_validator.validate(pyspark_code, cir_summary, dtsx_excerpt=excerpt)

    def parsing_fidelity(self, cir: CIR, dtsx_xml: str | None = None):
        """LLM audit of DTSX → CIR fidelity. Returns ParsingFidelityResult."""
        cir_summary = _build_cir_summary(cir)
        excerpt = _excerpt_dtsx(dtsx_xml) if dtsx_xml else "not available"
        return self._parsing_agent.audit(excerpt, cir_summary)

    # ── private ───────────────────────────────────────────────────────────────

    def _seed_memory(self, cir: CIR) -> None:
        """Load package-level facts into agent memory (idempotent)."""
        from pathlib import Path as _Path
        self.manifest.package = _Path(cir.metadata.source_file).stem
        if cir.parameters:
            self.memory.add_fact(
                "package_params", ", ".join(p.name for p in cir.parameters)
            )
        if cir.variables:
            self.memory.add_fact(
                "package_vars", ", ".join(v.name for v in cir.variables)
            )
        for conn in cir.connections:
            desc = conn.provider_type
            host = conn.resolved_parameters.get("host")
            db = conn.resolved_parameters.get("database")
            if host or db:
                desc += f" host={host or '?'} db={db or '?'}"
            self.memory.add_fact(f"connection[{conn.name}]", desc)

    def _resolve_connection(self, cir: CIR, connection_ref: str | None):
        """Match an executable/component connection ref to a CIRConnection."""
        if not connection_ref:
            return None
        ref = connection_ref.lower()
        for conn in cir.connections:
            if conn.id.lower() == ref or conn.name.lower() == ref:
                return conn
        for conn in cir.connections:      # refIds look like Package.ConnectionManagers[Name]
            if conn.name.lower() in ref:
                return conn
        return None

    def _process_executable(self, exe, cir: CIR, functional_context) -> None:
        if exe.conversion_status != ConversionStatus.LLM_REQUIRED:
            for child in exe.children:
                self._process_executable(child, cir, functional_context)
            return

        _STRUCTURAL = ("sequence", "for_loop", "foreach_loop", "data_flow")
        _OPERATIONAL = ("file_system", "ftp", "send_mail", "execute_process", "execute_sql")

        try:
            self._convert_executable(exe, cir, functional_context, _STRUCTURAL, _OPERATIONAL)
        except Exception as exc:
            # One broken item must never kill the whole package's LLM pass.
            logger.warning("Conversion of %s failed (%s) — escalating to human review", exe.id, exc)
            exe.conversion_status = ConversionStatus.HUMAN_REVIEW
            exe.conversion_notes = f"conversion error: {exc}"
            cir.flag_for_human_review(exe.id)

        for child in exe.children:
            self._process_executable(child, cir, functional_context)

    def _convert_executable(self, exe, cir: CIR, functional_context,
                            _STRUCTURAL, _OPERATIONAL) -> None:
        if exe.type in _STRUCTURAL:
            exe.conversion_status = ConversionStatus.DETERMINISTIC

        elif exe.type == "script_task" and exe.script_code:
            result = self._script_agent.convert(
                code=exe.script_code,
                language=exe.script_language or "csharp",
                referenced_assemblies=exe.referenced_assemblies,
                functional_context=functional_context,
                item_id=exe.id,
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
                exe.conversion_notes = result.notes or f"confidence={confidence:.2f}"
                cir.flag_for_human_review(exe.id)
                logger.warning("Script task %s escalated to human review (confidence=%.2f)", exe.id, confidence)

        elif exe.sql and exe.sql.transpilation_status == TranspilationStatus.LLM_REQUIRED:
            conn = self._resolve_connection(cir, exe.connection_ref)
            conn_kwargs = {}
            if conn is not None:
                conn_kwargs["connection_name"] = conn.name
                conn_kwargs["connection_type"] = conn.provider_type
                if conn.target_mapping and conn.target_mapping.url_template:
                    conn_kwargs["jdbc_url_template"] = conn.target_mapping.url_template
            result = self._sql_agent.convert(
                sql=exe.sql.original_text,
                partial_transpilation=exe.sql.transpiled_text or "",
                functional_context=functional_context,
                item_id=exe.id,
                **conn_kwargs,
            )
            if result.success:
                exe.sql.transpiled_text = result.code
                exe.sql.transpilation_status = TranspilationStatus.COMPLETE
                exe.sql.transpilation_notes = f"LLM iterations={result.iterations}"
                exe.conversion_status = ConversionStatus.LLM_COMPLETE
            else:
                exe.conversion_status = ConversionStatus.HUMAN_REVIEW
                exe.conversion_notes = result.notes or "SQL conversion failed review"
                if result.code:
                    exe.pyspark_snippet = result.code
                cir.flag_for_human_review(exe.id)

        elif exe.type in _OPERATIONAL:
            exe.conversion_status = ConversionStatus.DETERMINISTIC

        else:
            exe.conversion_status = ConversionStatus.HUMAN_REVIEW
            cir.flag_for_human_review(exe.id)
            logger.warning("No LLM handler for executable %s (type=%s)", exe.id, exe.type)

    def _process_component(self, comp, cir: CIR, functional_context) -> None:
        try:
            self._convert_component(comp, cir, functional_context)
        except Exception as exc:
            logger.warning("Conversion of component %s failed (%s) — human review", comp.id, exc)
            comp.conversion_status = ConversionStatus.HUMAN_REVIEW
            comp.conversion_notes = f"conversion error: {exc}"
            cir.flag_for_human_review(comp.id)

    def _convert_component(self, comp, cir: CIR, functional_context) -> None:
        if comp.subtype in ("script_component",) and comp.script_code:
            result = self._script_agent.convert(
                code=comp.script_code,
                language=comp.script_language or "csharp",
                referenced_assemblies=comp.referenced_assemblies,
                functional_context=functional_context,
                item_id=comp.id,
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


_DTSX_EXCERPT_LIMIT = 16000


def _excerpt_dtsx(dtsx_xml: str, limit: int = _DTSX_EXCERPT_LIMIT) -> str:
    """
    Trim a raw DTSX string to a token-budget-friendly excerpt for the judges.

    Most of a .dtsx's bulk is GUI layout metadata (DTS:DesignTimeProperties,
    component coordinates) that has no bearing on equivalence, so we drop those
    first and only then hard-truncate.
    """
    import re as _re

    if not dtsx_xml:
        return "not available"
    # Strip the layout blob if present — pure GUI metadata, never behavioural.
    cleaned = _re.sub(
        r"<DTS:DesignTimeProperties.*?</DTS:DesignTimeProperties>",
        "<!-- design-time layout omitted -->",
        dtsx_xml,
        flags=_re.DOTALL,
    )
    if len(cleaned) <= limit:
        return cleaned
    head = cleaned[: int(limit * 0.7)]
    tail = cleaned[-int(limit * 0.3):]
    return f"{head}\n\n<!-- … {len(cleaned) - limit} chars omitted … -->\n\n{tail}"


def _build_cir_summary(cir: CIR) -> str:
    """Produce a compact human-readable summary of the CIR for the functional validator."""
    from pathlib import Path as _Path

    lines = [
        f"Package: {_Path(cir.metadata.source_file).stem}",
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
            lines.append(f"  DataFlow id={df.id} name={df.name!r}")
            for comp in df.components:
                detail = [f"    [{comp.id}] {comp.subtype} ({comp.type}) name={comp.name!r}"]
                if comp.sql_command:
                    detail.append(f"      sql: {' '.join(comp.sql_command.original_text.split())[:160]}")
                for expr in comp.expressions:
                    detail.append(
                        f"      expr {expr.output_column} = {expr.ssis_expression[:100]}"
                        + (f"  → {expr.pyspark_expression[:80]}" if expr.pyspark_expression else "")
                    )
                if comp.aggregations:
                    aggs = ", ".join(f"{a.get('function')}({a.get('column')})" for a in comp.aggregations[:8])
                    detail.append(f"      aggregations: {aggs}")
                if comp.join_columns:
                    joins = ", ".join(f"{j.input}={j.lookup}" for j in comp.join_columns)
                    detail.append(f"      lookup join on: {joins}")
                if comp.table_name:
                    detail.append(f"      destination table: {comp.table_name}")
                if comp.column_mappings:
                    detail.append(f"      column mappings: {len(comp.column_mappings)}")
                if comp.extra_properties.get("ParameterMapping"):
                    detail.append(f"      parameter mapping: {comp.extra_properties['ParameterMapping']}")
                if comp.pyspark_snippet:
                    detail.append(f"      pyspark_snippet: {comp.pyspark_snippet[:160]!r}")
                lines.extend(detail)
            if df.paths:
                flow = "; ".join(f"{p.from_id}→{p.to_id}" + (f" [{p.type}]" if p.type != "default" else "")
                                 for p in df.paths)
                lines.append(f"    paths: {flow}")

    return "\n".join(lines)
