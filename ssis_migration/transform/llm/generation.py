"""
ChunkedGenerator — the generate → repair → record engine shared by all
code-producing agents.

Per item:
  1. the source is semantically chunked (chunking.chunk_source)
  2. each chunk is generated with AGENT MEMORY injected (facts, symbols defined
     by earlier chunks, relevant prior-chunk notes, known pitfalls)
  3. each chunk passes the EDITING syntax validator (repair.ensure_compilable)
     — chunk-by-chunk compile gating, exactly where damage is cheapest to fix
  4. the chunk lands in the AssemblyManifest (hybrid stage) and its symbols are
     recorded into memory before the next chunk is generated
  5. chunks are concatenated and the ASSEMBLED snippet passes the syntax
     validator again (whole-unit compile gate)

Semantic review stays with the caller (the agents' review→regen loop): this
module guarantees the code COMPILES; the reviewer judges whether it's RIGHT.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from ssis_migration.transform.llm.assembly import AssemblyManifest, ChunkRecord, ItemAssembly
from ssis_migration.transform.llm.chunking import AgentMemory, CodeChunk, chunk_source
from ssis_migration.transform.llm.prompts import CHUNK_NOTE, MEMORY_BLOCK
from ssis_migration.transform.llm.repair import SyntaxFixer, ensure_compilable

logger = logging.getLogger(__name__)

# Per-chunk transport retries (client already retries HTTP-level failures; this
# guards against empty/exception completions surfacing from simple_complete).
_CHUNK_ATTEMPTS = 2


class ChunkedGenerator:
    def __init__(
        self,
        client,
        fixer: SyntaxFixer | None,
        memory: AgentMemory,
        max_llm_fixes: int = 2,
        model: str | None = None,
    ) -> None:
        self._client = client
        self._fixer = fixer
        self._memory = memory
        self._max_llm_fixes = max_llm_fixes
        self._model = model

    def generate_item(
        self,
        item: ItemAssembly,
        source_text: str,
        language: str,
        system_prompt: str,
        make_user: Callable[[str, str], str],
        manifest: AssemblyManifest | None = None,
    ) -> tuple[str, bool]:
        """
        Generate the PySpark snippet for one CIR item.

        make_user(chunk_source, context_suffix) must return the full user
        prompt for one chunk — the agent owns its template; this engine owns
        chunking, memory injection, repair, and assembly recording.

        Returns (assembled_code, syntax_ok).
        """
        chunks = chunk_source(source_text, language)
        item.chunked = len(chunks) > 1
        item.chunks = []          # regen replaces previous chunk records

        if item.chunked:
            logger.info(
                "Chunked conversion for %s: %d chunks (%s)",
                item.item_id, len(chunks), ", ".join(c.title for c in chunks[:5]),
            )

        parts: list[str] = []
        for chunk in chunks:
            record = self._generate_chunk(item, chunk, system_prompt, make_user)
            item.chunks.append(record)
            if record.code.strip():
                header = (
                    f"# ── chunk {record.index}/{record.total}: {record.title} ──\n"
                    if item.chunked else ""
                )
                parts.append(header + record.code.rstrip("\n"))
                # Memory learns this chunk BEFORE the next one is generated —
                # that is what makes generation context-aware.
                self._memory.record_code(chunk.title, record.code)

        assembled = "\n\n".join(parts) + ("\n" if parts else "")

        # Whole-unit compile gate (chunk gates make this near-always a no-op,
        # but concatenation seams are exactly where surprises live).
        result = ensure_compilable(
            assembled, fixer=self._fixer,
            max_llm_fixes=self._max_llm_fixes,
            label=f"{item.item_id}/assembled",
        )
        item.assembled_code = result.code
        item.syntax_ok = result.ok
        if not result.ok:
            item.notes = f"assembled snippet not compilable: {result.error}"
            logger.warning("Assembled snippet for %s still broken: %s", item.item_id, result.error)

        if manifest is not None:
            manifest.items[item.item_id] = item
        return result.code, result.ok

    # ── private ───────────────────────────────────────────────────────────────

    def _generate_chunk(
        self,
        item: ItemAssembly,
        chunk: CodeChunk,
        system_prompt: str,
        make_user: Callable[[str, str], str],
    ) -> ChunkRecord:
        context = MEMORY_BLOCK.format(memory=self._memory.render(chunk.text)) \
            if self._memory.render(chunk.text) else ""
        if chunk.total > 1:
            context += CHUNK_NOTE.format(
                index=chunk.index, total=chunk.total,
                kind=chunk.kind, title=chunk.title,
            )
        user_msg = make_user(chunk.text, context)

        raw = ""
        error: str | None = None
        attempts = 0
        for attempt in range(1, _CHUNK_ATTEMPTS + 1):
            attempts = attempt
            try:
                raw = self._client.simple_complete(system_prompt, user_msg, model=self._model)
                if raw.strip():
                    error = None
                    break
                error = "empty completion"
            except Exception as exc:
                error = str(exc)
                logger.warning(
                    "Chunk %s/%s generation attempt %d failed: %s",
                    item.item_id, chunk.title, attempt, exc,
                )

        if not raw.strip():
            return ChunkRecord(
                index=chunk.index, total=chunk.total, kind=chunk.kind,
                title=chunk.title, source_excerpt=_excerpt(chunk.text),
                code="", syntax_ok=False, attempts=attempts,
                error=error or "no completion",
            )

        repair = ensure_compilable(
            raw, fixer=self._fixer,
            max_llm_fixes=self._max_llm_fixes,
            label=f"{item.item_id}/{chunk.title}",
        )
        return ChunkRecord(
            index=chunk.index, total=chunk.total, kind=chunk.kind,
            title=chunk.title, source_excerpt=_excerpt(chunk.text),
            code=repair.code, syntax_ok=repair.ok,
            repair_stages=repair.stages, attempts=attempts,
            error=repair.error,
        )


def _excerpt(text: str, limit: int = 160) -> str:
    first = " ".join(text.strip().split())
    return first[:limit] + ("…" if len(first) > limit else "")
