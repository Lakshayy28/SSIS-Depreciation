# Architecture — LLM-Assisted, Functionally-Equivalent SSIS → PySpark Migration

> Definitive, end-to-end architecture reference. For deeper treatment of a
> single subsystem see [GENERATION.md](GENERATION.md) (chunking, agent memory,
> repair), [SCORING.md](SCORING.md) (the dual-axis scorecard), and
> [RESILIENCE.md](RESILIENCE.md) (circuit breaker, retry, rate limiting).

## Mission

Translate legacy SSIS `.dtsx` packages into PySpark modules that are
**provably functionally equivalent** to the original — not merely
syntactically plausible. An LLM is used in two distinct roles: as a
**codegen assistant** for the logic a deterministic engine cannot safely
transpile, and as a **critical judge** that reviews every artifact against the
original SSIS source until it is compilable, correct, and version-valid. A
quantitative scorecard — not a human's impression — gates whether a migration
is considered complete.

---

## Table of contents

1. [System overview](#1-system-overview)
2. [Phase 0 — Inventory & assessment](#2-phase-0--inventory--assessment)
3. [Phase 1 — DTSX parsing → the canonical stage (CIR)](#3-phase-1--dtsx-parsing--the-canonical-stage-cir)
4. [The CIR schema](#4-the-cir-schema)
5. [Phase 2 — The deterministic engine](#5-phase-2--the-deterministic-engine)
6. [Phase 2.5 — AUTO routing](#6-phase-25--auto-routing)
7. [Phase 3 — The LLM subsystem](#7-phase-3--the-llm-subsystem)
   - [7.1 CopilotClient & NFRs](#71-copilotclient--non-functional-reliability)
   - [7.2 Semantic chunking](#72-semantic-chunking)
   - [7.3 Agent memory](#73-agent-memory)
   - [7.4 The two validators](#74-the-two-validators)
   - [7.5 The hybrid stage (assembly manifest)](#75-the-hybrid-stage-assembly-manifest)
   - [7.6 Generation agents](#76-generation-agents)
   - [7.7 The inner loop — component review → regen](#77-the-inner-loop--component-review--regen)
   - [7.8 The outer loop — package equivalence review → reconvert](#78-the-outer-loop--package-equivalence-review--reconvert)
8. [Phase 4 — Code generation](#8-phase-4--code-generation)
9. [Phase 5 — Validation](#9-phase-5--validation)
10. [Phase 6 — The dual-axis scorecard](#10-phase-6--the-dual-axis-scorecard)
11. [End-to-end sequence for one package](#11-end-to-end-sequence-for-one-package)
12. [Conversion modes](#12-conversion-modes)
13. [Models](#13-models)
14. [Complete module map](#14-complete-module-map)
15. [Configuration reference](#15-configuration-reference)
16. [CLI reference](#16-cli-reference)
17. [Testing](#17-testing)
18. [Known gaps / roadmap](#18-known-gaps--roadmap)

---

## 1. System overview

Migration crosses two lossy translations, and every artifact in between is
retained on disk for audit — nothing is a black box:

```
 ┌──────────┐  parse   ┌──────────────┐  route+  ┌──────────────┐  render  ┌──────────┐
 │  .dtsx   │ ───────► │  CIR          │  convert │  CIR          │ ───────► │  .py     │
 │  (XML)   │          │  (canonical)  │ ───────► │  (resolved)   │          │  module  │
 └──────────┘          └──────────────┘          └──────┬───────┘          └────┬─────┘
      │                       │                          │                       │
      │   parse_coverage      │   routing_report_*.json  │  hybrid_*.json        │
      │   (self-audited)      │   (AUTO decisions)        │  (chunk provenance)  │
      │                       │                          │                       │
      └───────────────────────┴──────────────┬───────────┴───────────────────────┘
                                              ▼
                                   validation_report_*.json  (static + semantic)
                                   scorecard_*.json          (parsing × functional)
```

Five numbered phases plus an inserted routing phase (2.5) and a scoring phase
that runs alongside validation (5):

| Phase | Stage | Deterministic? | Persisted artifact |
|------:|-------|:---:|--------|
| 0 | Inventory & assessment | yes | `inventory_report.json`, `wave_plan.json` |
| 1 | Parse — DTSX XML → CIR | yes (`lxml`) | `*_cir_annotated.json` |
| 2 | Deterministic transform | yes (`sqlglot` + rule tables) | (in-CIR) |
| 2.5 | AUTO routing | yes (rules + regex risk signals) | `routing_report_*.json` |
| 3 | LLM conversion + equivalence-review loop | LLM | `hybrid_*.json`, `*_cir_resolved.json` |
| 4 | Code generation | yes (Jinja2) + compile gate | `<module>.py`, `test_<module>.py` |
| 5 | Validation | yes (AST) + LLM judge | `validation_report_*.json` |
| 6 | Scoring | yes (counts) + LLM judge | `scorecard_*.json` |

Orchestration lives in [`pipeline.py`](../ssis_migration/pipeline.py)
(`MigrationPipeline.run()`); everything else is a library the pipeline calls.

---

## 2. Phase 0 — Inventory & assessment

[`inventory.py`](../ssis_migration/inventory.py) scans a directory of `.dtsx`
files (no conversion — parse only) and produces a portfolio-level view before
any migration work starts:

```
  packages/*.dtsx
        │
        ▼
  DTSXParser.parse() for each file
        │
        ▼
  ComplexityScorer.classify()  ──►  simple | medium | high | very_high
        │
        ▼
  inventory_report.json  { packages: [...], dependency_graph: {...},
                           complexity_summary: {simple: n, medium: n, ...} }
  wave_plan.json         (packages grouped by complexity for staged rollout)
```

Complexity classification (`ComplexityScorer`, in
[`parser/complexity_scorer.py`](../ssis_migration/parser/complexity_scorer.py)):

| Level | Trigger |
|-------|---------|
| VERY_HIGH | any custom/unknown component, or any cross-package reference |
| HIGH | any Script Task, or >15 data-flow components |
| MEDIUM | >3 SSIS expressions, or >5 data-flow components |
| SIMPLE | none of the above |

CLI: `ssis-migrate assess ./packages/`.

---

## 3. Phase 1 — DTSX parsing → the canonical stage (CIR)

[`parser/dtsx_parser.py`](../ssis_migration/parser/dtsx_parser.py)
(`DTSXParser.parse()`) is the single entry point. It never raises on partial or
unexpected structure — missing elements are logged and best-effort CIR is still
produced.

```
  .dtsx (raw XML bytes)
        │  lxml.etree.fromstring
        ▼
  root element ──┬─► ParameterExtractor    ──► CIRParameter[]
                  ├─► VariableExtractor     ──► CIRVariable[]
                  ├─► ConnectionExtractor   ──► CIRConnection[]
                  ├─► EventHandlerExtractor ──► EventHandler[]
                  ├─► ControlFlowExtractor  ──► ControlFlow (execution tree +
                  │                              precedence constraints)
                  └─► (recursive) DataFlowExtractor per Data Flow Task
                         found ANYWHERE in the tree, incl. nested inside
                         Sequence/loop containers          ──► DataFlow[]
        │
        ▼
  Lineage builder (regex FROM/JOIN table-ref extraction — heuristic; sqlglot
  does the real lineage work during transpilation)
        │
        ▼
  ComplexityScorer.score() + .classify()
        │
        ▼
  ★ Canonical-completeness self-audit ★
      count_dtsx_elements(path)  vs  count_cir_elements(cir)
      → cir.metadata.parse_coverage = {"coverage": r, "detail": {...}}
      → logged as a WARNING whenever coverage < 100%
        │
        ▼
  CIR (Pydantic model, JSON-serialisable)
```

### Namespace handling — the hard-won lesson

Real-world `.dtsx` files mix **two dialects** in the same file: control-flow
elements (`DTS:Executable`, `DTS:ConnectionManager`, …) are namespaced, but the
embedded **data-flow pipeline XML** (`<pipeline><components><component>…`) is
frequently **completely unprefixed**, and property collections appear as
either `properties/property` (modern) or
`customPropertyCollection/customProperty` (legacy). Every element lookup in
[`data_flow.py`](../ssis_migration/parser/extractors/data_flow.py) therefore
matches by **local name**, ignoring namespace, via small helpers
(`_children`, `_first`, `_descendants`). Component class identification accepts
**both** GUID `componentClassID`s (`{BCEFE59B-…}`) and modern logical names
(`Microsoft.OLEDBSource`) via `map_component_class()` in
[`parser/ns.py`](../ssis_migration/parser/ns.py). SSIS pipeline `dataType`
values are normalized (`i4` → `DT_I4`) by `normalize_ssis_type()` in
[`cir/type_mapping.py`](../ssis_migration/cir/type_mapping.py) before type
resolution.

Two more completeness fixes worth naming because they silently discarded whole
packages before being caught by the self-audit:

- **Connections**: a key=value regex that couldn't handle spaced keys
  (`Data Source`, `Initial Catalog`, `User ID`) meant `host`/`database` were
  never resolved; fixed, and canonical aliases (`host`, `port`, `database`,
  `user`, `password`) are now added alongside the raw parsed keys so codegen
  and LLM prompts have a stable contract regardless of provider dialect.
- **Nested data flows**: `_extract_data_flows()` now does a full-tree
  `iterfind`, not a top-level-only scan — Data Flow Tasks living inside a
  Sequence Container were previously invisible to the parser entirely.

### The parse-coverage self-audit

[`scoring.py`](../ssis_migration/scoring.py) provides
`count_dtsx_elements()` / `count_cir_elements()` / `structural_coverage()`,
called both **inside the parser** (recorded on `cir.metadata.parse_coverage`)
and again **inside the scorecard** (Phase 6) so parsing fidelity is visible at
parse time *and* re-verified as part of the final score:

| Category | Weight | Counted as |
|----------|-------:|------------|
| executables | 3.0 | `DTS:Executable` (recursive) |
| data-flow components | 3.0 | any element locally named `component` inside a pipeline |
| connections | 1.0 | `DTS:ConnectionManagers/DTS:ConnectionManager` (outer only — the inner `DTS:ObjectData` copy is not double-counted) |
| parameters | 1.0 | `DTS:PackageParameter` |
| variables | 1.0 | `DTS:Variable` |

On the three bundled samples this self-audit currently reports **100%
structural coverage** after the fixes above.

---

## 4. The CIR schema

The Canonical Intermediate Representation
([`cir/models.py`](../ssis_migration/cir/models.py)) is a set of Pydantic v2
models — the single artifact that decouples the parser from the code
generator and from the LLM subsystem. It is fully JSON-serialisable
(`cir.save()` / `CIR.load()`), which is what makes the audit trail
(`*_cir_annotated.json`, `*_cir_resolved.json`) possible.

```
CIR
├── metadata: CIRMetadata
│     source_file, source_hash, parse_timestamp, complexity_score,
│     complexity_details, parse_coverage   ← the self-audit result
├── parameters: [CIRParameter]              (name, data_type, default, scope)
├── variables:  [CIRVariable]               (+ optional SSIS expression)
├── connections: [CIRConnection]
│     id, name, provider_type, connection_string_template,
│     resolved_parameters{host,port,database,user,...},
│     target_mapping (→ spark_jdbc/spark_csv/spark_excel/spark_xml + driver)
├── control_flow: ControlFlow
│     execution_tree: [ControlFlowExecutable]   (recursive: .children)
│         type, sql, connection_ref, script_code, loop_*, expression,
│         conversion_status, pyspark_snippet, conversion_notes
│     precedence_constraints: [PrecedenceConstraint]  (from/to/eval/expr)
├── data_flows: [DataFlow]
│     components: [DataFlowComponent]
│         type (source|transformation|destination), subtype (oledb_source,
│         derived_column, lookup, merge_join, aggregate, …), sql_command,
│         expressions, join_columns, aggregations, column_mappings,
│         extra_properties (EVERY raw DTSX property — nothing is dropped),
│         conversion_status, pyspark_snippet
│     paths: [DataFlowPath]   (default | error | no_match)
├── event_handlers: [EventHandler]
├── lineage: Lineage           (sources, destinations, column_lineage)
└── conversion_metadata: ConversionMetadata
      deterministic_coverage, llm_required_items[], human_review_required[],
      conversion_status
```

`ConversionStatus` is the state machine every executable and component moves
through: `PENDING → DETERMINISTIC | LLM_REQUIRED → LLM_COMPLETE | HUMAN_REVIEW`.
`TranspilationStatus` tracks the SQL/expression sub-state the same way. Two
resolver helpers live on `CIR` itself: `find_connection(ref)` (matches by id,
name, or a substring of a DTSX `refId` like
`Package.ConnectionManagers[Name]`) and `flag_for_llm()` /
`flag_for_human_review()`.

---

## 5. Phase 2 — The deterministic engine

[`transform/deterministic/engine.py`](../ssis_migration/transform/deterministic/engine.py)
(`DeterministicEngine.process()`) runs two passes over the CIR and never calls
an LLM:

```
 Pass 1 — control flow          Pass 2 — data flow components
 ───────────────────           ──────────────────────────────
 for each executable:           ComponentMapper.process():
   if has SQL → sql_transpiler   for each component, dispatch by subtype:
     sqlglot tsql→spark            oledb_source/dest, flat_file_*,
     procedural? (EXEC/CURSOR/     derived_column, conditional_split,
     WHILE/DECLARE @/GO/…)         lookup, merge_join, aggregate, sort,
       → LLM_REQUIRED               union_all, multicast, row_count,
     else → DETERMINISTIC           copy_column, data_conversion
   script_task → LLM_REQUIRED     each subtype → a pure-function template
   sequence/loop → DETERMINISTIC   producing an f-string PySpark snippet;
     (children handled below)     unsupported subtype → LLM_REQUIRED
   file_system/ftp/send_mail/
     execute_process → DETERMINISTIC
```

Notable engine behaviour:

- **SQL transpilation** ([`sql_transpiler.py`](../ssis_migration/transform/deterministic/sql_transpiler.py))
  uses `sqlglot` (`tsql` → `spark`) and pre-filters procedural constructs via
  regex (`EXEC`, `CURSOR`/`FETCH`, `WHILE`, `DECLARE @`, dynamic SQL,
  `sp_executesql`, `GO`, `CREATE PROC`, `BEGIN TRAN`, `##temp`) straight to
  `LLM_REQUIRED` — sqlglot is never asked to guess at procedural T-SQL.
- **Expression translation** ([`expression_translator.py`](../ssis_migration/transform/deterministic/expression_translator.py))
  maps the SSIS expression grammar to PySpark column expressions via a
  function table (`EXPRESSION_FUNCTION_MAP` in `cir/type_mapping.py`); anything
  it can't confidently map is left `LLM_REQUIRED`.
- **Component mapping** ([`component_mapper.py`](../ssis_migration/transform/deterministic/component_mapper.py))
  threads a single running `df` variable through the generated function body
  (source assigns it, transforms mutate it, destination consumes it — no
  per-component `df_<id>` fragmentation), resolves connection refs to the
  **real connection-manager name** via `CIR.find_connection()` with a
  role-hinted fallback (`prefer="source"`/`"dest"`) plus an explicit
  `# TODO(verify)` comment when the DTSX gave no usable ref, derives aggregate
  group-by columns and source columns from the aggregation entries, matches
  SSIS full-cache Lookup semantics with a Spark broadcast join, and emits
  version-appropriate syntax (e.g. `unionByName(..., allowMissingColumns=True)`
  only on Spark 3.1+).
- **Coverage bookkeeping**: `cir.conversion_metadata.deterministic_coverage`
  is the fraction of data-flow components resolved without the LLM — visible
  in every CLI run (`det. coverage=NN%`).

---

## 6. Phase 2.5 — AUTO routing

[`transform/routing.py`](../ssis_migration/transform/routing.py) (`Router`)
runs **after** the deterministic engine and adds a transparent, auditable
decision layer. It is the difference between HYBRID (trust the engine's
"I can/can't" verdict) and AUTO (**re-examine** what the engine thought it
could handle, and escalate when a mechanical transpile would risk changing
behaviour):

```
                     ┌─────────────────────────────┐
   CIR (annotated)   │            Router            │   CIR (routed) +
   ───────────────►  │  per executable/component:    │──► RoutingPlan
                     │   risk-signal scan            │    (routing_report_*.json)
                     └─────────────────────────────┘

 decision = DETERMINISTIC | LLM | HUMAN_REVIEW   (never downgrades "I can't")

 risk signals (regex, transform/routing.py):
   SQL:    stored_proc_exec, dynamic_sql, cursor, while_loop, local_variables,
           merge, try_catch, transaction, temp_table, pivot
   script: com_interop, threading, reflection, external_io, db_access,
           long_script (>40 lines)
```

| Decision | Examples |
|----------|----------|
| DETERMINISTIC | clean set-based SQL, structural containers (Sequence/loop), templated operational tasks (File System/FTP/Send Mail), fully-mapped expressions |
| LLM | procedural/high-risk T-SQL, `.NET` Script Tasks/Components, partial sqlglot transpiles, unresolved expression sets |
| HUMAN_REVIEW | cross-package execution (`execute_package`), unknown/empty component bodies, unrecognised executable types |

Every `RoutingDecision` records `item_id`, `item_kind`, `target`, a
human-readable `reason`, and the `risk_signals` that fired — the full list is
serialised to `routing_report_<pkg>.json` and summarised in the CLI
(`AUTO routing: deterministic=N llm=N human_review=N`).

---

## 7. Phase 3 — The LLM subsystem

This is the heart of the framework:
[`transform/llm/`](../ssis_migration/transform/llm/) contains the client, the
generation agents, the judges, chunking, agent memory, syntax repair, and the
hybrid assembly stage. Orchestrated per-package by
[`transform/llm/pipeline.py`](../ssis_migration/transform/llm/pipeline.py)
(`LLMPipeline`), constructed **once per package run** so its `AgentMemory` and
`AssemblyManifest` accumulate across outer equivalence-review iterations.

### 7.1 CopilotClient & non-functional reliability

[`copilot_client.py`](../ssis_migration/transform/llm/copilot_client.py) is
the only LLM transport in the framework — the GitHub Copilot Chat completions
endpoint (`POST https://api.githubcopilot.com/chat/completions`), Bearer-token
authenticated.

```
 simple_complete(system, user, model?, max_tokens?)
        │
        ▼
 CompletionRequest ──► rate limiter (TokenBucket, opt-in)
        │
        ▼
 circuit breaker .before_call()  ──► OPEN? → CopilotUnavailableError (fast-fail)
        │
        ▼
 HTTP POST (jittered exp. backoff retry loop, ≤ COPILOT_MAX_RETRIES attempts)
        │
    ┌───┼────────────────────────────────────────────────────────┐
    │   │ 200                                  │ 429/5xx          │ 400 model_not_supported
    ▼   ▼                                      ▼                  ▼
  parse response                        retry w/ backoff    swap to COPILOT_FALLBACK_MODEL,
  breaker.record_success()              breaker.record_      remember model as dead
                                         failure()            process-wide, retry SAME attempt
        │
        ▼
 empty content? → retry once → still empty → raise
        │
        ▼
 finish_reason == "length"? → retry w/ doubled token budget
        │                     → still truncated → CONTINUATION request,
        │                       stitch_continuation() removes repeated tail
        ▼
 return completion text
```

Every request and response is logged as masked JSON-L to
`copilot_chat_completions/<pid-timestamp>.log` (Authorization headers and
`ghp_*`/`gho_*`/`github_pat_*` tokens replaced with `***MASKED***`
by `_mask_sensitive()`), git-ignored but human-inspectable.

Full detail on the circuit breaker, retry, and rate limiter is in
[RESILIENCE.md](RESILIENCE.md).

### 7.2 Semantic chunking

[`chunking.py`](../ssis_migration/transform/llm/chunking.py). One-shot
generation of a whole Script Task or SQL script has two measured failure
modes: **truncation** (broken syntax) and **context drift** (late output
contradicts early output). `chunk_source()` splits the *source* — not the
model's output — at semantically meaningful boundaries before generation:

```
  should_chunk(text)?  (> 60 lines or > 3500 chars)
       │ no                              │ yes
       ▼                                  ▼
  single "block" chunk         language == sql?
                                    │ yes                    │ no (csharp/vbnet)
                                    ▼                          ▼
                          GO-batch split (hard          method-boundary split
                          boundary); oversized           (regex over access
                          batches further split          modifiers + signature);
                          at top-level ';'               everything before the
                          (string/bracket/comment/       first method → ONE
                          paren-aware scanner),           "prologue" chunk
                          greedily grouped ≤ 80 lines     (usings/fields), then
                                                          one chunk per method
```

### 7.3 Agent memory

`AgentMemory` (same module) is per-package working memory, shared by every
generation agent and injected into every chunk's prompt:

| Section | Always injected? | Retrieval |
|---|:---:|---|
| **facts** (params, vars, connection host/db, spark version) | yes | — |
| **symbols** defined by previously generated chunks (AST-extracted) | yes | "reuse these exact names, do NOT redefine" |
| **chunk notes** (one-line summaries of prior chunks) | no | deterministic **lexical identifier overlap**, top-K within a char budget |
| **pitfalls** (deduped reviewer/validator issues seen this package) | last 5 | — |

Retrieval is **deliberately embedding-free**: what a later chunk needs from an
earlier one is identifier consistency, and lexical overlap captures exactly
that signal, deterministically and dependency-free.

### 7.4 The two validators

A hard separation the whole subsystem is built around:

| | Semantic reviewer (`ReviewAgent`) | Syntax validator (`repair.py` + `SyntaxFixer`) |
|---|---|---|
| Judges | correctness vs. the original SSIS source | does it **compile** |
| Edit authority | **never** — returns `issues: [...]`, `corrected_code: null` always | **yes** — mechanical damage gets fixed |
| Runs at | assembled item, whole package | chunk → assembled item → whole file |
| On failure | issues fed back to the **generator** (regenerate from scratch) | escalating repair, see below |

`repair.ensure_compilable()` escalates through three stages, cheapest first:

```
 1. extract_code()      largest fenced block / dangling-fence removal /
                        leading-prose stripping (never strips code-looking lines)
 2. normalize_code()    smart quotes/dashes → ascii, zero-width/BOM strip,
                        CRLF→LF, tabs→spaces, dedent, trailing-whitespace trim
 3. SyntaxFixer (LLM)   bounded edit loop (SYNTAX_FIX_MAX_ITERATIONS), fed the
                        EXACT compiler error + surrounding lines; instructed to
                        fix ONLY syntax, never logic; stubs clearly-truncated
                        statements with raise NotImplementedError(...)
```

Code that never compiles is **never shown to the semantic reviewer** — the
compile failure itself becomes the next regeneration's issue.

### 7.5 The hybrid stage (assembly manifest)

[`assembly.py`](../ssis_migration/transform/llm/assembly.py) is the explicit
intermediate the user asked for between the CIR and the rendered code.
Conversion **appends** into it chunk by chunk:

```
AssemblyManifest (hybrid_<package>.json)
└── items: { item_id: ItemAssembly }
       item_kind, chunked, iterations, status (pending|complete|human_review|failed)
       review_passed, review_issues[]
       assembled_code, syntax_ok
       chunks: [ChunkRecord]
           index/total, kind, title, source_excerpt
           code, syntax_ok, repair_stages[], attempts, error
```

Every generated line's provenance — which chunk produced it, what repair it
took, what the reviewer said — is inspectable without re-running anything.

### 7.6 Generation agents

[`agents.py`](../ssis_migration/transform/llm/agents.py) builds on
[`generation.py`](../ssis_migration/transform/llm/generation.py)'s
`ChunkedGenerator` (the shared generate → repair → record engine):

```
                         ChunkedGenerator.generate_item()
                         ─────────────────────────────────
  source_text ──► chunk_source() ──► for each CodeChunk:
                                         render prompt (system + user +
                                         MEMORY_BLOCK + CHUNK_NOTE)
                                            │
                                            ▼
                                       client.simple_complete()
                                       (per-chunk transport retry ×2)
                                            │
                                            ▼
                                       ensure_compilable()  (chunk gate)
                                            │
                                            ▼
                                       memory.record_code(chunk)  ◄── BEFORE
                                                                       next chunk
                                            │
                                       ChunkRecord → manifest
                    ◄───────────────────────┘
                    │
                    ▼
              concatenate all chunks
                    │
                    ▼
              ensure_compilable()   (whole-UNIT gate — seams are exactly
                    │                where concatenation surprises live)
                    ▼
              (assembled_code, syntax_ok)
```

Two concrete agents wrap this engine with their own prompt templates and
connection/parameter context:

- **`ScriptTaskAgent`** — C#/VB.NET Script Task → Python function.
  `chunk_dotnet()` boundaries; system prompt bans .NET APIs (`ComObject`,
  `Marshal`, `SqlConnection`, …) and requires `pyspark.sql.functions as F`.
- **`ComplexSQLAgent`** — procedural T-SQL → Spark SQL / PySpark. `chunk_sql()`
  boundaries; system prompt gives step-by-step (not templated — templates
  caused literal copy-paste of undefined placeholder variables in earlier
  iterations) instructions for stored-procedure execution via `pyodbc`,
  explicitly bans `spark.sql("CALL ...")` and `spark.read.jdbc()` for stored
  procs, and receives the **real resolved connection name** from the CIR
  (`LLMPipeline._resolve_connection()`).
- **`ExpressionAgent`** — single-shot (expressions are small; no chunking
  needed) SSIS expression → PySpark column expression.

Both chunked agents are version-aware: every system prompt is formatted with
`python_compat_note(spark_version)` (Spark 2.4 targets get an explicit "target
Python 3.7, no PEP 604 unions, no built-in generics, no walrus" instruction),
because a syntax fix that satisfies the local interpreter but not the target
cluster's Python is not actually fixed.

### 7.7 The inner loop — component review → regen

```
        ┌──────────────────────────────────────────────────────┐
        │                                                        │
        ▼                                                        │
  ChunkedGenerator.generate_item()                                │
        │                                                        │
        ▼                                                        │
   syntax_ok?                                                    │
   │ no  → issue = "generated code does not compile: <error>"    │
   │ yes │                                                       │
   ▼     ▼                                                       │
  ReviewAgent.review(code, component_type, source_context, …)     │
        │                                                        │
        ▼                                                        │
    passed?                                                      │
   │ no → memory.record_issues(issues)                           │
   │      regen suffix = REGEN_SUFFIX(issues) [+ FUNCTIONAL_CONTEXT_SUFFIX] │
   │      ─────────────────────────────────────────────────────────┘
   │      (iteration += 1, up to COPILOT_MAX_REVIEW_ITERATIONS)
   ▼ yes
  AgentResult(success=True, review_passed=True) ──► item.status = "complete"
```

`ReviewAgent` (`agents.py`) always runs on the (optionally stronger)
`COPILOT_REVIEWER_MODEL` and its JSON contract sets `corrected_code: null`
unconditionally — it is a critic, never a patcher. Its system prompt
(`REVIEW_SYSTEM` in `prompts.py`) enumerates mandatory checks: undefined
variables, PySpark version compatibility, banned APIs (`spark.sql("CALL...")`,
`.NET` connectors) vs. explicitly-allowed ones (`pyodbc`/`pymssql`/`psycopg2`),
syntax, and logic (join direction, null handling, off-by-one).

### 7.8 The outer loop — package equivalence review → reconvert

After every LLM conversion pass and codegen, `MigrationPipeline.run()`
(`pipeline.py`) equivalence-reviews the **entire generated module** — this
runs even when 0 items needed the LLM, so deterministic-only output is still
checked:

```
 for func_iter in 1..FUNCTIONAL_VALIDATION_MAX_ITERATIONS:
     if mode == llm: flag_all_for_llm(cir)
     LLMPipeline.process(cir, functional_context=<prior critical issues>)
     codegen(cir) ──► whole-file compile gate (§8)
     func_result = LLMPipeline.functional_validate(pyspark_code, cir, dtsx_xml)
                       │
                       ▼
              FunctionalValidatorAgent — 3-WAY comparison:
                 (1) raw DTSX excerpt   (ground truth; layout XML stripped,
                                         head+tail-truncated to a token budget)
                 (2) CIR summary        (executables, sql, expressions,
                                         aggregations, joins, paths, params)
                 (3) generated PySpark module
                       │
                       ▼
              { passed, equivalence_score, critical_issues[],
                warnings[], version_issues[] }
     │
     ├─ not passed AND iterations remain?
     │     functional_feedback = critical_issues
     │     reset_llm_items(cir)     (LLM_COMPLETE → LLM_REQUIRED again;
     │                               human_review_required cleared for
     │                               another chance)
     │     continue
     └─ passed, or iterations exhausted → break
```

The DTSX is treated as ground truth: if the CIR and the PySpark agree with
each other but both diverge from the DTSX, that is still flagged critical.
The judge also emits `version_issues` — APIs used that don't exist on the
configured `SPARK_VERSION` — deliberately **conservative** (only flags APIs
it's certain postdate the target; core DataFrame/SQL functions are never
flagged) because a deterministic checker (`scoring.check_pyspark_version()`)
already covers the mechanical cases.

The loop is bounded — unbounded "loop until perfect" is a cost/availability
hazard — and the honest (possibly failing) score is what gets reported.

---

## 8. Phase 4 — Code generation

[`codegen/generator.py`](../ssis_migration/codegen/generator.py)
(`CodeGenerator.generate()`) renders the resolved CIR through Jinja2 templates
in [`codegen/templates/`](../ssis_migration/codegen/templates/):

```
  CIR (resolved) ──► module.py.j2 ──► <module>.py
                 ──► test_module.py.j2 ──► test_<module>.py
        │
        ▼
  black + isort post-processing (best-effort; failures are non-fatal)
        │
        ▼
  ★ WHOLE-FILE COMPILE GATE ★  (pipeline.py, _codegen())
       ast-parse the rendered source
          │ fails
          ▼
       repair.ensure_compilable(source, fixer=llm_pipeline.fixer, …)
          │ ok → rewrite file in place, log repair stages
          │ still broken → log error, leave file as the honest failing artifact
```

`module.py.j2` renders:

- **`CONNECTIONS`** keyed by the connection-manager **name** (matching what
  generated snippets look up), with resolved `host`/`port`/`database`/`user`
  — **passwords are never emitted**, only a comment to inject them at
  runtime from a secret store.
- **Data-flow functions**, one per `DataFlow`, threading a single running `df`
  variable through each component's snippet.
- **`run_package()`**, walking `control_flow.execution_tree`
  **recursively** (a macro, `render_exe`, so Sequence/loop container children
  are rendered — a prior version silently dropped them) in SSIS precedence
  order.
- **Human-review banners** for anything unimplemented — a boxed comment with
  the item name, the reason, and an excerpt of the original SSIS source,
  plus a `logger.warning(...)` call; a data flow with any unresolved
  component `raise`s `NotImplementedError` instead of silently returning
  partial data.
- **Target-Python-version-safe syntax**: `typing.Optional[...]`, not the PEP
  604 `X | None` union operator, so Spark 2.4 (Python 3.7) clusters can
  import the module.

```python
# ╔═══ HUMAN REVIEW REQUIRED ══════════════════════════════════════════════
# ║ Item   : Execute Merge Order Fact [execute_sql]
# ║ Reason : Review-regen loop exhausted after 3 iterations
# ║ Original SSIS source (excerpt):
# ║   EXEC dbo.sp_MergeOrderFact @BatchDate = ?
# ╚════════════════════════════════════════════════════════════════════════
```

---

## 9. Phase 5 — Validation

Two validators merge into one `ValidationReport`
([`validation/report.py`](../ssis_migration/validation/report.py) —
`Finding{stage, severity, code, message, location, detail}`):

```
StaticValidator (validation/static.py)          SemanticValidator (validation/semantic.py)
────────────────────────────────────           ───────────────────────────────────────────
SYNTAX_ERROR    — ast.parse                     compares CIR control-flow graph against the
API_COMPAT      — Spark-3-only API called       call graph extracted from the generated
                  on a 2.4 target (method-call    module's AST: every SSIS path has a code
                  anchored, not bare-word —       path, every error handler has a try/except,
                  avoids flagging comments)       precedence order is preserved, cross-package
ANTIPATTERN     — toPandas()/collect()/show()/   refs have a matching import/call
                  udf() usage warnings
UNDEFINED_NAME  — AST-wide name-use vs.
                  name-definition union; catches
                  the classic LLM failure of
                  calling a helper that was
                  never emitted (NameError at
                  runtime)
HUMAN_REVIEW_REQUIRED — one per pending item
UNKNOWN_COLUMN  — heuristic F.col() vs known
                  output columns
```

`ValidationReport.passed` is simply "zero errors" (warnings don't block).
Saved as `validation_report_<pkg>.json`; summarised in every CLI run.

---

## 10. Phase 6 — The dual-axis scorecard

[`scoring.py`](../ssis_migration/scoring.py), assembled by
`MigrationPipeline._build_scorecard()` after validation. Full design and a
worked example are in [SCORING.md](SCORING.md); the essential shape:

```
 PARSING axis (DTSX → CIR)              FUNCTIONAL axis (CIR/DTSX → PySpark)
 ──────────────────────────             ─────────────────────────────────────
 structural_coverage()                  FunctionalValidatorAgent.equivalence_score
   weighted element-count ratio         (§7.8's 3-way judge)
        │                                       │
        ▼                                       ▼
 blended 60/40 with                      hard-capped at 0.5 if
 ParsingFidelityAgent.fidelity_score      check_pyspark_version() OR the judge's
 (LLM semantic audit of what the         version_issues found an invalid API
 CIR captured vs. the raw DTSX)                  │
        │                                       │
        ▼                                       ▼
   parsing.score                          functional.score
        └──────────────┬────────────────────────┘
                        ▼
              composite = parsing.score × functional.score

  PASS requires ALL of:
    composite ≥ MIGRATION_PASS_THRESHOLD (default 0.75)
    AND no functional critical_issues
    AND version_ok
    AND functional.judged == True     (judge must have actually RUN —
                                        an LLM outage cannot silently score 1.0)
    AND human_review_items == 0        (nothing pending review)
```

The multiplicative composite is deliberate: if the parser dropped half the
package, even a perfect codegen of the remaining half is only half-equivalent
to the original — fidelity loss compounds across the two hops rather than
averaging them away.

Saved as `scorecard_<pkg>.json`; the CLI prints composite, both axis scores,
the version verdict, and the top critical/version issues.

---

## 11. End-to-end sequence for one package

```
ssis-migrate convert pkg.dtsx --mode auto
        │
        ▼
[1] DTSXParser.parse()              → CIR, parse_coverage recorded
        │
        ▼
[2] DeterministicEngine.process()   → SQL transpiled/flagged, components mapped
        │
        ▼
[2.5] Router.plan()                 → routing_report_<pkg>.json
        │
        ▼
┌─── outer loop (≤ FUNCTIONAL_VALIDATION_MAX_ITERATIONS) ──────────────────┐
│ [3] LLMPipeline.process(cir, functional_context)                          │
│       for each LLM_REQUIRED item:                                         │
│         ScriptTaskAgent / ComplexSQLAgent .convert()                      │
│           chunk → generate → repair → memory.record → assemble           │
│           → ReviewAgent.review() → pass? accept : regen (≤ N times)       │
│       hybrid_<pkg>.json written (AssemblyManifest)                       │
│                                                                            │
│ [4] CodeGenerator.generate()  → <pkg>.py                                  │
│       whole-file compile gate (repair if broken)                         │
│                                                                            │
│ [3b] LLMPipeline.functional_validate(code, cir, dtsx_xml)                 │
│        3-way judge → passed? break : reset_llm_items(cir), loop           │
└────────────────────────────────────────────────────────────────────────┘
        │
        ▼
[5] StaticValidator + SemanticValidator → validation_report_<pkg>.json
        │
        ▼
[6] build_scorecard()               → scorecard_<pkg>.json
        │
        ▼
CLI prints: det. coverage, AUTO routing counts, validation PASS/FAIL,
            scorecard composite/parsing/functional + top issues
```

---

## 12. Conversion modes

| Mode | Phase 2 runs? | What calls the LLM | Use case |
|------|:---:|---|---|
| `deterministic` | ✔ | nothing | fast, free, no token; leaves TODO/human-review stubs |
| `hybrid` | ✔ | items the engine *couldn't* transpile | cost-controlled baseline |
| `llm` | ✘ | everything (Phase 1 only, then all-LLM) | pure-LLM comparison / worst-case fidelity |
| **`auto`** | ✔ | items the **Router** deems risky | **default** — re-examines the engine's own verdicts |

`ssis-migrate compare pkg.dtsx` runs all four modes and prints a side-by-side
table including each mode's scorecard composite.

---

## 13. Models

The GitHub Copilot Chat completions endpoint is the **only** LLM provider.
Model availability is a **live seat policy** that changes server-side without
notice — `GET /models` listing a model is not proof it will complete a
`/chat/completions` request (observed directly: `claude-haiku-4.5` worked in
June 2026 and returned `400 model_not_supported` in July 2026 despite still
being listed). Consequently:

- `COPILOT_MODEL` / `COPILOT_REVIEWER_MODEL` configure the intended generation
  and judge models (`gpt-4o` is the currently-verified-working default).
- `COPILOT_FALLBACK_MODEL` (default `gpt-4o`) is swapped in automatically the
  instant a model returns `model_not_supported`; the dead model is remembered
  process-wide so subsequent calls don't burn a request rediscovering it.
- The reviewer/judge roles can use a stronger or different model than
  generation via `COPILOT_REVIEWER_MODEL` — `ReviewAgent`,
  `FunctionalValidatorAgent`, and `ParsingFidelityAgent` all accept a model
  override on `simple_complete()`.

A Postman collection for manually probing the endpoint (model discovery +
a reviewer-agent request) lives at
[`docs/GitHub_Copilot_Chat.postman_collection.json`](GitHub_Copilot_Chat.postman_collection.json).

---

## 14. Complete module map

```
ssis_migration/
├── cir/
│   ├── models.py            Pydantic CIR schema (§4) — CIR, all sub-models,
│   │                        ConversionStatus/TranspilationStatus enums,
│   │                        find_connection(), flag_for_llm/human_review()
│   └── type_mapping.py       SSIS↔PySpark type table, normalize_ssis_type(),
│                             EXPRESSION_FUNCTION_MAP, KNOWN_DIVERGENCES
│
├── parser/                   Phase 1 (§3)
│   ├── dtsx_parser.py         DTSXParser — orchestrates all extractors,
│   │                          recursive data-flow discovery, coverage audit
│   ├── ns.py                  Namespace constants, EXECUTABLE_TYPE_MAP,
│   │                          COMPONENT_CLASS_MAP + LOGICAL_COMPONENT_MAP
│   ├── complexity_scorer.py   ComplexityScorer (Phase 0 + CIR metadata)
│   └── extractors/
│       ├── connections.py     ConnectionExtractor — provider detection,
│       │                      JDBC driver inference, key normalization
│       ├── control_flow.py    ControlFlowExecutable tree + precedence
│       ├── data_flow.py       DataFlowExtractor — namespace-agnostic (§3)
│       ├── parameters.py, variables.py, event_handlers.py
│
├── transform/
│   ├── deterministic/         Phase 2 (§5)
│   │   ├── engine.py           DeterministicEngine — 2-pass orchestration
│   │   ├── sql_transpiler.py   sqlglot wrapper + procedural-SQL detection
│   │   ├── expression_translator.py
│   │   └── component_mapper.py  per-subtype PySpark snippet generation
│   │
│   ├── routing.py              Phase 2.5 (§6) — Router, RoutingDecision,
│   │                           RoutingPlan, risk-signal regexes
│   │
│   └── llm/                    Phase 3 (§7)
│       ├── copilot_client.py    CopilotClient, NFRs, truncation handling,
│       │                        model fallback, masked JSON-L logging
│       ├── chunking.py          chunk_sql/chunk_dotnet/chunk_source,
│       │                        AgentMemory (§7.2, §7.3)
│       ├── repair.py            extract_code/normalize_code/ensure_compilable,
│       │                        SyntaxFixer (§7.4)
│       ├── assembly.py          AssemblyManifest/ItemAssembly/ChunkRecord (§7.5)
│       ├── generation.py        ChunkedGenerator (§7.6)
│       ├── agents.py            ScriptTaskAgent, ComplexSQLAgent,
│       │                        ExpressionAgent, ReviewAgent,
│       │                        FunctionalValidatorAgent, ParsingFidelityAgent
│       ├── prompts.py           every system/user prompt template,
│       │                        version-threaded, REGEN_SUFFIX,
│       │                        FUNCTIONAL_CONTEXT_SUFFIX
│       ├── confidence.py        compute_confidence()/confidence_action()
│       │                        (script-task/component auto-accept gate)
│       └── pipeline.py          LLMPipeline — per-package orchestration,
│                                shared memory+manifest, CIR summary builder
│
├── codegen/                    Phase 4 (§8)
│   ├── generator.py             CodeGenerator, AirflowDAGGenerator
│   └── templates/
│       ├── module.py.j2          the PySpark module template
│       ├── test_module.py.j2     test scaffold
│       └── airflow_dag.py.j2
│
├── validation/                 Phase 5 (§9)
│   ├── report.py                ValidationReport / Finding / Severity
│   ├── static.py                StaticValidator (incl. UNDEFINED_NAME)
│   └── semantic.py               SemanticValidator (CIR ↔ AST call graph)
│
├── scoring.py                   Phase 6 (§10) — dual-axis scorecard,
│                                element counters, version checker
├── resilience.py                CircuitBreaker, retry_call/backoff_delay,
│                                TokenBucket (see RESILIENCE.md)
├── inventory.py                 Phase 0 (§2)
├── pipeline.py                  MigrationPipeline — top-level orchestrator,
│                                ConversionMode, PipelineConfig/Result
├── config.py                    Config — .env loading, every tunable (§15)
└── cli.py                       ssis-migrate: assess/convert/compare/pipeline/config
```

---

## 15. Configuration reference

All settings load from `.env` (see `.env.example`) via
[`config.py`](../ssis_migration/config.py); everything is overridable by
shell environment variables of the same name.

| Variable | Default | Phase | Meaning |
|---|---|:---:|---|
| `GITHUB_TOKEN` | — | 3 | Copilot auth; required for any LLM phase |
| `COPILOT_MODEL` | `gpt-4o` | 3 | generation model |
| `COPILOT_REVIEWER_MODEL` | `gpt-4o` | 3 | reviewer/judge model |
| `COPILOT_FALLBACK_MODEL` | `gpt-4o` | 3 | swapped in on `model_not_supported` |
| `COPILOT_TEMPERATURE` | `0.1` | 3 | generation sampling temperature |
| `COPILOT_MAX_TOKENS` | `4096` | 3 | completion budget (auto-doubled once on truncation) |
| `COPILOT_MAX_REVIEW_ITERATIONS` | `4` | 3 | inner review→regen loop cap (§7.7) |
| `FUNCTIONAL_VALIDATION_MAX_ITERATIONS` | `2` | 3 | outer equivalence loop cap (§7.8) |
| `SYNTAX_FIX_MAX_ITERATIONS` | `2` | 3/4 | LLM syntax-edit attempts per artifact |
| `COPILOT_REQUEST_TIMEOUT` | `120` | 3 | per-HTTP-request seconds |
| `COPILOT_MAX_RETRIES` | `3` | 3 | HTTP attempts per call |
| `COPILOT_CIRCUIT_BREAKER_THRESHOLD` | `5` | 3 | consecutive failures → OPEN |
| `COPILOT_CIRCUIT_BREAKER_COOLDOWN` | `30` | 3 | seconds OPEN before a probe |
| `COPILOT_RATE_LIMIT_PER_MIN` | `0` (off) | 3 | client-side request cap |
| `CONVERSION_MODE` | `auto` | — | `deterministic`\|`hybrid`\|`llm`\|`auto` |
| `LLM_CONFIDENCE_THRESHOLD` | `0.50` | 3 | script/component auto-accept gate |
| `MIGRATION_PASS_THRESHOLD` | `0.75` | 6 | composite score required to PASS |
| `SPARK_VERSION` | `3.3` (`config.py` fallback); this project's `.env` pins `2.4.8` | 3/4/5/6 | target API level — threaded into every prompt, template, and both compile checks |
| `OUTPUT_DIR` | `output` | 4/5/6 | where `.py`/reports/manifests land |
| `LOG_LEVEL` | `INFO` | — | logging verbosity |

---

## 16. CLI reference

```
ssis-migrate assess   <dtsx_dir>                      Phase 0 inventory + wave plan
ssis-migrate convert  <dtsx_file> [--mode auto|hybrid|deterministic|llm]
                                  [--spark-version X] [--github-token T]
ssis-migrate compare  <dtsx_file>                      run all 4 modes, side-by-side table
ssis-migrate pipeline <dtsx_dir>  [--wave simple|medium|high|very_high|all]
ssis-migrate config                                    print resolved configuration
```

`convert` writes, per package, into `--output` (default `output/`):
`<module>.py`, `test_<module>.py`, `routing_report_<pkg>.json`,
`hybrid_<pkg>.json`, `validation_report_<pkg>.json`, `scorecard_<pkg>.json`,
and — `PipelineConfig.save_cir` defaults to `True` and is not yet exposed as a
CLI flag — `cir/<pkg>_cir_annotated.json` / `cir/<pkg>_cir_resolved.json`.

---

## 17. Testing

143 tests across 10 files, all offline except the manual E2E CLI runs against
the live Copilot endpoint described in this document's commit history:

| File | Covers |
|---|---|
| `test_parser.py`, `test_parser_completeness.py` | extractors, namespace handling, canonical-completeness regressions |
| `test_chunking.py` | SQL/`.NET` chunk boundaries, AgentMemory retrieval/rendering |
| `test_repair.py` | extract/normalize stages, `SyntaxFixer` edit loop (fake client) |
| `test_chunked_generation.py` | full generate→repair→review→manifest flow with a routing fake client |
| `test_client_hardening.py` | truncation stitching, request budget fallbacks |
| `test_resilience.py` | circuit breaker states, jittered backoff, token bucket |
| `test_routing.py` | risk-signal detection, per-item AUTO decisions |
| `test_scoring.py` | structural coverage, parsing/functional composition, version checker, scorecard pass gates |
| `test_codegen_quality.py` | rendered-module compile/quality gates on real samples |

Run with `python -m pytest -q` (rootdir `pyproject.toml`, `testpaths = tests`).

---

## 18. Known gaps / roadmap

Captured from the most recent end-to-end verification — quality work, not
architectural gaps:

- Operational-task templates (`file_system`, `send_mail`, `ftp`,
  `execute_process`) currently render as human-review banners rather than
  concrete deterministic snippets.
- SSIS `?`-style query parameters (bound via `@[User::Var]` parameter
  mappings) are not yet wired from `params` into the generated source query
  string.
- Great Expectations data-level validation and Airflow DAG generation from the
  dependency graph (`AirflowDAGGenerator` exists but isn't invoked by the
  pipeline yet) are designed but not yet part of the automated `run()` flow.
