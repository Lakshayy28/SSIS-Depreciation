"""
SSIS data type → CIR canonical type → PySpark type mapping table.

Handles precision/scale for DECIMAL/NUMERIC and codepage awareness for strings.
"""

from __future__ import annotations

# Maps (ssis_type_prefix, ...) → (cir_type, pyspark_type)
SSIS_TYPE_MAP: dict[str, tuple[str, str]] = {
    "DT_I1": ("int8", "ByteType"),
    "DT_I2": ("int16", "ShortType"),
    "DT_I4": ("int32", "IntegerType"),
    "DT_I8": ("int64", "LongType"),
    "DT_UI1": ("uint8", "ShortType"),    # PySpark has no unsigned; promote
    "DT_UI2": ("uint16", "IntegerType"),
    "DT_UI4": ("uint32", "LongType"),
    "DT_UI8": ("uint64", "LongType"),    # may overflow — flagged in notes
    "DT_R4": ("float32", "FloatType"),
    "DT_R8": ("float64", "DoubleType"),
    "DT_DECIMAL": ("decimal", "DecimalType"),
    "DT_NUMERIC": ("decimal", "DecimalType"),
    "DT_CY": ("decimal(19,4)", "DecimalType(19,4)"),
    "DT_BOOL": ("boolean", "BooleanType"),
    "DT_STR": ("string", "StringType"),
    "DT_WSTR": ("string", "StringType"),
    "DT_TEXT": ("string", "StringType"),
    "DT_NTEXT": ("string", "StringType"),
    "DT_DATE": ("date", "DateType"),
    "DT_DBDATE": ("date", "DateType"),
    "DT_DBTIMESTAMP": ("timestamp", "TimestampType"),
    "DT_DBTIMESTAMP2": ("timestamp", "TimestampType"),
    "DT_DBTIME": ("time", "StringType"),   # no native PySpark time
    "DT_DBTIME2": ("time", "StringType"),
    "DT_FILETIME": ("timestamp", "TimestampType"),
    "DT_GUID": ("uuid", "StringType"),
    "DT_BYTES": ("binary", "BinaryType"),
    "DT_IMAGE": ("binary", "BinaryType"),
    "DT_EMPTY": ("null", "NullType"),
    "DT_NULL": ("null", "NullType"),
}

# Types where precision/scale must be extracted from SSIS metadata
DECIMAL_TYPES = {"DT_DECIMAL", "DT_NUMERIC"}
STRING_WITH_LENGTH = {"DT_STR", "DT_WSTR", "DT_BYTES"}

# Divergences that must be logged in the acceptable-divergence register
KNOWN_DIVERGENCES: dict[str, str] = {
    "DT_UI8": "UINT64 may overflow LongType; review if values exceed 2^63-1",
    "DT_DBTIME": "No native PySpark TIME type; stored as STRING 'HH:mm:ss.nnnnnnn'",
    "DT_DBTIME2": "No native PySpark TIME type; stored as STRING 'HH:mm:ss.nnnnnnn'",
    "DT_DBTIMESTAMP2": "SSIS supports 7 fractional second digits; TimestampType is microsecond (6 digits)",
    "DT_GUID": "GUID stored as STRING in Spark; no UUID type",
}


def normalize_ssis_type(raw: str) -> str:
    """
    Normalize a DTSX pipeline dataType to the canonical DT_* name.

    Modern DTSX pipeline XML writes lowercase pipeline names ("i4", "wstr",
    "dbTimeStamp", "numeric") while older packages and most documentation use
    the DT_* enum ("DT_I4"). Accept both.
    """
    if not raw:
        return "DT_WSTR"
    if raw.upper().startswith("DT_"):
        return raw.upper()
    return f"DT_{raw.upper()}"


def resolve_type(ssis_type: str, precision: int | None = None, scale: int | None = None) -> tuple[str, str, str | None]:
    """Return (cir_type, pyspark_type, divergence_note)."""
    key = normalize_ssis_type(ssis_type)
    if key not in SSIS_TYPE_MAP:
        return ("unknown", "StringType", f"Unrecognised SSIS type '{ssis_type}'; defaulted to StringType")

    cir_type, pyspark_type = SSIS_TYPE_MAP[key]
    divergence = KNOWN_DIVERGENCES.get(key)

    if key in DECIMAL_TYPES:
        p = precision or 18
        s = scale or 0
        cir_type = f"decimal({p},{s})"
        pyspark_type = f"DecimalType({p},{s})"

    return cir_type, pyspark_type, divergence


# SSIS Expression Language → PySpark column expression mapping
# Format: ssis_function_name (lower) → (pyspark_template, is_deterministic)
EXPRESSION_FUNCTION_MAP: dict[str, tuple[str, bool]] = {
    "upper": ("F.upper({0})", True),
    "lower": ("F.lower({0})", True),
    "ltrim": ("F.ltrim({0})", True),
    "rtrim": ("F.rtrim({0})", True),
    "trim": ("F.trim({0})", True),
    "len": ("F.length({0})", True),
    "length": ("F.length({0})", True),
    "substring": ("F.substring({0}, {1}, {2})", True),
    "replace": ("F.regexp_replace({0}, {1}, {2})", True),
    "replaceall": ("F.regexp_replace({0}, {1}, {2})", True),
    "replacenull": ("F.coalesce({0}, F.lit({1}))", True),
    "isnull": ("{0}.isNull()", True),
    "iif": ("F.when({0}, {1}).otherwise({2})", True),
    "getdate": ("F.current_timestamp()", True),
    "dateadd": ("F.date_add({2}, {1})", True),
    "datediff": ("F.datediff({2}, {1})", True),
    "year": ("F.year({0})", True),
    "month": ("F.month({0})", True),
    "day": ("F.dayofmonth({0})", True),
    "hour": ("F.hour({0})", True),
    "minute": ("F.minute({0})", True),
    "second": ("F.second({0})", True),
    "findstring": ("F.locate({1}, {0})", True),   # occurrence arg not directly supported
    "charindex": ("F.locate({0}, {1})", True),
    "left": ("F.substring({0}, 1, {1})", True),
    "right": ("F.substring({0}, F.length({0}) - {1} + 1, {1})", True),
    "sqrt": ("F.sqrt({0})", True),
    "abs": ("F.abs({0})", True),
    "round": ("F.round({0}, {1})", True),
    "ceiling": ("F.ceil({0})", True),
    "floor": ("F.floor({0})", True),
    "concat": ("F.concat({args})", True),
    "nullif": ("F.nullif({0}, {1})", True),
    "coalesce": ("F.coalesce({args})", True),
    "token": (None, False),              # No direct equivalent → LLM
    "tokencount": (None, False),
    "codepoint": (None, False),
    "hex": (None, False),
    "unhex": (None, False),
}
