"""
Full-spectrum DTSX parsing tests — format variants observed in the wild.

The DTSX format changed fundamentally at SQL Server 2012 ("Denali refactor"):
  - PackageFormatVersion 2 (2005) / 3 (2008): most properties stored as
    <DTS:Property DTS:Name="ObjectName">value</DTS:Property> CHILD ELEMENTS
  - PackageFormatVersion 6 (2012) / 8 (2014+): properties became ATTRIBUTES

ExecutableType values also vary: modern logical names (Microsoft.ExecuteSQLTask),
versioned stock names (SSIS.Pipeline.3, STOCK:SEQUENCE), and 2008–2014
assembly-qualified .NET type names. componentClassID GUIDs differ per SSIS
version, so GUID tables can never be exhaustive — contactInfo/name fallback is
required. Encrypted packages (ProtectionLevel EncryptAllWith*) must fail
LOUDLY, never parse as silently-empty.
"""

from __future__ import annotations

import pytest

from lxml import etree

from ssis_migration.parser import DTSXParser
from ssis_migration.parser.ns import map_executable_type


def _parse_xml(tmp_path, xml: str, name: str = "pkg.dtsx"):
    p = tmp_path / name
    p.write_text(xml, encoding="utf-8")
    return DTSXParser().parse(p)


# ─── ExecutableType normalization across eras ────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    # modern logical names (already worked)
    ("Microsoft.ExecuteSQLTask", "execute_sql"),
    ("Microsoft.Pipeline", "data_flow"),
    # versioned stock names (2008–2016 era)
    ("SSIS.Pipeline.2", "data_flow"),
    ("SSIS.Pipeline.3", "data_flow"),
    ("STOCK:SEQUENCE", "sequence"),
    ("STOCK:FORLOOP", "for_loop"),
    ("STOCK:FOREACHLOOP", "foreach_loop"),
    # assembly-qualified .NET type names (2008–2014 packages)
    ("Microsoft.SqlServer.Dts.Tasks.ExecuteSQLTask.ExecuteSQLTask, "
     "Microsoft.SqlServer.SQLTask, Version=11.0.0.0, Culture=neutral, "
     "PublicKeyToken=89845dcd8080cc91", "execute_sql"),
    ("Microsoft.SqlServer.Dts.Tasks.ScriptTask.ScriptTask, "
     "Microsoft.SqlServer.ScriptTask, Version=10.0.0.0, Culture=neutral, "
     "PublicKeyToken=89845dcd8080cc91", "script_task"),
    ("Microsoft.SqlServer.Dts.Tasks.SendMailTask.SendMailTask, "
     "Microsoft.SqlServer.SendMailTask, Version=10.0.0.0, Culture=neutral, "
     "PublicKeyToken=89845dcd8080cc91", "send_mail"),
    ("Microsoft.SqlServer.Dts.Tasks.FileSystemTask.FileSystemTask, "
     "Microsoft.SqlServer.FileSystemTask, Version=10.0.0.0, Culture=neutral, "
     "PublicKeyToken=89845dcd8080cc91", "file_system"),
    ("Microsoft.SqlServer.Dts.Tasks.ExecutePackageTask.ExecutePackageTask, "
     "Microsoft.SqlServer.ExecPackageTask, Version=10.0.0.0, Culture=neutral, "
     "PublicKeyToken=89845dcd8080cc91", "execute_package"),
])
def test_map_executable_type_across_eras(raw, expected):
    assert map_executable_type(raw) == expected


# ─── 2008-era property-element format (PackageFormatVersion 3) ────────────────

