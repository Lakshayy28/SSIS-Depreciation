"""
Tests for the DTSX parser layer.

Uses the three real .dtsx files in samples/ that were pulled from public repos.
No SSIS runtime is needed — these tests exercise pure XML parsing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SAMPLES_DIR = Path(__file__).parent.parent / "samples"


def _sample(name: str) -> Path:
    p = SAMPLES_DIR / name
    if not p.exists():
        pytest.skip(f"Sample file not found: {p}")
    return p


# ─── Parser basic parse ───────────────────────────────────────────────────────

class TestDTSXParser:
    def test_parse_returns_cir(self):
        from ssis_migration.parser import DTSXParser
        cir = DTSXParser().parse(_sample("ETL_Load_Customers.dtsx"))
        assert cir is not None
        assert cir.metadata.source_file == "ETL_Load_Customers.dtsx"

    def test_parse_extracts_parameters(self):
        from ssis_migration.parser import DTSXParser
        cir = DTSXParser().parse(_sample("ETL_Load_Customers.dtsx"))
        assert len(cir.parameters) > 0
        param_names = {p.name for p in cir.parameters}
        assert "SourceConnectionString" in param_names or len(param_names) > 0

    def test_parse_extracts_connections(self):
        from ssis_migration.parser import DTSXParser
        cir = DTSXParser().parse(_sample("ETL_Load_Customers.dtsx"))
        assert len(cir.connections) > 0

    def test_parse_sets_complexity_level(self):
        from ssis_migration.parser import DTSXParser
        from ssis_migration.cir.models import ComplexityLevel
        cir = DTSXParser().parse(_sample("ETL_Load_Customers.dtsx"))
        assert cir.metadata.complexity_score in list(ComplexityLevel)

    def test_parse_sql_extract_sample(self):
        from ssis_migration.parser import DTSXParser
        cir = DTSXParser().parse(_sample("SSIS_SQLExtractSample.dtsx"))
        assert cir is not None

    def test_parse_orders(self):
        from ssis_migration.parser import DTSXParser
        cir = DTSXParser().parse(_sample("ETL_Load_Orders.dtsx"))
        assert cir is not None
        assert cir.metadata.source_file == "ETL_Load_Orders.dtsx"

    def test_parse_produces_sha256_hash(self):
        from ssis_migration.parser import DTSXParser
        cir = DTSXParser().parse(_sample("ETL_Load_Customers.dtsx"))
        assert cir.metadata.source_hash is not None
        assert cir.metadata.source_hash.startswith("sha256:")

    def test_cir_round_trip_json(self, tmp_path):
        from ssis_migration.parser import DTSXParser
        from ssis_migration.cir.models import CIR
        cir = DTSXParser().parse(_sample("ETL_Load_Customers.dtsx"))
        out = tmp_path / "test.json"
        cir.save(out)
        loaded = CIR.load(out)
        assert loaded.metadata.source_file == cir.metadata.source_file
        assert loaded.metadata.source_hash == cir.metadata.source_hash


# ─── Complexity Scorer ────────────────────────────────────────────────────────

class TestComplexityScorer:
    def test_simple_package_score(self):
        from ssis_migration.parser import DTSXParser
        from ssis_migration.cir.models import ComplexityLevel
        cir = DTSXParser().parse(_sample("SSIS_SQLExtractSample.dtsx"))
        # This package has basic SQL tasks — should be Simple or Medium
        assert cir.metadata.complexity_score in (ComplexityLevel.SIMPLE, ComplexityLevel.MEDIUM)

    def test_complexity_details_populated(self):
        from ssis_migration.parser import DTSXParser
        cir = DTSXParser().parse(_sample("ETL_Load_Customers.dtsx"))
        d = cir.metadata.complexity_details
        assert d.total_executables >= 0
        assert d.data_flow_components >= 0


# ─── Type Mapping ─────────────────────────────────────────────────────────────

class TestTypeMapping:
    def test_known_types_resolve(self):
        from ssis_migration.cir.type_mapping import resolve_type
        cir_type, pyspark_type, div = resolve_type("DT_I4")
        assert cir_type == "int32"
        assert pyspark_type == "IntegerType"
        assert div is None

    def test_decimal_with_precision(self):
        from ssis_migration.cir.type_mapping import resolve_type
        cir_type, pyspark_type, _ = resolve_type("DT_DECIMAL", precision=10, scale=2)
        assert "10" in cir_type
        assert "2" in cir_type

    def test_unknown_type_defaults_to_string(self):
        from ssis_migration.cir.type_mapping import resolve_type
        _, pyspark_type, div = resolve_type("DT_UNKNOWN_MADE_UP")
        assert pyspark_type == "StringType"
        assert div is not None

    def test_divergence_types_have_notes(self):
        from ssis_migration.cir.type_mapping import KNOWN_DIVERGENCES
        assert "DT_DBTIME2" in KNOWN_DIVERGENCES
        assert "DT_GUID" in KNOWN_DIVERGENCES


# ─── Expression Translator ────────────────────────────────────────────────────

class TestExpressionTranslator:
    def test_upper(self):
        from ssis_migration.transform.deterministic.expression_translator import translate_expression
        from ssis_migration.cir.models import TranspilationStatus
        result = translate_expression('UPPER([CustomerName])')
        assert result.status == TranspilationStatus.COMPLETE
        assert "F.upper" in result.pyspark_expr

    def test_column_ref(self):
        from ssis_migration.transform.deterministic.expression_translator import translate_expression
        from ssis_migration.cir.models import TranspilationStatus
        result = translate_expression('[CustomerID]')
        assert result.status == TranspilationStatus.COMPLETE
        assert 'F.col("CustomerID")' in result.pyspark_expr

    def test_ternary(self):
        from ssis_migration.transform.deterministic.expression_translator import translate_expression
        from ssis_migration.cir.models import TranspilationStatus
        result = translate_expression('ISNULL([Email]) ? "N/A" : [Email]')
        assert result.status == TranspilationStatus.COMPLETE
        assert "F.when" in result.pyspark_expr

    def test_string_concat(self):
        from ssis_migration.transform.deterministic.expression_translator import translate_expression
        from ssis_migration.cir.models import TranspilationStatus
        result = translate_expression('[FirstName] + " " + [LastName]')
        assert result.status == TranspilationStatus.COMPLETE
        assert "F.concat" in result.pyspark_expr

    def test_replacenull(self):
        from ssis_migration.transform.deterministic.expression_translator import translate_expression
        from ssis_migration.cir.models import TranspilationStatus
        result = translate_expression('REPLACENULL([Region], "UNKNOWN")')
        assert result.status == TranspilationStatus.COMPLETE
        assert "coalesce" in result.pyspark_expr

    def test_unknown_function_flags_llm(self):
        from ssis_migration.transform.deterministic.expression_translator import translate_expression
        from ssis_migration.cir.models import TranspilationStatus
        result = translate_expression('TOKEN([PathCol], "/", 2)')
        assert result.status == TranspilationStatus.LLM_REQUIRED


# ─── SQL Transpiler ───────────────────────────────────────────────────────────

class TestSQLTranspiler:
    def test_simple_select(self):
        from ssis_migration.transform.deterministic.sql_transpiler import transpile_sql
        from ssis_migration.cir.models import SqlStatement, TranspilationStatus
        stmt = SqlStatement(original_text="SELECT TOP 10 CustomerID, Name FROM dbo.Customers")
        transpile_sql(stmt)
        assert stmt.transpilation_status == TranspilationStatus.COMPLETE
        assert stmt.transpiled_text is not None
        # LIMIT should appear instead of TOP
        assert "LIMIT" in stmt.transpiled_text.upper() or "limit" in stmt.transpiled_text

    def test_truncate_table(self):
        from ssis_migration.transform.deterministic.sql_transpiler import transpile_sql
        from ssis_migration.cir.models import SqlStatement, TranspilationStatus
        stmt = SqlStatement(original_text="TRUNCATE TABLE stg.Customers")
        transpile_sql(stmt)
        assert stmt.transpilation_status == TranspilationStatus.COMPLETE

    def test_procedural_sql_flags_llm(self):
        from ssis_migration.transform.deterministic.sql_transpiler import transpile_sql
        from ssis_migration.cir.models import SqlStatement, TranspilationStatus
        stmt = SqlStatement(original_text="DECLARE @x INT; WHILE @x < 10 BEGIN SET @x = @x + 1; END")
        transpile_sql(stmt)
        assert stmt.transpilation_status == TranspilationStatus.LLM_REQUIRED

    def test_isnull_to_coalesce(self):
        from ssis_migration.transform.deterministic.sql_transpiler import transpile_sql
        from ssis_migration.cir.models import SqlStatement, TranspilationStatus
        stmt = SqlStatement(original_text="SELECT ISNULL(Email, 'N/A') FROM dbo.Customers")
        transpile_sql(stmt)
        assert stmt.transpilation_status == TranspilationStatus.COMPLETE
        assert stmt.transpiled_text is not None


# ─── CIR model round-trips ────────────────────────────────────────────────────

class TestCIRModels:
    def test_cir_json_serialisation(self):
        from ssis_migration.cir.models import CIR, CIRMetadata, ComplexityLevel
        cir = CIR(metadata=CIRMetadata(source_file="test.dtsx", complexity_score=ComplexityLevel.SIMPLE))
        json_str = cir.to_json()
        assert '"source_file"' in json_str
        loaded = CIR.model_validate_json(json_str)
        assert loaded.metadata.source_file == "test.dtsx"

    def test_flag_for_llm(self):
        from ssis_migration.cir.models import CIR, CIRMetadata
        cir = CIR(metadata=CIRMetadata(source_file="test.dtsx"))
        cir.flag_for_llm("comp_001")
        cir.flag_for_llm("comp_001")  # duplicate should not double-add
        assert cir.conversion_metadata.llm_required_items.count("comp_001") == 1
