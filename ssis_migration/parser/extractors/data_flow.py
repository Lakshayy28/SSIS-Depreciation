"""
Extract Data Flow Tasks from DTSX XML.

Each Data Flow Task contains a pipeline component graph. Components are
identified by their componentClassID (a GUID in older packages, a logical name
like "Microsoft.OLEDBSource" in modern ones) and their properties vary by
component type. This extractor produces DataFlow CIR objects.

Namespace handling — IMPORTANT
──────────────────────────────
The pipeline XML embedded under DTS:ObjectData is written in TWO different
styles in the wild:

  - namespaced:   <pipeline xmlns="www.microsoft.com/SqlServer/Dts/Pipeline">
  - unprefixed:   <pipeline> … <components> … (no namespace at all)

Property collections likewise appear as either properties/property (modern) or
customPropertyCollection/customProperty (legacy). Every lookup in this module
therefore matches elements by LOCAL NAME, accepting any namespace, so the
canonical stage captures the package regardless of which dialect produced it.
"""

from __future__ import annotations

from collections.abc import Iterator

from lxml import etree

from ssis_migration.cir.models import (
    CacheMode,
    ColumnMapping,
    DataFlow,
    DataFlowComponent,
    DataFlowPath,
    ExpressionNode,
    JoinColumn,
    JoinType,
    NoMatchBehavior,
    OutputColumn,
    SqlStatement,
    TranspilationStatus,
)
from ssis_migration.cir.type_mapping import normalize_ssis_type, resolve_type
from ssis_migration.parser.ns import map_component_class

# Custom property names we care about
_PROP_SQL_COMMAND = "SqlCommand"
_PROP_SQL_COMMAND_VARIABLE = "SqlCommandVariable"
_PROP_OPEN_ROWSET = "OpenRowset"
_PROP_ACCESS_MODE = "AccessMode"
_PROP_TABLE_OR_VIEW_NAME = "TableOrViewName"
_PROP_FRIENDLY_EXPRESSION = "FriendlyExpression"
_PROP_EXPRESSION = "Expression"
_PROP_CACHE_TYPE = "CacheType"
_PROP_NO_MATCH_BEHAVIOR = "NoMatchBehavior"
_PROP_JOIN_TYPE = "JoinType"
_PROP_LOOKUP_SQL = "SqlCommand"

_CACHE_TYPE_MAP: dict[str, CacheMode] = {
    "0": CacheMode.FULL, "1": CacheMode.PARTIAL, "2": CacheMode.NONE,
}
_NO_MATCH_MAP: dict[str, NoMatchBehavior] = {
    "0": NoMatchBehavior.FAIL,
    "1": NoMatchBehavior.REDIRECT_TO_NO_MATCH,
    "2": NoMatchBehavior.REDIRECT_TO_ERROR,
    "3": NoMatchBehavior.IGNORE_FAILURE,
}
_JOIN_TYPE_MAP: dict[str, JoinType] = {
    "1": JoinType.LEFT_OUTER,
    "2": JoinType.INNER,
    "3": JoinType.RIGHT_OUTER,
    "4": JoinType.FULL_OUTER,
}
_ACCESS_MODE_MAP: dict[str, str] = {
    "0": "table", "1": "table_variable", "2": "sql_command", "3": "sql_command_variable",
}


# ─── Namespace-agnostic element helpers ───────────────────────────────────────

def _localname(el: etree._Element) -> str:
    tag = el.tag
    if not isinstance(tag, str):        # comments / processing instructions
        return ""
    return tag.rsplit("}", 1)[-1]


def _children(el: etree._Element, *names: str) -> Iterator[etree._Element]:
    """Direct children whose local name matches any of ``names``."""
    for child in el:
        if _localname(child) in names:
            yield child


def _first(el: etree._Element, *names: str) -> etree._Element | None:
    for child in _children(el, *names):
        return child
    return None


def _descendants(el: etree._Element, *names: str) -> Iterator[etree._Element]:
    for node in el.iter():
        if node is not el and _localname(node) in names:
            yield node


def _get_props(el: etree._Element) -> dict[str, str]:
    """Read the property collection (modern OR legacy element names)."""
    props: dict[str, str] = {}
    coll = _first(el, "properties", "customPropertyCollection")
    if coll is None:
        return props
    for prop in _children(coll, "property", "customProperty"):
        name = prop.get("name", "")
        props[name] = (prop.text or "").strip()
    return props


