"""
Component Mapper — converts CIR DataFlow components to PySpark code snippets.

Each mapping is a pure function: CIR component → PySpark code string.
Items that cannot be deterministically converted set conversion_status to
LLM_REQUIRED on the component and register with the CIR conversion metadata.
"""

from __future__ import annotations

import logging

from ssis_migration.cir.models import (
    CIR,
    CacheMode,
    ConversionStatus,
    DataFlow,
    DataFlowComponent,
    JoinType,
    TranspilationStatus,
)
from ssis_migration.transform.deterministic.expression_translator import translate_expression_node
from ssis_migration.transform.deterministic.sql_transpiler import transpile_sql

logger = logging.getLogger(__name__)

# Join type → PySpark how= argument
_JOIN_HOW: dict[JoinType, str] = {
    JoinType.INNER: "inner",
    JoinType.LEFT_OUTER: "left",
    JoinType.RIGHT_OUTER: "right",
    JoinType.FULL_OUTER: "outer",
}

# Aggregate function → PySpark agg function
_AGG_FUNC: dict[str, str] = {
    "count": "F.count",
    "count_all": "F.count",
    "count_distinct": "F.countDistinct",
    "sum": "F.sum",
    "avg": "F.avg",
    "min": "F.min",
    "max": "F.max",
    "group_by": None,  # group_by is not an aggregation, handled separately
}


