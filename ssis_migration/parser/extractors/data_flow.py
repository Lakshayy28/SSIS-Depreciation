"""
Extract Data Flow Tasks from DTSX XML.

Each Data Flow Task contains a pipeline component graph. Components are
identified by their componentClassID GUID and their properties vary
by component type. This extractor produces DataFlow CIR objects.
"""

from __future__ import annotations

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
from ssis_migration.cir.type_mapping import resolve_type
from ssis_migration.parser.ns import (
    DTS_OBJECT_DATA,
    NAMESPACES,
    map_component_class,
)

_PIPELINE_NS = NAMESPACES["pipeline"]

# Pipeline element tags (in the pipeline namespace)
_PIPELINE_COMPONENTS = f"{{{_PIPELINE_NS}}}components"
_PIPELINE_COMPONENT = f"{{{_PIPELINE_NS}}}component"
_PIPELINE_PATHS = f"{{{_PIPELINE_NS}}}paths"
_PIPELINE_PATH = f"{{{_PIPELINE_NS}}}path"
_PIPELINE_OUTPUTS = f"{{{_PIPELINE_NS}}}outputs"
_PIPELINE_OUTPUT = f"{{{_PIPELINE_NS}}}output"
_PIPELINE_OUTPUT_COLUMNS = f"{{{_PIPELINE_NS}}}outputColumns"
_PIPELINE_OUTPUT_COLUMN = f"{{{_PIPELINE_NS}}}outputColumn"
_PIPELINE_CUSTOM_PROPS = f"{{{_PIPELINE_NS}}}customPropertyCollection"
_PIPELINE_CUSTOM_PROP = f"{{{_PIPELINE_NS}}}customProperty"
_PIPELINE_INPUTS = f"{{{_PIPELINE_NS}}}inputs"
_PIPELINE_INPUT = f"{{{_PIPELINE_NS}}}input"
_PIPELINE_INPUT_COLUMNS = f"{{{_PIPELINE_NS}}}inputColumns"
_PIPELINE_INPUT_COLUMN = f"{{{_PIPELINE_NS}}}inputColumn"
_PIPELINE_EXTERNAL_META = f"{{{_PIPELINE_NS}}}externalMetadataColumnCollection"
_PIPELINE_EXT_COL = f"{{{_PIPELINE_NS}}}externalMetadataColumn"

# Custom property names we care about
_PROP_SQL_COMMAND = "SqlCommand"
_PROP_SQL_COMMAND_VARIABLE = "SqlCommandVariable"
_PROP_OPEN_ROWSET = "OpenRowset"
_PROP_ACCESS_MODE = "AccessMode"
_PROP_CONNECTION_NAME = "ConnectionName"
_PROP_TABLE_OR_VIEW_NAME = "TableOrViewName"
_PROP_FRIENDLY_EXPRESSION = "FriendlyExpression"
_PROP_EXPRESSION = "Expression"
_PROP_CACHE_TYPE = "CacheType"
_PROP_NO_MATCH_BEHAVIOR = "NoMatchBehavior"
_PROP_JOIN_TYPE = "JoinType"
_PROP_LOOKUP_SQL = "SqlCommand"
_PROP_CHARACTER_MAP_OP = "MapOperation"

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


def _get_props(component: etree._Element) -> dict[str, str]:
    props: dict[str, str] = {}
    cp_el = component.find(_PIPELINE_CUSTOM_PROPS)
    if cp_el is None:
        return props
    for prop in cp_el.findall(_PIPELINE_CUSTOM_PROP):
        name = prop.get("name", "")
        props[name] = prop.text or ""
    return props


def _get_output_columns(component: etree._Element) -> list[OutputColumn]:
    cols: list[OutputColumn] = []
    outputs_el = component.find(_PIPELINE_OUTPUTS)
    if outputs_el is None:
        return cols
    # Take first non-error output
    for out_el in outputs_el.findall(_PIPELINE_OUTPUT):
        is_error = out_el.get("isErrorOut", "false").lower() == "true"
        if is_error:
            continue
        cols_el = out_el.find(_PIPELINE_OUTPUT_COLUMNS)
        if cols_el is None:
            continue
        for col_el in cols_el.findall(_PIPELINE_OUTPUT_COLUMN):
            ssis_type = col_el.get("dataType", "DT_WSTR")
            precision = _int(col_el.get("precision"))
            scale = _int(col_el.get("scale"))
            cir_type, pyspark_type, _ = resolve_type(ssis_type, precision, scale)
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
        break  # only process the primary output
    return cols


