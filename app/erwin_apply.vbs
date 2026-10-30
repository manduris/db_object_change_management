Option Explicit
On Error Resume Next

If WScript.Arguments.Count < 2 Then
  WScript.StdErr.WriteLine "usage: apply.vbs <model.erwin> <op.txt>"
  WScript.Quit 2
End If

Dim modelPath, opPath
modelPath = WScript.Arguments(0)
opPath = WScript.Arguments(1)

Function PropVal(props, name)
  Dim pr, v
  PropVal = ""
  Err.Clear
  If props Is Nothing Then Exit Function
  If Not props.HasProperty(name) Then Exit Function
  Set pr = props.Item(name)
  If Err.Number <> 0 Then
    Err.Clear
    Exit Function
  End If
  v = pr.FormatAsString
  If Err.Number <> 0 Or IsNull(v) Or IsEmpty(v) Then
    Err.Clear
    v = pr.Value
  End If
  If Err.Number <> 0 Or IsNull(v) Or IsEmpty(v) Then
    PropVal = ""
  Else
    PropVal = CStr(v)
  End If
  Err.Clear
End Function

Sub SetProp(obj, name, value)
  Dim pr
  If obj Is Nothing Then Exit Sub
  Err.Clear
  If Not obj.Properties.HasProperty(name) Then
    Err.Clear
    Exit Sub
  End If
  Set pr = obj.Properties.Item(name)
  If Err.Number <> 0 Then
    Err.Clear
    Exit Sub
  End If
  pr.Value = value
  Err.Clear
End Sub

Function SameName(a, b)
  SameName = (StrComp(Trim(a & ""), Trim(b & ""), vbTextCompare) = 0)
End Function

Function ReadText(path)
  Dim stm
  Set stm = CreateObject("ADODB.Stream")
  stm.Type = 2
  stm.Charset = "utf-8"
  stm.Open
  stm.LoadFromFile path
  ReadText = stm.ReadText
  stm.Close
End Function

Dim op
Set op = CreateObject("Scripting.Dictionary")
op.CompareMode = 1

Sub LoadOp(path)
  Dim text, lines, i, line, p, key, value
  text = ReadText(path)
  lines = Split(Replace(text, vbCr, ""), vbLf)
  For i = 0 To UBound(lines)
    line = Trim(lines(i))
    If line <> "" And Left(line, 1) <> "#" Then
      p = InStr(line, "=")
      If p > 1 Then
        key = Trim(Left(line, p - 1))
        value = Mid(line, p + 1)
        If op.Exists(key) Then
          op(key) = value
        Else
          op.Add key, value
        End If
      End If
    End If
  Next
End Sub

Function OpVal(name)
  If op.Exists(name) Then
    OpVal = op(name)
  Else
    OpVal = ""
  End If
End Function

LoadOp opPath

Dim scapi, pu, session, mos
WScript.StdErr.WriteLine "SCAPI"
Set scapi = CreateObject("AllFusionERwin.SCAPI")
If Err.Number <> 0 Then
  WScript.StdErr.WriteLine "FAIL CreateObject " & Err.Number & " " & Err.Description
  WScript.Quit 1
End If

Set pu = scapi.PersistenceUnits.Add(modelPath, "RDO=Yes")
If Err.Number <> 0 Then
  WScript.StdErr.WriteLine "FAIL Add " & Err.Number & " " & Err.Description
  WScript.Quit 1
End If

Set session = scapi.Sessions.Add()
session.Open pu
If Err.Number <> 0 Then
  WScript.StdErr.WriteLine "FAIL Open " & Err.Number & " " & Err.Description
  WScript.Quit 1
End If

Set mos = session.ModelObjects
Err.Clear
session.BeginNamedTransaction "gap-apply"
If Err.Number <> 0 Then
  Err.Clear
  session.BeginNamedTransaction("gap-apply")
  Err.Clear
End If

Function FindEnt(target)
  Dim ent, tLog, tPhys
  Set FindEnt = Nothing
  If Trim(target) = "" Then Exit Function
  For Each ent In mos.Collect(mos.Root, "Entity")
    tLog = PropVal(ent.Properties, "Name")
    tPhys = PropVal(ent.Properties, "Physical Name")
    If SameName(tPhys, target) Or SameName(tLog, target) Then
      Set FindEnt = ent
      Exit Function
    End If
  Next
End Function

Function FindAttr(ent, target)
  Dim attr, aLog, aPhys
  Set FindAttr = Nothing
  If ent Is Nothing Or Trim(target) = "" Then Exit Function
  For Each attr In mos.Collect(ent, "Attribute")
    aLog = PropVal(attr.Properties, "Name")
    aPhys = PropVal(attr.Properties, "Physical Name")
    If SameName(aPhys, target) Or SameName(aLog, target) Then
      Set FindAttr = attr
      Exit Function
    End If
  Next
End Function

Function AddObj(className, owner)
  Set AddObj = Nothing
  Err.Clear
  Set AddObj = mos.Add(className, owner)
  If Err.Number <> 0 Or AddObj Is Nothing Then
    Err.Clear
    Set AddObj = session.ModelObjects.Add(className, owner)
  End If
  If Err.Number <> 0 Then
    WScript.StdErr.WriteLine "FAIL Add " & className & " " & Err.Number & " " & Err.Description
    Err.Clear
    Set AddObj = Nothing
  End If
End Function

Sub RemoveObj(obj)
  If obj Is Nothing Then Exit Sub
  Err.Clear
  mos.Remove obj
  If Err.Number <> 0 Then
    Err.Clear
    session.ModelObjects.Remove obj
  End If
  If Err.Number <> 0 Then
    Err.Clear
    obj.Delete
  End If
  Err.Clear
End Sub

