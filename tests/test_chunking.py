"""Tests for semantic chunking and agent memory."""

from __future__ import annotations

from ssis_migration.transform.llm.chunking import (
    AgentMemory,
    chunk_dotnet,
    chunk_source,
    chunk_sql,
    should_chunk,
    _extract_symbols,
    _split_top_level_statements,
)


# ─── should_chunk ─────────────────────────────────────────────────────────────

def test_small_body_not_chunked():
    assert should_chunk("SELECT 1") is False


def test_long_body_chunked():
    assert should_chunk("x\n" * 100) is True


def test_chunk_source_small_single_block():
    chunks = chunk_source("SELECT 1", "tsql")
    assert len(chunks) == 1
    assert chunks[0].kind == "block"


# ─── SQL chunking ─────────────────────────────────────────────────────────────

def test_go_batches_are_boundaries():
    sql = "TRUNCATE TABLE a;\nGO\nINSERT INTO a SELECT * FROM b;\nGO\nUPDATE c SET x=1;"
    chunks = chunk_sql(sql)
    assert len(chunks) == 3
    assert all(c.kind == "sql_batch" for c in chunks)
    assert chunks[0].total == 3
    assert "TRUNCATE" in chunks[0].title


def test_go_case_insensitive_and_no_false_split_inside_words():
    sql = "SELECT category FROM catalog;\ngo\nSELECT 2;"
    chunks = chunk_sql(sql)
    assert len(chunks) == 2
    assert "category" in chunks[0].text     # 'go' inside words untouched


def test_oversized_batch_splits_at_statements():
    stmts = [f"UPDATE t{i} SET c = {i};" for i in range(120)]
    sql = "\n".join(stmts)
    chunks = chunk_sql(sql, max_chunk_lines=40)
    assert len(chunks) >= 3
    joined = "\n".join(c.text for c in chunks)
    for i in (0, 60, 119):
        assert f"UPDATE t{i} " in joined     # nothing lost


def test_statement_splitter_respects_strings_and_comments():
    sql = "INSERT INTO t VALUES ('a;b');\n-- comment; with semicolon\nSELECT 1;"
    stmts = _split_top_level_statements(sql)
    assert len(stmts) == 2
    assert "'a;b'" in stmts[0]


def test_statement_splitter_respects_parens():
    sql = "SELECT (SELECT max(x) FROM t; ) FROM u"  # ';' inside parens not a split
    stmts = _split_top_level_statements(sql.replace("; )", ")"))
    assert len(stmts) == 1


# ─── .NET chunking ────────────────────────────────────────────────────────────

_CSHARP = """
using System;
using System.IO;

public class ScriptMain
{
    private int counter = 0;

    public void Main()
    {
        LoadFiles();
        Dts.TaskResult = (int)ScriptResults.Success;
    }

    private void LoadFiles()
    {
        var files = Directory.GetFiles("C:\\\\data");
        counter = files.Length;
    }

    private static string Clean(string input)
    {
        return input.Trim();
    }
}
"""


def test_csharp_methods_split():
    chunks = chunk_dotnet(_CSHARP, "csharp")
    kinds = [c.kind for c in chunks]
    assert kinds[0] == "dotnet_prologue"
    assert kinds.count("dotnet_method") == 3
    titles = " ".join(c.title for c in chunks)
    assert "Main" in titles and "LoadFiles" in titles and "Clean" in titles


def test_csharp_prologue_contains_fields():
    chunks = chunk_dotnet(_CSHARP, "csharp")
    assert "using System" in chunks[0].text
    assert "counter" in chunks[0].text


def test_vb_methods_split():
    vb = (
        "Imports System\n\nPublic Class ScriptMain\n"
        "  Public Sub Main()\n    ProcessRows()\n  End Sub\n"
        "  Private Function ProcessRows() As Integer\n    Return 1\n  End Function\n"
        "End Class\n"
    )
    chunks = chunk_dotnet(vb, "vbnet")
    assert sum(1 for c in chunks if c.kind == "dotnet_method") == 2


def test_single_method_stays_single_block():
    code = "public void Main()\n{\n    int x = 1;\n}\n"
    chunks = chunk_dotnet(code, "csharp")
    assert len(chunks) == 1
    assert chunks[0].kind == "block"


# ─── AgentMemory ──────────────────────────────────────────────────────────────

def test_memory_records_symbols():
    mem = AgentMemory()
    mem.record_code("chunk 1", "def load_orders(spark):\n    return 1\nBATCH_SIZE = 10\n")
    assert "load_orders" in mem.symbols
    assert "BATCH_SIZE" in mem.symbols


def test_memory_symbol_extraction_survives_broken_code():
    symbols = _extract_symbols("def broken(:\nx = 1")
    assert "broken" in symbols


def test_memory_render_always_includes_facts_and_symbols():
    mem = AgentMemory(facts={"spark_version": "2.4.8"})
    mem.record_code("c1", "conn_str = 'x'\n")
    block = mem.render("anything")
    assert "spark_version=2.4.8" in block
    assert "conn_str" in block
    assert "do NOT redefine" in block


def test_memory_retrieval_ranks_by_overlap():
    mem = AgentMemory()
    mem.record_code("customers chunk", "def load_customers(customer_df):\n    pass\n")
    mem.record_code("orders chunk", "def load_orders(order_df):\n    pass\n")
    notes = mem.relevant_notes("process customer_df records for customers", k=1)
    assert notes[0].title == "customers chunk"


def test_memory_issue_dedup_and_render():
    mem = AgentMemory()
    mem.record_issues(["undefined variable value", "undefined variable value", "bad join"])
    assert len(mem.issues) == 2
    assert "do NOT repeat" in mem.render("")


def test_memory_render_respects_budget():
    mem = AgentMemory()
    for i in range(50):
        mem.record_code(f"chunk {i}", f"def very_long_function_name_number_{i}():\n    pass\n")
    block = mem.render("query", budget_chars=500)
    assert len(block) <= 540
