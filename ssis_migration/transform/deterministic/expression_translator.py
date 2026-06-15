"""
SSIS Expression Language → PySpark column expression translator.

The SSIS Expression Language is a C-like syntax distinct from T-SQL.
This translator handles the deterministic subset; complex nested expressions
that exceed the rule map are flagged for the LLM pipeline.

Reference: https://learn.microsoft.com/en-us/sql/integration-services/expressions
"""

from __future__ import annotations

import logging
import re
from typing import NamedTuple

from ssis_migration.cir.models import ExpressionNode, TranspilationStatus
from ssis_migration.cir.type_mapping import EXPRESSION_FUNCTION_MAP

logger = logging.getLogger(__name__)


class TranslationResult(NamedTuple):
    pyspark_expr: str | None
    status: TranspilationStatus
    notes: str | None


# ── Tokeniser ─────────────────────────────────────────────────────────────────
# We use a simple recursive token-based approach rather than a full AST
# parser, which is sufficient for common Derived Column / Conditional Split
# expressions.  Complex nested expressions fall back to LLM.

_FUNC_CALL_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*\(', re.I)
_COLUMN_REF_RE = re.compile(r'\[([^\]]+)\]')        # [ColumnName]
_PARAM_REF_RE = re.compile(r'\$Package::[^\s,)]+')   # $Package::ParamName
_TYPE_CAST_RE = re.compile(r'\(DT_[A-Z0-9]+(?:,\s*\d+(?:,\s*\d+)?)?\)\s*')

# Conditional ternary: condition ? true_val : false_val
_TERNARY_RE = re.compile(
    r'^(.+?)\s*\?\s*(.+?)\s*:\s*(.+)$',
    re.DOTALL,
)

# Null-coalescing: REPLACENULL(col, default)
_REPLACENULL_RE = re.compile(r'REPLACENULL\((.+),\s*(.+)\)', re.I)

# SSIS type cast pattern: (DT_WSTR, 100) col
_SSIS_CAST_RE = re.compile(r'\((?P<dtype>DT_[A-Z0-9]+)(?:,\s*(?P<len>\d+))?\)\s*(?P<expr>[^\s(].+)', re.I)


def _col(name: str) -> str:
    """Produce F.col('name') expression."""
    return f'F.col("{name}")'


def _translate_column_refs(expr: str) -> str:
    """Replace [ColName] with F.col("ColName")."""
    return _COLUMN_REF_RE.sub(lambda m: _col(m.group(1)), expr)


def _translate_param_refs(expr: str) -> str:
    """Replace $Package::Param with a Python dict lookup placeholder."""
    return _PARAM_REF_RE.sub(lambda m: f'params["{m.group(0)[10:]}"]', expr)


def _translate_type_cast(expr: str) -> str:
    """
    Replace SSIS explicit cast syntax (DT_WSTR, 50) col → col.cast(StringType()).
    """
    match = _SSIS_CAST_RE.match(expr.strip())
    if not match:
        return expr
    dtype = match.group("dtype").upper()
    inner = match.group("expr").strip()

    from ssis_migration.cir.type_mapping import SSIS_TYPE_MAP
    _, pyspark_type, _ = (SSIS_TYPE_MAP.get(dtype, ("string", "StringType", None))
                          if dtype in SSIS_TYPE_MAP else (None, "StringType", None))
    # Recursively translate inner
    inner_tr = translate_expression(inner).pyspark_expr or inner
    return f"{inner_tr}.cast({pyspark_type}())"


def translate_expression(expr: str) -> TranslationResult:
    """
    Best-effort translation of a single SSIS expression string to PySpark.
    Returns None pyspark_expr if the expression requires LLM handling.
    """
    if not expr or not expr.strip():
        return TranslationResult(None, TranspilationStatus.COMPLETE, None)

    expr = expr.strip()

    # 1. Ternary: condition ? val_true : val_false → F.when(cond, t).otherwise(f)
    ternary = _TERNARY_RE.match(expr)
    if ternary:
        cond_raw, true_raw, false_raw = ternary.groups()
        cond = translate_expression(cond_raw.strip())
        true_v = translate_expression(true_raw.strip())
        false_v = translate_expression(false_raw.strip())
        if all(r.pyspark_expr for r in (cond, true_v, false_v)):
            py = f"F.when({cond.pyspark_expr}, {true_v.pyspark_expr}).otherwise({false_v.pyspark_expr})"
            return TranslationResult(py, TranspilationStatus.COMPLETE, None)
        return TranslationResult(None, TranspilationStatus.LLM_REQUIRED,
                                 "Ternary sub-expression not translatable")

    # 2. Type cast: (DT_WSTR, 50) expr
    if _SSIS_CAST_RE.match(expr):
        result = _translate_type_cast(expr)
        return TranslationResult(result, TranspilationStatus.COMPLETE, None)

    # 3. ISNULL(col) — very common
    if re.match(r'^ISNULL\s*\((.+)\)$', expr, re.I):
        inner = re.match(r'^ISNULL\s*\((.+)\)$', expr, re.I).group(1)
        inner_tr = translate_expression(inner.strip())
        if inner_tr.pyspark_expr:
            return TranslationResult(f"{inner_tr.pyspark_expr}.isNull()",
                                     TranspilationStatus.COMPLETE, None)

    # 4. Simple column reference [Col] → F.col("Col")
    col_match = _COLUMN_REF_RE.fullmatch(expr.strip())
    if col_match:
        return TranslationResult(_col(col_match.group(1)),
                                 TranspilationStatus.COMPLETE, None)

    # 5. String literal "..." → F.lit("...")
    if re.match(r'^"[^"]*"$', expr):
        return TranslationResult(f"F.lit({expr})", TranspilationStatus.COMPLETE, None)

    # 6. Numeric literal
    if re.match(r'^-?\d+(\.\d+)?$', expr):
        return TranslationResult(f"F.lit({expr})", TranspilationStatus.COMPLETE, None)

    # 7. NULL literal
    if expr.upper() == "NULL":
        return TranslationResult("F.lit(None)", TranspilationStatus.COMPLETE, None)

    # 8. String concatenation: expr + expr (SSIS uses + for string concat)
    # Split on + that aren't inside parentheses
    parts = _split_on_plus(expr)
    if len(parts) > 1:
        translated_parts = [translate_expression(p.strip()) for p in parts]
        if all(r.pyspark_expr for r in translated_parts):
            args = ", ".join(r.pyspark_expr for r in translated_parts)
            return TranslationResult(f"F.concat({args})", TranspilationStatus.COMPLETE, None)

    # 9. Function calls: FUNC(arg1, arg2, ...)
    func_match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*\((.+)\)$', expr, re.DOTALL)
    if func_match:
        func_name = func_match.group(1).lower()
        args_raw = func_match.group(2)
        args = _split_args(args_raw)

        if func_name in EXPRESSION_FUNCTION_MAP:
            template, is_deterministic = EXPRESSION_FUNCTION_MAP[func_name]
            if not is_deterministic or template is None:
                return TranslationResult(None, TranspilationStatus.LLM_REQUIRED,
                                         f"Function {func_name} requires LLM translation")

            translated_args = [translate_expression(a.strip()) for a in args]
            if not all(r.pyspark_expr for r in translated_args):
                return TranslationResult(None, TranspilationStatus.LLM_REQUIRED,
                                         f"Argument in {func_name}() not translatable")

            if "{args}" in template:
                joined = ", ".join(r.pyspark_expr for r in translated_args)
                py = template.replace("{args}", joined)
            else:
                try:
                    py = template.format(*[r.pyspark_expr for r in translated_args])
                except IndexError:
                    return TranslationResult(None, TranspilationStatus.LLM_REQUIRED,
                                             f"Argument count mismatch for {func_name}")
            return TranslationResult(py, TranspilationStatus.COMPLETE, None)

    # 10. Replace column references and param references for complex leftover expressions
    transformed = _translate_column_refs(expr)
    transformed = _translate_param_refs(transformed)
    if transformed != expr:
        # Partial translation — still useful context for LLM
        return TranslationResult(transformed, TranspilationStatus.LLM_REQUIRED,
                                 "Partial column reference resolution; review expression")

    # Fallback: flag for LLM
    return TranslationResult(None, TranspilationStatus.LLM_REQUIRED,
                             "Expression complexity exceeds deterministic translator")


def translate_expression_node(node: ExpressionNode) -> ExpressionNode:
    """Translate a single ExpressionNode in-place and return it."""
    result = translate_expression(node.ssis_expression)
    node.pyspark_expression = result.pyspark_expr
    node.translation_status = result.status
    node.translation_notes = result.notes
    return node


# ── Helpers ───────────────────────────────────────────────────────────────────

def _split_on_plus(expr: str) -> list[str]:
    """Split expression on + operators that are not inside parentheses or quotes."""
    parts = []
    current = []
    depth = 0
    in_string = False
    i = 0
    while i < len(expr):
        ch = expr[i]
        if ch == '"' and not in_string:
            in_string = True
            current.append(ch)
        elif ch == '"' and in_string:
            in_string = False
            current.append(ch)
        elif ch == '(' and not in_string:
            depth += 1
            current.append(ch)
        elif ch == ')' and not in_string:
            depth -= 1
            current.append(ch)
        elif ch == '+' and depth == 0 and not in_string:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    if current:
        parts.append("".join(current))
    return parts if len(parts) > 1 else [expr]


def _split_args(args_str: str) -> list[str]:
    """Split function arguments respecting nested parentheses and string literals."""
    args = []
    current = []
    depth = 0
    in_string = False
    for ch in args_str:
        if ch == '"' and not in_string:
            in_string = True
            current.append(ch)
        elif ch == '"' and in_string:
            in_string = False
            current.append(ch)
        elif ch == '(' and not in_string:
            depth += 1
            current.append(ch)
        elif ch == ')' and not in_string:
            depth -= 1
            current.append(ch)
        elif ch == ',' and depth == 0 and not in_string:
            args.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        args.append("".join(current).strip())
    return args
