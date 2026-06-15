"""Extract connection managers from DTSX XML."""

from __future__ import annotations

import re

from lxml import etree

from ssis_migration.cir.models import CIRConnection, ConnectionTargetMapping
from ssis_migration.parser.ns import (
    ATTR_NAME,
    ATTR_OBJECT_NAME,
    DTS,
    DTS_CONNECTION_MANAGER,
    DTS_CONNECTION_MANAGERS,
)

ATTR_CONNECTION_STRING = f"{{{DTS}}}ConnectionString"
ATTR_RETAIN_SAME_CONNECTION = f"{{{DTS}}}RetainSameConnection"
ATTR_CREATION_NAME = f"{{{DTS}}}CreationName"

# Map SSIS provider type prefix → canonical name
_PROVIDER_MAP: dict[str, str] = {
    "OLEDB": "oledb",
    "ADO.NET": "adonet",
    "ODBC": "odbc",
    "FLATFILE": "flatfile",
    "EXCEL": "excel",
    "MULTIFILE": "multifile",
    "MULTIFLATFILE": "multiflatfile",
    "XML": "xml",
    "SMO": "smo",
    "MSOLAP": "msolap",
    "FILE": "file",
    "SMTP": "smtp",
    "FTP": "ftp",
    "HTTP": "http",
    "MSMQ": "msmq",
    "WMI": "wmi",
}

# Provider string patterns → JDBC driver
_JDBC_DRIVER_MAP: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"sqlserver|sql\s*server|mssql", re.I),
     "jdbc:sqlserver://{host}:{port};databaseName={database}",
     "com.microsoft.sqlserver.jdbc.SQLServerDriver"),
    (re.compile(r"oracle", re.I),
     "jdbc:oracle:thin:@{host}:{port}/{database}",
     "oracle.jdbc.OracleDriver"),
    (re.compile(r"mysql", re.I),
     "jdbc:mysql://{host}:{port}/{database}",
     "com.mysql.cj.jdbc.Driver"),
    (re.compile(r"postgresql|postgres", re.I),
     "jdbc:postgresql://{host}:{port}/{database}",
     "org.postgresql.Driver"),
]

# Simple extraction patterns for connection string key=value pairs
_CONN_STRING_RE = re.compile(r'(?P<key>[^;=\s]+)\s*=\s*(?P<value>[^;]+)', re.I)


def _parse_connection_string(cs: str) -> dict[str, str]:
    return {m.group("key").strip().lower(): m.group("value").strip()
            for m in _CONN_STRING_RE.finditer(cs)}


def _infer_target(provider_type: str, cs: str) -> ConnectionTargetMapping | None:
    if provider_type in ("oledb", "adonet", "odbc"):
        for pattern, url_tmpl, driver in _JDBC_DRIVER_MAP:
            if pattern.search(cs):
                return ConnectionTargetMapping(
                    type="spark_jdbc",
                    url_template=url_tmpl,
                    driver=driver,
                )
    elif provider_type == "flatfile":
        return ConnectionTargetMapping(type="spark_csv")
    elif provider_type == "excel":
        return ConnectionTargetMapping(
            type="spark_excel",
            format="com.crealytics.spark.excel",
        )
    elif provider_type == "xml":
        return ConnectionTargetMapping(
            type="spark_xml",
            format="com.databricks.spark.xml",
        )
    return None


def _slugify(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')


class ConnectionExtractor:
    def __init__(self, root: etree._Element) -> None:
        self._root = root

    def extract(self) -> list[CIRConnection]:
        results: list[CIRConnection] = []
        managers_el = self._root.find(DTS_CONNECTION_MANAGERS)
        if managers_el is None:
            return results

        for cm_el in managers_el.findall(DTS_CONNECTION_MANAGER):
            name = cm_el.get(ATTR_OBJECT_NAME) or cm_el.get(ATTR_NAME, "")
            creation_name = cm_el.get(ATTR_CREATION_NAME, "").upper()
            # ConnectionString may be on the outer element OR in a nested
        # DTS:ObjectData/DTS:ConnectionManager element (common in SSIS 2012+)
        cs = cm_el.get(ATTR_CONNECTION_STRING, "")
        if not cs:
            obj_data = cm_el.find(f"{{{DTS}}}ObjectData")
            if obj_data is not None:
                inner = obj_data.find(DTS_CONNECTION_MANAGER)
                if inner is not None:
                    cs = inner.get(ATTR_CONNECTION_STRING, "")

            provider_type = "unknown"
            for prefix, canonical in _PROVIDER_MAP.items():
                if prefix in creation_name:
                    provider_type = canonical
                    break

            resolved = _parse_connection_string(cs)
            target = _infer_target(provider_type, cs)

            results.append(
                CIRConnection(
                    id=f"conn_{_slugify(name)}",
                    name=name,
                    provider_type=provider_type,
                    connection_string_template=cs or None,
                    resolved_parameters=resolved,
                    target_mapping=target,
                )
            )
        return results
