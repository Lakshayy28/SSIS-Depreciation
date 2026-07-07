"""
DTSX XML namespace constants and era-independent lookup helpers.

All .dtsx files use these namespace URIs. We register them once here so
every extractor uses consistent prefixes via lxml's XPath API.
"""

import re

NAMESPACES: dict[str, str] = {
    "DTS": "www.microsoft.com/SqlServer/Dts",
    "SQLTask": "www.microsoft.com/sqlserver/dts/tasks/sqltask",
    "ForEachFileEnumeratorProperties": "www.microsoft.com/sqlserver/dts/tasks/foreachfileenumerator",
    "FEL": "www.microsoft.com/SqlServer/Dts/Tasks/ForeachEnumeratorHost",
    "pipeline": "www.microsoft.com/SqlServer/Dts/Pipeline",
}

DTS = NAMESPACES["DTS"]
PIPELINE = NAMESPACES["pipeline"]

# Helper to build Clark-notation tag: {namespace}LocalName
def tag(ns_prefix: str, local: str) -> str:
    return f"{{{NAMESPACES[ns_prefix]}}}{local}"


# Commonly accessed tags
DTS_EXECUTABLE = tag("DTS", "Executable")
DTS_EXECUTABLES = tag("DTS", "Executables")
DTS_PRECEDENCE_CONSTRAINT = tag("DTS", "PrecedenceConstraint")
DTS_PRECEDENCE_CONSTRAINTS = tag("DTS", "PrecedenceConstraints")
DTS_CONNECTION_MANAGER = tag("DTS", "ConnectionManager")
DTS_CONNECTION_MANAGERS = tag("DTS", "ConnectionManagers")
DTS_VARIABLE = tag("DTS", "Variable")
DTS_VARIABLES = tag("DTS", "Variables")
DTS_PARAMETER = tag("DTS", "PackageParameter")
DTS_PARAMETERS = tag("DTS", "PackageParameters")
DTS_OBJECT_DATA = tag("DTS", "ObjectData")
DTS_EVENT_HANDLER = tag("DTS", "EventHandler")
DTS_EVENT_HANDLERS = tag("DTS", "EventHandlers")
DTS_PROPERTY = tag("DTS", "Property")
DTS_PROPERTY_EXPRESSION = tag("DTS", "PropertyExpression")

# Attribute names used in DTS namespace
ATTR_OBJECT_NAME = f"{{{DTS}}}ObjectName"
ATTR_EXECUTABLE_TYPE = f"{{{DTS}}}ExecutableType"
ATTR_DESCRIPTION = f"{{{DTS}}}Description"
ATTR_REFID = f"{{{DTS}}}refId"
ATTR_LOGICAL_AND = f"{{{DTS}}}LogicalAnd"
ATTR_EVAL_OP = f"{{{DTS}}}EvalOp"
ATTR_EXPRESSION = f"{{{DTS}}}Expression"
ATTR_FROM = f"{{{DTS}}}From"
ATTR_TO = f"{{{DTS}}}To"
ATTR_VALUE = f"{{{DTS}}}Value"
ATTR_DATA_TYPE = f"{{{DTS}}}DataType"
ATTR_NAMESPACE = f"{{{DTS}}}Namespace"
ATTR_CONTAINS_CONFIGURATION = f"{{{DTS}}}ContainsConfiguration"
ATTR_IS_ENCRYPTED = f"{{{DTS}}}IsEncrypted"
ATTR_SENSITIVE = f"{{{DTS}}}Sensitive"
ATTR_NAME = f"{{{DTS}}}Name"
ATTR_CREATION_NAME = f"{{{DTS}}}CreationName"
ATTR_EVENT_NAME = f"{{{DTS}}}EventName"

# EvalOp values for PrecedenceConstraints
EVAL_OP_SUCCESS = "1"
EVAL_OP_FAILURE = "2"
EVAL_OP_COMPLETION = "3"
EVAL_OP_EXPRESSION = "5"
EVAL_OP_EXPR_AND_CONSTRAINT = "6"
EVAL_OP_EXPR_OR_CONSTRAINT = "7"