_PKG_2008 = """<?xml version="1.0"?>
<DTS:Executable xmlns:DTS="www.microsoft.com/SqlServer/Dts"
    DTS:ExecutableType="MSDTS.Package.2">
  <DTS:Property DTS:Name="PackageFormatVersion">3</DTS:Property>
  <DTS:Property DTS:Name="ObjectName">LegacyPackage2008</DTS:Property>
  <DTS:Property DTS:Name="ProtectionLevel">0</DTS:Property>
  <DTS:ConnectionManagers>
    <DTS:ConnectionManager>
      <DTS:Property DTS:Name="ObjectName">LegacyConn</DTS:Property>
      <DTS:Property DTS:Name="CreationName">OLEDB</DTS:Property>
      <DTS:ObjectData>
        <DTS:ConnectionManager>
          <DTS:Property DTS:Name="ConnectionString">Data Source=OLDSRV;Initial Catalog=LegacyDB;Provider=SQLNCLI10;</DTS:Property>
        </DTS:ConnectionManager>
      </DTS:ObjectData>
    </DTS:ConnectionManager>
  </DTS:ConnectionManagers>
  <DTS:Variables>
    <DTS:Variable DTS:Namespace="User">
      <DTS:Property DTS:Name="ObjectName">BatchId</DTS:Property>
      <DTS:VariableValue DTS:DataType="3">42</DTS:VariableValue>
    </DTS:Variable>
  </DTS:Variables>
  <DTS:Executables>
    <DTS:Executable DTS:ExecutableType="Microsoft.SqlServer.Dts.Tasks.ExecuteSQLTask.ExecuteSQLTask, Microsoft.SqlServer.SQLTask, Version=10.0.0.0, Culture=neutral, PublicKeyToken=89845dcd8080cc91">
      <DTS:Property DTS:Name="ObjectName">Truncate Legacy Table</DTS:Property>
      <DTS:ObjectData>
        <SQLTask:SqlTaskData
            xmlns:SQLTask="www.microsoft.com/sqlserver/dts/tasks/sqltask"
            SQLTask:SqlStatementSource="TRUNCATE TABLE dbo.Legacy"
            SQLTask:Connection="LegacyConn" />
      </DTS:ObjectData>
    </DTS:Executable>
  </DTS:Executables>
</DTS:Executable>
"""


def test_2008_property_element_package_name(tmp_path):
    cir = _parse_xml(tmp_path, _PKG_2008)
    # Executable name comes from a DTS:Property child, not an attribute.
    assert cir.control_flow.execution_tree
    exe = cir.control_flow.execution_tree[0]
    assert exe.name == "Truncate Legacy Table"
    assert exe.type == "execute_sql"
    assert exe.sql is not None and "TRUNCATE TABLE dbo.Legacy" in exe.sql.original_text


def test_2008_property_element_connection(tmp_path):
    cir = _parse_xml(tmp_path, _PKG_2008)
    assert len(cir.connections) == 1
    conn = cir.connections[0]
    assert conn.name == "LegacyConn"
    assert conn.provider_type == "oledb"
    assert conn.resolved_parameters.get("host") == "OLDSRV"
    assert conn.resolved_parameters.get("database") == "LegacyDB"


def test_2008_property_element_variable(tmp_path):
    cir = _parse_xml(tmp_path, _PKG_2008)
    names = [v.name for v in cir.variables]
    assert "BatchId" in names


# ─── SSIS.Pipeline.N data flows with unknown-version GUID components ──────────

_PKG_PIPELINE3 = """<?xml version="1.0"?>
<DTS:Executable xmlns:DTS="www.microsoft.com/SqlServer/Dts"
    DTS:ExecutableType="SSIS.Package.3" DTS:ObjectName="P2012">
  <DTS:Executables>
    <DTS:Executable DTS:ExecutableType="SSIS.Pipeline.3" DTS:ObjectName="DFT Old Style">
      <DTS:ObjectData>
        <pipeline version="1">
          <components>
            <component name="Flat File Source"
                componentClassID="{D23FD76B-F51D-420F-BBCB-19CBF6AC1AB4}"
                contactInfo="Flat File Source;Microsoft Corporation; Microsoft SQL Server; (C) Microsoft Corporation; All Rights Reserved;">
              <properties>
                <property name="RetainNulls">false</property>
              </properties>
            </component>
            <component name="OLE DB Destination Old"
                componentClassID="{5A0B62E8-D91D-49F5-94A5-7BE58DE508F0}"
                contactInfo="OLE DB Destination;Microsoft Corporation; Microsoft SQL Server; (C) Microsoft Corporation; All Rights Reserved;">
              <properties>
                <property name="OpenRowset">[dbo].[Target]</property>
              </properties>
            </component>
          </components>
        </pipeline>
      </DTS:ObjectData>
    </DTS:Executable>
  </DTS:Executables>
</DTS:Executable>
"""


