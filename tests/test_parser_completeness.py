"""
Canonical-completeness regression tests: the CIR must capture EVERYTHING
structurally significant in the DTSX (connections, data flows, components,
expressions), and record its own parse coverage.

These lock in the fixes for three silent-loss bugs:
  1. connections extractor appended only the last manager (indentation bug)
  2. data-flow extractor searched namespaced tags but pipeline XML is unprefixed
  3. data flows nested inside Sequence containers were never discovered
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ssis_migration.parser import DTSXParser
from ssis_migration.parser.extractors.connections import (
    _normalize_conn_params,
    _parse_connection_string,
)

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
ORDERS = SAMPLES / "ETL_Load_Orders.dtsx"
CUSTOMERS = SAMPLES / "ETL_Load_Customers.dtsx"

pytestmark = pytest.mark.skipif(not ORDERS.exists(), reason="samples missing")


@pytest.fixture(scope="module")
def orders_cir():
    return DTSXParser().parse(ORDERS)


@pytest.fixture(scope="module")
def customers_cir():
    return DTSXParser().parse(CUSTOMERS)


# ─── connections ──────────────────────────────────────────────────────────────

def test_all_connection_managers_extracted(orders_cir):
    names = {c.name for c in orders_cir.connections}
    assert names == {"OLEDB_Source", "OLEDB_Dest", "FlatFile_ErrorLog"}


def test_connection_strings_resolved_with_canonical_keys(orders_cir):
    src = next(c for c in orders_cir.connections if c.name == "OLEDB_Source")
    assert src.resolved_parameters.get("host") == "SQLPROD01"
    assert src.resolved_parameters.get("database") == "AdventureWorks"


def test_conn_string_keys_with_spaces():
    parsed = _parse_connection_string(
        "Data Source=srv,1433;Initial Catalog=DB1;User ID=etl;Password=x"
    )
    assert parsed["data source"] == "srv,1433"
    assert parsed["initial catalog"] == "DB1"
    norm = _normalize_conn_params(parsed)
    assert norm["host"] == "srv"
    assert norm["port"] == "1433"
    assert norm["database"] == "DB1"
    assert norm["user"] == "etl"
    assert norm["password"] == "x"


# ─── data flows ───────────────────────────────────────────────────────────────

def test_nested_data_flow_discovered(orders_cir):
    # DFT Load Orders lives inside a Sequence container.
    assert len(orders_cir.data_flows) == 1
    assert orders_cir.data_flows[0].name == "DFT Load Orders"


def test_all_components_extracted(orders_cir):
    comps = orders_cir.data_flows[0].components
    assert len(comps) == 7
    subtypes = [c.subtype for c in comps]
    assert subtypes.count("oledb_destination") == 2
    assert "oledb_source" in subtypes
    assert "aggregate" in subtypes
    assert "derived_column" in subtypes


def test_source_sql_captured(orders_cir):
    src = next(c for c in orders_cir.data_flows[0].components if c.subtype == "oledb_source")
    assert src.type == "source"
    assert src.sql_command is not None
    assert "FROM dbo.Orders" in src.sql_command.original_text


def test_derived_column_expressions_captured(orders_cir):
    dc = next(c for c in orders_cir.data_flows[0].components if c.subtype == "derived_column")
    assert len(dc.expressions) >= 1
    assert all(e.ssis_expression for e in dc.expressions)


def test_aggregate_functions_captured(orders_cir):
    agg = next(c for c in orders_cir.data_flows[0].components if c.subtype == "aggregate")
    assert len(agg.aggregations) >= 1


def test_destination_tables_captured(orders_cir):
    tables = {c.table_name for c in orders_cir.data_flows[0].components
              if c.subtype == "oledb_destination"}
    assert "[staging].[Orders]" in tables


def test_conditional_split_and_lookup(customers_cir):
    subtypes = {c.subtype for c in customers_cir.data_flows[0].components}
    assert {"conditional_split", "lookup"} <= subtypes


def test_component_ids_unique_across_package(orders_cir, customers_cir):
    for cir in (orders_cir, customers_cir):
        ids = [c.id for df in cir.data_flows for c in df.components]
        assert len(ids) == len(set(ids))


def test_component_extra_properties_amplified(orders_cir):
    # Canonical completeness: the ParameterMapping property on the source must
    # survive into the CIR even though no specialised field models it yet.
    src = next(c for c in orders_cir.data_flows[0].components if c.subtype == "oledb_source")
    assert "ParameterMapping" in src.extra_properties


# ─── parse coverage audit ─────────────────────────────────────────────────────

def test_parse_coverage_recorded_and_full(orders_cir, customers_cir):
    for cir in (orders_cir, customers_cir):
        pc = cir.metadata.parse_coverage
        assert pc is not None
        assert pc["coverage"] == 1.0, f"lost elements: {pc['detail']}"