# ExecutableType class IDs and logical names
EXECUTABLE_TYPE_MAP: dict[str, str] = {
    "Microsoft.ExecuteSQLTask": "execute_sql",
    "Microsoft.Pipeline": "data_flow",
    "Microsoft.ScriptTask": "script_task",
    "Microsoft.ForLoopTask": "for_loop",
    "Microsoft.ForeachLoopContainer": "foreach_loop",
    "SSIS.Sequence.2": "sequence",
    "Microsoft.Sequence": "sequence",
    "STOCK:SEQUENCE": "sequence",
    "STOCK:FOREACHLOOP": "foreach_loop",
    "STOCK:FORLOOP": "for_loop",
    "Microsoft.ExpressionTask": "expression_task",
    "Microsoft.ExecutePackageTask": "execute_package",
    "Microsoft.FileSystemTask": "file_system",
    "Microsoft.FtpTask": "ftp",
    "Microsoft.SendMailTask": "send_mail",
    "Microsoft.ExecuteProcessTask": "execute_process",
    "Microsoft.BulkInsertTask": "bulk_insert",
    "Microsoft.DataProfilingTask": "data_profiling",
    # Fallback: preserve the raw type string
}

# Data flow component class IDs
COMPONENT_CLASS_MAP: dict[str, str] = {
    "{BCEFE59B-6819-47F7-A557-EF3C9023D08F}": "oledb_source",
    "{90C7770B-DE7C-435E-880E-E718C92C0573}": "oledb_destination",
    "{BF01D463-7089-41EE-8F05-0A6DC17CE633}": "flat_file_source",
    "{D658C424-8CF0-4AD0-8CB5-8D70F3AE9446}": "flat_file_destination",
    "{34BCBE70-2658-4452-8A7A-B2D87DD50699}": "derived_column",
    "{A4B956F5-C18E-4462-A0D7-6B6FBA2F0D6C}": "conditional_split",
    "{F2C7D489-A860-4E49-9CA6-CFCF003BD1E2}": "lookup",
    "{3F96CE2D-B91B-476B-B03F-D2EBC394B4AD}": "merge_join",
    "{B5E1B94E-DC47-4BB6-880D-F7FB0EB0B8F5}": "sort",
    "{63988CCD-9DFB-4CDE-BEB9-3B8E2E8E3A43}": "aggregate",
    "{B90A37A2-5019-4FD7-9B40-0B40FDE44EA6}": "union_all",
    "{9D2E8D81-C3B0-4C5E-9C97-8C09C90DC6E8}": "multicast",
    "{1ACA4459-ACE0-496F-814A-8611F9C27E23}": "data_conversion",
    "{A18CFB75-B600-4E45-94D9-F580B5BB0D66}": "copy_column",
    "{2932025B-AB99-40F6-B5B8-783A73F80E24}": "row_count",
    "{C5736B3D-E3F8-4B10-9560-9EB6EB2E671B}": "character_map",
    "{D1B3EBE4-E5E3-4888-8F8F-61D5F88CF91C}": "slowly_changing_dimension",
    "{CCDC72F8-8AC5-4E6E-8CE3-D69B51C6FFD6}": "fuzzy_lookup",
    "{F9DBE025-4FAB-4B38-8640-7BE8B84EB79E}": "term_extraction",
    "{A5A04B84-B3C3-4C4B-8F18-B012C5B8B673}": "pivot",
    "{C3B7EC3C-08DE-4024-8C17-C25B18DF0A75}": "unpivot",
    "{874F7595-FB5F-40FF-96AF-FBFF8250E3EF}": "xml_source",
    "{A04D00CD-FFFB-4468-9B9B-68D15C0E9B1C}": "ado_net_source",
    "{449DD0D7-3956-4667-B8FD-60973A72F2B2}": "ado_net_destination",
    # Script component (transformation)
    "{874F7595-FB5F-40FF-96AF-FBFF8250E3EE}": "script_component",
}


# Class-name tokens for era-independent executable-type matching. 2008–2014
# packages write assembly-qualified .NET type names like
#   "Microsoft.SqlServer.Dts.Tasks.ExecuteSQLTask.ExecuteSQLTask,
#    Microsoft.SqlServer.SQLTask, Version=10.0.0.0, ..."
# and stock names like "SSIS.Pipeline.3" / "SSIS.Package.3" — none of which
# contain the modern logical names, so a token table is matched against the
# dotted segments of the type portion (before the first comma).
_TYPE_TOKEN_MAP: dict[str, str] = {
    "executesqltask": "execute_sql",
    "pipeline": "data_flow",
    "scripttask": "script_task",
    "forlooptask": "for_loop",
    "forloop": "for_loop",
    "foreachloopcontainer": "foreach_loop",
    "foreachloop": "foreach_loop",
    "sequence": "sequence",
    "expressiontask": "expression_task",
    "executepackagetask": "execute_package",
    "filesystemtask": "file_system",
    "ftptask": "ftp",
    "sendmailtask": "send_mail",
    "executeprocesstask": "execute_process",
    "executeprocess": "execute_process",
    "bulkinserttask": "bulk_insert",
    "dataprofilingtask": "data_profiling",
    "webservicetask": "web_service",
    "xmltask": "xml_task",
    "wmidatareadertask": "wmi_data_reader",
    "wmieventwatchertask": "wmi_event_watcher",
    "transferdatabasetask": "transfer_database",
    "messagequeuetask": "message_queue",
    "activexscripttask": "activex_script",
    "package": "package",
}


