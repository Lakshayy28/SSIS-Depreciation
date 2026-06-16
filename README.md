# Enterprise SSIS-to-PySpark Migration Framework

Automates conversion of legacy SSIS `.dtsx` packages into **functionally
equivalent** PySpark modules. An LLM acts as both **codegen assistant** and
**critical judge**, an `auto` router decides per item when to use it, and a
quantitative **dual-axis scorecard** gates every result — all centred on a
Canonical Intermediate Representation (CIR).

📖 **Deep dives:** [Architecture](docs/ARCHITECTURE.md) ·
[Scoring](docs/SCORING.md) · [Resilience / NFRs](docs/RESILIENCE.md)

## Architecture overview

```
        parse                         generate
 DTSX  ───────►  CIR (canonical)  ───────────►  PySpark
 (XML)          (typed JSON IR)               (.py module)
   │                  │                            │
   │   ┌── AUTO router decides per item ──┐        │
   │   │   deterministic | LLM | human    │        │
   │   └──────────────────────────────────┘        │
   │                                                │
   └─ parsing fidelity ─┐         ┌─ functional equivalence ─┘
                        ▼         ▼   (DTSX vs CIR vs PySpark, LLM judge)
                     SCORECARD (parsing × functional, version-gated)
```

The LLM-converted items pass through a **review → regenerate** loop, and the
whole package through an **equivalence-review → reconvert** loop, until the
3-way reviewer (DTSX = ground truth) passes or iterations are exhausted.

## Pipeline phases

| Phase | Description |
|-------|-------------|
| 0 | Inventory & Assessment — scan all `.dtsx` files, build dependency graph, assign complexity scores |
| 1 | Parsing — DTSX XML → CIR JSON via `lxml` |
| 2 | Deterministic Transformation — `sqlglot` SQL transpilation, component mapping, expression translation |
| 2.5 | **AUTO routing** — risk-aware, auditable per-item decision (deterministic / LLM / human) |
| 3 | **LLM conversion + equivalence-review loop** — Script Tasks, complex SQL, custom components; reviewed against the DTSX until equivalent |
| 4 | Code Generation — Jinja2 templates → `.py` PySpark modules |
| 5 | **Validation + scorecard** — static/semantic checks plus the dual-axis fidelity score |

## Conversion modes

| Mode | LLM used for | Notes |
|------|--------------|-------|
| `deterministic` | nothing | fast, free, no token |
| `hybrid` | items the engine couldn't transpile | cost-controlled |
| `llm` | everything | pure-LLM comparison |
| **`auto`** | items the router deems risky | **default** — best fidelity/cost trade-off |

## Quick start

```bash
pip install -e ".[dev]"

# Assess a folder of .dtsx files
ssis-migrate assess ./packages/

# Convert a single package in AUTO mode (default)
ssis-migrate convert ./packages/CustomerLoad.dtsx --mode auto --output ./output/

# Compare all four modes side by side
ssis-migrate compare ./packages/CustomerLoad.dtsx

# Run full pipeline on a wave
ssis-migrate pipeline ./packages/ --wave simple --output ./output/

# Inspect resolved configuration
ssis-migrate config
```

Each `convert` run writes, alongside the `.py` module:
`routing_report_<pkg>.json` (AUTO decisions), `scorecard_<pkg>.json` (dual-axis
fidelity score), and `validation_report_<pkg>.json`.

## LLM provider & models

All LLM-augmented steps use the **GitHub Copilot Chat completions endpoint**.
Set `GITHUB_TOKEN` before running LLM-dependent phases. The default model for
**both generation and review** is `claude-haiku-4.5` (it beat `gpt-4o` on both
tasks here); override via `COPILOT_MODEL` / `COPILOT_REVIEWER_MODEL`. Every
request/response is logged with the bearer token masked under
`copilot_chat_completions/` (git-ignored). See
[docs/GitHub_Copilot_Chat.postman_collection.json](docs/GitHub_Copilot_Chat.postman_collection.json)
to discover supported models for your seat.

## Reliability

Copilot calls are guarded by a process-wide **circuit breaker**, **jittered
retry**, and an optional **rate limiter** — see [docs/RESILIENCE.md](docs/RESILIENCE.md).
If Copilot is unreachable the pipeline degrades gracefully to deterministic
output rather than failing hard.

## Target

PySpark version is set by `SPARK_VERSION` and is **threaded into every
generation and review prompt** so output targets that exact API level; the
reviewer and scorecard flag version-incompatible APIs. See
[docs/pyspark-version-analysis.md](docs/pyspark-version-analysis.md) for the
2.4 vs 3.x discussion.
