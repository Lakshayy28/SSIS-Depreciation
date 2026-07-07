"""
AUTO-mode routing intelligence.

The deterministic engine already decides "I can transpile this" vs "I can't"
(LLM_REQUIRED).  AUTO mode adds a transparent, risk-aware layer on top that runs
*after* the deterministic pass and can **escalate** items the engine thought it
handled but whose faithfulness is in doubt.

Routing philosophy
──────────────────
  DETERMINISTIC  — safe, mechanical, and provably faithful (simple SELECT/INSERT,
                   straightforward expressions, structural containers, templated
                   operational tasks).
  LLM            — anything where a mechanical transpile risks changing behaviour
                   (procedural T-SQL, MERGE, dynamic SQL, .NET script code) or
                   where the deterministic engine gave up.
  HUMAN_REVIEW   — inherently non-automatable (cross-package execution, unknown
                   third-party components, an empty/garbled body).

The router never *downgrades* an LLM_REQUIRED item to DETERMINISTIC — it trusts
the engine's "I can't" signal — it only confirms or escalates.  Every decision
carries a human-readable reason and the risk signals that drove it, so AUTO runs
are fully auditable (see RoutingPlan.to_report()).
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from enum import Enum

from ssis_migration.cir.models import (
    CIR,
    ConversionStatus,
    TranspilationStatus,
)

logger = logging.getLogger(__name__)


class RoutingTarget(str, Enum):
    DETERMINISTIC = "deterministic"
    LLM = "llm"
    HUMAN_REVIEW = "human_review"


@dataclass
class RoutingDecision:
    item_id: str
    item_kind: str          # executable type or component subtype
    target: RoutingTarget
    reason: str
    risk_signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["target"] = self.target.value
        return d


@dataclass
class RoutingPlan:
    decisions: list[RoutingDecision] = field(default_factory=list)

    def add(self, decision: RoutingDecision) -> None:
        self.decisions.append(decision)
        logger.debug(
            "ROUTE %-13s %-16s %s",
            decision.target.value, decision.item_kind, decision.reason,
        )

    def counts(self) -> dict[str, int]:
        out = {t.value: 0 for t in RoutingTarget}
        for d in self.decisions:
            out[d.target.value] += 1
        return out

    def to_report(self) -> dict:
        return {"counts": self.counts(), "decisions": [d.to_dict() for d in self.decisions]}


# ─── Risk-signal detectors ────────────────────────────────────────────────────

# T-SQL constructs whose semantics a mechanical transpile is likely to distort.
_SQL_RISK = {
    "stored_proc_exec": re.compile(r'\bEXEC(?:UTE)?\b\s+[\[\w]', re.I),
    "dynamic_sql": re.compile(r'\bEXEC(?:UTE)?\s*\(|sp_executesql', re.I),
    "cursor": re.compile(r'\bCURSOR\b|\bFETCH\b', re.I),
    "while_loop": re.compile(r'\bWHILE\b', re.I),
    "local_variables": re.compile(r'\bDECLARE\s+@', re.I),
    "merge": re.compile(r'\bMERGE\b', re.I),
    "try_catch": re.compile(r'\bBEGIN\s+TRY\b', re.I),
    "transaction": re.compile(r'\bBEGIN\s+TRAN', re.I),
    "temp_table": re.compile(r'#\w+'),
    "pivot": re.compile(r'\bPIVOT\b|\bUNPIVOT\b', re.I),
}

# .NET constructs in Script Tasks that need careful LLM translation.
_SCRIPT_RISK = {
    "com_interop": re.compile(r'\bComObject\b|\bMarshal\b|Interop', re.I),
    "threading": re.compile(r'\bThread\b|\bTask\.Run\b|async\b', re.I),
    "reflection": re.compile(r'\bReflection\b|GetType\(\)|Activator', re.I),
    "external_io": re.compile(r'\bHttpClient\b|WebRequest|\bSocket\b|SmtpClient', re.I),
    "db_access": re.compile(r'SqlConnection|SqlCommand|OleDb', re.I),
}


def sql_risk_signals(sql: str) -> list[str]:
    if not sql:
        return []
    return [name for name, pat in _SQL_RISK.items() if pat.search(sql)]


def script_risk_signals(code: str) -> list[str]:
    if not code:
        return []
    signals = [name for name, pat in _SCRIPT_RISK.items() if pat.search(code)]
    if len(code.splitlines()) > 40:
        signals.append("long_script")
    return signals


# ─── Router ───────────────────────────────────────────────────────────────────

_STRUCTURAL = ("sequence", "for_loop", "foreach_loop")
_OPERATIONAL = ("file_system", "ftp", "send_mail", "execute_process")


class Router:
    """
    Produces and applies routing decisions for AUTO mode.

    Call ``plan(cir)`` *after* the deterministic engine has run.  It mutates the
    CIR's conversion statuses to reflect the decisions and returns a RoutingPlan
    describing every choice for auditing.
    """

    def plan(self, cir: CIR) -> RoutingPlan:
        plan = RoutingPlan()
        for exe in cir.control_flow.execution_tree:
            self._route_executable(exe, cir, plan)
        for df in cir.data_flows:
            for comp in df.components:
                self._route_component(comp, cir, plan)
        logger.info("AUTO routing: %s", plan.counts())
        return plan

    # -- executables ------------------------------------------------------------

    def _route_executable(self, exe, cir: CIR, plan: RoutingPlan) -> None:
        decision = self._decide_executable(exe)
        self._apply_executable(exe, cir, decision)
        plan.add(decision)
        for child in exe.children:
            self._route_executable(child, cir, plan)

    def _decide_executable(self, exe) -> RoutingDecision:
        kind = exe.type

        if kind == "data_flow":
            return RoutingDecision(exe.id, kind, RoutingTarget.DETERMINISTIC,
                                   "Orchestration only; components routed individually")

        if kind in _STRUCTURAL:
            return RoutingDecision(exe.id, kind, RoutingTarget.DETERMINISTIC,
                                   "Structural container — no code body to convert")

        if kind in _OPERATIONAL:
            return RoutingDecision(exe.id, kind, RoutingTarget.DETERMINISTIC,
                                   "Operational task maps to a deterministic template")

        if kind == "script_task":
            if not exe.script_code:
                return RoutingDecision(exe.id, kind, RoutingTarget.HUMAN_REVIEW,
                                       "Script task has no script body")
            signals = script_risk_signals(exe.script_code)
            return RoutingDecision(exe.id, kind, RoutingTarget.LLM,
                                   "Arbitrary .NET script — requires LLM translation", signals)

        if kind == "execute_package":
            return RoutingDecision(exe.id, kind, RoutingTarget.HUMAN_REVIEW,
                                   "Cross-package execution — migrate child package separately")

        if exe.sql is not None:
            return self._decide_sql(exe.id, kind, exe.sql)

        if kind == "expression_task" and exe.expression:
            return RoutingDecision(exe.id, kind, RoutingTarget.LLM,
                                   "Expression task — translate via LLM for fidelity")

        # Unknown executable type with no recognisable body.
        return RoutingDecision(exe.id, kind, RoutingTarget.HUMAN_REVIEW,
                               f"Unrecognised executable type '{kind}'")

    def _decide_sql(self, item_id: str, kind: str, sql) -> RoutingDecision:
        text = (sql.original_text or "").strip()
        if not text:
            if sql.transpilation_notes:
                # Statement text is resolved at runtime (Variable / file
                # connection source) — nothing static to convert.
                return RoutingDecision(item_id, kind, RoutingTarget.HUMAN_REVIEW,
                                       sql.transpilation_notes)
            return RoutingDecision(item_id, kind, RoutingTarget.DETERMINISTIC,
                                   "Empty SQL statement")

        signals = sql_risk_signals(text)
        if signals:
            return RoutingDecision(item_id, kind, RoutingTarget.LLM,
                                   "Procedural / high-risk T-SQL — mechanical transpile "
                                   "would risk changing behaviour", signals)

        if sql.transpilation_status == TranspilationStatus.LLM_REQUIRED:
            return RoutingDecision(item_id, kind, RoutingTarget.LLM,
                                   "Deterministic transpiler could not produce clean output")

        if sql.transpilation_status == TranspilationStatus.COMPLETE and sql.transpilation_notes \
                and "Partial" in sql.transpilation_notes:
            return RoutingDecision(item_id, kind, RoutingTarget.LLM,
                                   "Only a partial sqlglot transpile was possible")

        return RoutingDecision(item_id, kind, RoutingTarget.DETERMINISTIC,
                               "Set-based SQL transpiled cleanly by sqlglot")

    def _apply_executable(self, exe, cir: CIR, decision: RoutingDecision) -> None:
        if decision.target == RoutingTarget.LLM:
            exe.conversion_status = ConversionStatus.LLM_REQUIRED
            exe.pyspark_snippet = None
            cir.flag_for_llm(exe.id)
            if exe.sql is not None and exe.sql.transpilation_status != TranspilationStatus.LLM_REQUIRED:
                # Keep any partial transpile as a hint for the LLM, but mark for LLM.
                exe.sql.transpilation_status = TranspilationStatus.LLM_REQUIRED
        elif decision.target == RoutingTarget.HUMAN_REVIEW:
            exe.conversion_status = ConversionStatus.HUMAN_REVIEW
            cir.flag_for_human_review(exe.id)
        else:
            exe.conversion_status = ConversionStatus.DETERMINISTIC

    # -- data flow components ---------------------------------------------------

    def _route_component(self, comp, cir: CIR, plan: RoutingPlan) -> None:
        decision = self._decide_component(comp)
        self._apply_component(comp, cir, decision)
        plan.add(decision)

    def _decide_component(self, comp) -> RoutingDecision:
        kind = comp.subtype or comp.type

        if comp.subtype == "script_component":
            if not comp.script_code:
                return RoutingDecision(comp.id, kind, RoutingTarget.HUMAN_REVIEW,
                                       "Script component has no script body")
            signals = script_risk_signals(comp.script_code)
            return RoutingDecision(comp.id, kind, RoutingTarget.LLM,
                                   "Script component — requires LLM translation", signals)

        # SQL-bearing source (e.g. OLE DB source with a SQL command)
        if comp.sql_command is not None:
            sql_decision = self._decide_sql(comp.id, kind, comp.sql_command)
            if sql_decision.target == RoutingTarget.LLM:
                return sql_decision

        # Expression-bearing components (derived column, etc.)
        if comp.expressions:
            unresolved = [
                e for e in comp.expressions
                if e.translation_status == TranspilationStatus.LLM_REQUIRED
            ]
            if unresolved:
                return RoutingDecision(
                    comp.id, kind, RoutingTarget.LLM,
                    f"{len(unresolved)}/{len(comp.expressions)} expression(s) exceeded "
                    "the deterministic map",
                )
            return RoutingDecision(comp.id, kind, RoutingTarget.DETERMINISTIC,
                                   "All expressions translated deterministically")

        # Known structural data-flow components handled by the component mapper.
        if comp.conversion_status == ConversionStatus.LLM_REQUIRED:
            return RoutingDecision(comp.id, kind, RoutingTarget.LLM,
                                   "Component mapper flagged this for LLM")

        return RoutingDecision(comp.id, kind, RoutingTarget.DETERMINISTIC,
                               "Standard data-flow component mapped deterministically")

    def _apply_component(self, comp, cir: CIR, decision: RoutingDecision) -> None:
        if decision.target == RoutingTarget.LLM:
            comp.conversion_status = ConversionStatus.LLM_REQUIRED
            comp.pyspark_snippet = None
            cir.flag_for_llm(comp.id)
        elif decision.target == RoutingTarget.HUMAN_REVIEW:
            comp.conversion_status = ConversionStatus.HUMAN_REVIEW
            cir.flag_for_human_review(comp.id)
        else:
            comp.conversion_status = ConversionStatus.DETERMINISTIC
