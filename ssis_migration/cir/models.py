"""
Pydantic models for the Canonical Intermediate Representation (CIR).

The CIR is the central artefact of the migration framework — it bridges the
SSIS XML parser and the PySpark code generator, capturing both business logic
and execution semantics (precedence constraints, transaction scoping, event
handlers, variable mutation order).
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


# ─── Enumerations ────────────────────────────────────────────────────────────

class ComplexityLevel(str, Enum):
    SIMPLE = "simple"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very_high"


class ConversionStatus(str, Enum):
    PENDING = "pending"
    DETERMINISTIC = "deterministic"
    LLM_REQUIRED = "llm_required"
    LLM_COMPLETE = "llm_complete"
    HUMAN_REVIEW = "human_review"
    COMPLETE = "complete"
    FAILED = "failed"


class TranspilationStatus(str, Enum):
    PENDING = "pending"
    COMPLETE = "complete"
    LLM_REQUIRED = "llm_required"
    FAILED = "failed"


class PrecedenceEvaluation(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    COMPLETION = "completion"
    EXPRESSION = "expression"


class ResultSetType(str, Enum):
    NONE = "none"
    SINGLE_ROW = "single_row"
    FULL = "full"
    XML = "xml"


class CacheMode(str, Enum):
    FULL = "full"
    PARTIAL = "partial"
    NONE = "none"


class NoMatchBehavior(str, Enum):
    FAIL = "fail"
    REDIRECT_TO_ERROR = "redirect_to_error"
    REDIRECT_TO_NO_MATCH = "redirect_to_no_match"
    IGNORE_FAILURE = "ignore_failure"


class JoinType(str, Enum):
    INNER = "inner"
    LEFT_OUTER = "left_outer"
    RIGHT_OUTER = "right_outer"
    FULL_OUTER = "full_outer"


# ─── Sub-models ──────────────────────────────────────────────────────────────

class SqlStatement(BaseModel):
    original_dialect: str = "tsql"
    original_text: str
    transpiled_dialect: str = "spark_sql"
    transpiled_text: str | None = None
    transpilation_status: TranspilationStatus = TranspilationStatus.PENDING
    transpilation_notes: str | None = None


class ColumnMapping(BaseModel):
    source: str
    destination: str
    cast_to: str | None = None


class OutputColumn(BaseModel):
    name: str
    ssis_type: str
    mapped_type: str          # CIR canonical type
    pyspark_type: str | None = None
    nullable: bool = True
    length: int | None = None
    precision: int | None = None
    scale: int | None = None


class ExpressionNode(BaseModel):
    output_column: str
    ssis_expression: str
    pyspark_expression: str | None = None
    translation_status: TranspilationStatus = TranspilationStatus.PENDING
    translation_notes: str | None = None


class JoinColumn(BaseModel):
    input: str
    lookup: str


class ConnectionTargetMapping(BaseModel):
    type: str                   # e.g. "spark_jdbc", "s3_csv"
    url_template: str | None = None
    driver: str | None = None
    format: str | None = None
    options: dict[str, str] = Field(default_factory=dict)


# ─── Top-level CIR sub-sections ──────────────────────────────────────────────

class CIRParameter(BaseModel):
    name: str
    data_type: str
    default_value: Any = None
    scope: str = "package"
    sensitive: bool = False


class CIRVariable(BaseModel):
    name: str
    data_type: str
    default_value: Any = None
    scope: str = "package"
    expression: str | None = None
    evaluate_as_expression: bool = False


class CIRConnection(BaseModel):
    id: str
    name: str
    provider_type: str          # oledb | adonet | odbc | flatfile | excel | xml
    connection_string_template: str | None = None
    resolved_parameters: dict[str, str] = Field(default_factory=dict)
    target_mapping: ConnectionTargetMapping | None = None


class PrecedenceConstraint(BaseModel):
    from_id: str = Field(alias="from")
    to_id: str = Field(alias="to")
    evaluation: PrecedenceEvaluation = PrecedenceEvaluation.SUCCESS
    expression: str | None = None
    logical_and: bool = True

    model_config = {"populate_by_name": True}


# ─── Data Flow components ─────────────────────────────────────────────────────

class DataFlowComponent(BaseModel):
    id: str
    name: str
    type: str                   # source | transformation | destination
    subtype: str                # oledb_source | derived_column | lookup | …
    connection_ref: str | None = None

    # Source-specific
    access_mode: str | None = None
    sql_command: SqlStatement | None = None
    output_columns: list[OutputColumn] = Field(default_factory=list)

    # Derived Column / Expression Task
    expressions: list[ExpressionNode] = Field(default_factory=list)

    # Lookup
    cache_mode: CacheMode | None = None
    lookup_sql: str | None = None
    join_columns: list[JoinColumn] = Field(default_factory=list)
    no_match_behavior: NoMatchBehavior | None = None

    # Merge Join
    join_type: JoinType | None = None
    join_key_columns: list[str] = Field(default_factory=list)

    # Aggregate
    group_by_columns: list[str] = Field(default_factory=list)
    aggregations: list[dict[str, str]] = Field(default_factory=list)

    # Destination
    table_name: str | None = None
    column_mappings: list[ColumnMapping] = Field(default_factory=list)

    # Script Component
    script_language: str | None = None        # csharp | vbnet
    script_code: str | None = None
    referenced_assemblies: list[str] = Field(default_factory=list)

    # Flat File specifics
    file_path: str | None = None
    delimiter: str | None = None
    text_qualifier: str | None = None
    has_header: bool = True

    # Generic extra properties (for vendor components)
    extra_properties: dict[str, Any] = Field(default_factory=dict)

    # Conversion tracking
    pyspark_snippet: str | None = None
    conversion_status: ConversionStatus = ConversionStatus.PENDING
    conversion_notes: str | None = None


class DataFlowPath(BaseModel):
    from_id: str = Field(alias="from")
    to_id: str | None = Field(alias="to")
    type: str = "default"       # default | match | no_match | error
    handler: str | None = None

    model_config = {"populate_by_name": True}


class DataFlow(BaseModel):
    id: str
    name: str
    components: list[DataFlowComponent] = Field(default_factory=list)
    paths: list[DataFlowPath] = Field(default_factory=list)


# ─── Control Flow executables ─────────────────────────────────────────────────

class ControlFlowExecutable(BaseModel):
    id: str
    name: str
    type: str   # execute_sql | data_flow | script_task | for_loop | foreach_loop
                # | sequence | expression_task | execute_package | file_system | ftp
                # | send_mail | execute_process

    # Execute SQL Task
    sql: SqlStatement | None = None
    connection_ref: str | None = None
    result_set: ResultSetType | None = None
    parameter_mappings: list[dict[str, str]] = Field(default_factory=list)

    # Data Flow Task
    data_flow_ref: str | None = None

    # Script Task
    script_language: str | None = None
    script_code: str | None = None
    referenced_assemblies: list[str] = Field(default_factory=list)
    read_only_variables: list[str] = Field(default_factory=list)
    read_write_variables: list[str] = Field(default_factory=list)

    # For Loop / Foreach Loop
    loop_variable: str | None = None
    loop_init_expression: str | None = None
    loop_eval_expression: str | None = None
    loop_assign_expression: str | None = None

    # Expression Task
    expression: str | None = None

    # Execute Package Task
    child_package_ref: str | None = None

    # Nested executables (for Sequence/Loop containers)
    children: list[ControlFlowExecutable] = Field(default_factory=list)

    # Conversion tracking
    pyspark_snippet: str | None = None
    conversion_status: ConversionStatus = ConversionStatus.PENDING
    conversion_notes: str | None = None


class ControlFlow(BaseModel):
    execution_tree: list[ControlFlowExecutable] = Field(default_factory=list)
    precedence_constraints: list[PrecedenceConstraint] = Field(default_factory=list)


# ─── Event Handlers ──────────────────────────────────────────────────────────

class EventHandler(BaseModel):
    event: str                  # OnError | OnWarning | OnPreExecute | …
    scope: str = "package"
    executables: list[ControlFlowExecutable] = Field(default_factory=list)


# ─── Lineage ─────────────────────────────────────────────────────────────────

class ColumnLineageEntry(BaseModel):
    destination: str
    derived_from: list[str]
    transformation: str | None = None


class Lineage(BaseModel):
    sources: list[str] = Field(default_factory=list)
    destinations: list[str] = Field(default_factory=list)
    column_lineage: list[ColumnLineageEntry] = Field(default_factory=list)


# ─── Complexity details ───────────────────────────────────────────────────────

class ComplexityDetails(BaseModel):
    total_executables: int = 0
    data_flow_components: int = 0
    script_tasks: int = 0
    custom_components: int = 0
    ssis_expressions: int = 0
    sql_statements: int = 0
    cross_package_refs: int = 0
    third_party_components: list[str] = Field(default_factory=list)


# ─── Conversion Metadata ──────────────────────────────────────────────────────

class ConversionMetadata(BaseModel):
    deterministic_coverage: float = 0.0   # fraction 0.0–1.0
    llm_required_items: list[str] = Field(default_factory=list)
    human_review_required: list[str] = Field(default_factory=list)
    conversion_status: ConversionStatus = ConversionStatus.PENDING


# ─── Top-level CIR ───────────────────────────────────────────────────────────

class CIRMetadata(BaseModel):
    schema_version: str = "cir-schema-v1"
    source_file: str
    source_hash: str | None = None
    parse_timestamp: str | None = None
    parser_version: str = "0.1.0"
    complexity_score: ComplexityLevel = ComplexityLevel.SIMPLE
    complexity_details: ComplexityDetails = Field(default_factory=ComplexityDetails)
    # DTSX→CIR structural capture, recorded at parse time so canonical-stage
    # completeness is auditable without re-reading the source file:
    # {"coverage": 0.97, "detail": {category: {"dtsx": n, "cir": m, "coverage": r}}}
    parse_coverage: dict[str, Any] | None = None
    # DTSX package format era ("2"/"3" = 2005/2008 property-element style,
    # "6"/"8" = 2012+/2014+ attribute style) and protection level, so downstream
    # stages know what dialect they came from and whether sensitive values
    # were stripped by encryption.
    package_format_version: str | None = None
    protection_level: str | None = None


class CIR(BaseModel):
    """
    Canonical Intermediate Representation for a single SSIS package.

    This is the single source of truth between the parser and code generator.
    It must be serialisable to JSON (for version control diffing) and
    human-readable without reference to the source DTSX.
    """

    metadata: CIRMetadata
    parameters: list[CIRParameter] = Field(default_factory=list)
    variables: list[CIRVariable] = Field(default_factory=list)
    connections: list[CIRConnection] = Field(default_factory=list)
    control_flow: ControlFlow = Field(default_factory=ControlFlow)
    data_flows: list[DataFlow] = Field(default_factory=list)
    event_handlers: list[EventHandler] = Field(default_factory=list)
    lineage: Lineage = Field(default_factory=Lineage)
    conversion_metadata: ConversionMetadata = Field(default_factory=ConversionMetadata)

    # ── serialisation helpers ──────────────────────────────────────────────

    def to_json(self, indent: int = 2) -> str:
        return self.model_dump_json(indent=indent, by_alias=True)

    def save(self, path: Path | str) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: Path | str) -> "CIR":
        raw = Path(path).read_text(encoding="utf-8")
        return cls.model_validate(json.loads(raw))

    def find_connection(self, ref: str | None) -> "CIRConnection | None":
        """Resolve a connection reference (id, name, or a DTSX refId like
        'Package.ConnectionManagers[Name]') to the CIRConnection."""
        if not ref:
            return None
        needle = ref.lower()
        for conn in self.connections:
            if conn.id.lower() == needle or conn.name.lower() == needle:
                return conn
        for conn in self.connections:
            if conn.name and conn.name.lower() in needle:
                return conn
        return None

    def flag_for_llm(self, item_id: str) -> None:
        if item_id not in self.conversion_metadata.llm_required_items:
            self.conversion_metadata.llm_required_items.append(item_id)

    def flag_for_human_review(self, item_id: str) -> None:
        if item_id not in self.conversion_metadata.human_review_required:
            self.conversion_metadata.human_review_required.append(item_id)