def test_ssis_pipeline_3_dataflow_detected(tmp_path):
    cir = _parse_xml(tmp_path, _PKG_PIPELINE3)
    assert len(cir.data_flows) == 1
    assert len(cir.data_flows[0].components) == 2


def test_unknown_guid_falls_back_to_contact_info(tmp_path):
    # These GUIDs are from OTHER SSIS versions than our table — the
    # contactInfo display name must rescue classification.
    cir = _parse_xml(tmp_path, _PKG_PIPELINE3)
    subtypes = {c.name: c.subtype for c in cir.data_flows[0].components}
    assert subtypes["Flat File Source"] == "flat_file_source"
    assert subtypes["OLE DB Destination Old"] == "oledb_destination"


# ─── Encrypted packages must fail LOUDLY ──────────────────────────────────────

_PKG_ENCRYPTED = """<?xml version="1.0"?>
<DTS:Executable xmlns:DTS="www.microsoft.com/SqlServer/Dts"
    DTS:ObjectName="SecretPkg" DTS:ProtectionLevel="EncryptAllWithPassword">
  <EncryptedData Salt="AAAA" IV="BBBB">gibberishbase64==</EncryptedData>
</DTS:Executable>
"""


def test_encrypt_all_raises_clear_error(tmp_path):
    with pytest.raises(ValueError, match="[Ee]ncrypt"):
        _parse_xml(tmp_path, _PKG_ENCRYPTED)


_PKG_SENSITIVE = """<?xml version="1.0"?>
<DTS:Executable xmlns:DTS="www.microsoft.com/SqlServer/Dts"
    DTS:ObjectName="SensPkg" DTS:ProtectionLevel="EncryptSensitiveWithUserKey">
  <DTS:Executables>
    <DTS:Executable DTS:ExecutableType="Microsoft.ExecuteSQLTask" DTS:ObjectName="Q">
      <DTS:ObjectData>
        <SQLTask:SqlTaskData
            xmlns:SQLTask="www.microsoft.com/sqlserver/dts/tasks/sqltask"
            SQLTask:SqlStatementSource="SELECT 1" />
      </DTS:ObjectData>
    </DTS:Executable>
  </DTS:Executables>
</DTS:Executable>
"""


def test_encrypt_sensitive_parses_with_metadata(tmp_path):
    cir = _parse_xml(tmp_path, _PKG_SENSITIVE)
    assert cir.metadata.protection_level == "EncryptSensitiveWithUserKey"
    assert cir.control_flow.execution_tree     # body still parseable


# ─── Execute SQL: statement source variations ─────────────────────────────────

_PKG_SQL_FROM_VARIABLE = """<?xml version="1.0"?>
<DTS:Executable xmlns:DTS="www.microsoft.com/SqlServer/Dts" DTS:ObjectName="P">
  <DTS:Executables>
    <DTS:Executable DTS:ExecutableType="Microsoft.ExecuteSQLTask" DTS:ObjectName="Run Dynamic">
      <DTS:ObjectData>
        <SQLTask:SqlTaskData
            xmlns:SQLTask="www.microsoft.com/sqlserver/dts/tasks/sqltask"
            SQLTask:SqlStatementSourceType="Variable"
            SQLTask:SqlStatementSource="User::DynamicSql" />
      </DTS:ObjectData>
    </DTS:Executable>
  </DTS:Executables>
</DTS:Executable>
"""


def test_sql_from_variable_not_treated_as_sql_text(tmp_path):
    cir = _parse_xml(tmp_path, _PKG_SQL_FROM_VARIABLE)
    exe = cir.control_flow.execution_tree[0]
    # The variable NAME must not be transpiled as if it were SQL.
    assert exe.sql is None or "User::DynamicSql" not in (exe.sql.original_text or "") \
        or exe.sql.transpilation_status.value != "complete"
    # It must be visible for the LLM/human instead of silently mis-handled.
    notes = (exe.conversion_notes or "") + (exe.sql.transpilation_notes or "" if exe.sql else "")
    assert "Variable" in notes or "variable" in notes


# ─── Modern ScriptProject script tasks ────────────────────────────────────────