def _int(val: str | None) -> int | None:
    try:
        return int(val) if val is not None else None
    except (ValueError, TypeError):
        return None


def _get_connection_ref(component: etree._Element) -> str | None:
    # connection managers are under pipeline:connections/pipeline:connection
    conns_el = component.find(f"{{{_PIPELINE_NS}}}connections")
    if conns_el is not None:
        conn_el = conns_el.find(f"{{{_PIPELINE_NS}}}connection")
        if conn_el is not None:
            return conn_el.get("connectionManagerRefId") or conn_el.get("name")
    return None


_comp_id_counter: dict[str, int] = {}


def _comp_id(subtype: str) -> str:
    _comp_id_counter[subtype] = _comp_id_counter.get(subtype, 0) + 1
    return f"comp_{subtype}_{_comp_id_counter[subtype]:04d}"


class DataFlowExtractor:
    """
    Extracts a DataFlow CIR object from the DTS:ObjectData element of a
    Data Flow Task executable.
    """

    def __init__(self, obj_data_el: etree._Element, df_id: str, df_name: str) -> None:
        self._el = obj_data_el
        self._df_id = df_id
        self._df_name = df_name
        _comp_id_counter.clear()

    def extract(self) -> DataFlow:
        pipeline_el = self._el.find(f"{{{_PIPELINE_NS}}}pipeline")
        if pipeline_el is None:
            # Some packages use a different nesting
            for child in self._el:
                if "pipeline" in child.tag.lower():
                    pipeline_el = child
                    break

        components: list[DataFlowComponent] = []
        paths: list[DataFlowPath] = []

        if pipeline_el is not None:
            comps_el = pipeline_el.find(_PIPELINE_COMPONENTS)
            if comps_el is not None:
                for comp_el in comps_el.findall(_PIPELINE_COMPONENT):
                    comp = self._parse_component(comp_el)
                    if comp:
                        components.append(comp)

            paths_el = pipeline_el.find(_PIPELINE_PATHS)
            if paths_el is not None:
                for path_el in paths_el.findall(_PIPELINE_PATH):
                    paths.append(self._parse_path(path_el))

        return DataFlow(id=self._df_id, name=self._df_name, components=components, paths=paths)

    def _parse_component(self, el: etree._Element) -> DataFlowComponent | None:
        class_id = el.get("componentClassID", "")
        subtype = map_component_class(class_id)
        name = el.get("name", "")
        comp_id = _comp_id(subtype[:8])

        props = _get_props(el)
        output_cols = _get_output_columns(el)
        conn_ref = _get_connection_ref(el)

        comp = DataFlowComponent(id=comp_id, name=name, subtype=subtype,
                                 connection_ref=conn_ref, output_columns=output_cols)

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

        elif subtype == "oledb_destination":
            comp.type = "destination"
            comp.table_name = props.get(_PROP_TABLE_OR_VIEW_NAME) or props.get(_PROP_OPEN_ROWSET)
            comp.column_mappings = self._extract_column_mappings(el)

        elif subtype == "flat_file_destination":
            comp.type = "destination"

        elif subtype == "derived_column":
            comp.type = "transformation"
            comp.expressions = self._extract_derived_col_expressions(el)

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

        elif subtype == "sort":
            comp.type = "transformation"

        elif subtype == "union_all":
            comp.type = "transformation"

        elif subtype in ("script_component",):
            comp.type = "transformation"
            # Code may be binary-encoded; flag for LLM
            comp.script_language = "csharp"

        elif subtype in ("row_count", "copy_column", "character_map", "data_conversion",
                         "multicast", "pivot", "unpivot"):
            comp.type = "transformation"

        else:
            comp.type = "transformation"
            comp.extra_properties = props

        return comp

    def _parse_path(self, el: etree._Element) -> DataFlowPath:
        src = el.get("startId", "")
        dst = el.get("endId", "")
        name = el.get("name", "")
        path_type = "default"
        if "error" in name.lower():
            path_type = "error"
        elif "nomatch" in name.lower() or "no match" in name.lower():
            path_type = "no_match"
        return DataFlowPath(**{"from": src, "to": dst, "type": path_type})

    def _extract_column_mappings(self, comp_el: etree._Element) -> list[ColumnMapping]:
        mappings: list[ColumnMapping] = []
        inputs_el = comp_el.find(_PIPELINE_INPUTS)
        if inputs_el is None:
            return mappings
        for inp_el in inputs_el.findall(_PIPELINE_INPUT):
            cols_el = inp_el.find(_PIPELINE_INPUT_COLUMNS)
            if cols_el is None:
                continue
            ext_meta_el = inp_el.find(_PIPELINE_EXTERNAL_META)
            ext_map: dict[str, str] = {}
            if ext_meta_el is not None:
                for ext_col in ext_meta_el.findall(_PIPELINE_EXT_COL):
                    ext_map[ext_col.get("id", "")] = ext_col.get("name", "")

            for col_el in cols_el.findall(_PIPELINE_INPUT_COLUMN):
                src_name = col_el.get("name", "")
                dst_id = col_el.get("externalMetadataColumnId", "")
                dst_name = ext_map.get(dst_id, src_name)
                mappings.append(ColumnMapping(source=src_name, destination=dst_name))
        return mappings

    def _extract_derived_col_expressions(self, comp_el: etree._Element) -> list[ExpressionNode]:
        exprs: list[ExpressionNode] = []
        outputs_el = comp_el.find(_PIPELINE_OUTPUTS)
        if outputs_el is None:
            return exprs
        for out_el in outputs_el.findall(_PIPELINE_OUTPUT):
            cols_el = out_el.find(_PIPELINE_OUTPUT_COLUMNS)
            if cols_el is None:
                continue
            for col_el in cols_el.findall(_PIPELINE_OUTPUT_COLUMN):
                col_name = col_el.get("name", "")
                props = _get_props(col_el)
                expr_text = props.get(_PROP_FRIENDLY_EXPRESSION, "") or props.get(_PROP_EXPRESSION, "")
                if expr_text:
                    exprs.append(ExpressionNode(
                        output_column=col_name,
                        ssis_expression=expr_text,
                        translation_status=TranspilationStatus.PENDING,
                    ))
        return exprs

    def _extract_conditional_split_expressions(self, comp_el: etree._Element) -> list[ExpressionNode]:
        exprs: list[ExpressionNode] = []
        outputs_el = comp_el.find(_PIPELINE_OUTPUTS)
        if outputs_el is None:
            return exprs
        for out_el in outputs_el.findall(_PIPELINE_OUTPUT):
            out_name = out_el.get("name", "")
            is_default = out_el.get("isDefaultOut", "false").lower() == "true"
            is_error = out_el.get("isErrorOut", "false").lower() == "true"
            if is_default or is_error:
                continue
            props = _get_props(out_el)
            expr_text = props.get(_PROP_FRIENDLY_EXPRESSION, "") or props.get(_PROP_EXPRESSION, "")
            if expr_text:
                exprs.append(ExpressionNode(
                    output_column=out_name,
                    ssis_expression=expr_text,
                    translation_status=TranspilationStatus.PENDING,
                ))
        return exprs

    def _extract_lookup_join_columns(self, comp_el: etree._Element) -> list[JoinColumn]:
        joins: list[JoinColumn] = []
        inputs_el = comp_el.find(_PIPELINE_INPUTS)
        if inputs_el is None:
            return joins
        for inp_el in inputs_el.findall(_PIPELINE_INPUT):
            cols_el = inp_el.find(_PIPELINE_INPUT_COLUMNS)
            if cols_el is None:
                continue
            for col_el in cols_el.findall(_PIPELINE_INPUT_COLUMN):
                if col_el.get("joinToReferenceColumn"):
                    joins.append(JoinColumn(
                        input=col_el.get("name", ""),
                        lookup=col_el.get("joinToReferenceColumn", ""),
                    ))
        return joins

    def _extract_aggregations(self, comp_el: etree._Element) -> list[dict[str, str]]:
        aggs: list[dict[str, str]] = []
        outputs_el = comp_el.find(_PIPELINE_OUTPUTS)
        if outputs_el is None:
            return aggs
        for out_el in outputs_el.findall(_PIPELINE_OUTPUT):
            cols_el = out_el.find(_PIPELINE_OUTPUT_COLUMNS)
            if cols_el is None:
                continue
            for col_el in cols_el.findall(_PIPELINE_OUTPUT_COLUMN):
                props = _get_props(col_el)
                agg_func = props.get("AggregationFunction", "0")
                agg_map = {
                    "0": "group_by", "1": "count", "2": "count_all",
                    "3": "count_distinct", "4": "sum", "5": "avg",
                    "6": "min", "7": "max",
                }
                aggs.append({
                    "column": col_el.get("name", ""),
                    "function": agg_map.get(agg_func, agg_func),
                })
        return aggs
