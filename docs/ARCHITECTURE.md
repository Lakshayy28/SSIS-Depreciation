# Architecture — LLM-Assisted, Functionally-Equivalent SSIS → PySpark Migration

The heart of this framework is a **functionally-equivalent** migration:
every SSIS `.dtsx` package is translated into a PySpark module that provably
does the same thing, with an LLM acting as both **codegen assistant** and
**critical judge**, and a quantitative scorecard gating the result.

Migration is never a single leap. It crosses two lossy boundaries:

```
        parse                         generate
 DTSX  ───────►  CIR (canonical)  ───────────►  PySpark
 (XML)          (typed JSON IR)               (.py module)
   │                  │                            │
   └────── parsing ───┘                            │
              fidelity            functional        │
                          equivalence ◄─────────────┘
                          (judged against DTSX *and* CIR)
```

Because fidelity can be lost at either hop, we **measure each hop separately**
and combine them (see [SCORING.md](SCORING.md)).

---

## The five phases

| Phase | Stage | Deterministic? | Output |
|------:|-------|----------------|--------|
| 1 | **Parse** — DTSX XML → CIR | yes (`lxml`) | `*_cir_*.json` |
| 2 | **Deterministic transform** — sqlglot SQL, component mapping, expressions | yes | annotated CIR |
| 2.5 | **AUTO routing** — risk-aware per-item decision | yes | `routing_report_*.json` |
| 3 | **LLM conversion + equivalence review loop** | LLM | resolved CIR |
| 4 | **Code generation** — CIR → PySpark | yes (Jinja2) | `*.py` |
| 5 | **Validation + scorecard** — static/semantic + dual-axis score | yes + LLM judge | `validation_report_*.json`, `scorecard_*.json` |

---

## Conversion modes

The mode controls *who decides* what gets the LLM treatment.

| Mode | Phase 2 | What calls the LLM | When to use |
|------|---------|--------------------|-------------|
| `deterministic` | ✔ | nothing | fast, free, no token; leaves TODO stubs |
| `hybrid` | ✔ | items the engine *couldn't* transpile | cost-controlled baseline |
| `llm` | ✘ | everything | pure-LLM comparison / worst-case fidelity |
| **`auto`** | ✔ | items the **router** deems risky | **default — best fidelity/cost trade-off** |

### Why AUTO is more than HYBRID

HYBRID trusts the deterministic engine's "I can / I can't" signal. AUTO adds a
transparent [`Router`](../ssis_migration/transform/routing.py) that runs *after*
the engine and can **escalate items the engine thought it handled** when a
mechanical translation would risk changing behaviour:

- **DETERMINISTIC** — clean set-based SQL, simple expressions, structural
  containers, templated operational tasks (File System / FTP / Send Mail).
- **LLM** — procedural / high-risk T-SQL (`EXEC`, `CURSOR`, `WHILE`, `MERGE`,
  dynamic SQL, temp tables, `PIVOT`), .NET Script Tasks/Components, and partial
  transpiles.
- **HUMAN_REVIEW** — cross-package execution, unknown third-party components,
  empty/garbled bodies.

The router **never downgrades** the engine's "I can't" verdict — it only
confirms or escalates — and records a reason + the risk signals behind every
decision, so an AUTO run is fully auditable (`routing_report_<pkg>.json`).

---

## The four artifact stages

```
 DTSX ──► CIR (canonical, 100%-coverage audited) ──► ASSEMBLY MANIFEST (hybrid,
 chunk-by-chunk provenance) ──► .py module (compile-gated, human-review banners)
```

Generation itself is **chunked** (SQL batches / .NET methods) with a shared
**agent memory** carrying defined symbols and reviewer pitfalls between chunks,
and every artifact passes an **editing syntax validator** at three altitudes
(chunk → item → whole file). See [GENERATION.md](GENERATION.md) for the full
design — chunking boundaries, lexical retrieval, and the two-validator split
(the semantic reviewer never edits; the syntax validator only edits).

## Phase 3 — the conversion + equivalence-review loop

This loop is what makes the output *functionally equivalent* rather than merely
*plausible*. Two nested feedback loops operate:

### Inner loop — component review→regen (per item)

For each LLM item ([`agents.py`](../ssis_migration/transform/llm/agents.py)):

```
generate ──► review ──► pass? ──► accept
   ▲                      │ no
   └──── issues fed back ─┘   (regenerate from scratch, max N iterations)
```

The reviewer **returns issues, it never patches code**. Feeding issues back to
the *generator* (rather than re-reviewing the reviewer's own edits) eliminates
the oscillation where a reviewer keeps "fixing" its own corrections.

### Outer loop — package equivalence review→reconvert

After all items convert and code is generated, the **3-way equivalence reviewer**
compares the raw DTSX, the CIR, and the generated PySpark
([`FUNCTIONAL_VALIDATOR`](../ssis_migration/transform/llm/prompts.py)). The DTSX
is ground truth: if the CIR and PySpark agree with each other but both diverge
from the DTSX, that is still a critical issue.

```
convert all items ─► generate ─► EQUIVALENCE REVIEW (DTSX vs CIR vs PySpark)
       ▲                                    │
       │  critical issues as feedback       │ passed?
       └────────────── no ──────────────────┤
                                             │ yes / iterations exhausted
                                             ▼
                                    build scorecard
```

The reviewer also performs an explicit **PySpark version check** against the
configured `SPARK_VERSION`, so generated code targets the exact API level and
version-incompatible APIs surface as critical (`version_issues`).

The loop is **bounded** by `FUNCTIONAL_VALIDATION_MAX_ITERATIONS` — unbounded
"loop until perfect" is a cost/availability hazard, so we cap it and report the
honest score if equivalence isn't reached.

---

## Models

`claude-haiku-4.5` is the default for **both** generation and review — it
outperformed `gpt-4o` on both tasks here. The reviewer/judge can be pointed at a
different model with `COPILOT_REVIEWER_MODEL`. All LLM traffic goes through the
GitHub Copilot Chat completions endpoint; every request/response is logged
(bearer token masked) under `copilot_chat_completions/`.

---

## Non-functional reliability

All Copilot calls are wrapped by [`resilience.py`](../ssis_migration/resilience.py):
a process-wide **circuit breaker**, **jittered exponential retry**, and an
optional **token-bucket rate limiter**. See [RESILIENCE.md](RESILIENCE.md).

---

## Key source map

| Concern | Module |
|---------|--------|
| Canonical IR schema | `ssis_migration/cir/models.py` |
| DTSX parser | `ssis_migration/parser/` |
| Deterministic transforms | `ssis_migration/transform/deterministic/` |
| **AUTO router** | `ssis_migration/transform/routing.py` |
| **LLM agents (gen + review + judges)** | `ssis_migration/transform/llm/agents.py` |
| **Prompts (version-threaded)** | `ssis_migration/transform/llm/prompts.py` |
| LLM orchestration | `ssis_migration/transform/llm/pipeline.py` |
| **Dual-axis scoring** | `ssis_migration/scoring.py` |
| **Resilience / NFRs** | `ssis_migration/resilience.py` |
| Pipeline orchestrator | `ssis_migration/pipeline.py` |
| CLI | `ssis_migration/cli.py` |