def map_executable_type(raw_type: str) -> str:
    """
    Map an SSIS ExecutableType/CreationName string to a CIR type name.

    Handles all observed eras:
      - modern logical names:      Microsoft.ExecuteSQLTask
      - versioned stock names:     SSIS.Pipeline.3, SSIS.Package.3, STOCK:SEQUENCE
      - assembly-qualified names:  Microsoft.SqlServer.Dts.Tasks.X.X, Assembly, Version=…
    """
    if not raw_type:
        return ""
    lowered = raw_type.lower()

    # 1. Modern logical / STOCK names (substring match on the curated map)
    for key, value in EXECUTABLE_TYPE_MAP.items():
        if key.lower() in lowered:
            return value

    # 2. Token match on the .NET type portion (before any assembly qualifier),
    #    with version suffixes stripped ("SSIS.Pipeline.3" → segment "pipeline").
    type_portion = lowered.split(",", 1)[0]
    segments = [
        seg for seg in re.split(r"[.\s:]+", type_portion)
        if seg and not seg.isdigit()
    ]
    # Longest-token-first so "executepackagetask" beats "package".
    for token in sorted(_TYPE_TOKEN_MAP, key=len, reverse=True):
        if token in segments:
            return _TYPE_TOKEN_MAP[token]
    # Suffix match rescues concatenated forms like "myexecutesqltask"
    for token in sorted(_TYPE_TOKEN_MAP, key=len, reverse=True):
        if any(seg.endswith(token) for seg in segments):
            return _TYPE_TOKEN_MAP[token]

    return segments[-1] if segments else lowered


def dts_attr(el, name: str, default: str = "") -> str:
    """
    Read a DTS property regardless of package format era.

    PackageFormatVersion 6+ (SQL 2012+) stores properties as namespaced
    ATTRIBUTES (DTS:ObjectName="x"); versions 2/3 (2005/2008) store most of
    them as CHILD ELEMENTS (<DTS:Property DTS:Name="ObjectName">x</DTS:Property>).
    Some third-party writers also emit un-namespaced attributes. Check all three.
    """
    val = el.get(f"{{{DTS}}}{name}")
    if val is not None:
        return val
    val = el.get(name)
    if val is not None:
        return val
    for child in el:
        if child.tag == DTS_PROPERTY and (
            child.get(ATTR_NAME) == name or child.get("Name") == name
        ):
            return (child.text or "").strip()
    return default


# Logical component class names (modern DTSX writes these instead of GUIDs)
LOGICAL_COMPONENT_MAP: dict[str, str] = {
    "microsoft.oledbsource": "oledb_source",
    "microsoft.oledbdestination": "oledb_destination",
    "microsoft.flatfilesource": "flat_file_source",
    "microsoft.flatfiledestination": "flat_file_destination",
    "microsoft.derivedcolumn": "derived_column",
    "microsoft.conditionalsplit": "conditional_split",
    "microsoft.lookup": "lookup",
    "microsoft.mergejoin": "merge_join",
    "microsoft.merge": "merge_join",
    "microsoft.sort": "sort",
    "microsoft.aggregate": "aggregate",
    "microsoft.unionall": "union_all",
    "microsoft.multicast": "multicast",
    "microsoft.dataconvert": "data_conversion",
    "microsoft.copycolumn": "copy_column",
    "microsoft.rowcount": "row_count",
    "microsoft.charactermap": "character_map",
    "microsoft.scd": "slowly_changing_dimension",
    "microsoft.fuzzylookup": "fuzzy_lookup",
    "microsoft.pivot": "pivot",
    "microsoft.unpivot": "unpivot",
    "microsoft.xmlsourceadapter": "xml_source",
    "microsoft.adonetsource": "ado_net_source",
    "microsoft.adonetdestination": "ado_net_destination",
    "microsoft.managedcomponenthost": "script_component",
    "microsoft.scriptcomponent": "script_component",
    "microsoft.oledbcommand": "oledb_command",
}


def map_component_class(class_id: str) -> str:
    """Map SSIS component classID (GUID or logical name) to CIR subtype."""
    if not class_id:
        return "unknown_component"
    guid_hit = COMPONENT_CLASS_MAP.get(class_id.upper())
    if guid_hit:
        return guid_hit
    return LOGICAL_COMPONENT_MAP.get(class_id.lower(), "unknown_component")
