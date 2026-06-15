# Commit: feat(transform) — Deterministic Engine

## What changed
- `ssis_migration/transform/deterministic/sql_transpiler.py` — sqlglot T-SQL→Spark SQL with procedural SQL detection
- `ssis_migration/transform/deterministic/expression_translator.py` — SSIS Expression Language → PySpark column expressions
- `ssis_migration/transform/deterministic/component_mapper.py` — All standard SSIS component types → PySpark snippets
- `ssis_migration/transform/deterministic/engine.py` — Three-pass orchestrator

## Key design decisions
- sqlglot is used at `ErrorLevel.RAISE` first; falls back to `WARN` for partial transpilation before flagging LLM_REQUIRED
- Expression translator uses a recursive token-based approach (not a full AST parser) — handles ~85% of common Derived Column patterns
- Functions with no deterministic equivalent (`TOKEN`, `CODEPOINT`, etc.) map to `None` in `EXPRESSION_FUNCTION_MAP` → auto-routed to LLM
- ComponentMapper.process() updates `deterministic_coverage` on the CIR after all components are processed — this metric drives benchmarking
- Script Tasks are always flagged LLM_REQUIRED regardless of content
