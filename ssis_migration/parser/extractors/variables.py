"""Extract package and container variables from DTSX XML."""

from __future__ import annotations

from lxml import etree

from ssis_migration.cir.models import CIRVariable
from ssis_migration.parser.ns import (
    ATTR_DATA_TYPE,
    ATTR_NAME,
    ATTR_NAMESPACE,
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
    Recursively extracts variables from the root package scope.
    Container-scoped variables are captured by the ControlFlowExtractor
    alongside their parent executable.
    """

    def __init__(self, root: etree._Element) -> None:
        self._root = root

    def extract(self, scope: str = "package") -> list[CIRVariable]:
        return self._extract_from(self._root, scope)

    def _extract_from(self, node: etree._Element, scope: str) -> list[CIRVariable]:
        results: list[CIRVariable] = []
        vars_el = node.find(DTS_VARIABLES)
        if vars_el is None:
            return results

        for var_el in vars_el.findall(DTS_VARIABLE):
            name = var_el.get(ATTR_NAME, "")
            ns = var_el.get(ATTR_NAMESPACE, "User")
            dtype_raw = var_el.get(ATTR_DATA_TYPE, "8")
            evaluate_as_expr = var_el.get(ATTR_EVALUATE_AS_EXPRESSION, "0") == "1"

            # Value and Expression are child elements
            default_val = None
            expression = None
            for child in var_el:
                local = etree.QName(child.tag).localname
                if local == "VariableValue":
                    default_val = child.text
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
