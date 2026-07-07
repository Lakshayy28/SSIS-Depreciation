"""
Generated-module quality gates: the final .py must compile, chain DataFrames
correctly, resolve real connection names, use target-compatible syntax, and
carry HUMAN REVIEW banners for anything unimplemented.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from ssis_migration.pipeline import ConversionMode, MigrationPipeline, PipelineConfig
from ssis_migration.validation.static import StaticValidator

SAMPLES = Path(__file__).resolve().parents[1] / "samples"

pytestmark = pytest.mark.skipif(
    not (SAMPLES / "ETL_Load_Orders.dtsx").exists(), reason="samples missing"
)


@pytest.fixture(scope="module")
def orders_module(tmp_path_factory):
    out = tmp_path_factory.mktemp("codegen")
    config = PipelineConfig(output_dir=out, mode=ConversionMode.DETERMINISTIC,
                            spark_version="2.4.8")
    result = MigrationPipeline(config).run(SAMPLES / "ETL_Load_Orders.dtsx")
    assert result.module_path is not None
    return result.module_path.read_text(encoding="utf-8")


def test_module_compiles(orders_module):
    ast.parse(orders_module)     # raises on failure


def test_no_py310_only_syntax(orders_module):
    # Spark 2.4 clusters run Python 3.7 — PEP 604 unions must not appear.
    assert "dict | None" not in orders_module
    assert "Optional[dict]" in orders_module


def test_df_chain_is_consistent(orders_module):
    # The source assigns the running variable `df`; downstream reads it.
    assert re.search(r"df = \(\s*\n\s*spark\.read\.format\(\"jdbc\"\)", orders_module)
    assert "df_input" not in orders_module


def test_connections_keyed_by_name(orders_module):
    assert '"OLEDB_Source"' in orders_module
    assert 'connections["OLEDB_Source"]["url"]' in orders_module
    # destination prefers the dest connection when the DTSX omits the ref
    assert 'connections["OLEDB_Dest"]' in orders_module


def test_no_passwords_emitted(orders_module):
    for line in orders_module.splitlines():
        if re.search(r'"password"\s*:', line):
            assert re.search(r'"password":\s*""', line), f"password value leaked: {line}"


def test_aggregate_uses_real_functions(orders_module):
    assert 'df.groupBy("RegionCode")' in orders_module
    assert 'F.sum("TotalAmount").alias("TotalRevenue")' in orders_module
    assert 'F.avg("TotalAmount").alias("AvgOrderValue")' in orders_module


def test_sequence_children_are_rendered(orders_module):
    # 'Get Max OrderID' lives INSIDE the SEQ container — the old template
    # dropped container children entirely.
    assert "Get Max OrderID" in orders_module
    assert "(nested level 1)" in orders_module
    assert "Sequence container: SEQ Pre-Processing" in orders_module


def test_human_review_banner_with_source_excerpt(orders_module):
    assert "HUMAN REVIEW REQUIRED" in orders_module
    # the stored-proc EXEC that deterministic mode can't convert
    banner_zone = orders_module[orders_module.index("Execute Merge Order Fact"):]
    assert "EXEC" in banner_zone[:2000]


def test_unimplemented_dataflow_raises_not_silent(orders_module):
    assert "raise NotImplementedError" in orders_module or "df is None" in orders_module


# ─── undefined-name static check ──────────────────────────────────────────────

def _report_for(source: str, tmp_path):
    from ssis_migration.cir.models import CIR, CIRMetadata
    mod = tmp_path / "m.py"
    mod.write_text(source, encoding="utf-8")
    cir = CIR(metadata=CIRMetadata(source_file="x.dtsx"))
    return StaticValidator("3.3").validate(mod, cir)


def test_undefined_name_flagged(tmp_path):
    report = _report_for("def run():\n    return helper_never_defined()\n", tmp_path)
    assert any(f.code == "UNDEFINED_NAME" for f in report.errors)


def test_defined_names_not_flagged(tmp_path):
    src = (
        "import os\n"
        "def helper():\n    return os.getcwd()\n"
        "def run(spark):\n    x = helper()\n    return [i for i in range(3)] + [x, spark]\n"
    )
    report = _report_for(src, tmp_path)
    assert not any(f.code == "UNDEFINED_NAME" for f in report.errors)


def test_api_compat_ignores_comments(tmp_path):
    src = "# AGG Region Summary (aggregate)\nx = 1\n"
    report = _report_for(src, tmp_path)
    # StaticValidator("3.3") never flags; use 2.4 target for the check
    report24 = None
    from ssis_migration.cir.models import CIR, CIRMetadata
    mod = tmp_path / "m24.py"
    mod.write_text(src, encoding="utf-8")
    cir = CIR(metadata=CIRMetadata(source_file="x.dtsx"))
    report24 = StaticValidator("2.4").validate(mod, cir)
    assert not any(f.code == "API_COMPAT" for f in report24.errors)


def test_api_compat_flags_real_calls(tmp_path):
    from ssis_migration.cir.models import CIR, CIRMetadata
    mod = tmp_path / "m24b.py"
    mod.write_text("out = df.mapInPandas(fn, schema)\ndf = out\n", encoding="utf-8")
    cir = CIR(metadata=CIRMetadata(source_file="x.dtsx"))
    report = StaticValidator("2.4").validate(mod, cir)
    assert any(f.code == "API_COMPAT" for f in report.errors)
