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
    dts_attr,
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
        # Type may live in DTS:ExecutableType or DTS:CreationName, as an
        # attribute (2012+) or a DTS:Property child (2005/2008).
        raw_type = dts_attr(el, "ExecutableType") or dts_attr(el, "CreationName")

        # Fall back to description for bare Sequence Containers
        if not raw_type:
            desc = dts_attr(el, "Description")
            if "sequence" in desc.lower():
                raw_type = "Microsoft.Sequence"
            elif el.find(DTS_EXECUTABLES) is not None:
                # Has children but no type — treat as sequence container
                raw_type = "Microsoft.Sequence"

        cir_type = map_executable_type(raw_type) if raw_type else "sequence"
        name = dts_attr(el, "ObjectName")
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
            # Some writers omit the namespace on SqlTaskData — match by local name.
            for child in obj_data.iter():
                if isinstance(child.tag, str) and child.tag.rsplit("}", 1)[-1] == "SqlTaskData":
                    sql_data = child
                    break
        if sql_data is None:
            return

        def _sql_attr(name: str) -> str:
            # SQLTask-namespaced, bare, or child-element storage.
            val = sql_data.get(f"{{{_SQL_TASK_NS}}}{name}") or sql_data.get(name)
            if val is not None:
                return val
            for child in sql_data:
                if isinstance(child.tag, str) and child.tag.rsplit("}", 1)[-1] == name:
                    return (child.text or "").strip()
            return ""

        sql_text = _sql_attr("SqlStatementSource")
        source_type = _sql_attr("SqlStatementSourceType") or "DirectInput"
        result_raw = _sql_attr("ResultSet") or "0"
        conn_ref = _sql_attr("Connection")

        exe.result_set = _RESULT_SET_MAP.get(result_raw, ResultSetType.NONE)
        exe.connection_ref = conn_ref or None

        if source_type.lower() in ("variable", "fileconnection"):
            # The "SQL" is actually a variable/connection NAME, resolved at
            # runtime — transpiling it as SQL text would be silently wrong.
            exe.sql = SqlStatement(
                original_text="",
                transpilation_notes=(
                    f"SQL source is a {source_type} reference ({sql_text!r}) — "
                    "statement text is resolved at runtime and cannot be "
                    "statically transpiled"
                ),
            )
            exe.conversion_notes = (
                f"Execute SQL Task reads its statement from {source_type} "
                f"{sql_text!r} — needs LLM/human to resolve the dynamic SQL"
            )
            return

        exe.sql = SqlStatement(original_text=sql_text.strip())

        # Parameter and result bindings — canonical completeness (the ?
        # placeholders in the SQL map to these).
        for child in sql_data.iter():
            if not isinstance(child.tag, str):
                continue
            local = child.tag.rsplit("}", 1)[-1]
            if local == "ParameterBinding":
                exe.parameter_mappings.append({
                    "kind": "parameter",
                    "variable": child.get(f"{{{_SQL_TASK_NS}}}DtsVariableName")
                                or child.get("DtsVariableName", ""),
                    "name": child.get(f"{{{_SQL_TASK_NS}}}ParameterName")
                            or child.get("ParameterName", ""),
                    "direction": child.get(f"{{{_SQL_TASK_NS}}}ParameterDirection")
                                 or child.get("ParameterDirection", "Input"),
                })
            elif local == "ResultBinding":
                exe.parameter_mappings.append({
                    "kind": "result",
                    "variable": child.get(f"{{{_SQL_TASK_NS}}}DtsVariableName")
                                or child.get("DtsVariableName", ""),
                    "name": child.get(f"{{{_SQL_TASK_NS}}}ResultName")
                            or child.get("ResultName", ""),
                })

    # Project files that hold source code (vs. binary VSTA project plumbing)
    _SCRIPT_SOURCE_EXTS = (".cs", ".vb")
    _SCRIPT_SKIP_EXTS = (".vsaproj", ".vsproj", ".csproj", ".vbproj", ".dll",
                         ".myapp", ".resx", ".settings", ".datasource")

    def _populate_script_task(self, exe: ControlFlowExecutable, el: etree._Element) -> None:
        obj_data = el.find(DTS_OBJECT_DATA)
        if obj_data is None:
            return

        sources: list[str] = []
        for child in obj_data.iter():
            if not isinstance(child.tag, str):
                continue
            local = etree.QName(child.tag).localname

            if local == "ScriptProject":
                # Modern (2008R2+) VSTA format: metadata on the project element.
                lang = (child.get("Language") or "").lower()
                if lang:
                    exe.script_language = "vbnet" if "basic" in lang or "vb" in lang else "csharp"
                ro = child.get("ReadOnlyVariables") or ""
                rw = child.get("ReadWriteVariables") or ""
                exe.read_only_variables = [v.strip() for v in ro.split(",") if v.strip()]
                exe.read_write_variables = [v.strip() for v in rw.split(",") if v.strip()]

            elif local == "ProjectItem":
                item_name = (child.get("Name") or child.get("SourceName") or "").lower()
                text = (child.text or "").strip()
                if not text:
                    continue
                if item_name.endswith(self._SCRIPT_SKIP_EXTS):
                    continue          # binary/VSTA plumbing, not business logic
                if item_name.endswith(self._SCRIPT_SOURCE_EXTS) or _looks_like_code(text):
                    header = f"// ─── {child.get('Name', 'ProjectItem')} ───\n" \
                        if len(sources) or item_name else ""
                    sources.append(header + text)
                    if item_name.endswith(".vb"):
                        exe.script_language = exe.script_language or "vbnet"

            elif local in ("BinaryCode", "ScriptCode", "Code"):
                # Legacy single-blob storage
                text = (child.text or "").strip()
                if text and _looks_like_code(text):
                    sources.append(text)

            elif local == "ScriptLanguage":
                lang_raw = (child.text or "").lower()
                exe.script_language = (
                    "vbnet" if "basic" in lang_raw or "vb" in lang_raw else "csharp"
                )

        if sources:
            exe.script_code = "\n\n".join(sources)
        if exe.script_code and not exe.script_language:
            exe.script_language = "csharp"

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


import re as _re

_BASE64_RE = _re.compile(r"^[A-Za-z0-9+/=\s]+$")
_CODE_HINT_RE = _re.compile(
    r"\b(public|private|void|class|namespace|using|Sub|Function|Dim|Imports|Dts)\b"
)


def _looks_like_code(text: str) -> bool:
    """Distinguish C#/VB source from base64-encoded binary project blobs."""
    sample = text[:2000]
    if _CODE_HINT_RE.search(sample):
        return True
    # A long unbroken base64-alphabet run with no code keywords is binary.
    return not (_BASE64_RE.match(sample) and len(sample) > 40)
