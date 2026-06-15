"""Extract package parameters from DTSX XML."""

from __future__ import annotations

from lxml import etree

from ssis_migration.cir.models import CIRParameter
from ssis_migration.parser.ns import (
    ATTR_DATA_TYPE,
    ATTR_NAME,
    ATTR_SENSITIVE,
    DTS_PARAMETER,
    DTS_PARAMETERS,
    NAMESPACES,
)

# SSIS package parameter data-type integer codes → canonical names
_DTYPE_INT: dict[str, str] = {
    "2": "int16", "3": "int32", "4": "float32", "5": "float64",
    "6": "decimal", "7": "date", "8": "string", "11": "boolean",
    "14": "decimal", "16": "int8", "17": "uint8", "18": "uint16",
    "19": "uint32", "20": "uint64", "21": "int64", "72": "uuid",
}


def _resolve_dtype(raw: str) -> str:
    return _DTYPE_INT.get(raw.strip(), raw)


class ParameterExtractor:
    def __init__(self, root: etree._Element) -> None:
        self._root = root

    def extract(self) -> list[CIRParameter]:
        params: list[CIRParameter] = []
        container = self._root.find(DTS_PARAMETERS)
        if container is None:
            return params

        for el in container.findall(DTS_PARAMETER):
            name = el.get(ATTR_NAME, "")
            dtype_raw = el.get(ATTR_DATA_TYPE, "8")
            sensitive = el.get(ATTR_SENSITIVE, "0") == "1"

            # Default value lives in a nested DTS:Property name="ParameterValue"
            default_val = None
            for prop in el:
                prop_name = prop.get(f"{{{NAMESPACES['DTS']}}}Name", "")
                if prop_name == "ParameterValue":
                    default_val = prop.text

            params.append(
                CIRParameter(
                    name=name,
                    data_type=_resolve_dtype(dtype_raw),
                    default_value=default_val,
                    scope="package",
                    sensitive=sensitive,
                )
            )
        return params
