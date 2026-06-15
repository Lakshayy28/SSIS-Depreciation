# Commit: feat(cir) — CIR Pydantic models and type-mapping table

## What changed
- `ssis_migration/cir/models.py` — Full Pydantic v2 model hierarchy for the CIR
- `ssis_migration/cir/type_mapping.py` — SSIS DT_* → CIR canonical → PySpark type table + expression function map

## Key design decisions
- All models use `model_config = {"populate_by_name": True}` to allow both Python names and JSON aliases (e.g. `from_id` / `"from"`)
- `CIR.save()` / `CIR.load()` provide round-trip JSON serialisation with `by_alias=True` so the on-disk format uses JSON-idiomatic names
- `flag_for_llm()` and `flag_for_human_review()` are the primary side-channel between the deterministic engine and the LLM pipeline
- `KNOWN_DIVERGENCES` in type_mapping.py feeds the acceptable-divergence register in the validation report
- Expression functions that have no direct PySpark equivalent (TOKEN, CODEPOINT, HEX, UNHEX) map to `None` — the deterministic engine uses this to route to the LLM pipeline
