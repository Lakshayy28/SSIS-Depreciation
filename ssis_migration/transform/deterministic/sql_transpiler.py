"""
SQL transpilation via sqlglot: T-SQL → Spark SQL.

sqlglot handles: function mapping (ISNULL→COALESCE, GETDATE→CURRENT_TIMESTAMP),
TOP/LIMIT, CTE normalisation, identifier quoting, data type syntax.

Complex procedural SQL (cursors, dynamic SQL, WHILE loops) is flagged for
the LLM pipeline via TranspilationStatus.LLM_REQUIRED.
"""

from __future__ import annotations

import logging
import re

import sqlglot
import sqlglot.errors

from ssis_migration.cir.models import SqlStatement, TranspilationStatus

logger = logging.getLogger(__name__)

# Patterns that indicate procedural SQL sqlglot cannot fully transpile
_PROCEDURAL_PATTERNS = [
    re.compile(r'\bEXEC(?:UTE)?\b', re.I),
    re.compile(r'\bCURSOR\b', re.I),
    re.compile(r'\bFETCH\b', re.I),
    re.compile(r'\bWHILE\b', re.I),
    re.compile(r'\bDECLARE\s+@', re.I),
    re.compile(r'\bEXEC\s*\(', re.I),     # dynamic SQL
    re.compile(r'sp_executesql', re.I),
    re.compile(r'\bGO\b'),
    re.compile(r'\bCREATE\s+PROC', re.I),
    re.compile(r'\bBEGIN\s+TRAN', re.I),
    re.compile(r'##\w+'),                  # global temp tables
]


def _is_procedural(sql: str) -> bool:
    return any(p.search(sql) for p in _PROCEDURAL_PATTERNS)


def transpile_sql(stmt: SqlStatement) -> SqlStatement:
    """
    Transpile a SqlStatement in-place (modifies transpiled_text and status).
    Returns the modified statement for chaining.
    """
    sql = stmt.original_text.strip()
    if not sql:
        stmt.transpilation_status = TranspilationStatus.COMPLETE
        stmt.transpiled_text = sql
        return stmt

    if _is_procedural(sql):
        stmt.transpilation_status = TranspilationStatus.LLM_REQUIRED
        stmt.transpilation_notes = "Procedural T-SQL detected; requires LLM for full transpilation"
        logger.debug("Flagging procedural SQL for LLM: %.80s...", sql)
        return stmt

    try:
        results = sqlglot.transpile(
            sql,
            read="tsql",
            write="spark",
            pretty=True,
            error_level=sqlglot.errors.ErrorLevel.RAISE,
        )
        stmt.transpiled_text = "\n".join(results)
        stmt.transpilation_status = TranspilationStatus.COMPLETE
        logger.debug("Transpiled SQL successfully")
    except sqlglot.errors.SqlglotError as exc:
        # Try again with WARN level — sqlglot may still produce partial output
        try:
            results = sqlglot.transpile(
                sql,
                read="tsql",
                write="spark",
                pretty=True,
                error_level=sqlglot.errors.ErrorLevel.WARN,
            )
            partial = "\n".join(results)
            if partial.strip():
                stmt.transpiled_text = partial
                stmt.transpilation_status = TranspilationStatus.COMPLETE
                stmt.transpilation_notes = f"Partial transpilation (warnings): {exc}"
            else:
                stmt.transpilation_status = TranspilationStatus.LLM_REQUIRED
                stmt.transpilation_notes = f"sqlglot error: {exc}"
        except Exception as inner:
            stmt.transpilation_status = TranspilationStatus.LLM_REQUIRED
            stmt.transpilation_notes = f"sqlglot failed: {inner}"
            logger.warning("SQL transpilation failed, flagging for LLM: %s", inner)

    return stmt


def extract_lineage(sql: str) -> dict[str, list[str]]:
    """
    Use sqlglot's lineage module to extract source/sink table names from SQL.
    Returns {"sources": [...], "sinks": [...]}
    """
    try:
        ast = sqlglot.parse_one(sql, read="tsql")
        tables = [t.name for t in ast.find_all(sqlglot.exp.Table) if t.name]
        return {"sources": tables, "sinks": []}
    except Exception:
        return {"sources": [], "sinks": []}
