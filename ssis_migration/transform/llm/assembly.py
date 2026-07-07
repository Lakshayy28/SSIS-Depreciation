"""
Hybrid assembly stage — the auditable intermediate between CIR and PySpark.

    DTSX ──► CIR (canonical) ──► ASSEMBLY MANIFEST (hybrid) ──► .py module
                                  ▲ chunk-by-chunk record

Code conversion APPENDS into this stage progressively: every generated chunk is
recorded with its source excerpt, produced code, syntax status, repair stages
and attempts; every item records its assembled snippet and review outcome. The
manifest is saved as ``hybrid_<package>.json`` next to the module, so the exact
provenance of every line of generated code is inspectable — which chunk it came
from, what it took to make it compile, and what the reviewer said.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class ChunkRecord:
    index: int
    total: int
    kind: str
    title: str
    source_excerpt: str          # first line(s) of the source unit
    code: str                    # generated (repaired) code
    syntax_ok: bool
    repair_stages: list[str] = field(default_factory=list)
    attempts: int = 1
    error: str | None = None


@dataclass
class ItemAssembly:
    item_id: str
    item_kind: str               # script_task | complex_sql | expression | …
    chunked: bool = False
    chunks: list[ChunkRecord] = field(default_factory=list)
    assembled_code: str = ""
    syntax_ok: bool = False
    review_passed: bool = False
    review_issues: list[str] = field(default_factory=list)
    iterations: int = 0          # review→regen iterations consumed
    status: str = "pending"      # pending | complete | human_review | failed
    notes: str = ""


@dataclass
class AssemblyManifest:
    package: str
    spark_version: str
    items: dict[str, ItemAssembly] = field(default_factory=dict)

    def item(self, item_id: str, item_kind: str) -> ItemAssembly:
        """Get-or-create the assembly record for an item (regen replaces chunks)."""
        existing = self.items.get(item_id)
        if existing is None:
            existing = ItemAssembly(item_id=item_id, item_kind=item_kind)
            self.items[item_id] = existing
        return existing

    def to_dict(self) -> dict:
        return {
            "package": self.package,
            "spark_version": self.spark_version,
            "summary": self.summary(),
            "items": {k: asdict(v) for k, v in self.items.items()},
        }

    def summary(self) -> dict:
        statuses: dict[str, int] = {}
        chunk_total = 0
        repaired = 0
        for item in self.items.values():
            statuses[item.status] = statuses.get(item.status, 0) + 1
            chunk_total += len(item.chunks)
            repaired += sum(1 for c in item.chunks if c.repair_stages)
        return {
            "items": len(self.items),
            "statuses": statuses,
            "chunks": chunk_total,
            "chunks_repaired": repaired,
        }

    def save(self, path: Path | str) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