class ComponentMapper:
    """
    Processes all DataFlow components in a CIR, attaching PySpark code snippets
    and setting conversion statuses.
    """

    def process(self, cir: CIR) -> None:
        for df in cir.data_flows:
            for comp in df.components:
                self._map_component(comp, cir, df)
        self._update_coverage(cir)

    def _map_component(self, comp: DataFlowComponent, cir: CIR, df: DataFlow) -> None:
        subtype = comp.subtype

        if subtype in ("oledb_source", "ado_net_source"):
            self._map_source(comp, cir)
        elif subtype == "flat_file_source":
            self._map_flat_file_source(comp)
        elif subtype in ("oledb_destination", "ado_net_destination"):
            self._map_destination(comp, cir)
        elif subtype == "flat_file_destination":
            self._map_flat_file_destination(comp)
        elif subtype == "derived_column":
            self._map_derived_column(comp, cir)
        elif subtype == "conditional_split":
            self._map_conditional_split(comp, cir)
        elif subtype == "lookup":
            self._map_lookup(comp, cir)
        elif subtype == "merge_join":
            self._map_merge_join(comp)
        elif subtype == "aggregate":
            self._map_aggregate(comp)
        elif subtype == "sort":
            self._map_sort(comp)
        elif subtype == "union_all":
            self._map_union_all(comp)
        elif subtype == "multicast":
            self._map_multicast(comp)
        elif subtype == "row_count":
            self._map_row_count(comp)
        elif subtype == "copy_column":
            self._map_copy_column(comp)
        elif subtype == "data_conversion":
            self._map_data_conversion(comp)
        elif subtype in ("script_component", "fuzzy_lookup", "fuzzy_grouping",
                         "term_extraction", "data_mining_query"):
            self._flag_llm(comp, cir, f"{subtype} has no deterministic equivalent")
        else:
            self._flag_llm(comp, cir, f"Unknown component subtype: {subtype}")

    # ── Sources ───────────────────────────────────────────────────────────────

    def _map_source(self, comp: DataFlowComponent, cir: CIR) -> None:
        conn, resolved = self._conn_name(cir, comp.connection_ref, prefer="source")
        note = self._verify_note(resolved, conn)
        if comp.sql_command:
            transpile_sql(comp.sql_command)
            if comp.sql_command.transpilation_status == TranspilationStatus.LLM_REQUIRED:
                self._flag_llm(comp, cir, "Source SQL requires LLM transpilation")
                return
            sql_var = "source_sql"
            snippet = (
                f'{note}'
                f'{sql_var} = """\n{comp.sql_command.transpiled_text}\n"""\n'
                f'df = (\n'
                f'    spark.read.format("jdbc")\n'
                f'    .option("url", connections["{conn}"]["url"])\n'
                f'    .option("driver", connections["{conn}"]["driver"])\n'
                f'    .option("query", {sql_var})\n'
                f'    .load()\n'
                f')'
            )
        else:
            # Table-mode access
            table = comp.table_name or "UNKNOWN_TABLE"
            snippet = (
                f'{note}'
                f'df = (\n'
                f'    spark.read.format("jdbc")\n'
                f'    .option("url", connections["{conn}"]["url"])\n'
                f'    .option("driver", connections["{conn}"]["driver"])\n'
                f'    .option("dbtable", "{table}")\n'
                f'    .load()\n'
                f')'
            )
        comp.pyspark_snippet = snippet
        comp.conversion_status = ConversionStatus.DETERMINISTIC

    def _map_flat_file_source(self, comp: DataFlowComponent) -> None:
        path = comp.file_path or "UNKNOWN_PATH"
        delim = comp.delimiter or ","
        header = str(comp.has_header).lower()
        snippet = (
            f'df = (\n'
            f'    spark.read.format("csv")\n'
            f'    .option("header", "{header}")\n'
            f'    .option("sep", "{delim}")\n'
            f'    .option("inferSchema", "false")\n'
            f'    .load("{path}")\n'
            f')'
        )
        comp.pyspark_snippet = snippet
        comp.conversion_status = ConversionStatus.DETERMINISTIC

    # ── Destinations ──────────────────────────────────────────────────────────

    def _map_destination(self, comp: DataFlowComponent, cir: CIR) -> None:
        conn, resolved = self._conn_name(cir, comp.connection_ref, prefer="dest")
        table = comp.table_name or "UNKNOWN_TABLE"
        snippet = (
            f'{self._verify_note(resolved, conn)}'
            f'(\n'
            f'    df\n'
            f'    .write.format("jdbc")\n'
            f'    .option("url", connections["{conn}"]["url"])\n'
            f'    .option("driver", connections["{conn}"]["driver"])\n'
            f'    .option("dbtable", "{table}")\n'
            f'    .mode("append")\n'
            f'    .save()\n'
            f')'
        )
        comp.pyspark_snippet = snippet
        comp.conversion_status = ConversionStatus.DETERMINISTIC

    def _map_flat_file_destination(self, comp: DataFlowComponent) -> None:
        path = comp.file_path or "UNKNOWN_PATH"
        snippet = (
            f'(\n'
            f'    df\n'
            f'    .write.format("csv")\n'
            f'    .option("header", "true")\n'
            f'    .mode("overwrite")\n'
            f'    .save("{path}")\n'
            f')'
        )
        comp.pyspark_snippet = snippet
        comp.conversion_status = ConversionStatus.DETERMINISTIC

    # ── Transformations ───────────────────────────────────────────────────────

    def _map_derived_column(self, comp: DataFlowComponent, cir: CIR) -> None:
        lines = [f"# Derived Column: {comp.name}"]
        all_deterministic = True
        for expr_node in comp.expressions:
            translate_expression_node(expr_node)
            if expr_node.translation_status == TranspilationStatus.COMPLETE and expr_node.pyspark_expression:
                lines.append(
                    f'df = df.withColumn("{expr_node.output_column}", {expr_node.pyspark_expression})'
                )
            else:
                all_deterministic = False
                lines.append(
                    f'# LLM REQUIRED: {expr_node.output_column} = {expr_node.ssis_expression}'
                )
                cir.flag_for_llm(f"{comp.id}::expr::{expr_node.output_column}")

        comp.pyspark_snippet = "\n".join(lines)
        comp.conversion_status = (
            ConversionStatus.DETERMINISTIC if all_deterministic else ConversionStatus.LLM_REQUIRED
        )

    def _map_conditional_split(self, comp: DataFlowComponent, cir: CIR) -> None:
        lines = ["# Conditional Split"]
        all_deterministic = True
        for expr_node in comp.expressions:
            translate_expression_node(expr_node)
            if expr_node.translation_status == TranspilationStatus.COMPLETE and expr_node.pyspark_expression:
                safe = expr_node.output_column.lower().replace(" ", "_")
                lines.append(f'df_{safe} = df.filter({expr_node.pyspark_expression})')
            else:
                all_deterministic = False
                lines.append(f'# LLM REQUIRED: split on {expr_node.ssis_expression}')
                cir.flag_for_llm(f"{comp.id}::split::{expr_node.output_column}")

        comp.pyspark_snippet = "\n".join(lines)
        comp.conversion_status = (
            ConversionStatus.DETERMINISTIC if all_deterministic else ConversionStatus.LLM_REQUIRED
        )

    def _map_lookup(self, comp: DataFlowComponent, cir: CIR) -> None:
        if not comp.lookup_sql:
            self._flag_llm(comp, cir, "Lookup has no reference SQL — cache-connection lookups need LLM")
            return

        conn, _resolved = self._conn_name(cir, comp.connection_ref, prefer="source")
        join_keys = [f'"{j.input}"' for j in comp.join_columns]
        join_cols_str = f"[{', '.join(join_keys)}]" if join_keys else '"key"'

        lookup_load = (
            f'lookup_df = (\n'
            f'    spark.read.format("jdbc")\n'
            f'    .option("url", connections["{conn}"]["url"])\n'
            f'    .option("driver", connections["{conn}"]["driver"])\n'
            f'    .option("query", """{comp.lookup_sql}""")\n'
            f'    .load()\n'
            f')'
        )

        how = "left"  # SSIS Lookup ≈ left join
        # SSIS full-cache lookup pulls the reference table into memory — the
        # Spark analog is a broadcast join. No-cache lookups stay shuffle joins.
        right = "F.broadcast(lookup_df)" if comp.cache_mode == CacheMode.FULL else "lookup_df"
        snippet = (
            f'{lookup_load}\n'
            f'df = df.join({right}, on={join_cols_str}, how="{how}")'
        )
        comp.pyspark_snippet = snippet
        comp.conversion_status = ConversionStatus.DETERMINISTIC

    def _map_merge_join(self, comp: DataFlowComponent) -> None:
        how = _JOIN_HOW.get(comp.join_type or JoinType.INNER, "inner")
        keys = [f'"{k}"' for k in comp.join_key_columns]
        keys_str = f"[{', '.join(keys)}]" if keys else '"key"'
        snippet = f'df = df_left.join(df_right, on={keys_str}, how="{how}")'
        comp.pyspark_snippet = snippet
        comp.conversion_status = ConversionStatus.DETERMINISTIC

    def _map_aggregate(self, comp: DataFlowComponent) -> None:
        # group-by columns arrive as aggregation entries with function=group_by
        group_cols = [f'"{c}"' for c in comp.group_by_columns]
        agg_calls = []
        for agg in comp.aggregations:
            func_name = agg.get("function", "")
            col = agg.get("column", "col")
            src = agg.get("source") or col
            if func_name == "group_by":
                quoted = f'"{col}"'
                if quoted not in group_cols:
                    group_cols.append(quoted)
                continue
            func = _AGG_FUNC.get(func_name)
            if func:
                agg_calls.append(f'{func}("{src}").alias("{col}")')

        group_str = ", ".join(group_cols)
        agg_str = ", ".join(agg_calls) if agg_calls else 'F.count("*").alias("row_count")'
        snippet = f'df = df.groupBy({group_str}).agg({agg_str})'
        comp.pyspark_snippet = snippet
        comp.conversion_status = ConversionStatus.DETERMINISTIC

    def _map_sort(self, comp: DataFlowComponent) -> None:
        # Sort columns are in output_columns; SSIS sort order is in properties
        cols = [f'"{c.name}"' for c in comp.output_columns[:3]] or ['"col"']
        snippet = f'df = df.orderBy({", ".join(cols)})'
        comp.pyspark_snippet = snippet
        comp.conversion_status = ConversionStatus.DETERMINISTIC

    def _map_union_all(self, comp: DataFlowComponent) -> None:
        from ssis_migration.config import cfg
        try:
            major, minor = (int(x) for x in cfg.spark_version.split(".")[:2])
        except ValueError:
            major, minor = 3, 3
        # allowMissingColumns is Spark 3.1+; older targets get the plain form.
        if (major, minor) >= (3, 1):
            snippet = 'df = df_branch_1.unionByName(df_branch_2, allowMissingColumns=True)'
        else:
            snippet = 'df = df_branch_1.unionByName(df_branch_2)'
        comp.pyspark_snippet = snippet
        comp.conversion_status = ConversionStatus.DETERMINISTIC

    def _map_multicast(self, comp: DataFlowComponent) -> None:
        snippet = '# Multicast: df is referenced by multiple downstream components\ndf_copy = df'
        comp.pyspark_snippet = snippet
        comp.conversion_status = ConversionStatus.DETERMINISTIC

    def _map_row_count(self, comp: DataFlowComponent) -> None:
        snippet = 'row_count = df.count()\nlogger.info("Row count: %d", row_count)'
        comp.pyspark_snippet = snippet
        comp.conversion_status = ConversionStatus.DETERMINISTIC

    def _map_copy_column(self, comp: DataFlowComponent) -> None:
        lines = []
        for mapping in comp.column_mappings:
            lines.append(f'df = df.withColumn("{mapping.destination}", F.col("{mapping.source}"))')
        comp.pyspark_snippet = "\n".join(lines) or "# Copy Column (no mappings)"
        comp.conversion_status = ConversionStatus.DETERMINISTIC

    def _map_data_conversion(self, comp: DataFlowComponent) -> None:
        lines = []
        for col in comp.output_columns:
            if col.pyspark_type:
                # pyspark_type is either bare ("IntegerType") or parameterised
                # ("DecimalType(18,4)") — only bare names need call parens.
                type_expr = col.pyspark_type if col.pyspark_type.endswith(")") \
                    else f"{col.pyspark_type}()"
                lines.append(
                    f'df = df.withColumn("{col.name}", F.col("{col.name}").cast({type_expr}))'
                )
        comp.pyspark_snippet = "\n".join(lines) or "# Data Conversion (no typed output columns)"
        comp.conversion_status = ConversionStatus.DETERMINISTIC

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _flag_llm(self, comp: DataFlowComponent, cir: CIR, reason: str) -> None:
        comp.conversion_status = ConversionStatus.LLM_REQUIRED
        comp.conversion_notes = reason
        cir.flag_for_llm(comp.id)
        logger.debug("Component %s flagged for LLM: %s", comp.id, reason)

    def _conn_name(self, cir: CIR, conn_ref: str | None,
                   prefer: str | None = None) -> tuple[str, bool]:
        """
        Resolve a component's connection ref to the connection-manager NAME
        (the generated module's CONNECTIONS dict is keyed by name).

        Returns (name, resolved). When the DTSX component carries no usable
        ref, we guess: prefer a connection whose name hints at the role
        ("dest"/"source"), else the first one — and the caller emits a
        verify-me comment so the guess is never silent.
        """
        conn = cir.find_connection(conn_ref)
        if conn is not None:
            return conn.name, True
        if cir.connections:
            if prefer:
                for c in cir.connections:
                    if prefer.lower() in c.name.lower():
                        return c.name, False
            return cir.connections[0].name, False
        return conn_ref or "default", False

    @staticmethod
    def _verify_note(resolved: bool, conn: str) -> str:
        if resolved:
            return ""
        return (f"# TODO(verify): DTSX component specified no connection manager — "
                f"defaulted to '{conn}'; confirm before running\n")

    def _update_coverage(self, cir: CIR) -> None:
        total = sum(len(df.components) for df in cir.data_flows)
        if total == 0:
            cir.conversion_metadata.deterministic_coverage = 1.0
            return
        deterministic = sum(
            1 for df in cir.data_flows
            for comp in df.components
            if comp.conversion_status == ConversionStatus.DETERMINISTIC
        )
        cir.conversion_metadata.deterministic_coverage = deterministic / total
