"""
Complexity Scorer — assigns a complexity level to a parsed SSIS package.

Modelled on the Lakebridge Analyzer patterns and migration-spec-kit's
complexity scoring approach. Scores drive wave planning and LLM routing.
"""

from __future__ import annotations

from ssis_migration.cir.models import (
    ComplexityDetails,
    ComplexityLevel,
    ControlFlow,
    DataFlow,
)

# Component subtypes that are NOT deterministically convertible
_LLM_SUBTYPES = {
    "script_component", "fuzzy_lookup", "fuzzy_grouping",
    "term_extraction", "data_mining_query",
}

# Third-party component indicators (by subtype prefix after map_component_class returns "unknown")
_THIRD_PARTY_HINTS = {"kingswaysoft", "cozyroc", "task factory", "pragmaticworks", "attunity"}


class ComplexityScorer:
    """
    Produces a ComplexityDetails snapshot and a ComplexityLevel classification
    for a parsed package.

    Criteria (from the design spec):
        Simple    — only Execute SQL Tasks, Flat File sources, basic Derived
                    Columns, no Script Tasks, ≤5 data flow components
        Medium    — Lookups, Conditional Splits, parameterised queries,
                    Merge Joins, ≤15 data flow components
        High      — Script Tasks (C#), complex SSIS expressions, SCDs,
                    Fuzzy Lookups, >15 data flow components
        Very High — Custom components, COM interop, external assembly refs,
                    cross-package dependencies, dynamic SQL
    """

    def score(self, cf: ControlFlow, dfs: list[DataFlow]) -> ComplexityDetails:
        details = ComplexityDetails()
        details.total_executables = self._count_executables(cf)

        for exe in cf.execution_tree:
            if exe.type == "script_task":
                details.script_tasks += 1
                if exe.referenced_assemblies:
                    details.custom_components += len(exe.referenced_assemblies)
            if exe.type == "execute_sql" and exe.sql:
                details.sql_statements += 1
            if exe.type == "execute_package":
                details.cross_package_refs += 1

        for df in dfs:
            details.data_flow_components += len(df.components)
            for comp in df.components:
                if comp.type in ("source", "transformation"):
                    for expr in comp.expressions:
                        if expr.ssis_expression:
                            details.ssis_expressions += 1
                if comp.subtype == "unknown_component":
                    details.custom_components += 1
                    details.third_party_components.append(comp.name)
                elif comp.subtype in _LLM_SUBTYPES:
                    details.custom_components += 1

        return details

    def classify(self, d: ComplexityDetails) -> ComplexityLevel:
        # Very High: custom components, external assemblies, cross-package
        if d.custom_components > 0 or d.cross_package_refs > 0:
            return ComplexityLevel.VERY_HIGH

        # High: Script Tasks, Fuzzy Lookups, >15 data flow components
        if d.script_tasks > 0 or d.data_flow_components > 15:
            return ComplexityLevel.HIGH

        # Medium: Lookups, conditional splits (complex expressions), ≤15 components
        if d.ssis_expressions > 3 or d.data_flow_components > 5:
            return ComplexityLevel.MEDIUM

        return ComplexityLevel.SIMPLE

    def _count_executables(self, cf: ControlFlow) -> int:
        count = len(cf.execution_tree)
        for exe in cf.execution_tree:
            count += self._count_children(exe)
        return count

    def _count_children(self, exe) -> int:
        count = 0
        for child in exe.children:
            count += 1 + self._count_children(child)
        return count
