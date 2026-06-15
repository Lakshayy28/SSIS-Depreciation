# Commit: chore — repo scaffold

## What changed
- `pyproject.toml` — project metadata, all runtime/dev/optional dependencies pinned to stable ranges
- `README.md` — architecture diagram, pipeline-phase table, quick-start, LLM-provider note
- `docs/pyspark-version-analysis.md` — reasoning for targeting PySpark 3.3+ over 2.4.8
- `.gitignore` — Python, IDE, OS, and generated-artefact exclusions

## Key decisions
- PySpark target: **3.3+** (see docs/pyspark-version-analysis.md)
- LLM provider: **GitHub Copilot Chat completions endpoint** only (`GITHUB_TOKEN` env var)
- No database dependency for CIR — plain JSON files, version-control friendly
