"""
Dual-axis migration scoring.

A faithful SSIS→PySpark migration has to survive *two* lossy translations:

    DTSX  ──parse──►  CIR (canonical)  ──generate──►  PySpark

So a single "did it work" number hides where fidelity was lost.  We score each
hop independently and combine them multiplicatively, because end-to-end fidelity
is the product of the two stages — if the parser dropped half the package, even
a perfect codegen of what remains is only half-equivalent to the original:

    PARSING fidelity      = how completely the CIR captures the DTSX
                            (deterministic structural coverage × LLM audit)
    FUNCTIONAL equivalence = how faithfully the PySpark reproduces the CIR/DTSX
                            behaviour (LLM-as-judge) × PySpark version validity

    composite = parsing.score × functional.score          ∈ [0, 1]

The LLM is used **as a judge** on both axes; the structural-coverage and
version checks are deterministic and run with or without a token.  This module
is pure/deterministic — the pipeline feeds it the LLM judges' results.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ssis_migration.cir.models import CIR

logger = logging.getLogger(__name__)


# ─── Deterministic PySpark version validation ─────────────────────────────────

# PySpark APIs keyed by the (major, minor) version that introduced them. If the
# generated code calls one of these and the target version is older, it cannot
# run there — a hard version failure that caps the functional score.
_API_MIN_VERSION: dict[str, tuple[int, int]] = {
    "applyInPandas": (3, 0),
    "mapInPandas": (3, 0),
    "observe": (3, 0),
    "inputFiles": (3, 1),
    "pandas_api": (3, 2),
    "to_pandas_on_spark": (3, 2),
    "mapInArrow": (3, 3),
    "applyInPandasWithState": (3, 4),
    "withColumnsRenamed": (3, 4),
    "unpivot": (3, 4),
    "melt": (3, 4),
    "offset": (3, 4),
}


def parse_version(spark_version: str) -> tuple[int, int]:
    parts = spark_version.split(".")
    try:
        return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except (ValueError, IndexError):
        return (3, 3)


def check_pyspark_version(code: str, spark_version: str) -> tuple[bool, list[str]]:
    """
    Deterministically flag PySpark APIs in ``code`` that postdate ``spark_version``.
    Returns (version_ok, issues).  Complements the LLM judge's version check.
    """
    target = parse_version(spark_version)
    issues: list[str] = []
    for api, min_ver in _API_MIN_VERSION.items():
        if min_ver > target and re.search(rf'\b{re.escape(api)}\s*\(', code):
            issues.append(
                f"`{api}()` requires PySpark {min_ver[0]}.{min_ver[1]}+, "
                f"but target is {spark_version}"
            )
    return (not issues, issues)


# ─── Element counting (deterministic parsing coverage) ────────────────────────

# Category → weight when averaging structural coverage.  Executables and data-
# flow components carry the behaviour, so they dominate; parameters/variables/
# connections are lighter.
_COVERAGE_WEIGHTS = {
    "executables": 3.0,
    "dataflow_components": 3.0,
    "connections": 1.0,
    "parameters": 1.0,
    "variables": 1.0,
}


def count_dtsx_elements(dtsx_path: Path | str) -> dict[str, int]:
    """Count the structurally significant SSIS XML elements in the raw .dtsx."""
    from lxml import etree

    from ssis_migration.parser import ns

    try:
        tree = etree.parse(str(dtsx_path))
    except (OSError, etree.XMLSyntaxError) as exc:  # pragma: no cover - defensive
        logger.warning("Could not parse DTSX for scoring: %s", exc)
        return {k: 0 for k in _COVERAGE_WEIGHTS}

    root = tree.getroot()

    # The data-flow pipeline XML is embedded under DTS:ObjectData and its
    # <component> elements are UNPREFIXED (no namespace) — so match by local
    # name regardless of namespace rather than assuming the pipeline NS.
    dataflow_components = sum(
        1 for e in root.iter()
        if isinstance(e.tag, str) and e.tag.rsplit("}", 1)[-1] == "component"
    )
    return {
        "executables": len(root.findall(f".//{ns.DTS_EXECUTABLE}")),
        "dataflow_components": dataflow_components,
        "connections": len(root.findall(f".//{ns.DTS_CONNECTION_MANAGER}")),
        "parameters": len(root.findall(f".//{ns.DTS_PARAMETER}")),
        "variables": len(root.findall(f".//{ns.DTS_VARIABLE}")),
    }


def count_cir_elements(cir: CIR) -> dict[str, int]:
    """Count the same categories as captured in the CIR (recursing containers)."""
    def _count_exes(exes) -> int:
        total = 0
        for e in exes:
            total += 1 + _count_exes(e.children)
        return total

    return {
        "executables": _count_exes(cir.control_flow.execution_tree),
        "dataflow_components": sum(len(df.components) for df in cir.data_flows),
        "connections": len(cir.connections),
        "parameters": len(cir.parameters),
        "variables": len(cir.variables),
    }


def _unmapped_items(cir: CIR) -> list[str]:
    """CIR nodes the parser could not classify (unknown component / raw type)."""
    unmapped: list[str] = []
    for df in cir.data_flows:
        for comp in df.components:
            if comp.subtype in ("unknown_component", "unknown"):
                unmapped.append(f"component:{comp.name} ({comp.subtype})")
    known_exe_types = {
        "execute_sql", "data_flow", "script_task", "for_loop", "foreach_loop",
        "sequence", "expression_task", "execute_package", "file_system", "ftp",
        "send_mail", "execute_process", "bulk_insert", "data_profiling",
    }

    def _walk(exes):
        for e in exes:
            if e.type not in known_exe_types:
                unmapped.append(f"executable:{e.name} ({e.type})")
            _walk(e.children)

    _walk(cir.control_flow.execution_tree)
    return unmapped


def structural_coverage(
    dtsx_counts: dict[str, int],
    cir_counts: dict[str, int],
) -> tuple[float, dict[str, dict[str, float]]]:
    """
    Weighted fraction of DTSX elements represented in the CIR.

    Returns (overall_coverage, per_category_detail) where each detail entry is
    {"dtsx": n, "cir": m, "coverage": ratio}.
    """
    detail: dict[str, dict[str, float]] = {}
    weighted_sum = 0.0
    weight_total = 0.0
    for cat, weight in _COVERAGE_WEIGHTS.items():
        dtsx_n = dtsx_counts.get(cat, 0)
        cir_n = cir_counts.get(cat, 0)
        cov = 1.0 if dtsx_n == 0 else min(1.0, cir_n / dtsx_n)
        detail[cat] = {"dtsx": dtsx_n, "cir": cir_n, "coverage": round(cov, 4)}
        # Weight each category by configured weight × its size, so empty
        # categories don't dilute the score and large ones matter more.
        w = weight * max(dtsx_n, 1 if dtsx_n == 0 else dtsx_n)
        weighted_sum += cov * w
        weight_total += w
    overall = weighted_sum / weight_total if weight_total else 1.0
    return round(overall, 4), detail


# ─── Score dataclasses ────────────────────────────────────────────────────────

@dataclass
class ParsingScore:
    """DTSX → CIR fidelity."""
    score: float
    structural_coverage: float
    llm_fidelity: float | None       # None when no LLM audit ran
    element_detail: dict = field(default_factory=dict)
    unmapped_items: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


@dataclass
class FunctionalScore:
    """CIR/DTSX → PySpark equivalence."""
    score: float
    equivalence: float
    version_ok: bool
    critical_issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    version_issues: list[str] = field(default_factory=list)


@dataclass
class MigrationScorecard:
    spark_version: str
    parsing: ParsingScore
    functional: FunctionalScore
    composite: float
    passed: bool
    threshold: float

    def to_dict(self) -> dict:
        return {
            "spark_version": self.spark_version,
            "composite": self.composite,
            "passed": self.passed,
            "threshold": self.threshold,
            "parsing": asdict(self.parsing),
            "functional": asdict(self.functional),
        }

    def save(self, path: Path | str) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    def summary(self) -> str:
        return (
            f"composite={self.composite:.2f} "
            f"(parsing={self.parsing.score:.2f} × functional={self.functional.score:.2f}) "
            f"version_ok={self.functional.version_ok} "
            f"→ {'PASS' if self.passed else 'FAIL'}"
        )


# ─── Composition ──────────────────────────────────────────────────────────────

def compute_parsing_score(
    coverage: float,
    detail: dict,
    unmapped: list[str],
    llm_fidelity: float | None = None,
    issues: list[str] | None = None,
) -> ParsingScore:
    """
    Combine deterministic structural coverage with the optional LLM fidelity
    audit.  When the LLM audit ran, the two are averaged 60/40 in favour of the
    LLM (it catches semantic drops that element counts miss); otherwise the
    score is the structural coverage alone.
    """
    if llm_fidelity is not None:
        score = 0.4 * coverage + 0.6 * llm_fidelity
    else:
        score = coverage
    return ParsingScore(
        score=round(min(1.0, max(0.0, score)), 4),
        structural_coverage=coverage,
        llm_fidelity=llm_fidelity,
        element_detail=detail,
        unmapped_items=unmapped,
        issues=issues or [],
    )


def compute_functional_score(
    equivalence: float,
    critical_issues: list[str],
    warnings: list[str],
    version_ok: bool,
    version_issues: list[str] | None = None,
) -> FunctionalScore:
    """
    The functional score is the LLM equivalence judgment, hard-gated by version
    validity: code that uses APIs absent from the target PySpark version cannot
    be "functionally equivalent" on that runtime, so a version failure caps the
    score at 0.5 regardless of the equivalence judgment.
    """
    score = equivalence
    if not version_ok:
        score = min(score, 0.5)
    return FunctionalScore(
        score=round(min(1.0, max(0.0, score)), 4),
        equivalence=round(equivalence, 4),
        version_ok=version_ok,
        critical_issues=critical_issues,
        warnings=warnings,
        version_issues=version_issues or [],
    )


def build_scorecard(
    spark_version: str,
    parsing: ParsingScore,
    functional: FunctionalScore,
    threshold: float = 0.75,
) -> MigrationScorecard:
    """
    Composite = parsing × functional.  A migration only passes when the composite
    clears the threshold AND there are no functional critical issues AND the
    PySpark version is valid.
    """
    composite = round(parsing.score * functional.score, 4)
    passed = (
        composite >= threshold
        and not functional.critical_issues
        and functional.version_ok
    )
    return MigrationScorecard(
        spark_version=spark_version,
        parsing=parsing,
        functional=functional,
        composite=composite,
        passed=passed,
        threshold=threshold,
    )
