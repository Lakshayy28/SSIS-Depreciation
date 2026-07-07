# Generation — Chunking, Agent Memory, Repair, and the Hybrid Stage

The migration now flows through **four** explicit stages, each auditable:

```
 DTSX ──parse──► CIR (canonical) ──convert──► ASSEMBLY MANIFEST ──render──► .py
  XML             *_cir_*.json     chunk по    hybrid_<pkg>.json            module
                  100% coverage    chunk                                    compile-
                  audited                                                   gated
```

## Why one-shot generation failed

Sending a whole Script Task or SQL script to the model in one completion had
two systematic failure modes we measured directly in the Copilot logs:

1. **Truncation** — long outputs hit the token ceiling mid-line
   (`finish_reason: "length"`) and the snippet arrived syntactically broken.
2. **Context drift** — late parts of a long output contradicted early parts
   (renamed variables, re-imported modules, re-defined helpers).

## Semantic chunking

[`chunking.py`](../ssis_migration/transform/llm/chunking.py) splits the *source*
(not the output) at semantically meaningful boundaries:

| Source | Unit | Boundary |
|--------|------|----------|
| T-SQL  | `sql_batch` | `GO` batch separators (hard boundaries) |
| T-SQL  | `sql_group` | top-level `;` in oversized batches (string/comment/paren-aware scanner), greedily grouped ≤ 80 lines |
| C# / VB.NET | `dotnet_prologue` | usings / class header / fields — generated FIRST |
| C# / VB.NET | `dotnet_method` | one method per chunk |
| anything small (≤ 60 lines / 3500 chars) | `block` | fast path: single chunk |

## Agent memory (context-aware generation)

[`AgentMemory`](../ssis_migration/transform/llm/chunking.py) is a per-package
working memory shared by every agent, injected into each chunk prompt:

- **facts** — package params, variables, connections (host/db), Spark target: always injected
- **defined symbols** — AST-extracted from previously generated chunks:
  always injected as *"reuse these exact names, do NOT redefine"*
- **chunk notes** — one-line summaries of previous chunks, retrieved by
  **lexical-identifier overlap** with the chunk being generated (top-K in
  budget)
- **pitfalls** — deduplicated reviewer/validator issues, so regenerations don't
  repeat mistakes; functional-validation feedback from previous passes also
  lands here (the pipeline reuses ONE memory across outer passes)

**Retrieval is deliberately embedding-free.** What later chunks need from
earlier ones is identifier consistency — lexical overlap retrieves exactly that
signal, deterministically (reproducible runs, unit-testable) and with zero
dependencies.

## Two validators, two mandates

| | Semantic reviewer | Syntax validator (editor) |
|---|---|---|
| module | `agents.ReviewAgent` | `repair.py` + `SyntaxFixer` |
| judges | correctness vs. the SSIS source | does it compile |
| edit authority | **never** — issues go back to the generator | **yes** — compile failures are mechanical damage |
| altitude | assembled item | chunk → item → whole file |

The **editing** validator runs in escalating stages: extract (fences/prose) →
normalize (smart quotes, tabs, dedent, CRLF) → bounded LLM edit loop fed the
exact compiler error. Clearly-truncated statements are stubbed with
`raise NotImplementedError("truncated during generation: …")` rather than
guessed. Code that never compiles is **never shown to the semantic reviewer**;
the compile error becomes the regeneration issue.

The whole-file gate runs after every codegen: the rendered module must parse,
or it is repaired in place (deterministic always, LLM editor when a token is
available). The static validator additionally AST-checks for **undefined
names** — the classic LLM failure of calling a helper that was never emitted.

## The hybrid stage (assembly manifest)

`hybrid_<package>.json` records the provenance of every generated line:

```json
{
  "items": {
    "exec_0002": {
      "item_kind": "complex_sql",
      "chunked": true,
      "chunks": [
        {"index": 1, "title": "batch 1: MERGE dbo.OrderFact",
         "source_excerpt": "MERGE dbo.OrderFact AS tgt …",
         "syntax_ok": true, "repair_stages": ["deterministic"], "attempts": 1}
      ],
      "assembled_code": "…", "syntax_ok": true,
      "review_passed": true, "iterations": 2, "status": "complete"
    }
  }
}
```

Conversion *appends* into this stage chunk by chunk; the code generator then
renders from the CIR whose snippets the manifest documented.

## Human review, in the output itself

Anything unimplemented or low-confidence lands in the final module as a boxed
banner — with the reason and an excerpt of the original SSIS source — plus a
`logger.warning`, and a data flow with unimplemented components raises
`NotImplementedError` instead of silently returning wrong data:

```python
# ╔═══ HUMAN REVIEW REQUIRED ══════════════════════════════════════════════
# ║ Item   : Execute Merge Order Fact [execute_sql]
# ║ Reason : Review-regen loop exhausted after 3 iterations
# ║ Original SSIS source (excerpt):
# ║   EXEC dbo.sp_MergeOrderFact @BatchDate = ?
# ╚════════════════════════════════════════════════════════════════════════
```

## Knobs

| Env var | Default | Meaning |
|---------|---------|---------|
| `SYNTAX_FIX_MAX_ITERATIONS` | `2` | LLM syntax-edit attempts per artifact |
| `COPILOT_MAX_REVIEW_ITERATIONS` | `4` | review→regen loop per item |
| `FUNCTIONAL_VALIDATION_MAX_ITERATIONS` | `2` | outer equivalence passes |
| `COPILOT_MAX_TOKENS` | `4096` | per-completion budget (doubled once on truncation, then continuation-stitched) |

Chunking thresholds live in `chunking.py` (60 lines / 3500 chars trigger,
80-line chunk target) — deliberately code, not config: they interact with the
token budget and shouldn't drift independently.
