# Commit: feat(pipeline) — End-to-end pipeline, inventory, CLI + bug fixes

## What changed

### New files
- `ssis_migration/inventory.py` — Phase 0 assessment: scan dirs, build dep graph, wave plan
- `ssis_migration/pipeline.py` — MigrationPipeline: Parse→Deterministic→LLM→Generate→Validate
- `ssis_migration/cli.py` — Click CLI: `assess`, `convert`, `pipeline` commands with Rich output
- `docs/CHANGES-llm-pipeline.md`, `docs/CHANGES-deterministic-engine.md`

### Bug fixes (found by running real .dtsx files)
- `parameters.py` / `variables.py`: name attribute is `DTS:ObjectName`, not `DTS:Name`
- `connections.py`: ConnectionString is nested inside `DTS:ObjectData/DTS:ConnectionManager`
- `control_flow.py`: fall back to `DTS:CreationName`; default bare container to "sequence"
- `ns.py`: add `STOCK:SEQUENCE` to executable type map
- `engine.py`: data_flow/sequence/container executables → DETERMINISTIC immediately
- `codegen/generator.py`: remove broken `_enumerate_filter`
- `module.py.j2`: use `loop.index0` instead of `| enumerate`

## Validation results on real files
| Package | Errors | Note |
|---------|--------|------|
| SSIS_SQLExtractSample.dtsx | 0 | PASS |
| ETL_Load_Customers.dtsx | 1 | MERGE SQL → LLM_REQUIRED (correct) |
| ETL_Load_Orders.dtsx | 1 | MERGE SQL → LLM_REQUIRED (correct) |

The 2 FAIL results are **expected** — they contain MERGE statements (procedural SQL)
that deterministically route to `LLM_REQUIRED` and will be resolved by Phase 3 when
a GitHub Copilot token is provided.
