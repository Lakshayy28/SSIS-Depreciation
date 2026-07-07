"""Extract package and container variables from DTSX XML."""

from __future__ import annotations

from lxml import etree

from ssis_migration.cir.models import CIRVariable
from ssis_migration.parser.ns import (
    ATTR_DATA_TYPE,
    ATTR_NAME,
    ATTR_NAMESPACE,
    ATTR_OBJECT_NAME,
    ATTR_VALUE,
    DTS,
    DTS_VARIABLE,
    DTS_VARIABLES,
    NAMESPACES,
)

_DTYPE_INT: dict[str, str] = {
    "2": "int16", "3": "int32", "4": "float32", "5": "float64",
    "6": "decimal", "7": "date", "8": "string", "11": "boolean",
    "14": "decimal", "16": "int8", "17": "uint8", "18": "uint16",
    "19": "uint32", "20": "uint64", "21": "int64", "72": "uuid",
}

ATTR_EVALUATE_AS_EXPRESSION = f"{{{DTS}}}EvaluateAsExpression"
ATTR_EXPRESSION_ELEM = f"{{{DTS}}}Expression"


def _resolve_dtype(raw: str) -> str:
    return _DTYPE_INT.get(raw.strip(), raw)


class VariableExtractor:
    """
    Extracts variables from EVERY scope in the package — the root and any
    container (Sequence / For Loop / Foreach Loop) that declares its own
    DTS:Variables collection. Loop iteration variables live at container
    scope, so a root-only scan silently loses them.
    """

    def __init__(self, root: etree._Element) -> None:
        self._root = root

    def extract(self, scope: str = "package") -> list[CIRVariable]:
        from ssis_migration.parser.ns import DTS_EXECUTABLE, dts_attr

        results = self._extract_from(self._root, scope)
        # Container-scoped variables: any nested executable with its own
        # DTS:Variables collection.
        for exe_el in self._root.iterfind(f".//{DTS_EXECUTABLE}"):
            container_name = dts_attr(exe_el, "ObjectName") or "container"
            results.extend(self._extract_from(exe_el, scope=container_name))
        return results

    def _extract_from(self, node: etree._Element, scope: str) -> list[CIRVariable]:
        from ssis_migration.parser.ns import dts_attr

        results: list[CIRVariable] = []
        vars_el = node.find(DTS_VARIABLES)
        if vars_el is None:
            return results

        for var_el in vars_el.findall(DTS_VARIABLE):
            name = dts_attr(var_el, "ObjectName")
            ns = dts_attr(var_el, "Namespace") or "User"
            dtype_raw = var_el.get(ATTR_DATA_TYPE, "8")
            evaluate_as_expr = dts_attr(var_el, "EvaluateAsExpression") in ("1", "-1", "True", "true")

            # Value and Expression are child elements (both format eras)
            default_val = None
            expression = None
            for child in var_el:
                if not isinstance(child.tag, str):
                    continue
                local = etree.QName(child.tag).localname
                if local == "VariableValue":
                    default_val = child.text
                    # 2005/2008 puts the data type on the value element
                    dt = child.get(ATTR_DATA_TYPE) or child.get("DataType")
                    if dt:
                        dtype_raw = dt
                elif local == "Expression":
                    expression = child.text

            full_name = f"{ns}::{name}" if ns and ns.lower() != "user" else name

            results.append(
                CIRVariable(
                    name=full_name,
                    data_type=_resolve_dtype(dtype_raw),
                    default_value=default_val,
                    scope=scope,
                    expression=expression,
                    evaluate_as_expression=evaluate_as_expr,
                )
            )
        return results