_PKG_SCRIPT_PROJECT = """<?xml version="1.0"?>
<DTS:Executable xmlns:DTS="www.microsoft.com/SqlServer/Dts" DTS:ObjectName="P">
  <DTS:Executables>
    <DTS:Executable DTS:ExecutableType="Microsoft.ScriptTask" DTS:ObjectName="Do Things">
      <DTS:ObjectData>
        <ScriptProject xmlns="www.microsoft.com/sqlserver/dts/tasks/scripttask"
            Name="ST_abc" Language="Microsoft Visual C# 2019"
            ReadOnlyVariables="User::In1,User::In2" ReadWriteVariables="User::Out1">
          <ProjectItem Name="ScriptMain.cs"><![CDATA[
public void Main()
{
    int x = 1;
    Dts.TaskResult = (int)ScriptResults.Success;
}
]]></ProjectItem>
          <ProjectItem Name="Project.vsaproj">QmluYXJ5R2FyYmFnZQ==</ProjectItem>
        </ScriptProject>
      </DTS:ObjectData>
    </DTS:Executable>
  </DTS:Executables>
</DTS:Executable>
"""


def test_script_project_code_extracted(tmp_path):
    cir = _parse_xml(tmp_path, _PKG_SCRIPT_PROJECT)
    exe = cir.control_flow.execution_tree[0]
    assert exe.type == "script_task"
    assert exe.script_code and "public void Main()" in exe.script_code
    assert "QmluYXJ5R2FyYmFnZQ" not in exe.script_code   # binary item excluded
    assert exe.script_language == "csharp"
    assert "User::In1" in exe.read_only_variables
    assert "User::Out1" in exe.read_write_variables


# ─── Container-scoped variables ───────────────────────────────────────────────

_PKG_SCOPED_VARS = """<?xml version="1.0"?>
<DTS:Executable xmlns:DTS="www.microsoft.com/SqlServer/Dts" DTS:ObjectName="P">
  <DTS:Variables>
    <DTS:Variable DTS:Namespace="User" DTS:ObjectName="RootVar">
      <DTS:VariableValue DTS:DataType="8">a</DTS:VariableValue>
    </DTS:Variable>
  </DTS:Variables>
  <DTS:Executables>
    <DTS:Executable DTS:ExecutableType="STOCK:FOREACHLOOP" DTS:ObjectName="Loop Files">
      <DTS:Variables>
        <DTS:Variable DTS:Namespace="User" DTS:ObjectName="CurrentFile">
          <DTS:VariableValue DTS:DataType="8"></DTS:VariableValue>
        </DTS:Variable>
      </DTS:Variables>
      <DTS:Executables/>
    </DTS:Executable>
  </DTS:Executables>
</DTS:Executable>
"""


def test_container_scoped_variables_captured(tmp_path):
    cir = _parse_xml(tmp_path, _PKG_SCOPED_VARS)
    names = {v.name for v in cir.variables}
    assert "RootVar" in names
    assert "CurrentFile" in names
    scoped = next(v for v in cir.variables if v.name == "CurrentFile")
    assert scoped.scope != "package"


# ─── Malformed / hostile XML ──────────────────────────────────────────────────

def test_xxe_entities_not_resolved(tmp_path):
    evil = """<?xml version="1.0"?>
<!DOCTYPE DTS:Executable [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<DTS:Executable xmlns:DTS="www.microsoft.com/SqlServer/Dts" DTS:ObjectName="&xxe;">
  <DTS:Executables/>
</DTS:Executable>
"""
    p = tmp_path / "evil.dtsx"
    p.write_text(evil, encoding="utf-8")
    try:
        cir = DTSXParser().parse(p)
        # If it parses, the entity must NOT have been expanded to file contents
        assert "root:" not in (cir.metadata.source_file or "")
        for exe in cir.control_flow.execution_tree:
            assert "root:" not in exe.name
    except ValueError:
        pass  # rejecting the file outright is equally acceptable


def test_utf16_encoded_package(tmp_path):
    xml = """<?xml version="1.0" encoding="utf-16"?>
<DTS:Executable xmlns:DTS="www.microsoft.com/SqlServer/Dts" DTS:ObjectName="U16">
  <DTS:Executables/>
</DTS:Executable>
"""
    p = tmp_path / "u16.dtsx"
    p.write_bytes(xml.encode("utf-16"))
    cir = DTSXParser().parse(p)
    assert cir is not None
