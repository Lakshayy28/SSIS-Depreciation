# Enterprise SSIS-to-PySpark Migration Framework

Automates conversion of legacy SSIS `.dtsx` packages into functionally equivalent PySpark modules using a **hybrid deterministic + LLM** architecture centred on a Canonical Intermediate Representation (CIR).

## Architecture overview

```
.dtsx files
    │
    ▼
[ DTSX Parser ]  ──►  CIR (JSON)  ──►  [ Deterministic Engine ]  ──►  CIR (annotated)
                                                   │
                                          (complex items only)
                                                   │
                                                   ▼
                                         [ LLM Pipeline ]  ──►  CIR (resolved)
                                                                       │
                                                                       ▼
                                                           [ Code Generator ]  ──►  .py files
                                                                                        │
                                                                                        ▼
                                                                             [ Validator ]  ──►  Report
```

## Pipeline phases

| Phase | Description |
|-------|-------------|
| 0 | Inventory & Assessment — scan all `.dtsx` files, build dependency graph, assign complexity scores |
| 1 | Parsing — DTSX XML → CIR JSON via `lxml` |
| 2 | Deterministic Transformation — `sqlglot` SQL transpilation, component mapping, expression translation |
| 3 | LLM Augmentation — Script Tasks, complex SQL, custom components via GitHub Copilot Chat |
| 4 | Code Generation — Jinja2 templates → `.py` PySpark modules |
| 5 | Validation — static analysis, semantic equivalence, data equivalence (Great Expectations) |
| 6 | Orchestrator Generation — Airflow DAGs from dependency graph |

## Quick start

```bash
pip install -e ".[dev]"

# Assess a folder of .dtsx files
ssis-migrate assess ./packages/

# Convert a single package
ssis-migrate convert ./packages/CustomerLoad.dtsx --output ./output/

# Run full pipeline on a wave
ssis-migrate pipeline ./packages/ --wave simple --output ./output/
```

## LLM provider

The framework uses the **GitHub Copilot Chat completions endpoint** for all LLM-augmented steps.
Set `GITHUB_TOKEN` in your environment before running LLM-dependent phases.

## Target

PySpark 3.3+ (see `docs/pyspark-version-analysis.md` for details on the 2.4 vs 3.x decision).
