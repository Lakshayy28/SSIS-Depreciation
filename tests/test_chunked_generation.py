"""
Offline integration tests: chunked generation → repair → review → manifest.

A routing FakeClient stands in for Copilot: it answers review calls with canned
JSON verdicts, syntax-fix calls with repaired code, and generation calls with
queued snippets — so the whole agent flow runs without a network.
"""

from __future__ import annotations

import pytest

from ssis_migration.transform.llm.agents import ComplexSQLAgent, ReviewAgent, ScriptTaskAgent
from ssis_migration.transform.llm.assembly import AssemblyManifest
from ssis_migration.transform.llm.chunking import AgentMemory
from ssis_migration.transform.llm.repair import SyntaxFixer


class RoutingFakeClient:
    def __init__(self, gen_responses, review_responses=None, fix_responses=None):
        self.gen = list(gen_responses)
        self.reviews = list(review_responses or [])
        self.fixes = list(fix_responses or [])
        self.gen_calls: list[str] = []
        self.review_calls: list[str] = []
        self.fix_calls: list[str] = []

    def simple_complete(self, system, user, model=None, max_tokens=None):
        if "critical code review" in system:
            self.review_calls.append(user)
            return self.reviews.pop(0) if self.reviews else '{"passed": true, "issues": []}'
        if "syntax repair tool" in system:
            self.fix_calls.append(user)
            if not self.fixes:
                raise AssertionError("unexpected syntax-fix call")
            return self.fixes.pop(0)
        self.gen_calls.append(user)
        if not self.gen:
            raise AssertionError("generation responses exhausted")
        return self.gen.pop(0)


def _make_sql_agent(client, memory=None, manifest=None, max_iterations=2):
    memory = memory if memory is not None else AgentMemory()
    reviewer = ReviewAgent(client)
    fixer = SyntaxFixer(client, spark_version="3.3")
    return ComplexSQLAgent(
        client, reviewer, max_iterations=max_iterations, spark_version="3.3",
        fixer=fixer, memory=memory, manifest=manifest,
    ), memory


BIG_SQL = "\nGO\n".join(
    f"UPDATE dbo.t{i} SET c = {i};\n" + "\n".join(f"-- pad line {j}" for j in range(30))
    for i in range(3)
)


def test_chunked_sql_generation_assembles_all_chunks():
    client = RoutingFakeClient(gen_responses=[
        "spark.sql(\"UPDATE_0\")\n",
        "spark.sql(\"UPDATE_1\")\n",
        "spark.sql(\"UPDATE_2\")\n",
    ])
    manifest = AssemblyManifest(package="pkg", spark_version="3.3")
    agent, memory = _make_sql_agent(client, manifest=manifest)

    result = agent.convert(sql=BIG_SQL, item_id="exec_0001")

    assert result.success
    assert len(client.gen_calls) == 3                    # one per chunk
    for i in range(3):
        assert f"UPDATE_{i}" in result.code
    item = manifest.items["exec_0001"]
    assert item.chunked is True
    assert len(item.chunks) == 3
    assert item.syntax_ok and item.review_passed
    assert item.status == "complete"
    # chunk provenance headers present in assembled code
    assert "chunk 1/3" in result.code


def test_memory_carries_symbols_between_chunks():
    client = RoutingFakeClient(gen_responses=[
        "order_totals = spark.table('t0')\n",
        "x = order_totals.count()\n",
        "y = x + 1\n",
    ])
    agent, memory = _make_sql_agent(client)
    result = agent.convert(sql=BIG_SQL, item_id="exec_0002")

    assert result.success
    # Chunk 2's prompt must contain the symbol defined by chunk 1.
    assert "order_totals" in client.gen_calls[1]
    assert "do NOT redefine" in client.gen_calls[1]
    assert "order_totals" in memory.symbols


def test_broken_chunk_repaired_deterministically():
    client = RoutingFakeClient(gen_responses=[
        "```python\nspark.sql(\"A\")\n```",             # fenced → deterministic repair
        "spark.sql(\"B\")\n",
        "spark.sql(\"C\")\n",
    ])
    manifest = AssemblyManifest(package="pkg", spark_version="3.3")
    agent, _ = _make_sql_agent(client, manifest=manifest)
    result = agent.convert(sql=BIG_SQL, item_id="exec_0003")

    assert result.success
    chunk1 = manifest.items["exec_0003"].chunks[0]
    assert chunk1.syntax_ok
    assert chunk1.repair_stages == ["deterministic"]
    assert "```" not in result.code


