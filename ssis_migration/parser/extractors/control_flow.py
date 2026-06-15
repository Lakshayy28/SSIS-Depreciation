"""
Extract control flow executables and precedence constraints from DTSX XML.

Handles nesting: For Loop, Foreach Loop, and Sequence containers all
contain nested DTS:Executables. Script Tasks preserve raw C#/VB code.
"""

from __future__ import annotations

import textwrap
from lxml import etree

from ssis_migration.cir.models import (
    ControlFlow,
    ControlFlowExecutable,
    PrecedenceConstraint,
    PrecedenceEvaluation,
    ResultSetType,
    SqlStatement,
)
from ssis_migration.parser.ns import (
    ATTR_EVAL_OP,
    ATTR_EXECUTABLE_TYPE,
    ATTR_EXPRESSION,
    ATTR_FROM,
    ATTR_LOGICAL_AND,
    ATTR_OBJECT_NAME,
    ATTR_TO,
    DTS,
    DTS_EXECUTABLE,
    DTS_EXECUTABLES,
    DTS_OBJECT_DATA,
    DTS_PRECEDENCE_CONSTRAINT,
    DTS_PRECEDENCE_CONSTRAINTS,
    EVAL_OP_COMPLETION,
    EVAL_OP_EXPR_AND_CONSTRAINT,
    EVAL_OP_EXPR_OR_CONSTRAINT,
    EVAL_OP_EXPRESSION,
    EVAL_OP_FAILURE,
    EVAL_OP_SUCCESS,
    NAMESPACES,
    map_executable_type,
)

_SQL_TASK_NS = NAMESPACES["SQLTask"]
_SQLTASK_DATA = f"{{{_SQL_TASK_NS}}}SqlTaskData"
_SQLTASK_SQL_SOURCE = f"{{{_SQL_TASK_NS}}}SqlStatementSource"
_SQLTASK_RESULT_SET = f"{{{_SQL_TASK_NS}}}ResultSet"
_SQLTASK_CONNECTION = f"{{{_SQL_TASK_NS}}}Connection"

_ATTR_NAME = f"{{{DTS}}}ObjectName"
_ATTR_REFID = f"{{{DTS}}}refId"

# Connection ref attribute inside SQLTaskData
_ATTR_CONNECTION = f"{{{_SQL_TASK_NS}}}Connection"

_EVAL_MAP: dict[str, PrecedenceEvaluation] = {
    EVAL_OP_SUCCESS: PrecedenceEvaluation.SUCCESS,
    EVAL_OP_FAILURE: PrecedenceEvaluation.FAILURE,
    EVAL_OP_COMPLETION: PrecedenceEvaluation.COMPLETION,
    EVAL_OP_EXPRESSION: PrecedenceEvaluation.EXPRESSION,
    EVAL_OP_EXPR_AND_CONSTRAINT: PrecedenceEvaluation.EXPRESSION,
    EVAL_OP_EXPR_OR_CONSTRAINT: PrecedenceEvaluation.EXPRESSION,
}

_RESULT_SET_MAP: dict[str, ResultSetType] = {
    "0": ResultSetType.NONE,
    "1": ResultSetType.SINGLE_ROW,
    "2": ResultSetType.FULL,
    "3": ResultSetType.XML,
}

_id_counter: dict[str, int] = {}


def _gen_id(prefix: str) -> str:
    _id_counter[prefix] = _id_counter.get(prefix, 0) + 1
    return f"{prefix}_{_id_counter[prefix]:04d}"


def _reset_counters() -> None:
    _id_counter.clear()


