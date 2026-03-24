Set objShell = CreateObject("WScript.Shell")
Set objFSO   = CreateObject("Scripting.FileSystemObject")

' Run from the directory this script lives in
Dim scriptDir
scriptDir = objFSO.GetParentFolderName(WScript.ScriptFullName)
objShell.CurrentDirectory = scriptDir

' Find pythonw.exe via the Windows registry — reliable at startup
' when PATH may not be fully loaded yet.
Dim pythonw, pyPath, v
pythonw = ""

Dim versions(5)
versions(0) = "3.13"
versions(1) = "3.12"
versions(2) = "3.11"
versions(3) = "3.10"
versions(4) = "3.9"
versions(5) = "3.8"

For Each v In versions
    On Error Resume Next
    pyPath = objShell.RegRead("HKCU\SOFTWARE\Python\PythonCore\" & v & "\InstallPath\")
    If Err.Number = 0 And pyPath <> "" Then
        If objFSO.FileExists(pyPath & "pythonw.exe") Then
            pythonw = pyPath & "pythonw.exe"
        End If
    End If
    Err.Clear
    If pythonw = "" Then
        pyPath = objShell.RegRead("HKLM\SOFTWARE\Python\PythonCore\" & v & "\InstallPath\")
        If Err.Number = 0 And pyPath <> "" Then
            If objFSO.FileExists(pyPath & "pythonw.exe") Then
                pythonw = pyPath & "pythonw.exe"
            End If
        End If
        Err.Clear
    End If
    On Error GoTo 0
    If pythonw <> "" Then Exit For
Next

' Fallback: pythonw on PATH
If pythonw = "" Then pythonw = "pythonw"

' Launch silently — window style 0 = hidden, False = don't wait
objShell.Run """" & pythonw & """ """ & scriptDir & "\main.py""", 0, False