def test_broken_chunk_fixed_by_llm_editor():
    client = RoutingFakeClient(
        gen_responses=["x = (1 + 2\n", "y = 2\n", "z = 3\n"],
        fix_responses=["x = (1 + 2)\n"],
    )
    agent, _ = _make_sql_agent(client)
    result = agent.convert(sql=BIG_SQL, item_id="exec_0004")

    assert result.success
    assert len(client.fix_calls) == 1
    assert "x = (1 + 2)" in result.code


def test_review_failure_regenerates_and_memory_records_issue():
    client = RoutingFakeClient(
        gen_responses=["bad_logic = 1\n", "good_logic = 1\n"],
        review_responses=[
            '{"passed": false, "issues": ["wrong join direction"]}',
            '{"passed": true, "issues": []}',
        ],
    )
    memory = AgentMemory()
    reviewer = ReviewAgent(client)
    agent = ComplexSQLAgent(
        client, reviewer, max_iterations=2, spark_version="3.3",
        fixer=None, memory=memory, manifest=None,
    )
    result = agent.convert(sql="SELECT 1", item_id="exec_0005")

    assert result.success
    assert result.iterations == 2
    assert "wrong join direction" in memory.issues
    # Regen prompt carries the reviewer's issue back to the generator.
    assert "wrong join direction" in client.gen_calls[1]


def test_fixer_errors_are_swallowed_and_item_regenerates():
    # The fixer client raises (no fix responses queued) — repair must degrade
    # gracefully, the compile failure becomes the regen issue, and attempt 2
    # (compilable) succeeds.
    client = RoutingFakeClient(
        gen_responses=["def broken(:\n    pass\n", "fixed_val = 1\n"],
        review_responses=['{"passed": true, "issues": []}'],
    )
    agent, _ = _make_sql_agent(client)
    result = agent.convert(sql="SELECT 1", item_id="exec_0006")
    assert result.success
    assert result.iterations == 2


def test_uncompilable_code_regenerates_without_fixer():
    client = RoutingFakeClient(
        gen_responses=["def broken(:\n    pass\n", "ok = 1\n"],
        review_responses=['{"passed": true, "issues": []}'],
    )
    memory = AgentMemory()
    reviewer = ReviewAgent(client)
    agent = ComplexSQLAgent(
        client, reviewer, max_iterations=2, spark_version="3.3",
        fixer=None, memory=memory, manifest=None,
    )
    result = agent.convert(sql="SELECT 1", item_id="exec_0007")

    assert result.success
    # Reviewer saw only the SECOND (compilable) attempt.
    assert len(client.review_calls) == 1
    assert "ok = 1" in client.review_calls[0]


def test_script_agent_chunks_by_method(monkeypatch):
    csharp = "\n".join([
        "using System;",
        "public class ScriptMain {",
        "    public void Main() { Helper(); }",
    ] + [f"    // padding {i}" for i in range(60)] + [
        "    private void Helper() { int x = 1; }",
        "}",
    ])
    client = RoutingFakeClient(gen_responses=[
        "IMPORTS = True\n", "def main():\n    helper()\n", "def helper():\n    x = 1\n",
    ])
    memory = AgentMemory()
    reviewer = ReviewAgent(client)
    manifest = AssemblyManifest(package="pkg", spark_version="3.3")
    agent = ScriptTaskAgent(
        client, reviewer, max_iterations=1, spark_version="3.3",
        fixer=None, memory=memory, manifest=manifest,
    )
    result = agent.convert(code=csharp, language="csharp", item_id="script_01")

    assert result.success
    item = manifest.items["script_01"]
    assert item.chunked
    kinds = [c.kind for c in item.chunks]
    assert "dotnet_prologue" in kinds
    assert kinds.count("dotnet_method") == 2


def test_generation_exception_returns_failed_result():
    class ExplodingClient:
        def simple_complete(self, *a, **k):
            raise RuntimeError("circuit open")

    memory = AgentMemory()
    reviewer = ReviewAgent(ExplodingClient())
    agent = ComplexSQLAgent(
        ExplodingClient(), reviewer, max_iterations=2, spark_version="3.3",
        fixer=None, memory=memory,
    )
    result = agent.convert(sql="SELECT 1", item_id="exec_0008")
    assert result.success is False
