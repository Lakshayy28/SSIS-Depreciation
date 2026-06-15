"""
DTSXParser — orchestrates all extractors to produce a CIR from a .dtsx file.

Usage:
    parser = DTSXParser()
    cir = parser.parse(Path("CustomerLoad.dtsx"))
    cir.save("CustomerLoad_cir.json")
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path

from lxml import etree

from ssis_migration.cir.models import (
    CIR,
    CIRMetadata,
    ComplexityDetails,
    ComplexityLevel,
    ControlFlow,
    ConversionMetadata,
    DataFlow,
    Lineage,
)
from ssis_migration.parser.complexity_scorer import ComplexityScorer
from ssis_migration.parser.extractors import (
    ConnectionExtractor,
    ControlFlowExtractor,
    DataFlowExtractor,
    EventHandlerExtractor,
    ParameterExtractor,
    VariableExtractor,
)
from ssis_migration.parser.ns import (
    ATTR_EXECUTABLE_TYPE,
    ATTR_OBJECT_NAME,
    DTS,
    DTS_EXECUTABLE,
    DTS_EXECUTABLES,
    DTS_OBJECT_DATA,
    NAMESPACES,
    map_executable_type,
)

logger = logging.getLogger(__name__)

_VERSION = "0.1.0"
_ATTR_NAME = f"{{{DTS}}}ObjectName"


class DTSXParser:
    """
    Parses a single .dtsx file into a Canonical Intermediate Representation.

    The parser is intentionally tolerant — missing or unexpected elements
    are logged as warnings rather than raising exceptions, so that partially
    valid packages still produce a best-effort CIR.
    """

    def parse(self, path: Path | str) -> CIR:
        path = Path(path)
        logger.info("Parsing %s", path.name)

        raw_bytes = path.read_bytes()
        source_hash = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()

        try:
            root = etree.fromstring(raw_bytes)
        except etree.XMLSyntaxError as exc:
            raise ValueError(f"Failed to parse XML in {path}: {exc}") from exc

        package_name = root.get(_ATTR_NAME, path.stem)

        # ── Extract each section ──────────────────────────────────────────
        parameters = ParameterExtractor(root).extract()
        variables = VariableExtractor(root).extract()
        connections = ConnectionExtractor(root).extract()
        event_handlers = EventHandlerExtractor(root).extract()

        cf_extractor = ControlFlowExtractor(root)
        control_flow = cf_extractor.extract()

        # ── Data Flows ────────────────────────────────────────────────────
        data_flows = self._extract_data_flows(root, control_flow)

        # ── Lineage ───────────────────────────────────────────────────────
        lineage = self._build_lineage(data_flows)

        # ── Complexity scoring ────────────────────────────────────────────
        scorer = ComplexityScorer()
        details = scorer.score(control_flow, data_flows)
        level = scorer.classify(details)

        metadata = CIRMetadata(
            source_file=path.name,
            source_hash=source_hash,
            parse_timestamp=datetime.now(timezone.utc).isoformat(),
            parser_version=_VERSION,
            complexity_score=level,
            complexity_details=details,
        )

        cir = CIR(
            metadata=metadata,
            parameters=parameters,
            variables=variables,
            connections=connections,
            control_flow=control_flow,
            data_flows=data_flows,
            event_handlers=event_handlers,
            lineage=lineage,
            conversion_metadata=ConversionMetadata(),
        )

        logger.info(
            "Parsed %s: %d executables, %d data flows, complexity=%s",
            package_name,
            len(control_flow.execution_tree),
            len(data_flows),
            level.value,
        )
        return cir

    def _extract_data_flows(
        self, root: etree._Element, control_flow: ControlFlow
    ) -> list[DataFlow]:
        data_flows: list[DataFlow] = []
        execs_el = root.find(DTS_EXECUTABLES)
        if execs_el is None:
            return data_flows

        for exe_el in execs_el.findall(DTS_EXECUTABLE):
            raw_type = exe_el.get(ATTR_EXECUTABLE_TYPE, "")
            if map_executable_type(raw_type) != "data_flow":
                continue

            name = exe_el.get(_ATTR_NAME, "")
            obj_data = exe_el.find(DTS_OBJECT_DATA)
            if obj_data is None:
                continue

            # Find the matching control_flow executable to get its generated id
            df_id = self._find_df_id(control_flow, name)

            extractor = DataFlowExtractor(obj_data, df_id, name)
            try:
                df = extractor.extract()
                data_flows.append(df)
            except Exception as exc:
                logger.warning("Failed to extract data flow '%s': %s", name, exc)

        return data_flows

    def _find_df_id(self, control_flow: ControlFlow, name: str) -> str:
        for exe in control_flow.execution_tree:
            if exe.name == name and exe.data_flow_ref:
                return exe.data_flow_ref
        # Fallback: generate a stable id from the name
        safe = "".join(c if c.isalnum() else "_" for c in name.lower())
        return f"df_{safe}"

    def _build_lineage(self, data_flows: list[DataFlow]) -> Lineage:
        sources: list[str] = []
        destinations: list[str] = []

        for df in data_flows:
            for comp in df.components:
                if comp.type == "source":
                    if comp.sql_command and comp.sql_command.original_text:
                        # Extract table names from SQL (basic heuristic)
                        sources.extend(_extract_table_refs(comp.sql_command.original_text))
                elif comp.type == "destination":
                    if comp.table_name:
                        destinations.append(comp.table_name)

        return Lineage(
            sources=list(dict.fromkeys(sources)),   # deduplicate, preserve order
            destinations=list(dict.fromkeys(destinations)),
        )


def _extract_table_refs(sql: str) -> list[str]:
    """Naive FROM/JOIN table reference extractor — sqlglot handles the real thing."""
    import re
    pattern = re.compile(
        r'\b(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*){0,2})',
        re.IGNORECASE,
    )
    return [m.group(1) for m in pattern.finditer(sql)]
