# Commit: feat(parser) — DTSX XML parser

## What changed
- `ssis_migration/parser/ns.py` — All DTSX namespace URIs, attribute constants, componentClassID GUID map
- `ssis_migration/parser/extractors/parameters.py` — Package parameter extraction
- `ssis_migration/parser/extractors/variables.py` — Package/container variable extraction
- `ssis_migration/parser/extractors/connections.py` — Connection manager extraction with JDBC mapping
- `ssis_migration/parser/extractors/control_flow.py` — Recursive executor/container/constraint parsing
- `ssis_migration/parser/extractors/data_flow.py` — Full pipeline component graph extraction
- `ssis_migration/parser/extractors/event_handlers.py` — OnError/OnWarning executables
- `ssis_migration/parser/complexity_scorer.py` — Simple/Medium/High/VeryHigh scoring
- `ssis_migration/parser/dtsx_parser.py` — Top-level orchestrator

## Key design decisions
- Parser is **tolerant**: missing/unexpected XML elements are logged as warnings, not exceptions — so partial packages produce best-effort CIR
- All component class ID GUIDs are in `ns.py::COMPONENT_CLASS_MAP` — adding new vendor components is a one-line change
- Complexity scorer drives **wave planning** (which packages convert first) and **LLM routing** (which items go to the LLM pipeline)
- `_simplify_ref()` strips SSIS full refId paths to just the task name to make precedence constraints human-readable
- Lineage is built naively from SQL FROM/JOIN patterns; sqlglot (next commit) provides precise lineage
