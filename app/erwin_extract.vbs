Option Explicit
On Error Resume Next

If WScript.Arguments.Count < 2 Then
  WScript.StdErr.WriteLine "usage: extract.vbs <model.erwin> <out.json>"
  WScript.Quit 2
End If

Dim modelPath, outPath
modelPath = WScript.Arguments(0)
outPath = WScript.Arguments(1)

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

Function JsonEscape(s)
  Dim t
  If IsNull(s) Or IsEmpty(s) Then
    JsonEscape = ""
    Exit Function
  End If
  t = CStr(s)
  t = Replace(t, "\", "\\")
  t = Replace(t, """", "\""")
  t = Replace(t, Chr(13) & Chr(10), "\n")
  t = Replace(t, Chr(13), "\n")
  t = Replace(t, Chr(10), "\n")
  t = Replace(t, Chr(9), "\t")
  JsonEscape = t
End Function

Function IsMacro(s)
  If s = "" Then
    IsMacro = False
  Else
    IsMacro = (Left(s, 1) = "%")
  End If
End Function

Function IsPkGroup(kgType, kgName)
  Dim t, n
  t = UCase(Trim(kgType & ""))
  n = UCase(Trim(kgName & ""))
  If t = "PK" Or t = "PRIMARY KEY" Or t = "PRIMARYKEY" Then
    IsPkGroup = True
    Exit Function
  End If
  If InStr(t, "PRIMARY") > 0 And InStr(t, "FOREIGN") = 0 Then
    IsPkGroup = True
    Exit Function
  End If
  If Left(n, 3) = "XPK" Then
    IsPkGroup = True
  Else
    IsPkGroup = False
  End If
End Function

Dim saByEnt
Set saByEnt = CreateObject("Scripting.Dictionary")
saByEnt.CompareMode = 1

Sub RememberSA(entName, areaName)
  Dim key, areas
  key = Trim(entName & "")
  If key = "" Or areaName = "" Then Exit Sub
  If Not saByEnt.Exists(key) Then
    Set saByEnt(key) = CreateObject("Scripting.Dictionary")
    saByEnt(key).CompareMode = 1
  End If
  Set areas = saByEnt(key)
  If Not areas.Exists(areaName) Then areas.Add areaName, True
End Sub

Function SaJson(entName, physName)
  Dim areas, first, k
  SaJson = ""
  If saByEnt.Exists(entName) Then
    Set areas = saByEnt(entName)
  ElseIf physName <> "" And saByEnt.Exists(physName) Then
    Set areas = saByEnt(physName)
  Else
    Exit Function
  End If
  first = True
  For Each k In areas.Keys
    If Not first Then SaJson = SaJson & ","
    first = False
    SaJson = SaJson & """" & JsonEscape(k) & """"
  Next
End Function

Dim scapi, pu, session, mos, ent, attr, kg, mem, kids, kgs, members
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
Dim modelName
modelName = mos.Root.Name

Dim sas, sa, saName, refs, parts, part
Err.Clear
Set sas = mos.Collect(mos.Root, "Subject Area")
If Err.Number = 0 And Not sas Is Nothing Then
  WScript.StdErr.WriteLine "Subject Area count=" & sas.Count
  For Each sa In sas
    saName = sa.Name
    If saName = "" Then saName = PropVal(sa.Properties, "Name")
    refs = PropVal(sa.Properties, "Referenced Entities")
    parts = Split(refs, ";")
    For Each part In parts
      RememberSA part, saName
    Next
  Next
End If
Err.Clear

Dim json, firstTbl, tLog, tPhys, tDef, tSkip
json = "{""name"":""" & JsonEscape(modelName) & """,""tables"":["
firstTbl = True

Dim entities
Set entities = mos.Collect(mos.Root, "Entity")
WScript.StdErr.WriteLine "Entity count=" & entities.Count

For Each ent In entities
  tSkip = PropVal(ent.Properties, "Logical Only")
  If UCase(tSkip) = "TRUE" Or tSkip = "1" Then
    ' skip logical-only
  Else
    tLog = PropVal(ent.Properties, "Name")
    tPhys = PropVal(ent.Properties, "Physical Name")
    If tPhys = "" Or IsMacro(tPhys) Then tPhys = tLog
    tDef = PropVal(ent.Properties, "Comment")
    If tDef = "" Then tDef = PropVal(ent.Properties, "Definition")

    Dim pkNames, mn
    Set pkNames = CreateObject("Scripting.Dictionary")
    pkNames.CompareMode = 1
    Err.Clear
    Set kgs = mos.Collect(ent, "Key Group")
    If Err.Number = 0 Then
      For Each kg In kgs
        If IsPkGroup(PropVal(kg.Properties, "Key Group Type"), PropVal(kg.Properties, "Name")) Then
          Set members = mos.Collect(kg, "Key Group Member")
          For Each mem In members
            mn = PropVal(mem.Properties, "Name")
            If mn <> "" And Not pkNames.Exists(mn) Then pkNames.Add mn, True
          Next
        End If
      Next
    End If
    Err.Clear

    If Not firstTbl Then json = json & ","
    firstTbl = False
    json = json & "{""logical_name"":""" & JsonEscape(tLog) & """,""physical_name"":""" & JsonEscape(tPhys) & """,""comment"":""" & JsonEscape(tDef) & """,""subject_areas"":[" & SaJson(tLog, tPhys) & "],""columns"":["

    Dim firstCol, colLog, colPhys, colType, colNull, colDom, colDef, colPk, seenCols, colKey
    firstCol = True
    Set seenCols = CreateObject("Scripting.Dictionary")
    seenCols.CompareMode = 1
    Set kids = mos.Collect(ent, "Attribute")
    WScript.StdErr.WriteLine "TABLE " & tPhys & " cols=" & kids.Count

    For Each attr In kids
      colLog = PropVal(attr.Properties, "Name")
      colPhys = PropVal(attr.Properties, "Physical Name")
      If colPhys = "" Or IsMacro(colPhys) Then colPhys = colLog
      colKey = colPhys
      If colKey = "" Then colKey = colLog
      If colKey <> "" And Not seenCols.Exists(colKey) Then
        seenCols.Add colKey, True
        colType = PropVal(attr.Properties, "Physical Data Type")
        If colType = "" Then colType = PropVal(attr.Properties, "Datatype")
        If colType = "" Then colType = PropVal(attr.Properties, "Logical Data Type")
        colNull = PropVal(attr.Properties, "Null Option")
        colDom = PropVal(attr.Properties, "Parent Domain")
        colDef = PropVal(attr.Properties, "Comment")
        If colDef = "" Then colDef = PropVal(attr.Properties, "Definition")
        colPk = "false"
        If pkNames.Exists(colLog) Or pkNames.Exists(colPhys) Then colPk = "true"

        If Not firstCol Then json = json & ","
        firstCol = False
        json = json & "{""logical_name"":""" & JsonEscape(colLog) & """,""physical_name"":""" & JsonEscape(colPhys) & """,""datatype"":""" & JsonEscape(colType) & """,""null_option"":""" & JsonEscape(colNull) & """,""is_pk"":" & colPk & ",""domain"":""" & JsonEscape(colDom) & """,""comment"":""" & JsonEscape(colDef) & """}"
      End If
    Next
    json = json & "]}"
  End If
Next

json = json & "]}"

Dim stm
Set stm = CreateObject("ADODB.Stream")
stm.Type = 2
stm.Charset = "unicode"
stm.Open
stm.WriteText json
stm.SaveToFile outPath, 2
stm.Close

session.Close
scapi.Sessions.Clear
scapi.PersistenceUnits.Clear
WScript.StdErr.WriteLine "DONE"
WScript.Quit 0