Function FirstCustomSA()
  Dim sas, sa, n, compact
  Set FirstCustomSA = Nothing
  Err.Clear
  Set sas = mos.Collect(mos.Root, "Subject Area")
  If Err.Number <> 0 Then
    Err.Clear
    Exit Function
  End If
  For Each sa In sas
    n = sa.Name
    compact = LCase(Replace(Replace(Replace(Replace(n, "<", ""), ">", ""), " ", ""), "_", ""))
    If compact <> "mainsubjectarea" Then
      Set FirstCustomSA = sa
      Exit Function
    End If
  Next
End Function

Sub AttachToSA(entName)
  Dim sa, refs
  Set sa = FirstCustomSA()
  If sa Is Nothing Or Trim(entName) = "" Then Exit Sub
  refs = PropVal(sa.Properties, "Referenced Entities")
  If InStr(1, ";" & refs & ";", ";" & entName & ";", vbTextCompare) > 0 Then Exit Sub
  If refs <> "" And Right(refs, 1) <> ";" Then refs = refs & ";"
  SetProp sa, "Referenced Entities", refs & entName & ";"
End Sub

Sub FillColumn(attr, prefix)
  Dim logicalName, physName, datatype, nullable
  logicalName = OpVal(prefix & "logical_name")
  physName = OpVal(prefix & "physical_name")
  If physName = "" Then physName = logicalName
  If logicalName = "" Then logicalName = physName
  datatype = OpVal(prefix & "datatype")
  nullable = LCase(OpVal(prefix & "nullable"))
  SetProp attr, "Name", logicalName
  SetProp attr, "Physical Name", physName
  If datatype <> "" Then SetProp attr, "Physical Data Type", datatype
  If nullable = "0" Or nullable = "false" Then
    SetProp attr, "Null Option", "Not Null"
  Else
    SetProp attr, "Null Option", "Null"
  End If
  If OpVal(prefix & "comment") <> "" Then SetProp attr, "Comment", OpVal(prefix & "comment")
End Sub

Dim action, ent, attr, i, count, prefix
action = LCase(OpVal("action"))
WScript.StdErr.WriteLine "ACTION " & action

If action = "delete_table" Then
  Set ent = FindEnt(OpVal("table"))
  If ent Is Nothing Then
    WScript.StdErr.WriteLine "FAIL table not found " & OpVal("table")
    WScript.Quit 1
  End If
  RemoveObj ent
ElseIf action = "delete_column" Then
  Set ent = FindEnt(OpVal("table"))
  If ent Is Nothing Then
    WScript.StdErr.WriteLine "FAIL table not found " & OpVal("table")
    WScript.Quit 1
  End If
  Set attr = FindAttr(ent, OpVal("column"))
  If attr Is Nothing Then
    WScript.StdErr.WriteLine "FAIL column not found " & OpVal("column")
    WScript.Quit 1
  End If
  RemoveObj attr
ElseIf action = "add_column" Then
  Set ent = FindEnt(OpVal("table"))
  If ent Is Nothing Then
    WScript.StdErr.WriteLine "FAIL table not found " & OpVal("table")
    WScript.Quit 1
  End If
  Set attr = AddObj("Attribute", ent)
  If attr Is Nothing Then WScript.Quit 1
  FillColumn attr, ""
ElseIf action = "modify_column" Then
  Set ent = FindEnt(OpVal("table"))
  If ent Is Nothing Then
    WScript.StdErr.WriteLine "FAIL table not found " & OpVal("table")
    WScript.Quit 1
  End If
  Set attr = FindAttr(ent, OpVal("column"))
  If attr Is Nothing Then
    WScript.StdErr.WriteLine "FAIL column not found " & OpVal("column")
    WScript.Quit 1
  End If
  If OpVal("datatype") <> "" Then SetProp attr, "Physical Data Type", OpVal("datatype")
  If LCase(OpVal("nullable")) = "0" Or LCase(OpVal("nullable")) = "false" Then
    SetProp attr, "Null Option", "Not Null"
  ElseIf OpVal("nullable") <> "" Then
    SetProp attr, "Null Option", "Null"
  End If
ElseIf action = "add_table" Then
  Set ent = AddObj("Entity", mos.Root)
  If ent Is Nothing Then WScript.Quit 1
  If OpVal("logical_name") <> "" Then SetProp ent, "Name", OpVal("logical_name")
  If OpVal("physical_name") <> "" Then SetProp ent, "Physical Name", OpVal("physical_name")
  If OpVal("physical_name") = "" And OpVal("table") <> "" Then SetProp ent, "Physical Name", OpVal("table")
  If OpVal("comment") <> "" Then SetProp ent, "Comment", OpVal("comment")
  count = CInt("0" & OpVal("col_count"))
  For i = 0 To count - 1
    prefix = "c" & i & "_"
    Set attr = AddObj("Attribute", ent)
    If Not attr Is Nothing Then FillColumn attr, prefix
  Next
  If OpVal("logical_name") <> "" Then
    AttachToSA OpVal("logical_name")
  Else
    AttachToSA OpVal("table")
  End If
Else
  WScript.StdErr.WriteLine "FAIL unknown action " & action
  WScript.Quit 1
End If

Err.Clear
session.CommitNamedTransaction "gap-apply"
If Err.Number <> 0 Then
  Err.Clear
  session.CommitNamedTransaction("gap-apply")
  Err.Clear
End If

Err.Clear
pu.Save
If Err.Number <> 0 Then
  WScript.StdErr.WriteLine "FAIL Save " & Err.Number & " " & Err.Description
  WScript.Quit 1
End If

session.Close
scapi.Sessions.Clear
scapi.PersistenceUnits.Clear
WScript.StdErr.WriteLine "DONE"
WScript.Quit 0
