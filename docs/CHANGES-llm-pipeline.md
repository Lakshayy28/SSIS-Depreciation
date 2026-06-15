# Commit: feat(llm) — LLM Augmentation Pipeline

## What changed
- `ssis_migration/transform/llm/copilot_client.py` — GitHub Copilot Chat client with retry/backoff
- `ssis_migration/transform/llm/prompts.py` — Agent prompt templates
- `ssis_migration/transform/llm/agents.py` — ScriptTaskAgent, ComplexSQLAgent, ExpressionAgent, ReviewAgent
- `ssis_migration/transform/llm/confidence.py` — Weighted confidence scoring
- `ssis_migration/transform/llm/pipeline.py` — LLM routing orchestrator

## Key design decisions
- Only provider: **GitHub Copilot Chat** (`https://api.githubcopilot.com/chat/completions`, Bearer token from `GITHUB_TOKEN`)
- Temperature = 0.1 for all code generation — low temperature reduces hallucination
- ReviewAgent uses JSON-structured output to enable programmatic pass/fail detection
- Confidence score < 0.50 → HUMAN_REVIEW (not auto-accepted, not auto-discarded)
- `pyspark_snippet` is always set even for HUMAN_REVIEW items so reviewers have a starting point
- The client includes required Copilot API headers: `Editor-Version`, `Editor-Plugin-Version`, `Openai-Intent`