def _int(val: str | None) -> int | None:
    try:
        return int(val) if val is not None else None
    except (ValueError, TypeError):
        return None


class DataFlowExtractor:
    """
    Extracts a DataFlow CIR object from the DTS:ObjectData element of a
    Data Flow Task executable.
    """

    def __init__(self, obj_data_el: etree._Element, df_id: str, df_name: str) -> None:
        self._el = obj_data_el
        self._df_id = df_id
        self._df_name = df_name
        self._comp_seq = 0

    def extract(self) -> DataFlow:
        pipeline_el = _first(self._el, "pipeline")
        if pipeline_el is None:
            # Some packages nest the pipeline one level deeper
            for node in _descendants(self._el, "pipeline"):
                pipeline_el = node
                break

        components: list[DataFlowComponent] = []
        paths: list[DataFlowPath] = []

        if pipeline_el is not None:
            comps_el = _first(pipeline_el, "components")
            if comps_el is not None:
                for comp_el in _children(comps_el, "component"):
                    comp = self._parse_component(comp_el)
                    if comp:
                        components.append(comp)

            paths_el = _first(pipeline_el, "paths")
            if paths_el is not None:
                for path_el in _children(paths_el, "path"):
                    paths.append(self._parse_path(path_el))

        return DataFlow(id=self._df_id, name=self._df_name, components=components, paths=paths)

    # ── components ────────────────────────────────────────────────────────────

    def _comp_id(self, subtype: str) -> str:
        """Component ids are prefixed with the data-flow id so they stay unique
        across multiple data flows in the same package (flag_for_llm keys on them)."""
        self._comp_seq += 1
        return f"{self._df_id}_c{self._comp_seq:02d}_{subtype[:16]}"

    def _parse_component(self, el: etree._Element) -> DataFlowComponent | None:
        class_id = el.get("componentClassID", "")
        subtype = map_component_class(class_id)
        name = el.get("name", "")
        comp_id = self._comp_id(subtype)

        props = _get_props(el)
        output_cols = self._get_output_columns(el)
        conn_ref = self._get_connection_ref(el)

        comp = DataFlowComponent(id=comp_id, name=name, type="transformation",
                                 subtype=subtype, connection_ref=conn_ref,
                                 output_columns=output_cols)
        # Canonical completeness: keep EVERY component property in the CIR so
        # nothing from the DTSX is silently dropped (parameter mappings, error
        # dispositions, vendor settings, …). Specialised fields below are
        # projections of this raw capture.
        if props:
            comp.extra_properties = dict(props)

        if subtype in ("oledb_source", "ado_net_source"):
            comp.type = "source"
            access_raw = props.get(_PROP_ACCESS_MODE, "0")
            comp.access_mode = _ACCESS_MODE_MAP.get(access_raw, "table")
            sql_text = props.get(_PROP_SQL_COMMAND, "") or props.get(_PROP_OPEN_ROWSET, "")
            if sql_text:
                comp.sql_command = SqlStatement(original_text=sql_text.strip())

        elif subtype == "flat_file_source":
            comp.type = "source"
            comp.access_mode = "flat_file"

        elif subtype in ("oledb_destination", "ado_net_destination"):
            comp.type = "destination"
            comp.table_name = (
                props.get(_PROP_TABLE_OR_VIEW_NAME) or props.get(_PROP_OPEN_ROWSET)
            )
            comp.column_mappings = self._extract_column_mappings(el)

        elif subtype == "flat_file_destination":
            comp.type = "destination"
            comp.column_mappings = self._extract_column_mappings(el)

        elif subtype == "derived_column":
            comp.type = "transformation"
            comp.expressions = self._extract_output_column_expressions(el)

        elif subtype == "conditional_split":
            comp.type = "transformation"
            comp.expressions = self._extract_conditional_split_expressions(el)

        elif subtype == "lookup":
            comp.type = "transformation"
            comp.cache_mode = _CACHE_TYPE_MAP.get(props.get(_PROP_CACHE_TYPE, "0"), CacheMode.FULL)
            comp.no_match_behavior = _NO_MATCH_MAP.get(
                props.get(_PROP_NO_MATCH_BEHAVIOR, "0"), NoMatchBehavior.FAIL
            )
            sql_text = props.get(_PROP_LOOKUP_SQL, "")
            if sql_text:
                comp.lookup_sql = sql_text.strip()
            comp.join_columns = self._extract_lookup_join_columns(el)

        elif subtype == "merge_join":
            comp.type = "transformation"
            comp.join_type = _JOIN_TYPE_MAP.get(props.get(_PROP_JOIN_TYPE, "2"), JoinType.INNER)

        elif subtype == "aggregate":
            comp.type = "transformation"
            comp.aggregations = self._extract_aggregations(el)

        elif subtype in ("script_component",):
            comp.type = "transformation"
            # Code may be binary-encoded; flag for LLM
            comp.script_language = "csharp"

        elif subtype in ("sort", "union_all", "row_count", "copy_column",
                         "character_map", "data_conversion", "multicast",
                         "pivot", "unpivot", "oledb_command"):
            comp.type = "transformation"
            if subtype == "data_conversion":
                comp.expressions = self._extract_output_column_expressions(el)

        else:
            comp.type = "transformation"

        return comp

    # ── element readers (all namespace-agnostic) ──────────────────────────────

    def _get_output_columns(self, component: etree._Element) -> list[OutputColumn]:
        cols: list[OutputColumn] = []
        outputs_el = _first(component, "outputs")
        if outputs_el is None:
            return cols
        for out_el in _children(outputs_el, "output"):
            is_error = out_el.get("isErrorOut", "false").lower() == "true"
            if is_error or "error" in out_el.get("name", "").lower():
                continue
            cols_el = _first(out_el, "outputColumns")
            if cols_el is None:
                continue
            for col_el in _children(cols_el, "outputColumn"):
                ssis_type = normalize_ssis_type(col_el.get("dataType", "DT_WSTR"))
                precision = _int(col_el.get("precision"))
                scale = _int(col_el.get("scale"))
                cir_type, pyspark_type, _div = resolve_type(ssis_type, precision, scale)
                cols.append(OutputColumn(
                    name=col_el.get("name", ""),
                    ssis_type=ssis_type,
                    mapped_type=cir_type,
                    pyspark_type=pyspark_type,
                    nullable=col_el.get("nullable", "true").lower() == "true",
                    length=_int(col_el.get("length")),
                    precision=precision,
                    scale=scale,
                ))
            break  # only the primary output
        return cols

    def _get_connection_ref(self, component: etree._Element) -> str | None:
        conns_el = _first(component, "connections")
        if conns_el is not None:
            conn_el = _first(conns_el, "connection")
            if conn_el is not None:
                return (
                    conn_el.get("connectionManagerRefId")
                    or conn_el.get("connectionManagerID")
                    or conn_el.get("name")
                )
        # Some writers put the ref directly on the component element
        return component.get("connectionManagerID") or component.get("connectionManagerRefId")

    def _parse_path(self, el: etree._Element) -> DataFlowPath:
        src = el.get("startId", "")
        dst = el.get("endId", "")
        name = el.get("name", "")
        haystack = f"{name} {src}".lower()
        path_type = "default"
        if "error" in haystack:
            path_type = "error"
        elif "nomatch" in haystack or "no match" in haystack:
            path_type = "no_match"
        return DataFlowPath(**{"from": src, "to": dst, "type": path_type})

    def _extract_column_mappings(self, comp_el: etree._Element) -> list[ColumnMapping]:
        mappings: list[ColumnMapping] = []
        inputs_el = _first(comp_el, "inputs")
        if inputs_el is None:
            return mappings
        for inp_el in _children(inputs_el, "input"):
            cols_el = _first(inp_el, "inputColumns")
            if cols_el is None:
                continue
            ext_meta_el = _first(inp_el, "externalMetadataColumnCollection", "externalMetadataColumns")
            ext_map: dict[str, str] = {}
            if ext_meta_el is not None:
                for ext_col in _children(ext_meta_el, "externalMetadataColumn"):
                    ext_map[ext_col.get("id", "") or ext_col.get("refId", "")] = ext_col.get("name", "")

            for col_el in _children(cols_el, "inputColumn"):
                src_name = col_el.get("name", "") or col_el.get("cachedName", "")
                dst_key = (
                    col_el.get("externalMetadataColumnId", "")
                    or col_el.get("externalMetadataColumnName", "")
                )
                dst_name = ext_map.get(dst_key, dst_key or src_name)
                mappings.append(ColumnMapping(source=src_name, destination=dst_name or src_name))
        return mappings

    def _extract_output_column_expressions(self, comp_el: etree._Element) -> list[ExpressionNode]:
        """Derived Column / Data Conversion: one expression per output column."""
        exprs: list[ExpressionNode] = []
        outputs_el = _first(comp_el, "outputs")
        if outputs_el is None:
            return exprs
        for out_el in _children(outputs_el, "output"):
            cols_el = _first(out_el, "outputColumns")
            if cols_el is None:
                continue
            for col_el in _children(cols_el, "outputColumn"):
                col_name = col_el.get("name", "")
                props = _get_props(col_el)
                expr_text = props.get(_PROP_FRIENDLY_EXPRESSION, "") or props.get(_PROP_EXPRESSION, "")
                # Modern DTSX also writes the expression as an attribute
                expr_text = expr_text or col_el.get("expression", "")
                if expr_text:
                    exprs.append(ExpressionNode(
                        output_column=col_name,
                        ssis_expression=expr_text,
                        translation_status=TranspilationStatus.PENDING,
                    ))
        return exprs

    def _extract_conditional_split_expressions(self, comp_el: etree._Element) -> list[ExpressionNode]:
        exprs: list[ExpressionNode] = []
        outputs_el = _first(comp_el, "outputs")
        if outputs_el is None:
            return exprs
        for out_el in _children(outputs_el, "output"):
            out_name = out_el.get("name", "")
            is_default = out_el.get("isDefaultOut", "false").lower() == "true"
            is_error = out_el.get("isErrorOut", "false").lower() == "true"
            if is_default or is_error:
                continue
            props = _get_props(out_el)
            expr_text = props.get(_PROP_FRIENDLY_EXPRESSION, "") or props.get(_PROP_EXPRESSION, "")
            expr_text = expr_text or out_el.get("expression", "")
            if expr_text:
                exprs.append(ExpressionNode(
                    output_column=out_name,
                    ssis_expression=expr_text,
                    translation_status=TranspilationStatus.PENDING,
                ))
        return exprs

    def _extract_lookup_join_columns(self, comp_el: etree._Element) -> list[JoinColumn]:
        joins: list[JoinColumn] = []
        inputs_el = _first(comp_el, "inputs")
        if inputs_el is None:
            return joins
        for inp_el in _children(inputs_el, "input"):
            cols_el = _first(inp_el, "inputColumns")
            if cols_el is None:
                continue
            for col_el in _children(cols_el, "inputColumn"):
                if col_el.get("joinToReferenceColumn"):
                    joins.append(JoinColumn(
                        input=col_el.get("name", "") or col_el.get("cachedName", ""),
                        lookup=col_el.get("joinToReferenceColumn", ""),
                    ))
        return joins

    def _extract_aggregations(self, comp_el: etree._Element) -> list[dict[str, str]]:
        aggs: list[dict[str, str]] = []
        agg_map = {
            "0": "group_by", "1": "count", "2": "count_all",
            "3": "count_distinct", "4": "sum", "5": "avg",
            "6": "min", "7": "max",
            # Modern DTSX writes the function name directly
            "group_by": "group_by", "groupby": "group_by", "count": "count",
            "countall": "count_all", "countdistinct": "count_distinct",
            "sum": "sum", "avg": "avg", "average": "avg", "min": "min", "max": "max",
        }
        outputs_el = _first(comp_el, "outputs")
        if outputs_el is None:
            return aggs
        for out_el in _children(outputs_el, "output"):
            cols_el = _first(out_el, "outputColumns")
            if cols_el is None:
                continue
            for col_el in _children(cols_el, "outputColumn"):
                props = _get_props(col_el)
                agg_func = (
                    props.get("AggregationFunction")
                    or col_el.get("aggregationFunction")
                    or props.get("aggregationType")
                    or "0"
                )
                aggs.append({
                    "column": col_el.get("name", ""),
                    "function": agg_map.get(str(agg_func).lower(), str(agg_func)),
                    "source": col_el.get("sourceColumn", "") or props.get("AggregationColumnId", ""),
                })
        return aggs
