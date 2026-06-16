"""Tests for AUTO-mode routing: risk detection and per-item decisions."""

from __future__ import annotations

from pathlib import Path

import pytest

from ssis_migration.cir.models import (
    CIR,
    CIRMetadata,
    ControlFlow,
    ControlFlowExecutable,
    ConversionStatus,
    SqlStatement,
    TranspilationStatus,
)
from ssis_migration.transform.deterministic import DeterministicEngine
from ssis_migration.transform.routing import (
    Router,
    RoutingTarget,
    script_risk_signals,
    sql_risk_signals,
)

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


# ─── risk-signal detectors ────────────────────────────────────────────────────

def test_sql_risk_detects_procedural():
    assert "cursor" in sql_risk_signals("DECLARE c CURSOR FOR SELECT 1; FETCH NEXT FROM c")
    assert "stored_proc_exec" in sql_risk_signals("EXEC dbo.sp_LoadFacts @d=1")
    assert "merge" in sql_risk_signals("MERGE target USING src ON a=b WHEN MATCHED THEN UPDATE")
    assert "dynamic_sql" in sql_risk_signals("EXEC('SELECT * FROM ' + @tbl)")


def test_sql_risk_clean_select():
    assert sql_risk_signals("SELECT a, b FROM orders WHERE a > 1") == []


def test_script_risk_signals():
    assert "com_interop" in script_risk_signals("var x = new ComObject();")
    assert "db_access" in script_risk_signals("using (var c = new SqlConnection()) {}")
    assert script_risk_signals("int x = 1;") == []


# ─── decision logic via a synthetic CIR ───────────────────────────────────────

def _cir_with_executables(*executables) -> CIR:
    return CIR(
        metadata=CIRMetadata(source_file="synthetic.dtsx"),
        control_flow=ControlFlow(execution_tree=list(executables)),
    )


def test_router_escalates_procedural_sql_even_if_transpiled():
    # sqlglot "succeeded" but the SQL is a stored-proc exec → must go to LLM.
    sql = SqlStatement(
        original_text="EXEC dbo.sp_LoadOrders @run=1",
        transpiled_text="-- transpiled",
        transpilation_status=TranspilationStatus.COMPLETE,
    )
    exe = ControlFlowExecutable(id="e1", name="Load", type="execute_sql", sql=sql)
    cir = _cir_with_executables(exe)

    plan = Router().plan(cir)
    decision = plan.decisions[0]
    assert decision.target == RoutingTarget.LLM
    assert "stored_proc_exec" in decision.risk_signals
    assert exe.conversion_status == ConversionStatus.LLM_REQUIRED
    assert "e1" in cir.conversion_metadata.llm_required_items


def test_router_keeps_clean_sql_deterministic():
    sql = SqlStatement(
        original_text="SELECT a FROM t",
        transpiled_text="SELECT a FROM t",
        transpilation_status=TranspilationStatus.COMPLETE,
    )
    exe = ControlFlowExecutable(id="e2", name="Q", type="execute_sql", sql=sql)
    cir = _cir_with_executables(exe)

    plan = Router().plan(cir)
    assert plan.decisions[0].target == RoutingTarget.DETERMINISTIC
    assert exe.conversion_status == ConversionStatus.DETERMINISTIC


def test_router_script_task_to_llm():
    exe = ControlFlowExecutable(
        id="e3", name="S", type="script_task",
        script_code="int x = 1;", script_language="csharp",
    )
    plan = Router().plan(_cir_with_executables(exe))
    assert plan.decisions[0].target == RoutingTarget.LLM


def test_router_execute_package_to_human():
    exe = ControlFlowExecutable(id="e4", name="P", type="execute_package")
    plan = Router().plan(_cir_with_executables(exe))
    assert plan.decisions[0].target == RoutingTarget.HUMAN_REVIEW


def test_router_structural_container_deterministic():
    exe = ControlFlowExecutable(id="e5", name="Seq", type="sequence")
    plan = Router().plan(_cir_with_executables(exe))
    assert plan.decisions[0].target == RoutingTarget.DETERMINISTIC


# ─── end-to-end on a real sample ──────────────────────────────────────────────

@pytest.mark.skipif(not (SAMPLES / "ETL_Load_Orders.dtsx").exists(), reason="sample missing")
def test_router_on_real_sample_produces_report():
    from ssis_migration.parser import DTSXParser

    cir = DTSXParser().parse(SAMPLES / "ETL_Load_Orders.dtsx")
    cir = DeterministicEngine().process(cir)
    plan = Router().plan(cir)

    report = plan.to_report()
    assert "counts" in report and "decisions" in report
    assert sum(report["counts"].values()) == len(report["decisions"])
    # Every decision must carry a non-empty reason.
    assert all(d["reason"] for d in report["decisions"])