class ControlFlowExtractor:
    def __init__(self, root: etree._Element) -> None:
        self._root = root
        _reset_counters()

    def extract(self) -> ControlFlow:
        executables = self._extract_executables(self._root)
        constraints = self._extract_constraints(self._root)
        return ControlFlow(
            execution_tree=executables,
            precedence_constraints=constraints,
        )

    # ── Executables ────────────────────────────────────────────────────────

    def _extract_executables(self, node: etree._Element) -> list[ControlFlowExecutable]:
        results = []
        execs_el = node.find(DTS_EXECUTABLES)
        if execs_el is None:
            return results
        for el in execs_el.findall(DTS_EXECUTABLE):
            exe = self._parse_executable(el)
            if exe:
                results.append(exe)
        return results

    def _parse_executable(self, el: etree._Element) -> ControlFlowExecutable | None:
        # SSIS packages use either DTS:ExecutableType or DTS:CreationName
        raw_type = el.get(ATTR_EXECUTABLE_TYPE, "") or el.get(f"{{{DTS}}}CreationName", "")

        # Fall back to description for bare Sequence Containers
        if not raw_type:
            desc = el.get(f"{{{DTS}}}Description", "")
            if "sequence" in desc.lower():
                raw_type = "Microsoft.Sequence"
            elif el.find(DTS_EXECUTABLES) is not None:
                # Has children but no type — treat as sequence container
                raw_type = "Microsoft.Sequence"

        cir_type = map_executable_type(raw_type) if raw_type else "sequence"
        name = el.get(_ATTR_NAME, el.get(f"{{{DTS}}}ObjectName", ""))
        exec_id = _gen_id(cir_type[:4])

        exe = ControlFlowExecutable(id=exec_id, name=name, type=cir_type)

        if cir_type == "execute_sql":
            self._populate_sql_task(exe, el)
        elif cir_type == "data_flow":
            exe.data_flow_ref = f"df_{exec_id}"
        elif cir_type == "script_task":
            self._populate_script_task(exe, el)
        elif cir_type in ("for_loop", "foreach_loop", "sequence"):
            self._populate_container(exe, el)
        elif cir_type == "execute_package":
            self._populate_exec_package(exe, el)
        elif cir_type == "expression_task":
            self._populate_expression_task(exe, el)

        return exe

    def _populate_sql_task(self, exe: ControlFlowExecutable, el: etree._Element) -> None:
        obj_data = el.find(DTS_OBJECT_DATA)
        if obj_data is None:
            return
        sql_data = obj_data.find(_SQLTASK_DATA)
        if sql_data is None:
            return

        sql_text = sql_data.get(_SQLTASK_SQL_SOURCE, "")
        result_raw = sql_data.get(_SQLTASK_RESULT_SET, "0")
        conn_ref = sql_data.get(_ATTR_CONNECTION, "")

        exe.sql = SqlStatement(original_text=sql_text.strip())
        exe.result_set = _RESULT_SET_MAP.get(result_raw, ResultSetType.NONE)
        exe.connection_ref = conn_ref or None

    def _populate_script_task(self, exe: ControlFlowExecutable, el: etree._Element) -> None:
        obj_data = el.find(DTS_OBJECT_DATA)
        if obj_data is None:
            return
        # Script Task stores code inside a ProjectItem or BinaryCode element
        for child in obj_data.iter():
            local = etree.QName(child.tag).localname
            if local in ("BinaryCode", "ScriptCode", "Code"):
                exe.script_code = (child.text or "").strip() or None
            elif local == "ScriptLanguage":
                lang_raw = (child.text or "").lower()
                exe.script_language = "csharp" if "csharp" in lang_raw or "cs" in lang_raw else "vbnet"

    def _populate_container(self, exe: ControlFlowExecutable, el: etree._Element) -> None:
        exe.children = self._extract_executables(el)
        # For/Foreach loop expressions
        for prop in el.findall(f"{{{DTS}}}Property"):
            prop_name = prop.get(f"{{{DTS}}}Name", "")
            if prop_name == "InitExpression":
                exe.loop_init_expression = prop.text
            elif prop_name == "EvalExpression":
                exe.loop_eval_expression = prop.text
            elif prop_name == "AssignExpression":
                exe.loop_assign_expression = prop.text

    def _populate_exec_package(self, exe: ControlFlowExecutable, el: etree._Element) -> None:
        obj_data = el.find(DTS_OBJECT_DATA)
        if obj_data is None:
            return
        for child in obj_data.iter():
            local = etree.QName(child.tag).localname
            if local == "PackageName":
                exe.child_package_ref = child.text

    def _populate_expression_task(self, exe: ControlFlowExecutable, el: etree._Element) -> None:
        obj_data = el.find(DTS_OBJECT_DATA)
        if obj_data is None:
            return
        for child in obj_data.iter():
            local = etree.QName(child.tag).localname
            if local == "Expression":
                exe.expression = child.text

    # ── Precedence Constraints ─────────────────────────────────────────────

    def _extract_constraints(self, node: etree._Element) -> list[PrecedenceConstraint]:
        results = []
        pcs_el = node.find(DTS_PRECEDENCE_CONSTRAINTS)
        if pcs_el is None:
            return results
        for pc_el in pcs_el.findall(DTS_PRECEDENCE_CONSTRAINT):
            from_ref = pc_el.get(ATTR_FROM, "")
            to_ref = pc_el.get(ATTR_TO, "")
            eval_op = pc_el.get(ATTR_EVAL_OP, EVAL_OP_SUCCESS)
            expression = pc_el.get(ATTR_EXPRESSION) or None
            logical_and = pc_el.get(ATTR_LOGICAL_AND, "1") == "1"

            evaluation = _EVAL_MAP.get(eval_op, PrecedenceEvaluation.SUCCESS)
            results.append(
                PrecedenceConstraint(**{
                    "from": _simplify_ref(from_ref),
                    "to": _simplify_ref(to_ref),
                    "evaluation": evaluation,
                    "expression": expression,
                    "logical_and": logical_and,
                })
            )
        return results


def _simplify_ref(ref: str) -> str:
    """Trim full refId path to just the task name portion."""
    return ref.split("\\")[-1] if ref else ref
