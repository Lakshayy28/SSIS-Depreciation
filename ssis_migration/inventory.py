"""
Phase 0 — Inventory & Assessment.

Scans a directory of .dtsx files, builds the global dependency graph,
assigns complexity scores, and produces:
  - inventory_report.json
  - wave_plan.json
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ssis_migration.cir.models import ComplexityLevel
from ssis_migration.parser import DTSXParser

logger = logging.getLogger(__name__)


def build_inventory(dtsx_dir: Path) -> dict:
    """
    Scan all .dtsx files in dtsx_dir and return an inventory dict.

    Returns:
        {
            "packages": [{ "file": ..., "complexity": ..., "executables": ..., ... }],
            "dependency_graph": { "pkg_a.dtsx": ["pkg_b.dtsx", ...], ... },
            "complexity_summary": { "simple": n, "medium": n, "high": n, "very_high": n }
        }
    """
    parser = DTSXParser()
    packages = []
    dep_graph: dict[str, list[str]] = {}
    summary: dict[str, int] = {level.value: 0 for level in ComplexityLevel}

    dtsx_files = sorted(dtsx_dir.glob("**/*.dtsx"))
    logger.info("Found %d .dtsx files", len(dtsx_files))

    for dtsx_path in dtsx_files:
        try:
            cir = parser.parse(dtsx_path)
            d = cir.metadata.complexity_details
            pkg_info = {
                "file": str(dtsx_path.relative_to(dtsx_dir)),
                "complexity": cir.metadata.complexity_score.value,
                "total_executables": d.total_executables,
                "data_flow_components": d.data_flow_components,
                "script_tasks": d.script_tasks,
                "custom_components": d.custom_components,
                "ssis_expressions": d.ssis_expressions,
                "sql_statements": d.sql_statements,
                "cross_package_refs": d.cross_package_refs,
                "third_party_components": d.third_party_components,
            }
            packages.append(pkg_info)
            summary[cir.metadata.complexity_score.value] += 1

            # Build dependency edges
            refs: list[str] = []
            for exe in cir.control_flow.execution_tree:
                if exe.type == "execute_package" and exe.child_package_ref:
                    refs.append(exe.child_package_ref)
            dep_graph[dtsx_path.name] = refs

        except Exception as exc:
            logger.warning("Failed to parse %s: %s", dtsx_path.name, exc)
            packages.append({
                "file": str(dtsx_path.relative_to(dtsx_dir)),
                "complexity": "unknown",
                "error": str(exc),
            })

    return {
        "scanned_dir": str(dtsx_dir),
        "total_packages": len(dtsx_files),
        "packages": packages,
        "dependency_graph": dep_graph,
        "complexity_summary": summary,
    }


def build_wave_plan(inventory: dict) -> dict:
    """
    Determine migration wave ordering from complexity and dependency graph.

    Wave 0: Simple, no dependencies (pilot)
    Wave 1: All Simple
    Wave 2: Medium depending only on Wave 1 packages
    Wave 3: High complexity
    Wave 4: Very High + full ecosystem integration
    """
    dep_graph: dict[str, list[str]] = inventory["dependency_graph"]
    packages = inventory["packages"]

    def has_deps(pkg_file: str) -> bool:
        return bool(dep_graph.get(pkg_file, []))

    waves: dict[str, list[str]] = {"0": [], "1": [], "2": [], "3": [], "4": []}

    for pkg in packages:
        file = pkg.get("file", "")
        complexity = pkg.get("complexity", "unknown")
        if complexity == "simple" and not has_deps(Path(file).name):
            waves["0"].append(file)
        elif complexity == "simple":
            waves["1"].append(file)
        elif complexity == "medium":
            waves["2"].append(file)
        elif complexity == "high":
            waves["3"].append(file)
        else:
            waves["4"].append(file)

    return {
        "wave_0_pilot": waves["0"][:10],       # Max 10 pilot packages
        "wave_1_simple": waves["0"][10:] + waves["1"],
        "wave_2_medium": waves["2"],
        "wave_3_high": waves["3"],
        "wave_4_very_high_and_integration": waves["4"],
        "total_waves": 5,
    }


def save_inventory(inventory: dict, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "inventory_report.json").write_text(
        json.dumps(inventory, indent=2), encoding="utf-8"
    )
    wave_plan = build_wave_plan(inventory)
    (output_dir / "wave_plan.json").write_text(
        json.dumps(wave_plan, indent=2), encoding="utf-8"
    )
    logger.info("Saved inventory_report.json and wave_plan.json to %s", output_dir)
